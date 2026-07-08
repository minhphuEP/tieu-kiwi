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
import uuid
from datetime import datetime

from . import agent, config, db, memory, routing, slack_format, testcase_export, testcase_gen, tools

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

# Clarify-requirements intent: mimics the .claude/agents/brd-clarifier interview
# workflow (skills/requirement-clarity.md), but driven through Slack Block Kit
# instead of AskUserQuestion (Slack has no blocking equivalent).
_CLARIFY_RE = re.compile(r"clarify|làm rõ|resolve ambigui|ambiguous|ambiguity", re.I)
_CLARIFY_TRIGGER_STRIP_RE = re.compile(r"^\s*(clarify|làm rõ)\b[:\s]*", re.I)

# Section/input block `text` is capped at 3000 chars by Slack; stay well clear.
_CLARIFY_BLOCK_TEXT_MAX = 2800


def _golive_intent(text):
    # Return the requirement ref if this looks like a go-live question, else None.
    if not text or not _GOLIVE_RE.search(text):
        return None
    m = _REQ_RE.search(text)
    return m.group(1).upper() if m else None


# Test-case generation intent, e.g. "gen test case cho CDM-268", "tạo test case CDM-268".
_GEN_TC_RE = re.compile(r"gen(?:erate)?\s*test\s*case|t(ạ|a)o\s*test\s*case", re.I)


def _gen_testcase_intent(text):
    # Return the requirement ref if this looks like a "generate test cases" request, else None.
    if not text or not _GEN_TC_RE.search(text):
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


def handle_question(text, logger=None):
    # Shared logic for both entry points: run the Layer A agent and return a
    # Slack-friendly answer string. Never raises — a failure comes back as an error message.
    # The agent returns GitHub Markdown; convert it to the canonical Slack format.
    try:
        answer = agent.ask(text)
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


