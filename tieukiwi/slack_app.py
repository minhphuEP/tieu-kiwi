"""Layer B: Slack wrapper (Socket Mode).

Exposes the Layer A agent through two entry points:
  - the `/tieukiwi` slash command, and
  - an `app_mention` handler (reply when the bot is @mentioned), which always answers
    IN THREAD so Layer C's feedback loop can later read the conversation.

This module only calls `agent.ask(...)` — it does not change the agent loop.

Run it with:  python -m tieukiwi.slack_app  (needs SLACK_BOT_TOKEN + SLACK_APP_TOKEN, and
ANTHROPIC_API_KEY for the agent). Importing this module does NOT require the tokens.
"""

import json
import re
import time
import uuid
from datetime import datetime

from . import (
    agent, config, db, jira_ingest, memory, progress, routing, slack_format,
    testcase_export, testcase_gen, tools,
)

# In-memory dedup of handled invocations/events (single-process). Skips retries / duplicates.
_seen_ids = set()

# Stamped once at process start. Surfaced on clarify replies so a stale duplicate
# process (e.g. an old `python -m tieukiwi.slack_app` left running after a restart)
# is immediately visible instead of silently answering with pre-fix code.
_BOOT_TS = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# In-flight clarify interviews, keyed by a short opaque id. Real ambiguity
# questions (specific, PRD-derived) routinely blow past Slack's ~2000-3000 char
# limits on action `value` / modal `private_metadata` — so those fields never
# carry the ambiguities themselves, only this key. Single-process, like
# _seen_ids: an interview in flight during a restart is lost, and the user just
# re-runs the clarify command.
_pending_clarify = {}
_PENDING_CLARIFY_MAX = 200


def _store_pending_clarify(payload):
    key = uuid.uuid4().hex[:12]
    _pending_clarify[key] = payload
    if len(_pending_clarify) > _PENDING_CLARIFY_MAX:
        _pending_clarify.pop(next(iter(_pending_clarify)))
    return key

# Matches a leading Slack mention like "<@U12345>" at the start of the text.
_MENTION_RE = re.compile(r"^\s*<@[A-Z0-9]+>\s*")

# Go-live intent: a question about whether a story is ready for release.
_GOLIVE_RE = re.compile(r"go[\s\-]?live|đủ điều kiện|release|go\s*/?\s*no-?go|sẵn sàng", re.I)
# A Jira-style requirement ref, e.g. FRONT-3494.
_REQ_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{1,9}-\d+)\b")

# Force-refresh intent: user asks to bypass hash-gate on pre-flight ingest.
# Vietnamese + English phrasings; conservative enough that plain conversation
# doesn't accidentally trigger. Matches "cập nhật", "refresh", "PRD đã update",
# "chạy lại đi", "just updated", etc.
_FORCE_REFRESH_RE = re.compile(
    r"cập\s*nhật|làm\s*mới|đồng\s*bộ|refresh|resync|re-?fetch|reload|mới\s*nhất"
    r"|(?:đã|vừa|mới|đang)(?:\s+được)?\s+(?:update|updated|sửa|chỉnh|thay\s*đổi|edit)"
    r"|được\s+(?:update|updated)"
    r"|(?:xem|review|check|chạy|run)\s+lại"
    r"|(?:prd|brd|requirement|req|spec)\s+(?:mới|đã(?:\s+được)?\s*update"
        r"|vừa(?:\s+được)?\s*update|updated|changed)"
    r"|just\s+updated|please\s+re-?review|re-?check|re-?run",
    re.I,
)

# "List ACs" — a pure data query the LLM tends to summarise/drop titles from.
# Route it to a deterministic renderer instead so QE sees every AC verbatim.
_LIST_AC_RE = re.compile(
    r"(?:danh\s*sách|list|liệt\s*kê|show(?:\s+me)?|xem|các|những|all)"
    r"\s+(?:the\s+)?ac"
    r"|ac[^\n]{0,20}(?:là\s*gì|nào|có\s*gì|of\b)"
    r"|acceptance\s+criteri",
    re.I,
)


def _list_ac_intent(text, fallback_ref=None):
    # Return the requirement ref if the message is asking for the AC list,
    # else None. Sticky ticket kicks in when the message doesn't repeat the ref.
    if not text or not _LIST_AC_RE.search(text):
        return None
    m = _REQ_RE.search(text)
    if m:
        return m.group(1).upper()
    return fallback_ref

# Clarify-requirements intent: mimics the .claude/agents/brd-clarifier interview
# workflow (skills/requirement-clarity.md), but driven through Slack Block Kit
# instead of AskUserQuestion (Slack has no blocking equivalent).
# "ambigu" stem covers ambiguous / ambiguity / ambiguities (incl. "find ambiguities").
# Also match explicit PO-confirm phrasings ("po chốt", "cần po", "po confirm",
# "po xác nhận", "chốt requirement"). "po" alone is intentionally NOT a trigger.
_CLARIFY_RE = re.compile(
    r"clarify|làm rõ|ambigu"
    r"|po\s*chốt|cần\s*po|po\s*confirm|po\s*xác\s*nhận|chốt\s*requirement",
    re.I,
)
_CLARIFY_TRIGGER_STRIP_RE = re.compile(
    r"^\s*(clarify|làm rõ|find\s+ambiguit\w*"
    r"|cần\s*po\s*chốt|po\s*chốt|cần\s*po|po\s*confirm|po\s*xác\s*nhận|chốt\s*requirement)"
    r"\b[:\s]*",
    re.I,
)

# Section/input block `text` is capped at 3000 chars by Slack; stay well clear.
_CLARIFY_BLOCK_TEXT_MAX = 2800


def _golive_intent(text):
    # Return the requirement ref if this looks like a go-live question, else None.
    if not text or not _GOLIVE_RE.search(text):
        return None
    m = _REQ_RE.search(text)
    return m.group(1).upper() if m else None


# Test-case generation intent, e.g. "gen test case cho CDM-268", "write a testcase for AC-...",
# "viết test case", "tạo/sinh test case". `test\s*case` matches both "test case" and "testcase".
# Requires the words "test case" adjacent, so it never over-matches go-live/curator-test/bug.
_GEN_TC_RE = re.compile(
    r"gen(?:erate)?\s*test\s*case"
    r"|write\s*(?:a\s*)?test\s*case"
    r"|vi(?:ế|e)t\s*test\s*case"
    r"|t(?:ạ|a)o\s*test\s*case"
    r"|sinh\s*test\s*case",
    re.I,
)


def _gen_testcase_intent(text):
    # Return the requirement ref if this looks like a "generate test cases" request, else None.
    if not text or not _GEN_TC_RE.search(text):
        return None
    m = _REQ_RE.search(text)
    return m.group(1).upper() if m else None


# Status-update intent, e.g. "Cập nhật trạng thái CDM-268",
# "cap nhat trang thai CDM-268", "update status CDM-268". Fires the TR sync +
# TC↔TR linker in tieukiwi.jira_ingest.sync_testruns_and_link_tcs.
_STATUS_UPDATE_RE = re.compile(
    r"c(?:ậ|a)p\s*nh(?:ậ|a)t\s*tr(?:ạ|a)ng\s*th(?:á|a)i"
    r"|update\s*status",
    re.I,
)


def _status_update_intent(text):
    if not text or not _STATUS_UPDATE_RE.search(text):
        return None
    m = _REQ_RE.search(text)
    return m.group(1).upper() if m else None


# Discard intent: a command (not a button) to cancel the in-progress draft in this
# thread, e.g. "discard test case", "cancel test cases", "hủy test case".
_DISCARD_TC_RE = re.compile(r"(discard|cancel|h(ủ|u)y|b(ỏ|o))\s*(the\s*)?test\s*case", re.I)


def _discard_testcase_intent(text):
    return bool(text) and bool(_DISCARD_TC_RE.search(text))


def _clarify_intent(text):
    return bool(text) and bool(_CLARIFY_RE.search(text))


# Free-text rule teaching -> curator approval flow. Requires an explicit "rule" trigger
# (VN+EN) so it never swallows go-live / gen-testcase / clarify / bug / curator-test.
_RULE_TEACH_RE = re.compile(
    r"(học\s*rule(?:\s*mới)?|thêm\s*rule|dạy\s*rule|add\s*rule|remember\s*this\s*rule)",
    re.I,
)
# Optional trailing "for <domain>" / "cho <domain>" — only honored for known domains
# so it can't accidentally chop a rule that merely ends in "... for X".
_RULE_DOMAIN_RE = re.compile(r"\b(?:for|cho)\s+([A-Za-z][A-Za-z _-]{1,25})\s*$", re.I)
_RULE_DOMAIN_MAP = {
    "testcase": "TestCase", "test case": "TestCase", "testplan": "TestPlan",
    "testrun": "TestRun", "requirement": "Requirement", "req": "Requirement",
    "ac": "AcceptanceCriterion", "acceptancecriterion": "AcceptanceCriterion",
    "acceptance criterion": "AcceptanceCriterion", "bug": "Bug",
    "userstory": "UserStory", "user story": "UserStory", "business": "business",
}


def _rule_teach_intent(text):
    return bool(text) and bool(_RULE_TEACH_RE.search(text))


