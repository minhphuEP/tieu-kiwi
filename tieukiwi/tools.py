import json
import re
from html import unescape

import httpx
from anthropic import Anthropic

from . import config, db, rag, testcase_gen

_client = Anthropic(api_key=config.ANTHROPIC_API_KEY)


# --- Layer A skeletons (TODO: implement) ---
# These tools are registered so the agent knows they exist, but the generation /
# integration logic is not built yet. Fill these in during the Layer A build-out.

def _not_implemented(tool, todo):
    return {"tool": tool, "status": "not_implemented", "todo": todo}


def gen_testcase(requirement_ref, project_id=None):
    """Draft (or update) testcases for a requirement. Returns the draft dict from
    testcase_gen.generate_draft — plain-chat use (no Slack Approve/Refine loop;
    that loop is driven directly from tieukiwi/slack_app.py, see docs/Gen-testcase-design.md).
    Returns a {"tool": "gen_testcase", "status": "error", "error": ...} dict (instead of
    raising) if the requirement isn't found, the LLM leaves an AC uncovered, or the LLM
    returns malformed/incomplete JSON — consistent with fetch_jira's error convention."""
    try:
        return testcase_gen.generate_draft(requirement_ref, project_id=project_id)
    except (ValueError, KeyError) as e:
        return {"tool": "gen_testcase", "status": "error", "error": str(e)}


def gen_test_plan(requirement_ref):
    model = config.model_for("gen_test_plan")  # TODO: pass into the Claude call when implemented
    # TODO: aggregate ACs/testcases for the requirement and draft a structured test plan (model=model).
    return _not_implemented(
        "gen_test_plan", "Generate a structured test plan via Claude."
    )


_AMBIGUITY_SYSTEM = """You are Tieu Kiwi's requirement-clarity reviewer. Given a
requirement/BRD/PRD/Jira story below, flag genuine ambiguities against the three
dimensions in the KB rubric provided (Behaviour and Edge Cases, Constraints, Conflicts).
Do not manufacture problems in a well-specified section — a requirement with zero
findings is valid; return an empty list for it.

Phrase each ambiguity as a direct question the PO can answer inline, per the rubric's
"Turning Findings into PO Questions" guidance. If more than 3 genuine ambiguities are
found, prioritize the rubric's "Top 3 PO Questions" first — missing/invalid data
handling, feature-flag/rollout gating, and conflicting-requirement resolution — before
filling remaining slots with other findings.

Return ONLY valid JSON (no prose, no markdown fences), exactly this shape:
{"ambiguities": [{"dimension": "Behaviour and Edge Cases" | "Constraints" | "Conflicts", "question": "<direct question for the PO>", "gap": "<one-sentence description of what's missing>"}]}

At most 2 ambiguities per dimension (6 total), the most important ones."""

# Slack modal input blocks are capped for usability; keep the interview short.
MAX_AMBIGUITIES = 6

# Claude sometimes wraps JSON output in a ```json fence even when told not to,
# especially with a long user message (e.g. an expanded Confluence PRD). A bare
# json.loads() on that raw text raises and silently degrades to "no ambiguities
# found" — which looks identical to a genuinely well-specified requirement.
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.S)


def _strip_json_fence(raw):
    raw = (raw or "").strip()
    m = _JSON_FENCE_RE.match(raw)
    return m.group(1).strip() if m else raw