def post_candidate_to_curator(client, channel, candidate_id, rule_text, applies_to, approver_hint):
    # Post a candidate rule with approval buttons to `channel`.
    return client.chat_postMessage(
        channel=channel,
        blocks=_candidate_blocks(candidate_id, rule_text, applies_to, approver_hint),
        text=f"Candidate rule #{candidate_id} awaiting approval",
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


def _resolve_mention(role, project_id, logger=None, env_fallback=None):
    # Resolve a role -> Slack mention "<@id>" via the users table (project-scoped first).
    # Falls back to env_fallback id (last resort), then to a clear non-crashing text.
    sid = None
    try:
        sid = db.resolve_role_slack_id(role, project_id)
    except Exception:
        if logger is not None:
            logger.exception("resolve_role_slack_id failed")
    if not sid and env_fallback:
        sid = env_fallback
    if sid:
        return f"<@{sid}>"
    if logger is not None:
        logger.warning("No Slack user for role '%s' (project=%s) in users table", role, project_id)
    return f"@{role} (chưa cấu hình user cho role này trong bảng users)"


def _run_curator_demo(client, channel_id, user_id, logger=None):
    # Manual test path: enqueue a sample candidate and post it with buttons.
    applies_to = "TestCase"
    rule = "AC titles must be verifiable and unambiguous."
    cid = db.add_candidate_rule(
        rule, channel_id or "demo", applies_to,
        {"evidence": "manual curator-test", "by": user_id},
    )
    project_id = _project_for_channel(channel_id, logger)
    mention = _resolve_mention(routing.curator_role_for(applies_to), project_id, logger)
    post_candidate_to_curator(client, channel_id, cid, rule, applies_to, mention)
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


def _testcase_draft_blocks(draft):
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
    try:
        draft = testcase_gen.generate_draft(requirement_ref, project_id=project_id)
    except Exception as e:
        if logger is not None:
            logger.exception("generate_draft failed")
        say(text=slack_format.to_slack(f":warning: Error: {e}"))
        return
    kwargs = {"thread_ts": thread_ts} if thread_ts else {}
    posted = say(blocks=_testcase_draft_blocks(draft),
                 text=f"Draft test cases for {requirement_ref}", **kwargs)
    anchor_ts = thread_ts or posted["ts"]
    excel_file_id = _upload_draft_excel(
        client, channel_id, anchor_ts, draft["testcases"],
        filename=f"{requirement_ref}_testcases_v{draft['version']}.xlsx",
        comment=f":page_facing_up: Draft test cases (v{draft['version']}) — "
                f"{len(draft['testcases'])} testcase(s).",
        error_context="Excel export for this draft failed", logger=logger,
    )
    memory.save_thread_state(channel_id, anchor_ts,
                              {"flow": "gen_testcase", "draft_message_ts": posted["ts"],
                               "excel_file_id": excel_file_id, **draft})


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
        role = routing.approver_role_for("go_live")
        mention = _resolve_mention(
            role, project_id, logger, env_fallback=config.DELIVERY_MANAGER_SLACK_ID
        )
        blocks = blocks + _golive_approval_blocks(
            res.get("requirement") or requirement_ref, mention
        )
    say(blocks=blocks, text=f"Go/No-Go {requirement_ref}", **kwargs)


# ---------------------------------------------------------------- clarify-requirements interview

def _trunc(s, n):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _clarify_summary_blocks(ambiguities, requirement_ref):
    # Header + one section block PER question (not one joined block — a single
    # mrkdwn text field is capped at 3000 chars, and real questions times 8 would
    # blow past that if joined). The button's value is just a lookup key into
    # _pending_clarify — see its docstring for why the data never touches a
    # Slack length-limited field directly.
    header = slack_format.to_slack(
        f"*Requirement clarity check*{' — ' + requirement_ref if requirement_ref else ''}\n"
        f"Found *{len(ambiguities)}* open question(s): _(build {_BOOT_TS})_"
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
        say(blocks=_clarify_summary_blocks(ambiguities, requirement_ref),
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
            mention = _resolve_mention(routing.curator_role_for(applies_to), project_id, logger)
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
        channel_id = body["channel"]["id"]
        thread_ts = body["message"].get("thread_ts") or body["message"]["ts"]
        state = memory.get_thread_state(channel_id, thread_ts)
        if not state:
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                     text=":warning: No draft found for this thread.")
            return
        if _is_stale_draft_click(body, state):
            client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts,
                text=":warning: This draft has been superseded by a newer version — "
                     "please use the buttons on the latest draft message in this thread.",
            )
            return
        user = body["user"]["id"]
        try:
            testcase_gen.finalize_and_save(state, approved_by=user)
        except Exception as e:
            logger.exception("finalize_and_save failed")
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                     text=slack_format.to_slack(f":warning: Error saving testcases: {e}"))
            return
        # Remove the Approve/Refine buttons now that the DB write has succeeded,
        # so a double-click or Slack redelivery can't trigger a second export/upload.
        # Keep the rendered testcase list itself — only the actions block is
        # dropped — so the reviewer can still see what they approved.
        try:
            kept_blocks = [b for b in (body["message"].get("blocks") or [])
                           if b.get("type") != "actions"]
            kept_blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                "text": f":white_check_mark: Approved by <@{user}> (v{state['version']})"}})
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
        channel_id = body["channel"]["id"]
        thread_ts = body["message"].get("thread_ts") or body["message"]["ts"]
        state = memory.get_thread_state(channel_id, thread_ts)
        if not state:
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                     text=":warning: No draft found for this thread.")
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
        meta = json.loads(view["private_metadata"])
        channel_id, thread_ts = meta["channel_id"], meta["thread_ts"]
        comment = view["state"]["values"]["comment_block"]["comment_input"]["value"]
        state = memory.get_thread_state(channel_id, thread_ts)
        if not state:
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                     text=":warning: No draft found for this thread.")
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
        posted = client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            blocks=_testcase_draft_blocks(refined),
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
                                   "excel_file_id": excel_file_id, **refined})

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
        if not clean_text:
            usage = slack_format.to_slack(
                "Hi! Mention me with a QE question, e.g. "
                "`@Tieu Kiwi is FRONT-3494 ready to go live?`"
            )
            say(blocks=_mrkdwn_blocks(usage), text="Usage", thread_ts=thread_ts)
            return

        # Discard command -> cancel the in-progress testcase draft in this thread.
        # Checked before the interim "Processing…" ack so it stays snappy and
        # doesn't look like a new generation request is starting.
        if _discard_testcase_intent(clean_text):
            _do_discard_testcase(say, client, thread_ts, event.get("channel"),
                                  event.get("user"), logger)
            return

        # Interim ack in-thread so users see progress, then the final answer.
        try:
            say(text=slack_format.to_slack("Processing…"), thread_ts=thread_ts)
        except Exception:
            logger.exception("interim post failed")

        # Go-live question -> deterministic go_no_go + (on GO) curator sign-off buttons.
        ref = _golive_intent(clean_text)
        if ref:
            _do_golive(say, ref, logger, thread_ts=thread_ts, channel_id=event.get("channel"))
            return

        # Generate-testcase request -> draft + Approve/Refine buttons.
        tc_ref = _gen_testcase_intent(clean_text)
        if tc_ref:
            _do_gen_testcase(say, client, tc_ref, logger, thread_ts=thread_ts, channel_id=event.get("channel"))
            return

        # Clarify-requirements question -> find ambiguities + Slack interview modal.
        if _clarify_intent(clean_text):
            clarify_ref, raw_text = _clarify_target(clean_text)
            project_id = _project_for_channel(event.get("channel"), logger)
            source_text = _requirement_text_for_clarify(clarify_ref, logger) if clarify_ref else raw_text
            if not source_text:
                usage = slack_format.to_slack(
                    "Mention a requirement ref (e.g. `FRONT-3494`) or paste the BRD text "
                    "after \"clarify\"."
                )
                say(blocks=_mrkdwn_blocks(usage), text="Usage", thread_ts=thread_ts)
                return
            _do_clarify(say, clarify_ref, source_text, logger, thread_ts=thread_ts, project_id=project_id)
            return

        answer = handle_question(clean_text, logger)
        say(blocks=_mrkdwn_blocks(answer), text=answer, thread_ts=thread_ts)

    return app


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
    handler.start()


if __name__ == "__main__":
    main()