def _parse_rule_teach(text):
    # Return (rule_text, applies_to). Strip the trigger phrase (+ a following ':') and,
    # if the tail is "for/cho <known-domain>", use it as applies_to (default "TestCase").
    m = _RULE_TEACH_RE.search(text)
    remainder = (text[m.end():] if m else text).lstrip(" :–—-\t").strip()
    applies_to = "TestCase"
    dm = _RULE_DOMAIN_RE.search(remainder)
    if dm:
        cand = dm.group(1).strip().lower()
        if cand in _RULE_DOMAIN_MAP:
            applies_to = _RULE_DOMAIN_MAP[cand]
            remainder = remainder[:dm.start()].strip()
    return remainder, applies_to


def _clarify_target(text):
    # A requirement ref wins over pasted text (same heuristic as _golive_intent).
    # Returns (requirement_ref, None) or (None, raw_text_or_None).
    ref_m = _REQ_RE.search(text)
    if ref_m:
        return ref_m.group(1).upper(), None
    remainder = _CLARIFY_TRIGGER_STRIP_RE.sub("", text).strip()
    return None, (remainder or None)


def _requirement_text_for_clarify(requirement_ref, logger=None):
    # Best-effort: description text for a Requirement ref, fetching from Jira if the
    # graph doesn't have it yet. Mirrors _golive_report's fetch-then-read pattern.
    props = {}
    try:
        props = db.get_node_props(requirement_ref, "Requirement")
    except Exception:
        if logger is not None:
            logger.exception("get_node_props failed")
    if not props.get("description") and config.JIRA_BASE_URL and config.JIRA_EMAIL and config.JIRA_API_TOKEN:
        try:
            if tools.fetch_jira(requirement_ref).get("status") == "ok":
                props = db.get_node_props(requirement_ref, "Requirement")
        except Exception:
            if logger is not None:
                logger.exception("fetch_jira fallback failed")
    parts = [p for p in (props.get("summary"), props.get("description")) if p]
    text = "\n\n".join(parts) or None
    if not text:
        return None
    try:
        return tools.expand_with_confluence(text)
    except Exception:
        if logger is not None:
            logger.exception("expand_with_confluence failed")
        return text


def _missing_tokens():
    return [
        name
        for name, value in (
            ("SLACK_BOT_TOKEN", config.SLACK_BOT_TOKEN),
            ("SLACK_APP_TOKEN", config.SLACK_APP_TOKEN),
        )
        if not value
    ]


def _mrkdwn_blocks(text):
    # Slack section text is capped at 3000 chars; truncate defensively.
    text = text or "_(no response)_"
    if len(text) > 2900:
        text = text[:2900] + "…"
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def _seen_before(key):
    # Generic dedup: return True if this key was already handled; record it otherwise.
    # Used for both slash commands (trigger_id) and events (event_id).
    if not key:
        return False
    if key in _seen_ids:
        return True
    _seen_ids.add(key)
    if len(_seen_ids) > 1000:          # keep the set from growing unbounded
        _seen_ids.clear()
        _seen_ids.add(key)
    return False


def _strip_mention(text):
    # Remove the leading "<@BOT_ID>" mention to get the clean question.
    return _MENTION_RE.sub("", text or "")


def handle_question(text, logger=None, on_step=None, channel_id=None):
    # Shared logic for both entry points: run the Layer A agent and return a
    # Slack-friendly answer string. Never raises — a failure comes back as an error message.
    # The agent returns GitHub Markdown; convert it to the canonical Slack format.
    # on_step is an optional callback that fires as the agent thinks / calls tools,
    # so the Slack layer can chat_update a progress message in-place.
    try:
        answer = agent.ask(text, on_step=on_step)
    except Exception as e:
        if logger is not None:
            logger.exception("agent.ask failed")
        return slack_format.to_slack(f":warning: Error: {e}")
    return slack_format.to_slack(answer)


# ---------------------------------------------------------------- Layer C curator UI

def _candidate_blocks(candidate_id, rule_text, applies_to, approver_hint):
    # Block Kit: candidate rule + Approve / Edit / Reject buttons (candidate_id in value).
    header = slack_format.to_slack(
        f"*Đề xuất rule mới* — áp dụng cho *{applies_to}* · approver: *{approver_hint}*\n"
        f"> {rule_text}"
    )
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {
            "type": "actions",
            "block_id": f"curator_{candidate_id}",
            "elements": [
                {"type": "button", "action_id": "curator_approve", "style": "primary",
                 "text": {"type": "plain_text", "text": "Approve"}, "value": str(candidate_id)},
                {"type": "button", "action_id": "curator_edit",
                 "text": {"type": "plain_text", "text": "Edit"}, "value": str(candidate_id)},
                {"type": "button", "action_id": "curator_reject", "style": "danger",
                 "text": {"type": "plain_text", "text": "Reject"}, "value": str(candidate_id)},
            ],
        },
    ]


def post_candidate_to_curator(client, channel, candidate_id, rule_text, applies_to,
                              approver_hint, thread_ts=None):
    # Post a candidate rule with approval buttons to `channel` (in-thread if given).
    kwargs = {"thread_ts": thread_ts} if thread_ts else {}
    return client.chat_postMessage(
        channel=channel,
        blocks=_candidate_blocks(candidate_id, rule_text, applies_to, approver_hint),
        text=f"Candidate rule #{candidate_id} awaiting approval",
        **kwargs,
    )


def _finalize_curator_message(client, body, text):
    # Replace the candidate message (removing buttons) with a final status line.
    try:
        client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            blocks=_mrkdwn_blocks(text),
            text=text,
        )
    except Exception:
        pass


def _project_for_channel(channel_id, logger=None):
    # Resolve the project bound to a Slack channel (channel_project_map), or None.
    if not channel_id:
        return None
    try:
        return db.project_for_channel(channel_id)
    except Exception:
        if logger is not None:
            logger.exception("project_for_channel failed")
        return None


def _enqueue_candidate(client, channel_id, user_id, rule_text, applies_to="TestCase",
                       logger=None, thread_ts=None, evidence=None):
    # ONE path for posting a candidate rule to the curator (used by the demo AND the
    # free-text "teach a rule" command). Enqueues via db.add_candidate_rule, @mentions
    # the approver role for applies_to (curator_role_for), and posts Approve/Edit/Reject.
    cid = db.add_candidate_rule(
        rule_text, channel_id or "demo", applies_to,
        evidence or {"evidence": "taught via chat", "by": user_id},
    )
    project_id = _project_for_channel(channel_id, logger)
    mention = db.mention_for(routing.curator_role_for(applies_to), project_id)
    post_candidate_to_curator(client, channel_id, cid, rule_text, applies_to, mention, thread_ts=thread_ts)
    return cid


def _run_curator_demo(client, channel_id, user_id, logger=None, thread_ts=None):
    # Manual test path: enqueue a sample candidate and post it with buttons.
    return _enqueue_candidate(
        client, channel_id, user_id,
        "AC titles must be verifiable and unambiguous.", "TestCase",
        logger=logger, thread_ts=thread_ts,
        evidence={"evidence": "manual curator-test", "by": user_id},
    )
    return cid


# ---------------------------------------------------------------- Go-live sign-off

def _golive_report(requirement_ref, logger=None):
    # Assemble the structured report (ticket info + coverage) for the go-live message,
    # reusing the SAME slack_format builder as the story report. Reads the graph node
    # props; if empty and Jira is configured, fetch_jira to populate, then re-read.
    props = {}
    try:
        props = db.get_node_props(requirement_ref, "Requirement")
    except Exception:
        if logger is not None:
            logger.exception("get_node_props failed")
    has_info = any(props.get(k) for k in ("summary", "assignee", "priority", "status", "issuetype"))
    if not has_info and config.JIRA_BASE_URL and config.JIRA_EMAIL and config.JIRA_API_TOKEN:
        try:
            from .tools import fetch_jira
            if fetch_jira(requirement_ref).get("status") == "ok":
                props = db.get_node_props(requirement_ref, "Requirement")
        except Exception:
            if logger is not None:
                logger.exception("fetch_jira fallback failed")
    try:
        trace_result = db.trace(requirement_ref)
    except Exception:
        if logger is not None:
            logger.exception("trace failed")
        trace_result = {"acceptance_criteria": []}
    return slack_format.report_from_graph(requirement_ref, props, trace_result)


def _golive_approval_blocks(requirement_ref, mention):
    # Curator sign-off block shown ONLY when decision == GO. `mention` is the
    # already-resolved approver reference ("<@id>" or a fallback label).
    text = slack_format.to_slack(
        f":traffic_light: *GO* — {mention} vui lòng duyệt release cho *{requirement_ref}*"
    )
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "block_id": f"golive_{requirement_ref}",
            "elements": [
                {"type": "button", "action_id": "golive_approve", "style": "primary",
                 "text": {"type": "plain_text", "text": "Approve"}, "value": requirement_ref},
                {"type": "button", "action_id": "golive_reject", "style": "danger",
                 "text": {"type": "plain_text", "text": "Reject"}, "value": requirement_ref},
            ],
        },
    ]


# ---------------------------------------------------------------- Test-case generation

# Slack section blocks reject text over 3000 chars; a full-detail draft (steps,
# precondition, data tables) for a real requirement easily exceeds that in one
# block. Split on line boundaries into multiple section blocks in the SAME
# message instead (keeps the single message ts thread_state already relies on
# for chat_update / staleness tracking). Leave headroom under Slack's 50-block
# cap for the trailing actions block.
_SECTION_CHAR_LIMIT = 2900
_MAX_SECTION_BLOCKS = 45