def find_ambiguities(text, project_id=None):
    model = config.model_for("find_ambiguities")
    rules = rag.search(
        "requirement ambiguity scope behaviour constraints conflicts acceptance criteria testability",
        k=4, project_id=project_id, include_global=True,
    )
    rules_block = "\n\n".join(
        f"[{meta.get('parent_doc', doc_id)}"
        + (f" § {meta['section']}" if meta.get("section") else "")
        + f"]\n{doc}"
        for doc_id, doc, meta in rules
    ) or "(no matching rubric found in the KB)"

    user_msg = f"## KB rubric\n{rules_block}\n\n## Requirement text\n{text}"

    resp = _client.messages.create(
        model=model,
        max_tokens=1500,
        system=_AMBIGUITY_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = resp.content[0].text
    try:
        data = json.loads(_strip_json_fence(raw))
    except (json.JSONDecodeError, TypeError):
        data = {"ambiguities": []}

    ambiguities = [
        a for a in (data.get("ambiguities") or [])
        if isinstance(a, dict) and a.get("question")
    ][:MAX_AMBIGUITIES]

    return {"tool": "find_ambiguities", "status": "ok", "ambiguities": ambiguities}


def _adf_to_text(node):
    # Best-effort flatten of Atlassian Document Format (or a plain string) to text.
    # inlineCard/blockCard/embedCard are link previews (e.g. a linked PRD page) with
    # no "text" or "content" of their own — without this they silently vanish, which
    # is how a description that's *just* links (see CDM-268) flattens to "".
    if node is None:
        return None
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        if node.get("type") in ("inlineCard", "blockCard", "embedCard"):
            return (node.get("attrs") or {}).get("url") or ""
        return "".join(t for t in (_adf_to_text(c) for c in node.get("content", [])) if t)
    if isinstance(node, list):
        return "".join(_adf_to_text(n) or "" for n in node)
    return None


def _story_points(fields):
    # Story points live in a custom field whose id varies per Jira instance.
    # Try the common ids and return the first numeric value; skip if none present.
    for k in ("customfield_10016", "customfield_10026", "customfield_10004", "customfield_10002"):
        v = fields.get(k)
        if isinstance(v, (int, float)):
            return v
    return None


def fetch_jira(issue_key):
    # Read a Jira Cloud issue (REST v3) and upsert it into the graph as a Requirement.
    if not (config.JIRA_BASE_URL and config.JIRA_EMAIL and config.JIRA_API_TOKEN):
        return {
            "tool": "fetch_jira",
            "status": "error",
            "error": "Jira is not configured. Set JIRA_BASE_URL, JIRA_EMAIL, and "
                     "JIRA_API_TOKEN in .env (see .env.example).",
        }

    url = f"{config.JIRA_BASE_URL.rstrip('/')}/rest/api/3/issue/{issue_key}"
    try:
        resp = httpx.get(
            url,
            auth=(config.JIRA_EMAIL, config.JIRA_API_TOKEN),
            headers={"Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return {
            "tool": "fetch_jira",
            "status": "error",
            "error": f"Jira API returned HTTP {e.response.status_code} for {issue_key}.",
        }
    except httpx.HTTPError as e:
        return {"tool": "fetch_jira", "status": "error", "error": f"Jira request failed: {e}"}

    data = resp.json()
    fields = data.get("fields", {}) or {}
    key = data.get("key", issue_key)
    summary = fields.get("summary")
    description = _adf_to_text(fields.get("description"))
    issuetype = (fields.get("issuetype") or {}).get("name")
    status = (fields.get("status") or {}).get("name")
    priority = (fields.get("priority") or {}).get("name")

    # People: assignee may be JSON null -> "Unassigned". Guard every level with `or {}`.
    assignee = (fields.get("assignee") or {}).get("displayName") or "Unassigned"
    reporter = (fields.get("reporter") or {}).get("displayName")

    # Optional fields — vary per Jira instance; never fail if absent.
    fix_versions = [v.get("name") for v in (fields.get("fixVersions") or []) if v.get("name")]
    story_points = _story_points(fields)

    props = {
        "source": "jira",
        "summary": summary,
        "status": status,
        "issuetype": issuetype,
        "priority": priority,
        "assignee": assignee,
        "reporter": reporter,
        "description": description,
    }
    if fix_versions:
        props["fix_versions"] = fix_versions
    if story_points is not None:
        props["story_points"] = story_points

    node_id = db.upsert_node_by_ref("Requirement", key, props)

    issue = {
        "key": key,
        "summary": summary,
        "issuetype": issuetype,
        "status": status,
        "priority": priority,
        "assignee": assignee,
        "reporter": reporter,
    }
    if fix_versions:
        issue["fix_versions"] = fix_versions
    if story_points is not None:
        issue["story_points"] = story_points

    return {"tool": "fetch_jira", "status": "ok", "issue": issue, "node_id": node_id}


# --- Confluence (PRD pages linked from Jira descriptions) ------------------

_CONFLUENCE_PAGE_RE = re.compile(r"/wiki/spaces/[^/\s]+/pages/(\d+)")

# Cap how many linked pages we fetch per requirement — avoids unbounded fan-out
# if a description links to several pages.
MAX_CONFLUENCE_LINKS = 2


def _html_to_text(html):
    # Confluence page bodies are XHTML "storage format" — strip tags/entities for
    # a plain-text body. Regex-based on purpose: this repo has no HTML parser dep,
    # and a PRD body doesn't need real DOM handling, just its words.
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_confluence_page(page_id):
    """Fetch a Confluence Cloud page's body as plain text, or None if unavailable.

    Reuses the Jira email + API token (same Atlassian Cloud site convention),
    per CLAUDE.md's Confluence auth note. Never raises — a missing page or
    missing config is just "no extra context", not a hard error.
    """
    if not (config.CONFLUENCE_BASE_URL and config.JIRA_EMAIL and config.JIRA_API_TOKEN):
        return None
    url = f"{config.CONFLUENCE_BASE_URL.rstrip('/')}/rest/api/content/{page_id}"
    try:
        resp = httpx.get(
            url, params={"expand": "body.storage"},
            auth=(config.JIRA_EMAIL, config.JIRA_API_TOKEN),
            headers={"Accept": "application/json"}, timeout=30,
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    html = ((resp.json().get("body") or {}).get("storage") or {}).get("value") or ""
    return _html_to_text(html) or None


def expand_with_confluence(text):
    """Append the body of any linked Confluence page(s) found in `text`.

    Jira descriptions often link OUT to the real PRD (an inlineCard) instead of
    containing it inline — see _adf_to_text. Without this, tools like
    find_ambiguities only ever sees the label + URL, never the spec.
    """
    if not text:
        return text
    page_ids = list(dict.fromkeys(_CONFLUENCE_PAGE_RE.findall(text)))[:MAX_CONFLUENCE_LINKS]
    parts = [text]
    for page_id in page_ids:
        try:
            body = fetch_confluence_page(page_id)
        except Exception:
            body = None
        if body:
            parts.append(f"## Linked Confluence page ({page_id})\n{body}")
    return "\n\n".join(parts)


TOOLS = [
  {
    "name": "search_kb",
    "description": (
        "Find relevant rules/glossary/rubrics in the KB. Retrieves across all "
        "personas by default; pass `role` only when the user explicitly asks for "
        "docs owned by a specific persona."
    ),
    "input_schema": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "Natural-language question (Vietnamese or English).",
        },
        "role": {
          "type": "string",
          "enum": ["QE", "PO", "BO", "DEV"],
          "description": (
            "Optional persona filter. Set ONLY when the user asks for docs OWNED BY "
            "a persona (e.g. 'QE templates', 'PO PRD template'). Do NOT set based "
            "on the user's own role — a QE asking for context to write test cases "
            "still needs domain and spec docs from other personas, so leave omitted. "
            "Rule of thumb: 'QE templates' → role=QE; 'help me write tests' → omit."
          ),
        },
        "k": {
          "type": "integer",
          "description": "Max results to return (default 4).",
        },
      },
      "required": ["query"],
    },
  },
  {
    "name": "coverage_gap",
    "description": "List AcceptanceCriterion items not yet covered by any TestCase.",
    "input_schema": {"type": "object", "properties": {}},
  },
  {
    "name": "get_ticket",
    "description": (
        "READ-ONLY, POLYMORPHIC lookup for ANY Jira ticket ref (CDM-199, CDM-263, "
        "CDM-286, CDM-286-1...). Dispatches by node type: Requirement returns "
        "AC list + BRD + coverage; Bug returns severity + violates + finds; "
        "TestRun returns TestCase + bugs found; UserStory/Epic returns children; "
        "BRD returns preview + downstream requirements. Smart lookup: falls back "
        "to `TR-<ref>` when the direct key misses (test-subtasks are stored with "
        "the TR- prefix). ALWAYS call this FIRST when user asks about a ticket. "
        "If found=False OR warnings mention missing data, then call "
        "ingest_jira_ticket to pull from Jira. NEVER invent data not in the "
        "returned payload — echo `warnings` verbatim to the user."
    ),
    "input_schema": {
      "type": "object",
      "properties": {"ref": {"type": "string"}},
      "required": ["ref"],
    },
  },
  {
    "name": "go_no_go",
    "description": (
        "Assess whether a requirement/feature is ready to go live in production; "
        "returns a GO/NO-GO decision plus the actions needed if NO-GO."
    ),
    "input_schema": {
      "type": "object",
      "properties": {"requirement_ref": {"type": "string"}},
      "required": ["requirement_ref"],
    },
  },
  {
    "name": "trace",
    "description": (
        "Trace a requirement's path: Requirement -> AcceptanceCriteria -> TestCases "
        "-> TestRuns -> Bugs, showing which AC is covered and its pass/fail status."
    ),
    "input_schema": {
      "type": "object",
      "properties": {"requirement_ref": {"type": "string"}},
      "required": ["requirement_ref"],
    },
  },
  {
    "name": "bug_blast_radius",
    "description": (
        "Estimate a bug's blast radius: how many Requirements/AcceptanceCriteria depend "
        "on the Component(s) it affects, and a derived priority (P1-P4)."
    ),
    "input_schema": {
      "type": "object",
      "properties": {"bug_ref": {"type": "string"}},
      "required": ["bug_ref"],
    },
  },
  {
    "name": "classify_bug",
    "description": (
        "Classify how a bug was detected to route it into the improvement loop. "
        "Returns one of: caught_by_test | leaked_tc_missing | leaked_tc_not_run | "
        "leaked_tc_ran_missed | leaked_no_ac_link. Each 'leaked_*' category points at "
        "which pipeline needs improvement (gen_testcase, impact_analysis, execution_quality)."
    ),
    "input_schema": {
      "type": "object",
      "properties": {"bug_ref": {"type": "string"}},
      "required": ["bug_ref"],
    },
  },
  {
    "name": "mark_reviewed",
    "description": (
        "Advance a TestCase through the review state machine. Use when a QE or "
        "QE Lead explicitly approves/rejects a testcase in Slack (\"QE Dung "
        "approve TC-CDM-268-A\", \"lead reject CDM_DupScript_002 vì thiếu step\"). "
        "State transitions: draft → qe_pending → qe_reviewed → lead_pending → "
        "lead_approved (any state → rejected). Records reviewer_slack_id and "
        "timestamp per stage in TestCase.props for the audit trail. Do NOT call "
        "this for questions ABOUT a testcase — only when the user is actually "
        "signing off / rejecting."
    ),
    "input_schema": {
      "type": "object",
      "properties": {
        "tc_ref": {"type": "string",
                   "description": "TestCase ref, e.g. 'CDM_DupScript_002' or 'TC-CDM-268-A'."},
        "decision": {"type": "string", "enum": ["approve", "reject"],
                     "description": "'approve' advances to next state; 'reject' → 'rejected'."},
        "reviewer_slack_id": {"type": "string",
                              "description": "Slack user id of the reviewer (U0..). "
                                             "The Slack layer usually fills this from event.user."},
        "comments": {"type": "string",
                     "description": "Optional free-text note recorded on the transition. "
                                    "Include when the user gave a reason for rejection or a caveat."},
      },
      "required": ["tc_ref", "decision", "reviewer_slack_id"],
    },
  },
  {
    "name": "gen_testcase",
    "description": "Generate or update test cases for a requirement, following the KB template/rubric. Returns a draft — for interactive Approve/Refine review, use the Slack flow instead of this direct tool call.",
    "input_schema": {
      "type": "object",
      "properties": {"requirement_ref": {"type": "string"}},
      "required": ["requirement_ref"],
    },
  },
  {
    "name": "gen_test_plan",
    "description": "Generate a test plan for a requirement. (SKELETON — TODO: implement LLM generation.)",
    "input_schema": {
      "type": "object",
      "properties": {"requirement_ref": {"type": "string"}},
      "required": ["requirement_ref"],
    },
  },
  {
    "name": "find_ambiguities",
    "description": (
        "Identify genuine ambiguities in a requirement/BRD/PRD/Jira story against the "
        "four ambiguity dimensions (scope/ownership, behaviour/edge cases, constraints, "
        "conflicts), phrased as direct questions for the PO. Returns an empty list when "
        "the text is already sufficiently specified."
    ),
    "input_schema": {
      "type": "object",
      "properties": {"text": {"type": "string"}},
      "required": ["text"],
    },
  },
  {
    "name": "fetch_jira",
    "description": (
        "Fetch a Jira issue by key (e.g. PROJ-123) from Jira Cloud and store it in the "
        "graph as a Requirement node. Returns key, summary, issuetype, status, priority, "
        "assignee (or 'Unassigned'), reporter (+ optional fix_versions / story_points) and node id."
    ),
    "input_schema": {
      "type": "object",
      "properties": {"issue_key": {"type": "string"}},
      "required": ["issue_key"],
    },
  },
  {
    "name": "ingest_jira_ticket",
    "description": (
        "Full-fetch a Jira ticket into the graph: upsert the Story/Requirement, its "
        "parent Epic (as UserStory), all subtasks (test-env subtasks → TestRun nodes; "
        "[Bug]-prefixed subtasks → parse the description table into N Bug nodes per row), "
        "and EVERY Confluence page linked in the description (BRD node + chunks in KB). "
        "Optionally LLM-extract Acceptance Criteria from the BRD section. Idempotent: "
        "safe to re-run on the same ticket — nodes upsert, edges dedupe. "
        "Use this as the DEFAULT tool when the user asks to analyze / review / import a "
        "Jira ticket (e.g. 'phân tích CDM-269', 'kéo CDM-321 vào graph', 'review ticket ABC-123')."
    ),
    "input_schema": {
      "type": "object",
      "properties": {
        "issue_key": {
          "type": "string",
          "description": "Jira issue key of the top-level Story (or Epic), e.g. 'CDM-268'.",
        },
        "extract_acs": {
          "type": "boolean",
          "description": "If true (default), also run LLM to extract Acceptance Criteria "
                         "from the linked Confluence PRD section. Set false to skip when "
                         "the user just wants to sync structure without generating ACs.",
        },
      },
      "required": ["issue_key"],
    },
  },
  {
    "name": "fetch_confluence",
    "description": (
        "Fetch ONE Confluence page by page_id, upsert a BRD node with its metadata, "
        "and chunk-index the body into the KB (Chroma) so `search_kb` can retrieve it. "
        "Idempotent via content_hash — a repeat call on an unchanged page returns "
        "status='cached' and skips embedding. Use this when the user pastes a Confluence "
        "link directly, NOT after `ingest_jira_ticket` (which already fetches every "
        "Confluence page linked in the Jira description)."
    ),
    "input_schema": {
      "type": "object",
      "properties": {
        "page_id": {
          "type": "string",
          "description": "Numeric Confluence page id from the URL "
                         "(https://<site>.atlassian.net/wiki/spaces/<space>/pages/<PAGE_ID>/...).",
        },
        "section_anchor": {
          "type": "string",
          "description": "URL fragment slug from a section link "
                         "(e.g. '15.-Assign-new-creator-...'). Stored on the BRD node so "
                         "downstream tools can filter chunks by section. Omit if not provided.",
        },
      },
      "required": ["page_id"],
    },
  },
]