def _chunk_mrkdwn(text, limit=_SECTION_CHAR_LIMIT):
    chunks, current = [], ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


def _testcase_draft_blocks(draft, approver_mention=None):
    # Kept intentionally light: full per-testcase detail (steps, precondition,
    # data tables, API fields) lives in the exported Excel file only, so the
    # Slack message stays a quick "does this look complete" scan, not a wall
    # of text the reviewer has to read through.
    acs = draft.get("acs") or []
    intro = (
        f":sparkles: Đây là bộ Draft test cases cho `{draft['requirement_ref']}` (v{draft['version']}) "
        f"- {len(draft['testcases'])} test case cho {len(acs)} Acceptance Criteria — file Excel đầy "
        "đủ đã được đính kèm ngay dưới đây:point_down:\n"
        "Bạn review file Excel rồi bấm :white_check_mark: Approve nếu ok, hoặc :arrows_counterclockwise: "
        "Refine kèm comment nếu muốn mình chỉnh sửa thêm nhé! :raised_hands:"
    )
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": intro}},
        {"type": "divider"},
    ]
    # Surface uncovered ACs so the reviewer sees them BEFORE the AC list —
    # a partial draft is still returned (soft-fail after retry) rather than
    # blocked, so this banner is the only signal the LLM left gaps.
    gaps = draft.get("coverage_gaps") or []
    if gaps:
        gap_refs_by_id = {ac["ref"]: ac for ac in acs}
        gap_lines = "\n".join(
            f"• `{ref}` — {gap_refs_by_id.get(ref, {}).get('desc', '')[:100]}"
            for ref in gaps
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": slack_format.to_slack(
            f":warning: *{len(gaps)} AC chưa được cover* (LLM retry vẫn skip — "
            f"thường do AC quá ngắn/mơ hồ như section header). Refine kèm hướng "
            f"dẫn cụ thể để add testcase cho:\n{gap_lines}"
        )}})
        blocks.append({"type": "divider"})
    coverage_text = slack_format.render_ac_list(acs)
    chunks = _chunk_mrkdwn(coverage_text)
    truncated = len(chunks) > _MAX_SECTION_BLOCKS
    if truncated:
        chunks = chunks[:_MAX_SECTION_BLOCKS]
    blocks.extend({"type": "section", "text": {"type": "mrkdwn", "text": chunk}} for chunk in chunks)
    if truncated:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": ":warning: _Danh sách AC dài quá giới hạn hiển thị — xem đầy đủ trong "
                               "file Excel._"}})
    blocks.append({"type": "divider"})
    # Ask-routing: testcase -> qe_lead. @mention the QE Lead as the approver, near the
    # buttons. `approver_mention` is a resolved "<@id>" (or a graceful "@qe_lead
    # (unconfigured)" label from mention_for) — never raises.
    if approver_mention:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": slack_format.to_slack(
            f":raising_hand: {approver_mention} — vui lòng review file Excel và *Approve* nếu ổn.")}})
    blocks.append({
            "type": "actions",
            "block_id": f"tc_{draft['requirement_ref']}_{draft['version']}",
            "elements": [
                {"type": "button", "action_id": "tc_approve", "style": "primary",
                 "text": {"type": "plain_text", "text": "Approve test cases"},
                 "value": draft["requirement_ref"]},
                {"type": "button", "action_id": "tc_refine",
                 "text": {"type": "plain_text", "text": "Refine test cases"},
                 "value": draft["requirement_ref"]},
            ],
    })
    return blocks


def _upload_draft_excel(client, channel_id, thread_ts, testcases, filename,
                         comment, error_context, logger=None):
    """Export testcases to the QE Excel template (tieukiwi/testcase_export.py)
    and upload to the thread. Best-effort: failures are reported in-thread
    rather than raised, since callers use this alongside a draft/approval
    message that has already been posted successfully.

    Returns the uploaded file's id (so a later refine/approve can retire it
    via _delete_superseded_excel), or None if nothing was uploaded."""
    if not testcases:
        return None
    try:
        xlsx_bytes = testcase_export.export_excel(testcases)
        result = client.files_upload_v2(channel=channel_id, thread_ts=thread_ts,
                                         filename=filename, content=xlsx_bytes,
                                         initial_comment=comment)
        return (result.get("file") or {}).get("id")
    except Exception as e:
        if logger is not None:
            logger.exception("export/upload failed")
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                 text=slack_format.to_slack(f":warning: {error_context}: {e}"))
        return None


def _delete_superseded_excel(client, file_id, logger=None):
    # Retire the previous draft's Excel file once a newer version (refine or
    # approve) has its own file posted, so the thread doesn't accumulate a
    # stale file per round. Best-effort — a failure here shouldn't block the
    # new draft/approval, which has already succeeded by the time this runs.
    if not file_id:
        return
    try:
        client.files_delete(file=file_id)
    except Exception:
        if logger is not None:
            logger.exception("deleting superseded draft excel failed")


def _do_gen_testcase(say, client, requirement_ref, logger=None, thread_ts=None, channel_id=None):
    project_id = _project_for_channel(channel_id, logger)
    # Immediate progress message so the user doesn't stare at silence for
    # 30-60s while the LLM streams. Retained ts is used to chat_update
    # (or ignored on best-effort failure — the final draft post is what
    # matters).
    progress_kwargs = {"thread_ts": thread_ts} if thread_ts else {}
    progress_ts = None
    try:
        progress_resp = say(
            text=slack_format.to_slack(
                f":hourglass_flowing_sand: Đang generate draft test cases cho `{requirement_ref}`… "
                f"(LLM có thể mất 30-60s cho requirement nhiều AC)"
            ),
            **progress_kwargs,
        )
        progress_ts = progress_resp.get("ts") if progress_resp else None
    except Exception:
        if logger is not None:
            logger.exception("posting progress message failed (non-fatal)")

    try:
        draft = testcase_gen.generate_draft(requirement_ref, project_id=project_id)
    except Exception as e:
        if logger is not None:
            logger.exception("generate_draft failed")
        if progress_ts and channel_id:
            try:
                client.chat_update(
                    channel=channel_id, ts=progress_ts,
                    text=slack_format.to_slack(f":warning: Error: {e}"),
                )
            except Exception:
                say(text=slack_format.to_slack(f":warning: Error: {e}"))
        else:
            say(text=slack_format.to_slack(f":warning: Error: {e}"))
        return
    # Retire the progress placeholder now that the draft is ready.
    if progress_ts and channel_id:
        try:
            client.chat_delete(channel=channel_id, ts=progress_ts)
        except Exception:
            if logger is not None:
                logger.exception("deleting progress message failed (non-fatal)")
    kwargs = {"thread_ts": thread_ts} if thread_ts else {}
    qe_lead = db.mention_for(routing.approver_role_for("testcase"), project_id)
    posted = say(blocks=_testcase_draft_blocks(draft, qe_lead),
                 text=f"Draft test cases for {requirement_ref}", **kwargs)
    anchor_ts = thread_ts or posted["ts"]

    # Persist the draft FIRST — before the Excel export — so Approve can ALWAYS find
    # it. If the export fails (e.g. missing files:write scope), the save must not be
    # skipped: that Excel-before-save ordering was the "No draft found" bug.
    try:
        memory.save_thread_state(
            channel_id, anchor_ts,
            {"flow": "gen_testcase", "draft_message_ts": posted["ts"],
             "excel_file_id": None, "bot_participant": True,
             "current_ref": requirement_ref, **draft},
        )
    except Exception:
        if logger is not None:
            logger.exception("saving draft state failed")
        say(text=slack_format.to_slack(
            ":warning: Draft shown above, but I couldn't save it for approval "
            "(storage error) — please regenerate."), **kwargs)
        return

    # Best-effort Excel export; a failure is reported in-thread but does NOT affect
    # the saved draft or the Approve flow.
    excel_file_id = _upload_draft_excel(
        client, channel_id, anchor_ts, draft["testcases"],
        filename=f"{requirement_ref}_testcases_v{draft['version']}.xlsx",
        comment=f":page_facing_up: Draft test cases (v{draft['version']}) — "
                f"{len(draft['testcases'])} testcase(s).",
        error_context="Excel export for this draft failed", logger=logger,
    )
    if excel_file_id:
        try:
            state = memory.get_thread_state(channel_id, anchor_ts) or {}
            state["excel_file_id"] = excel_file_id
            memory.save_thread_state(channel_id, anchor_ts, state)
        except Exception:
            if logger is not None:
                logger.exception("recording excel_file_id on draft state failed")


def _saved_confirmation(state, user):
    # Confirm exactly what Approve saved: TC ref -> covered AC refs. Length-capped
    # so a huge draft can't make chat_update exceed Slack's 3000-char block limit.
    tcs = state.get("testcases") or []
    header = f":white_check_mark: Approved by <@{user}> (v{state.get('version')}) — saved {len(tcs)} test case(s):"
    lines = []
    for tc in tcs:
        acs = ", ".join(tc.get("ac_refs") or []) or "—"
        lines.append(f"• `{tc.get('ref')}` covering {acs}")
    text = header + "\n" + "\n".join(lines)
    if len(text) > 2800:
        text = header + f"\n• {len(tcs)} test cases saved (list too long to show)."
    return slack_format.to_slack(text)


def _load_draft_state(channel_id, thread_ts, logger=None):
    """Load a gen_testcase draft for (channel_id, thread_ts). Never raises.

    Returns (state, status) where status is:
      "ok"      -> state is a valid gen_testcase draft (has testcases),
      "absent"  -> no draft here (truly missing / a non-draft thread),
      "error"   -> a storage read error (transient).
    Lets callers show the right message and never crash the Slack handler.
    """
    try:
        state = memory.get_thread_state(channel_id, thread_ts)
    except Exception:
        if logger is not None:
            logger.exception("get_thread_state failed")
        return None, "error"
    if not state or state.get("flow") != "gen_testcase" or "testcases" not in state:
        return None, "absent"
    return state, "ok"


def _do_discard_testcase(say, client, thread_ts, channel_id, user_id, logger=None):
    # Text-command counterpart to the Approve/Refine buttons: cancels the
    # in-progress draft in this thread without needing a clickable button
    # (which risks an accidental click). Requires typing an explicit command.
    state = memory.get_thread_state(channel_id, thread_ts)
    if not state or state.get("flow") != "gen_testcase":
        say(text=":warning: No active test case draft found in this thread.", thread_ts=thread_ts)
        return
    try:
        deleted = memory.delete_thread_state(channel_id, thread_ts)
    except Exception:
        if logger is not None:
            logger.exception("delete_thread_state failed")
        say(text=":warning: Failed to discard the draft — please try again.", thread_ts=thread_ts)
        return
    if not deleted:
        # The initial read above found a draft, but by the time we deleted it was
        # already gone — a concurrent/duplicate discard call won the race. Don't
        # report a second "discarded" success for the same draft.
        say(text=":warning: No active test case draft found in this thread.", thread_ts=thread_ts)
        return
    draft_ts = state.get("draft_message_ts")
    if draft_ts:
        try:
            client.chat_update(
                channel=channel_id, ts=draft_ts,
                blocks=[{"type": "section", "text": {"type": "mrkdwn",
                         "text": f":no_entry_sign: Discarded by <@{user_id}> "
                                 f"(v{state.get('version')}) — nothing was saved."}}],
                text=f"Discarded by <@{user_id}>",
            )
        except Exception:
            if logger is not None:
                logger.exception("marking draft message as discarded failed")
    say(text=f":no_entry_sign: Discarded the test case draft for "
             f"{state.get('requirement_ref')} — nothing was saved.", thread_ts=thread_ts)


def _is_stale_draft_click(body, state):
    # True if this button/modal-trigger click came from a draft message that has
    # since been superseded by a newer refine round (state["draft_message_ts"]
    # points at the CURRENT live message; an older message's buttons are stale).
    clicked_ts = body["message"]["ts"]
    current_ts = state.get("draft_message_ts")
    return current_ts is not None and clicked_ts != current_ts


def _do_list_acs(say, requirement_ref, project_id=None, thread_ts=None,
                 channel_id=None, logger=None):
    """Deterministic 'list ACs' — bypasses the LLM. Prevents Claude from
    silently rephrasing/dropping AC titles when the user just wants to see
    every AC of a ticket."""
    kwargs = {"thread_ts": thread_ts} if thread_ts else {}
    try:
        res = db.get_ticket(requirement_ref, project_id=project_id)
        trace = db.trace(requirement_ref, project_id=project_id)
    except Exception as e:
        if logger is not None:
            logger.exception("list_acs failed")
        say(blocks=_mrkdwn_blocks(slack_format.to_slack(f":warning: Error: {e}")),
            text="Error", **kwargs)
        return
    if not res or not res.get("found"):
        text = (f":information_source: *{requirement_ref}* chưa có trong graph. "
                f"Chạy `ingest_jira_ticket({requirement_ref})` để pull từ Jira trước.")
        say(blocks=_mrkdwn_blocks(slack_format.to_slack(text)), text=text, **kwargs)
        return
    acs = (trace or {}).get("acceptance_criteria") or []
    if not acs:
        text = (f":information_source: *{requirement_ref}* có 0 Acceptance Criterion "
                f"trong graph. BRD có thể chưa được extract — chạy "
                f"`ingest_jira_ticket({requirement_ref}, force=True)`.")
        say(blocks=_mrkdwn_blocks(slack_format.to_slack(text)), text=text, **kwargs)
        return
    # Reuse the same report shape + line renderer used by the story report /
    # go-live output, so formatting stays consistent across all AC listings.
    report = slack_format.report_from_graph(requirement_ref, res.get("props") or {}, trace)
    header = f"*{report.get('title') or requirement_ref}*"
    summary = (f"Story này có *{len(acs)} Acceptance Criteria*. "
               f"Danh sách đầy đủ:")
    ac_lines = [slack_format._ac_line(a) for a in report.get("acs") or []]
    text = "\n".join([header, "", summary] + ac_lines)
    say(blocks=_mrkdwn_blocks(slack_format.to_slack(text)),
        text=f"AC list {requirement_ref}", **kwargs)


def _do_golive(say, requirement_ref, logger=None, thread_ts=None, channel_id=None):
    # Run go_no_go and post the analysis; add approve/reject buttons only on GO.
    kwargs = {"thread_ts": thread_ts} if thread_ts else {}
    try:
        res = db.go_no_go(requirement_ref)
    except Exception as e:
        if logger is not None:
            logger.exception("go_no_go failed")
        say(blocks=_mrkdwn_blocks(slack_format.to_slack(f":warning: Error: {e}")),
            text="Error", **kwargs)
        return
    # Blocks 1–4 (+ NO-GO next-actions): full ticket info + coverage + decision, built by
    # the SINGLE slack_format renderer (same one the story report uses).
    report = _golive_report(requirement_ref, logger)
    text = slack_format.render_golive(report, res)
    blocks = _mrkdwn_blocks(text)
    # Block 5 (GO only): curator mention + Approve/Reject buttons.
    if res.get("decision") == "GO":
        project_id = _project_for_channel(channel_id, logger)
        mention = db.mention_for(routing.approver_role_for("go_live"), project_id)
        blocks = blocks + _golive_approval_blocks(
            res.get("requirement") or requirement_ref, mention
        )
    say(blocks=blocks, text=f"Go/No-Go {requirement_ref}", **kwargs)


# ---------------------------------------------------------------- status-update (sync TR + link TC↔TR)

def _render_status_update(result, requirement_ref):
    trs = result.get("testruns") or []
    warnings = result.get("warnings") or []
    if not trs:
        body = (f":information_source: Không tìm thấy TestRun nào cho *{requirement_ref}* "
                "(chưa có subtask test-env nào được ingest).")
        return slack_format.to_slack(body)
    lines = [f"*Cập nhật trạng thái TestRun cho {requirement_ref}*"]
    for tr in trs:
        env = tr.get("environment") or "?"
        old_s = tr.get("old_status") or "?"
        new_s = tr.get("new_status") or "?"
        arrow = f"{old_s} → {new_s}" if old_s != new_s else new_s
        line = f"• `{tr['ref']}` ({env}): {arrow}"
        if tr.get("new_status") == "done" and tr.get("linked_tc_refs"):
            n_new = tr.get("edges_added") or 0
            n_total = len(tr["linked_tc_refs"])
            line += (f" — linked {n_total} TestCase(s) via `executedBy`"
                     f" (+{n_new} mới)")
        lines.append(line)
    if warnings:
        lines.append("")
        lines.append("_Warnings:_")
        for w in warnings:
            lines.append(f"  - {w}")
    return slack_format.to_slack("\n".join(lines))


def _do_status_update(say, requirement_ref, logger=None, thread_ts=None, channel_id=None):
    # Sync live Jira status for every TR of this Requirement and, when a TR
    # flips to 'done', link every covering TestCase to it via executedBy.
    kwargs = {"thread_ts": thread_ts} if thread_ts else {}
    project_id = _project_for_channel(channel_id, logger)
    try:
        result = jira_ingest.sync_testruns_and_link_tcs(
            requirement_ref, project_id=project_id,
        )
    except Exception as e:
        if logger is not None:
            logger.exception("sync_testruns_and_link_tcs failed")
        say(blocks=_mrkdwn_blocks(slack_format.to_slack(f":warning: Error: {e}")),
            text="Error", **kwargs)
        return
    text = _render_status_update(result, requirement_ref)
    say(blocks=_mrkdwn_blocks(text), text=f"Status update {requirement_ref}", **kwargs)


# ---------------------------------------------------------------- clarify-requirements interview

def _trunc(s, n):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _clarify_summary_blocks(ambiguities, requirement_ref, project_id=None):
    # Header + one section block PER question (not one joined block — a single
    # mrkdwn text field is capped at 3000 chars, and real questions times 8 would
    # blow past that if joined). The button's value is just a lookup key into
    # _pending_clarify — see its docstring for why the data never touches a
    # Slack length-limited field directly.
    # Open questions are PO-confirm items -> @mention the PO (role-resolved, one path).
    po = db.mention_for(routing.approver_role_for("po_confirm"), project_id)
    header = slack_format.to_slack(
        f"*Requirement clarity check*{' — ' + requirement_ref if requirement_ref else ''}\n"
        f"Found *{len(ambiguities)}* open question(s) — {po} vui lòng xác nhận: _(build {_BOOT_TS})_"
    )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}]
    for i, a in enumerate(ambiguities, 1):
        line = slack_format.to_slack(f"{i}. _{a.get('dimension', '?')}_ — {a.get('question', '?')}")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _trunc(line, _CLARIFY_BLOCK_TEXT_MAX)}})

    key = _store_pending_clarify({"ambiguities": ambiguities, "requirement_ref": requirement_ref})
    blocks.append({
        "type": "actions",
        "elements": [{
            "type": "button", "action_id": "clarify_open_modal", "style": "primary",
            "text": {"type": "plain_text", "text": "Open clarification form"},
            "value": key,
        }],
    })
    return blocks