def run_tool(name, args, context=None):
    """Dispatch a tool call.

    Args:
      name:    the tool name (must be in TOOLS)
      args:    dict of tool-specific arguments (matches input_schema)
      context: dict of ambient context propagated from the agent loop.
               Keys used here:
                 project_id  — scope Postgres queries + RAG search by tenant
               `context` is set by the Slack layer (channel_id -> project_id)
               before calling agent.ask(). It is NOT part of the LLM-facing
               input_schema — the LLM cannot spoof it.
    """
    ctx = context or {}
    project_id = ctx.get("project_id")

    if name == "search_kb":
        # `role` is an opt-in filter set by the LLM via args, NOT auto-injected
        # from the caller's persona. A QE writing tests still needs BO domain
        # docs and PO PRDs, so filtering by caller role would cripple retrieval.
        # include_global=True gives project docs + shared _global docs together.
        return rag.search(
            args["query"],
            k=args.get("k", 4),
            project_id=project_id,
            role=args.get("role"),
            include_global=True,
        )
    if name == "get_ticket":
        return db.get_ticket(args["ref"], project_id=project_id)
    if name == "mark_reviewed":
        return db.mark_reviewed(
            args["tc_ref"],
            decision=args["decision"],
            reviewer_slack_id=args["reviewer_slack_id"],
            comments=args.get("comments"),
            project_id=project_id,
        )
    if name == "coverage_gap":
        return db.coverage_gap(project_id=project_id)
    if name == "go_no_go":
        return db.go_no_go(args["requirement_ref"], project_id=project_id)
    if name == "trace":
        return db.trace(args["requirement_ref"], project_id=project_id)
    if name == "bug_blast_radius":
        return db.bug_blast_radius(args["bug_ref"], project_id=project_id)
    if name == "classify_bug":
        return db.classify_bug(args["bug_ref"], project_id=project_id)
    if name == "gen_testcase":
        return gen_testcase(args["requirement_ref"], project_id=project_id)
    if name == "gen_test_plan":
        return gen_test_plan(args["requirement_ref"])
    if name == "find_ambiguities":
        return find_ambiguities(args["text"], project_id=project_id)
    if name == "fetch_jira":
        return fetch_jira(args["issue_key"])
    if name == "ingest_jira_ticket":
        from . import jira_ingest
        on_step = ctx.get("on_step")
        return jira_ingest.ingest_jira_ticket(
            args["issue_key"],
            project_id=project_id,
            extract_acs=args.get("extract_acs", True),
            on_step=on_step,
        )
    if name == "fetch_confluence":
        from . import confluence
        return confluence.fetch_confluence(
            args["page_id"],
            project_id=project_id,
            section_anchor=args.get("section_anchor"),
        )
    raise ValueError(f"Unknown tool: {name}")