def _do_clarify(say, requirement_ref, text, logger=None, thread_ts=None, project_id=None):
    # Run find_ambiguities and post either "sufficiently specified" or the
    # open-questions summary + "Open clarification form" button.
    kwargs = {"thread_ts": thread_ts} if thread_ts else {}
    try:
        result = tools.find_ambiguities(text, project_id=project_id)
    except Exception as e:
        if logger is not None:
            logger.exception("find_ambiguities failed")
        say(blocks=_mrkdwn_blocks(slack_format.to_slack(f":warning: Error: {e}")),
            text="Error", **kwargs)
        return

    ambiguities = result.get("ambiguities") or []
    if not ambiguities:
        ref_note = f" *{requirement_ref}*" if requirement_ref else ""
        msg = slack_format.to_slack(
            f":white_check_mark: Requirement{ref_note} looks sufficiently specified — "
            f"no clarification needed. _(build {_BOOT_TS})_"
        )
        say(blocks=_mrkdwn_blocks(msg), text=msg, **kwargs)
        return

    try:
        say(blocks=_clarify_summary_blocks(ambiguities, requirement_ref, project_id),
            text=f"{len(ambiguities)} open question(s) found", **kwargs)
    except Exception:
        # A rendering/API failure here must still surface SOMETHING to the user —
        # the alternative is silence forever (what happened before this guard existed).
        if logger is not None:
            logger.exception("posting clarify summary failed")
        say(text=slack_format.to_slack(
            f":warning: Found *{len(ambiguities)}* open question(s) but couldn't render "
            f"the interactive form (check server logs). _(build {_BOOT_TS})_"
        ), **kwargs)


def _clarify_modal_blocks(ambiguities):
    blocks = []
    for i, a in enumerate(ambiguities):
        text = f"*{i + 1}. [{a.get('dimension', '?')}]* {a.get('question', '?')}"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": _trunc(text, _CLARIFY_BLOCK_TEXT_MAX)},
        })
        blocks.append({
            "type": "input",
            "block_id": f"answer_{i}",
            "optional": True,
            "label": {"type": "plain_text", "text": "Answer (or \"TBD\")"},
            "element": {
                "type": "plain_text_input",
                "action_id": "answer_input",
                "multiline": True,
            },
        })
    return blocks


def _clarified_requirements_text(ambiguities, values, requirement_ref):
    # Build the "Clarified Requirements" + "Open Items" block per the rubric's
    # Step 4 format (skills/requirement-clarity.md), in Slack mrkdwn.
    rows, open_items = [], []
    for i, a in enumerate(ambiguities):
        block = values.get(f"answer_{i}", {})
        answer = ((block.get("answer_input") or {}).get("value") or "").strip()
        question = a.get("question", "?")
        if not answer or answer.lower() in ("tbd", "unsure", "n/a", "?"):
            open_items.append(question)
        else:
            rows.append((question, answer))

    lines = ["*Clarified Requirements*" + (f" — {requirement_ref}" if requirement_ref else "")]
    if rows:
        lines.append("")
        lines.extend(f"• *{q}*\n   {ans}" for q, ans in rows)
    if open_items:
        lines.append("")
        lines.append(":warning: *Open Items* _(blockers before test-case writing begins)_")
        lines.extend(f"{i}. {q}" for i, q in enumerate(open_items, 1))
    return slack_format.to_slack("\n".join(lines)), rows, open_items


# ---------------------------------------------------------------- shared turn handler

def _extract_ref(text):
    # First ticket ref (e.g. CDM-268) in the text, uppercased, or None.
    m = _REQ_RE.search(text or "")
    return m.group(1).upper() if m else None


# Bug / failing-test question, e.g. "có bug hoặc test đang fail", "any failing tests?".
_BUG_RE = re.compile(r"\bbug\b|fail|đang fail|lỗi|broken", re.I)


def _is_bug_question(text):
    return bool(text) and bool(_BUG_RE.search(text))


def _is_golive_question(text):
    return bool(text) and bool(_GOLIVE_RE.search(text))


def _is_gen_testcase(text):
    return bool(text) and bool(_GEN_TC_RE.search(text))


def _recall_ref(channel_id, thread_ts, logger=None):
    # Tier-2 memory: the ticket this thread is about, or None.
    try:
        return (memory.get_thread_state(channel_id, thread_ts) or {}).get("current_ref")
    except Exception:
        if logger is not None:
            logger.exception("get_thread_state failed")
        return None


def _remember_thread(channel_id, thread_ts, ref=None, logger=None):
    # Mark the bot as a participant of this thread and (if given) remember the ticket.
    # Read-modify-write so the testcase-draft keys (flow/draft/...) are preserved.
    if not (channel_id and thread_ts):
        return
    try:
        state = memory.get_thread_state(channel_id, thread_ts) or {}
        state["bot_participant"] = True
        if ref:
            state["current_ref"] = ref
        memory.save_thread_state(channel_id, thread_ts, state)
    except Exception:
        if logger is not None:
            logger.exception("save_thread_state failed")


def _with_ref_context(ref, text):
    # Scope the agent to the remembered ticket without changing the agent loop —
    # only the input text is augmented.
    return (
        f"Current ticket in this thread: {ref}. "
        f"If the question omits a ticket key, assume they mean this one.\n\n{text}"
    )


def _ask_which_ticket(say, **kwargs):
    say(
        blocks=_mrkdwn_blocks(slack_format.to_slack(
            "Bạn muốn hỏi về ticket nào? (ví dụ: `CDM-268`). "
            "Tôi chưa thấy mã ticket trong tin nhắn này hoặc trong thread."
        )),
        text="Which ticket?",
        **kwargs,
    )


def _make_progress_callback(client, channel_id, progress_ts, logger=None,
                            min_interval=0.8):
    """Return an on_step callback that updates the given "Đang xử lý…" message
    in-place via chat_update. `tool_done` events are swallowed (avoid flicker
    between tool finish and next thinking event). Throttled to `min_interval`
    seconds so a burst of tool calls doesn't hit Slack rate limits.
    """
    if not progress_ts or not channel_id:
        return None
    last = [0.0]

    def _cb(ev):
        if (ev or {}).get("phase") == "tool_done":
            return
        now = time.monotonic()
        if now - last[0] < min_interval:
            return
        last[0] = now
        label = progress.label_for(ev)
        try:
            client.chat_update(
                channel=channel_id, ts=progress_ts,
                text=slack_format.to_slack(label),
            )
        except Exception:
            if logger is not None:
                logger.exception("progress chat_update failed")

    return _cb


def _update_or_post(client, channel_id, ts, thread_ts, text, logger=None):
    """chat_update the progress message in place; fall back to a new post."""
    slack_text = slack_format.to_slack(text)
    if ts:
        try:
            client.chat_update(channel=channel_id, ts=ts, text=slack_text)
            return
        except Exception:
            if logger is not None:
                logger.exception("chat_update failed")
    try:
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=slack_text)
    except Exception:
        if logger is not None:
            logger.exception("chat_postMessage fallback failed")


def _ensure_ticket_fresh(client, channel_id, thread_ts, ref, project_id,
                          force=False, logger=None):
    """Pre-flight: make sure `ref`'s subtree is in the graph (and reasonably
    fresh) before the agent runs its tools. Called for every question that
    resolves to a ticket.

    Deterministic (dev controls flow, not the LLM): posts progress to the
    thread and calls `jira_ingest.ingest_jira_ticket`. The tool itself hash-
    gates internally, so a cached ticket returns in <1s with no Confluence
    fetch and no LLM AC pass. On BRD drift, ingest auto-elevates to full
    re-extract of ACs (see `jira_ingest._check_brd_freshness`).

    Args:
      force: bypass hash-gate. Set true when the user's message contains a
             refresh keyword ("cập nhật", "refresh", …).

    Returns: the ingest summary dict (or None on total failure so the caller
    can still try to answer from whatever's already in the graph).
    """
    if not ref or not channel_id:
        return None
    progress_ts = None
    try:
        # Lightweight progress message; will be chat_update'd once ingest returns.
        resp = client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=slack_format.to_slack(
                f"🔄 Đang đồng bộ *{ref}* từ Jira{' (force)' if force else ''}…"
            ),
        )
        progress_ts = (resp or {}).get("ts")
    except Exception:
        if logger is not None:
            logger.exception("progress post failed")

    try:
        summary = jira_ingest.ingest_jira_ticket(
            ref, project_id=project_id, force=force,
            # Extract ACs on every fresh ingest (first-time or force). ~5-15s
            # only hits when the ticket is genuinely new — cached tickets
            # short-circuit at the hash-gate before the LLM pass runs. Without
            # this, ACs are never populated from Slack, and downstream tools
            # (coverage_gap, go_no_go, get_ticket) report 0 ACs.
            extract_acs=True,
        )
    except Exception as e:
        if logger is not None:
            logger.exception("ingest_jira_ticket failed")
        _update_or_post(client, channel_id, progress_ts, thread_ts,
                        f":warning: Không đồng bộ được *{ref}*: {e}", logger)
        return None

    status = summary.get("status")
    if status == "cached_fresh":
        text_line = f"✅ *{ref}* đã có dữ liệu."
    elif status == "ok":
        n_bugs = len(summary.get("bugs") or [])
        n_conf = len(summary.get("confluence_pages") or [])
        n_tr = len(((summary.get("subtasks") or {}).get("testruns")) or [])
        text_line = (f"✅ *{ref}* đã đồng bộ — "
                     f"{n_tr} test run, {n_bugs} bug, {n_conf} BRD.")
        # AC diff summary — only present when AC-extract actually ran.
        ac_kept = summary.get("acs_kept")
        if ac_kept is not None:
            ac_created = len(summary.get("acs_extracted") or [])
            ac_obsolete = len(summary.get("acs_obsoleted") or [])
            if ac_created or ac_obsolete:
                text_line += (f"\n📋 AC diff: *+{ac_created}* mới, "
                              f"{ac_kept} giữ nguyên, *−{ac_obsolete}* obsolete.")
            else:
                text_line += f"\n📋 AC không đổi ({ac_kept} AC giữ nguyên)."
    else:
        text_line = f":warning: *{ref}* ingest status = `{status}`"
    _update_or_post(client, channel_id, progress_ts, thread_ts, text_line, logger)
    return summary


def _handle_turn(say, client, channel_id, thread_ts, text, logger=None, user_id=None):
    """Handle ONE user turn — shared by the @mention handler and non-mention thread
    replies, so behaviour is identical whether or not the bot is tagged.

    Resolves the ticket ref (message first, else Tier-2 thread memory), routes the
    intent (discard / go-live / gen-testcase / clarify-ambiguities / general Q&A),
    replies in-thread, and remembers the thread + ticket for follow-ups.
    """
    kwargs = {"thread_ts": thread_ts} if thread_ts else {}

    if not text:
        say(blocks=_mrkdwn_blocks(slack_format.to_slack(
            "Hi! Ask me about a ticket, e.g. `@Tieu Kiwi thông tin CDM-268`.")),
            text="Usage", **kwargs)
        return

    # Curator demo shortcut — recognised via @mention too (not only the slash command).
    # Reuses the SAME _run_curator_demo: posts the candidate + Approve/Edit/Reject buttons
    # and @mentions the QE Lead. Replies in-thread.
    if "curator-test" in text.lower():
        _run_curator_demo(client, channel_id, user_id, logger, thread_ts=thread_ts)
        return

    # Teach a new quality rule from free text -> the SAME curator approval flow.
    # Checked early (explicit "rule" trigger) so it can't swallow other intents.
    if _rule_teach_intent(text):
        rule_text, applies_to = _parse_rule_teach(text)
        if not rule_text:
            say(blocks=_mrkdwn_blocks(slack_format.to_slack(
                "Bạn muốn dạy rule gì? Ví dụ: "
                "`@Tieu Kiwi học rule mới: Test data must cover boundary values`")),
                text="Usage", **kwargs)
            return
        _enqueue_candidate(client, channel_id, user_id, rule_text, applies_to,
                           logger=logger, thread_ts=thread_ts)
        return

    # Discard command -> cancel the in-progress draft. Handle before the interim ack.
    if _discard_testcase_intent(text):
        _do_discard_testcase(say, client, thread_ts, channel_id, user_id, logger)
        return

    # Resolve the ticket ref: message wins, else fall back to thread memory.
    ref = _extract_ref(text)
    _remember_thread(channel_id, thread_ts, ref, logger)  # mark participant (+ ref if any)
    if not ref:
        ref = _recall_ref(channel_id, thread_ts, logger)

    project_id = _project_for_channel(channel_id, logger)

    # Pre-flight ingest: sync the ticket's Jira + Confluence subtree BEFORE any
    # downstream tool runs. Hash-gate keeps this ~500ms when nothing changed;
    # detects PRD drift on Confluence and auto-re-extracts ACs. Users can force
    # bypass with "cập nhật" / "refresh" / "PRD đã update" / … keywords.
    # Skipped when there's no ref — general questions with no ticket context
    # (e.g. "curator-test") don't need it.
    if ref:
        force_refresh = bool(_FORCE_REFRESH_RE.search(text))
        _ensure_ticket_fresh(
            client, channel_id, thread_ts, ref, project_id,
            force=force_refresh, logger=logger,
        )

    # Interim ack in-thread — keep its ts so we can chat_update in place with
    # per-step labels while the agent works, then swap it for the final answer.
    progress_ts = None
    try:
        resp = say(text=slack_format.to_slack("🥝 Đang xử lý…"), **kwargs)
        progress_ts = (resp or {}).get("ts")
    except Exception:
        if logger is not None:
            logger.exception("interim post failed")

    # "List ACs" — deterministic AC dump; skips LLM to preserve every AC title verbatim.
    # Checked BEFORE go-live so a message like "list ACs of CDM-268 to check golive"
    # goes through the deterministic path, not the LLM.
    ac_ref = _list_ac_intent(text, fallback_ref=ref)
    if ac_ref:
        _do_list_acs(say, ac_ref, project_id=project_id,
                     thread_ts=thread_ts, channel_id=channel_id, logger=logger)
        return

    # Go-live readiness.
    if _is_golive_question(text):
        if ref:
            _do_golive(say, ref, logger, thread_ts=thread_ts, channel_id=channel_id)
        else:
            _ask_which_ticket(say, **kwargs)
        return

    # Generate test cases.
    if _is_gen_testcase(text):
        if ref:
            _do_gen_testcase(say, client, ref, logger, thread_ts=thread_ts, channel_id=channel_id)
        else:
            _ask_which_ticket(say, **kwargs)
        return

    # Status-update: sync TR status + link TC↔TR when done.
    if _STATUS_UPDATE_RE.search(text or ""):
        if ref:
            _do_status_update(say, ref, logger, thread_ts=thread_ts, channel_id=channel_id)
        else:
            _ask_which_ticket(say, **kwargs)
        return

    # Clarify / find ambiguities.
    if _clarify_intent(text):
        clarify_ref, raw_text = _clarify_target(text)
        clarify_ref = clarify_ref or ref            # reuse remembered ticket if none in text
        source_text = _requirement_text_for_clarify(clarify_ref, logger) if clarify_ref else raw_text
        if not source_text:
            _ask_which_ticket(say, **kwargs)
            return
        _do_clarify(say, clarify_ref, source_text, logger, thread_ts=thread_ts, project_id=project_id)
        return

    # General question. If the ref came from memory (not this message), scope the agent to it.
    agent_text = text if _extract_ref(text) else (_with_ref_context(ref, text) if ref else text)
    on_step = _make_progress_callback(client, channel_id, progress_ts, logger)
    answer = handle_question(agent_text, logger, on_step=on_step, channel_id=channel_id)
    # Bug / failing-test question -> route to the Dev owner after listing the issues.
    if _is_bug_question(text):
        dev = db.mention_for(routing.approver_role_for("bug"), project_id)  # "bug" -> "dev"
        answer = answer + "\n\n" + slack_format.to_slack(f":bust_in_silhouette: *Dev owner:* {dev}")

    # Replace the interim "Đang xử lý…" message with the final answer in-place
    # (single tidy message per question). Fall back to a new post if the update
    # fails so the user always gets a reply.
    posted = False
    if progress_ts:
        try:
            client.chat_update(
                channel=channel_id, ts=progress_ts,
                blocks=_mrkdwn_blocks(answer), text=answer,
            )
            posted = True
        except Exception:
            if logger is not None:
                logger.exception("final chat_update failed")
    if not posted:
        say(blocks=_mrkdwn_blocks(answer), text=answer, **kwargs)


def build_app():
    # Imported here so `import tieukiwi.slack_app` works even without slack tokens.
    from slack_bolt import App

    app = App(
        token=config.SLACK_BOT_TOKEN,
        signing_secret=config.SLACK_SIGNING_SECRET,
    )

    @app.command("/tieukiwi")
    def handle_tieukiwi(ack, command, say, client, logger):
        # 1) ACK within 3s (Slack requirement) — show a placeholder while we work.
        ack("Processing…")

        # 2) Skip retries / duplicate deliveries.
        if _seen_before(command.get("trigger_id")):
            return

        text = (command.get("text") or "").strip()

        # Demo shortcut (no extra Slack config needed): `/tieukiwi curator-test`
        if text == "curator-test":
            _run_curator_demo(client, command["channel_id"], command.get("user_id"), logger)
            return

        if not text:
            usage = slack_format.to_slack("Usage: `/tieukiwi <your question>`")
            say(blocks=_mrkdwn_blocks(usage), text="Usage")
            return

        # Go-live question -> deterministic go_no_go + (on GO) curator sign-off buttons.
        ref = _golive_intent(text)
        if ref:
            _do_golive(say, ref, logger, channel_id=command.get("channel_id"))
            return

        # Generate-testcase request -> draft + Approve/Refine buttons.
        tc_ref = _gen_testcase_intent(text)
        if tc_ref:
            _do_gen_testcase(say, client, tc_ref, logger, channel_id=command.get("channel_id"))
            return

        # Status-update request -> sync live Jira status for TRs of this Story
        # and link executedBy edges when a TR is 'done'.
        status_ref = _status_update_intent(text)
        if status_ref:
            _do_status_update(say, status_ref, logger, channel_id=command.get("channel_id"))
            return

        # Clarify-requirements question -> find ambiguities + Slack interview modal.
        if _clarify_intent(text):
            clarify_ref, raw_text = _clarify_target(text)
            project_id = _project_for_channel(command.get("channel_id"), logger)
            source_text = _requirement_text_for_clarify(clarify_ref, logger) if clarify_ref else raw_text
            if not source_text:
                usage = slack_format.to_slack(
                    "Usage: `/tieukiwi clarify <requirement ref>` or "
                    "`/tieukiwi clarify <pasted BRD text>`"
                )
                say(blocks=_mrkdwn_blocks(usage), text="Usage")
                return
            _do_clarify(say, clarify_ref, source_text, logger, project_id=project_id)
            return

        # 3) Otherwise: call the Layer A agent (shared helper) and post the result.
        answer = handle_question(text, logger)
        say(blocks=_mrkdwn_blocks(answer), text=answer)

    @app.command("/tieukiwi-curator-test")
    def handle_curator_test(ack, command, client, logger):
        # Manual trigger: enqueue a sample candidate and show the curator buttons.
        ack()
        try:
            _run_curator_demo(client, command["channel_id"], command.get("user_id"), logger)
        except Exception as e:
            logger.exception("curator-test failed")
            client.chat_postMessage(channel=command["channel_id"], text=f":warning: {e}")

    @app.action("curator_approve")
    def handle_curator_approve(ack, body, client, logger):
        ack()
        cid = int(body["actions"][0]["value"])
        user = body["user"]["id"]
        kb_id = None
        try:
            kb_id = db.approve_candidate(cid, approver=user)
        except Exception:
            logger.exception("approve_candidate failed")
        suffix = f" (kb_rules #{kb_id})" if kb_id else ""
        _finalize_curator_message(
            client, body, slack_format.to_slack(f":white_check_mark: Rule approved by <@{user}>{suffix}")
        )

    @app.action("curator_reject")
    def handle_curator_reject(ack, body, client, logger):
        ack()
        cid = int(body["actions"][0]["value"])
        user = body["user"]["id"]
        try:
            db.reject_candidate(cid, approver=user)
        except Exception:
            logger.exception("reject_candidate failed")
        _finalize_curator_message(
            client, body, slack_format.to_slack(f":x: Rule rejected by <@{user}>")
        )

    @app.action("curator_edit")
    def handle_curator_edit(ack, body, client, logger):
        ack()
        cid = int(body["actions"][0]["value"])
        rule_text = ""
        try:
            cand = db.get_candidate(cid) or {}
            rule_text = cand.get("candidate_rule") or ""
        except Exception:
            logger.exception("get_candidate failed")
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "curator_edit_submit",
                "private_metadata": json.dumps({
                    "candidate_id": cid,
                    "channel": body["channel"]["id"],
                }),
                "title": {"type": "plain_text", "text": "Edit rule"},
                "submit": {"type": "plain_text", "text": "Save"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [{
                    "type": "input",
                    "block_id": "rule_block",
                    "label": {"type": "plain_text", "text": "Rule text"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "rule_input",
                        "multiline": True,
                        "initial_value": rule_text,
                    },
                }],
            },
        )

    @app.view("curator_edit_submit")
    def handle_curator_edit_submit(ack, body, client, view, logger):
        ack()
        meta = json.loads(view.get("private_metadata") or "{}")
        cid = meta.get("candidate_id")
        new_text = view["state"]["values"]["rule_block"]["rule_input"]["value"]
        applies_to = "?"
        try:
            db.update_candidate_rule(cid, new_text)
            cand = db.get_candidate(cid) or {}
            applies_to = cand.get("applies_to") or "?"
        except Exception:
            logger.exception("update_candidate_rule failed")
        try:
            channel = meta.get("channel")
            project_id = _project_for_channel(channel, logger)
            mention = db.mention_for(routing.curator_role_for(applies_to), project_id)
            post_candidate_to_curator(client, channel, cid, new_text, applies_to, mention)
        except Exception:
            logger.exception("re-post after edit failed")

    @app.action("golive_approve")
    def handle_golive_approve(ack, body, client, logger):
        ack()
        ref = body["actions"][0]["value"]
        user = body["user"]["id"]
        try:
            db.record_golive_decision(ref, "approved", user)
        except Exception:
            logger.exception("record_golive_decision failed")
        _finalize_curator_message(
            client, body,
            slack_format.to_slack(f":white_check_mark: Release approved for *{ref}* by <@{user}>"),
        )

    @app.action("golive_reject")
    def handle_golive_reject(ack, body, client, logger):
        ack()
        ref = body["actions"][0]["value"]
        user = body["user"]["id"]
        try:
            db.record_golive_decision(ref, "rejected", user)
        except Exception:
            logger.exception("record_golive_decision failed")
        _finalize_curator_message(
            client, body,
            slack_format.to_slack(f":x: Release rejected for *{ref}* by <@{user}>"),
        )

    @app.action("tc_approve")
    def handle_tc_approve(ack, body, client, logger):
        ack()
        try:
            channel_id = body["channel"]["id"]
            thread_ts = body["message"].get("thread_ts") or body["message"]["ts"]
        except Exception:
            logger.exception("tc_approve: bad payload")
            return
        state, status = _load_draft_state(channel_id, thread_ts, logger)
        if status == "error":
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                     text=":warning: Couldn't read the draft (temporary storage error) — please try again.")
            return
        if status == "absent":
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                     text=":warning: No draft found for this thread — please regenerate the test cases.")
            return
        if _is_stale_draft_click(body, state):
            client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts,
                text=":warning: This draft has been superseded by a newer version — "
                     "please use the buttons on the latest draft message in this thread.",
            )
            return
        user = body["user"]["id"]
        # TODO (Level 2 — approver gating): restrict Approve to the QE Lead.
        #   qe = db.resolve_role_slack_id("qe_lead", _project_for_channel(channel_id, logger))
        #   if qe and user != qe:
        #       client.chat_postEphemeral(channel=channel_id, user=user,
        #           text="Chỉ QE Lead mới approve được.")  # (needs thread_ts for in-thread)
        #       return
        #   if not qe: logger.warning("qe_lead unresolved; allowing approve")  # never block demo
        # Left as a TODO on purpose: with a seeded qe_lead this would block any other
        # tester from approving during the demo. Enable once roles are finalized.
        try:
            testcase_gen.finalize_and_save(state, approved_by=user)
        except Exception as e:
            logger.exception("finalize_and_save failed")
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                     text=slack_format.to_slack(f":warning: Error saving testcases: {e}"))
            return
        # Mark the draft approved so a redelivery/double-click can't re-save (best-effort).
        try:
            state["status"] = "approved"
            memory.save_thread_state(channel_id, thread_ts, state)
        except Exception:
            logger.exception("marking draft approved failed")
        # Remove the Approve/Refine buttons now that the DB write has succeeded,
        # so a double-click or Slack redelivery can't trigger a second export/upload.
        # Keep the rendered testcase list itself — only the actions block is
        # dropped — so the reviewer can still see what they approved, and append a
        # confirmation of exactly what was saved (TC ref -> covered ACs).
        try:
            kept_blocks = [b for b in (body["message"].get("blocks") or [])
                           if b.get("type") != "actions"]
            kept_blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                "text": _saved_confirmation(state, user)}})
            client.chat_update(
                channel=channel_id, ts=body["message"]["ts"],
                blocks=kept_blocks,
                text=f"Approved by <@{user}>",
            )
        except Exception:
            logger.exception("removing tc_approve buttons failed")
        _upload_draft_excel(
            client, channel_id, thread_ts, state["testcases"],
            filename=f"{state['requirement_ref']}_testcases_v{state['version']}_approved.xlsx",
            comment=f":white_check_mark: Approved by <@{user}> "
                    f"(v{state['version']}) — {len(state['testcases'])} testcase(s) saved.",
            error_context=f"{len(state['testcases'])} testcase(s) were saved successfully, "
                          "but exporting/uploading the Excel file failed",
            logger=logger,
        )
        _delete_superseded_excel(client, state.get("excel_file_id"), logger=logger)

    @app.action("tc_refine")
    def handle_tc_refine(ack, body, client, logger):
        ack()
        try:
            channel_id = body["channel"]["id"]
            thread_ts = body["message"].get("thread_ts") or body["message"]["ts"]
        except Exception:
            logger.exception("tc_refine: bad payload")
            return
        state, status = _load_draft_state(channel_id, thread_ts, logger)
        if status == "error":
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                     text=":warning: Couldn't read the draft (temporary storage error) — please try again.")
            return
        if status == "absent":
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                     text=":warning: No draft found for this thread — please regenerate the test cases.")
            return
        if _is_stale_draft_click(body, state):
            client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts,
                text=":warning: This draft has been superseded by a newer version — "
                     "please use the buttons on the latest draft message in this thread.",
            )
            return
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "tc_refine_submit",
                "private_metadata": json.dumps({"channel_id": channel_id, "thread_ts": thread_ts}),
                "title": {"type": "plain_text", "text": "Refine test cases"},
                "submit": {"type": "plain_text", "text": "Submit"},
                "blocks": [{
                    "type": "input",
                    "block_id": "comment_block",
                    "label": {"type": "plain_text", "text": "Comment (or paste the full testcase list)"},
                    "element": {"type": "plain_text_input", "action_id": "comment_input", "multiline": True},
                }],
            },
        )

    @app.view("tc_refine_submit")
    def handle_tc_refine_submit(ack, body, client, view, logger):
        ack()
        try:
            meta = json.loads(view["private_metadata"])
            channel_id, thread_ts = meta["channel_id"], meta["thread_ts"]
            comment = view["state"]["values"]["comment_block"]["comment_input"]["value"]
        except Exception:
            logger.exception("tc_refine_submit: bad payload")
            return
        state, status = _load_draft_state(channel_id, thread_ts, logger)
        if status == "error":
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                     text=":warning: Couldn't read the draft (temporary storage error) — please try again.")
            return
        if status == "absent":
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                     text=":warning: No draft found for this thread — please regenerate the test cases.")
            return
        # The refine LLM call can take several seconds; post an immediate
        # acknowledgement so the user doesn't think the click was dropped.
        processing = client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text=":hourglass_flowing_sand: Refining test cases based on your comment…",
        )
        try:
            refined = testcase_gen.refine_draft(state, comment)
        except Exception as e:
            logger.exception("refine_draft failed")
            try:
                client.chat_update(channel=channel_id, ts=processing["ts"],
                                    text=slack_format.to_slack(f":warning: Error: {e}"))
            except Exception:
                logger.exception("updating processing message with error failed")
            return
        try:
            client.chat_update(channel=channel_id, ts=processing["ts"],
                                text=":white_check_mark: Refinement complete — see the new draft below.")
        except Exception:
            logger.exception("updating processing message to complete failed")
        try:
            old_ts = state.get("draft_message_ts")
            if old_ts:
                client.chat_update(
                    channel=channel_id, ts=old_ts,
                    blocks=[{"type": "section", "text": {"type": "mrkdwn",
                             "text": f":arrows_counterclockwise: Superseded by v{refined['version']} below."}}],
                    text=f"Superseded by v{refined['version']}",
                )
        except Exception:
            logger.exception("removing stale draft buttons failed")
        qe_lead = db.mention_for(routing.approver_role_for("testcase"),
                                  _project_for_channel(channel_id, logger))
        posted = client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            blocks=_testcase_draft_blocks(refined, qe_lead),
            text=f"Draft test cases for {refined['requirement_ref']} (v{refined['version']})",
        )
        excel_file_id = _upload_draft_excel(
            client, channel_id, thread_ts, refined["testcases"],
            filename=f"{refined['requirement_ref']}_testcases_v{refined['version']}.xlsx",
            comment=f":page_facing_up: Draft test cases (v{refined['version']}) — "
                    f"{len(refined['testcases'])} testcase(s).",
            error_context="Excel export for this draft failed", logger=logger,
        )
        _delete_superseded_excel(client, state.get("excel_file_id"), logger=logger)
        memory.save_thread_state(channel_id, thread_ts,
                                  {"flow": "gen_testcase", "draft_message_ts": posted["ts"],
                                   "excel_file_id": excel_file_id,
                                   "bot_participant": True,
                                   "current_ref": refined.get("requirement_ref"),
                                   **refined})

    @app.action("clarify_open_modal")
    def handle_clarify_open_modal(ack, body, client, logger):
        ack()
        key = body["actions"][0]["value"]
        payload = _pending_clarify.get(key)
        if payload is None:
            # Expired (app restarted) or double-clicked after submit already popped it.
            client.chat_postEphemeral(
                channel=body["channel"]["id"], user=body["user"]["id"],
                text=slack_format.to_slack(
                    ":warning: This clarification session has expired — please re-run the "
                    "clarify command."
                ),
            )
            return
        ambiguities = payload.get("ambiguities") or []
        # Fill in what's only known at click time; same cache entry, same key.
        payload["channel"] = body["channel"]["id"]
        payload["thread_ts"] = (body.get("message") or {}).get("thread_ts")
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "clarify_interview_submit",
                "private_metadata": key,
                "title": {"type": "plain_text", "text": "Clarify requirements"},
                "submit": {"type": "plain_text", "text": "Submit"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": _clarify_modal_blocks(ambiguities),
            },
        )

    @app.view("clarify_interview_submit")
    def handle_clarify_interview_submit(ack, body, client, view, logger):
        ack()
        key = view.get("private_metadata") or ""
        payload = _pending_clarify.pop(key, None)
        if payload is None:
            logger.warning("clarify_interview_submit: unknown/expired key %r", key)
            return
        ambiguities = payload.get("ambiguities") or []
        requirement_ref = payload.get("requirement_ref")
        channel = payload.get("channel")
        thread_ts = payload.get("thread_ts")

        text, rows, _open_items = _clarified_requirements_text(
            ambiguities, view["state"]["values"], requirement_ref
        )

        kwargs = {"thread_ts": thread_ts} if thread_ts else {}
        try:
            if channel:
                client.chat_postMessage(
                    channel=channel, blocks=_mrkdwn_blocks(text),
                    text="Clarified Requirements", **kwargs,
                )
        except Exception:
            logger.exception("post clarified requirements failed")

        if requirement_ref and rows:
            try:
                db.update_node_props(
                    requirement_ref, "clarified_requirements",
                    [{"question": q, "answer": ans} for q, ans in rows],
                )
            except Exception:
                logger.exception("persist clarified_requirements failed")

    @app.event("app_mention")
    def handle_app_mention(event, body, say, client, logger):
        # Ignore bot authors / bot_message so the bot never replies to itself or other bots.
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return

        # Dedup: skip Slack retries and already-handled events.
        if body.get("retry_attempt"):
            return
        if _seen_before(body.get("event_id")):
            return

        # Always reply IN THREAD (Layer C reads answers from the thread later).
        thread_ts = event.get("thread_ts") or event["ts"]
        clean_text = _strip_mention(event.get("text", "")).strip()

        # Same shared turn handler used by non-mention thread replies.
        _handle_turn(say, client, event.get("channel"), thread_ts, clean_text,
                     logger, event.get("user"))

    @app.event("message")
    def handle_thread_reply(event, body, say, client, context, logger):
        # Continue the conversation in threads the bot is ALREADY part of, WITHOUT a
        # mention — but stay silent on all other channel chatter. Proceed only if
        # every guard passes; otherwise return without replying.

        # (a) plain user messages only — skip edits/deletes/joins/file_share/bot_message.
        if event.get("subtype"):
            return
        # (b) never react to bots or ourselves (prevents self-loops).
        if event.get("bot_id"):
            return
        bot_user_id = context.get("bot_user_id")
        if bot_user_id and event.get("user") == bot_user_id:
            return
        # (c) must be inside a thread.
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return
        # (d) if it explicitly @mentions the bot, app_mention handles it (no double-handling).
        text = (event.get("text") or "").strip()
        mentions_bot = (f"<@{bot_user_id}>" in text) if bot_user_id else ("<@" in text)
        if mentions_bot:
            return
        # (e) dedup retries / already-handled events.
        if body.get("retry_attempt"):
            return
        if _seen_before(body.get("event_id")):
            return
        # (f) bot must be a participant of THIS thread: thread_state must already exist.
        channel_id = event.get("channel")
        try:
            state = memory.get_thread_state(channel_id, thread_ts)
        except Exception:
            logger.exception("get_thread_state failed")
            return
        if not state or not text:
            return

        _handle_turn(say, client, channel_id, thread_ts, text, logger, event.get("user"))

    return app


def _startup_role_check():
    # Boot diagnostic: print exactly who each ROLE @mention resolves to, via the SAME
    # path the curator/bug/go-live flows use (db.mention_for(routing.approver_role_for(...))).
    # If this prints the wrong person at startup, the running process is stale OR pointed at
    # a different DB — restart / check DATABASE_URL. If it prints the right ids but Slack still
    # shows someone else, the live process was not restarted after the last code change.
    checks = [
        ("go_live      -> delivery_manager", "go_live"),
        ("testcase     -> qe_lead", "testcase"),
        ("bug          -> dev", "bug"),
        ("po_confirm   -> po", "po_confirm"),
    ]
    try:
        print(f"[role-check] build {_BOOT_TS} — role @mentions resolve to:")
        for label, key in checks:
            print(f"             {label:34s} = {db.mention_for(routing.approver_role_for(key))}")
    except Exception as e:  # never block startup on a diagnostic
        print(f"[role-check] skipped: {e}")


def main():
    missing = _missing_tokens()
    if missing:
        raise SystemExit(
            "Slack is not configured. Missing: "
            + ", ".join(missing)
            + ". Set them in .env (see .env.example)."
        )

    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app = build_app()
    handler = SocketModeHandler(app, config.SLACK_APP_TOKEN)
    print(f"Tieu Kiwi Slack app starting (Socket Mode), build {_BOOT_TS}. Ctrl+C to stop.")
    _startup_role_check()
    handler.start()


if __name__ == "__main__":
    main()
