"""Live Jira ingest — fetch a ticket and materialise its whole subtree in the graph.

Public entry:
    ingest_jira_ticket(issue_key, project_id) → dict summary

This is the orchestrator: fetch Jira → parse links → fetch Confluence → LLM
extract ACs → route subtasks (TestRun / Bug-table / skip). Idempotent: safe
to re-run; nodes upsert by (project_id, ref), edges by (src, rel, dst).

Building blocks (also exported):
    fetch_jira_issue         GET one Jira REST issue + subtask summaries
    parse_bug_subtask_table  ADF description of [Bug] subtask → list[dict]
    route_subtask            classify a subtask → TestRun / bug-container / skip
"""
import json
import re

import httpx

from . import adf, config, confluence, db


# --- HTTP -----------------------------------------------------------------

def _jira_get(path):
    if not (config.JIRA_BASE_URL and config.JIRA_EMAIL and config.JIRA_API_TOKEN):
        raise RuntimeError(
            "Jira auth not configured. Set JIRA_BASE_URL, JIRA_EMAIL, and "
            "JIRA_API_TOKEN in .env."
        )
    url = f"{config.JIRA_BASE_URL.rstrip('/')}{path}"
    resp = httpx.get(
        url,
        auth=(config.JIRA_EMAIL, config.JIRA_API_TOKEN),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_jira_issue(issue_key, expand_subtasks=True):
    """GET one issue with parent + subtasks metadata. Returns Jira REST JSON.

    Uses ?expand=renderedFields to get description ADF (default v3 already
    returns ADF but explicit expand is safer). Subtasks come in `fields.subtasks`
    but only with summary+status — call fetch_jira_issue(subtask_key) if you
    need the subtask's own description (e.g. for [Bug]-table parsing).
    """
    query = "?fields=*all" if expand_subtasks else ""
    return _jira_get(f"/rest/api/3/issue/{issue_key}{query}")


# --- issuetype mapping ---------------------------------------------------

def _map_node_type(jira_issuetype):
    """Jira issuetype string → ontology node type. Everything unknown → Requirement."""
    t = (jira_issuetype or "").lower()
    if t == "epic":
        return "UserStory"
    if t == "story":
        return "Requirement"
    if t == "subtask":
        return None      # subtask type is decided by summary (route_subtask)
    if t in ("bug",):
        return "Bug"
    return "Requirement"


# --- subtask routing -----------------------------------------------------

_ENV_PATTERNS = [
    (re.compile(r"\b(test\s+on\s+)?prod(uction)?\b", re.I), "prod"),
    (re.compile(r"\b(test\s+on\s+)?beta\b", re.I), "beta"),
    (re.compile(r"\b(test\s+on\s+)?(dev(elopment)?|staging|uat)\b", re.I), "dev"),
    (re.compile(r"\bself[- ]?test\b", re.I), "self"),
]

_BUG_PREFIX_RE = re.compile(r"^\s*\[bug\]\s*", re.I)


def route_subtask(summary):
    """Classify a subtask by its summary.

    Returns:
      ('bug_container', None)      — description contains a bug table → parse rows
      ('testrun', <env>)           — TestRun with env='self'|'dev'|'beta'|'prod'
      ('skip',    None)            — workflow item (e.g. "Create test cases")
    """
    if not summary:
        return ("skip", None)
    if _BUG_PREFIX_RE.match(summary):
        return ("bug_container", None)
    for pat, env in _ENV_PATTERNS:
        if pat.search(summary):
            return ("testrun", env)
    return ("skip", None)


# --- [Bug] subtask table parser ------------------------------------------

# Header alias map for the bug table (CDM team convention). Case-insensitive.
_BUG_HEADER_ALIASES = {
    "bug": "summary",
    "title": "summary",
    "description": "summary",
    "step": "steps",
    "steps": "steps",
    "actual": "actual",
    "expected": "expected",
    "priority": "severity",     # team column = severity
    "severity": "severity",
    "find by": "find_by",
    "find_by": "find_by",
    "found by": "find_by",
    "status": "status",
}


def _canonicalise_headers(header_row):
    """[('Bug','Step','Actual','Expected','Priority','Find by')] → dict {canonical: col_idx}."""
    mapping = {}
    for i, h in enumerate(header_row):
        key = (h or "").strip().lower()
        canonical = _BUG_HEADER_ALIASES.get(key)
        if canonical and canonical not in mapping:
            mapping[canonical] = i
    return mapping


_SEVERITY_MAP = {
    "highest": "critical", "blocker": "critical", "critical": "critical",
    "high": "high", "major": "high",
    "medium": "medium", "normal": "medium",
    "low": "low", "minor": "low", "lowest": "low", "trivial": "low",
}


def _normalise_severity(raw):
    return _SEVERITY_MAP.get((raw or "").strip().lower(), (raw or "").strip().lower() or None)


_DONE_MARKERS = ("🟢", ":green_circle:", "done", "closed", "fixed", "resolved")
_OPEN_MARKERS = ("🔴", "open", "todo", "to do")
_INPROGRESS_MARKERS = ("🟡", "inprogress", "in progress", "in_progress", "doing")


def _detect_status_from_marker(text):
    """When there's no Status column, some teams put a 🟢 emoji in the Bug title
    to mean 'done'. Return normalised status or None."""
    t = (text or "").lower()
    for m in _DONE_MARKERS:
        if m in t:
            return "done"
    for m in _INPROGRESS_MARKERS:
        if m in t:
            return "inprogress"
    for m in _OPEN_MARKERS:
        if m in t:
            return "open"
    return None


def parse_bug_subtask_table(description_adf):
    """Parse the description ADF of a `[Bug]` subtask into per-row bug dicts.

    Returns []  if no table found or table has no data rows.
    Returns [{summary, steps, actual, expected, severity, find_by, status, raw}, ...]
    """
    tables = adf.extract_tables(description_adf)
    if not tables:
        return []
    # Take the FIRST table with a recognisable header row. Multiple tables in
    # one description is rare in CDM convention but tolerate it.
    for table in tables:
        if not table or len(table) < 2:
            continue
        header = table[0]
        cols = _canonicalise_headers(header)
        if "summary" not in cols:
            continue  # not a bug table
        bugs = []
        for row in table[1:]:
            def _cell(name):
                idx = cols.get(name)
                if idx is None or idx >= len(row):
                    return None
                v = row[idx]
                return v.strip() if isinstance(v, str) and v.strip() else None
            summary = _cell("summary")
            if not summary:
                continue
            raw_status = _cell("status") or _detect_status_from_marker(summary)
            # Normalise team-specific status text ("Done"/"Open"/"InProgress") to
            # the lowercase values classify_bug / go_no_go compare against.
            status_map = {"done": "done", "closed": "done", "fixed": "done",
                          "resolved": "done", "open": "open", "todo": "open",
                          "to do": "open", "inprogress": "inprogress",
                          "in progress": "inprogress", "in_progress": "inprogress",
                          "doing": "inprogress"}
            status = status_map.get((raw_status or "").strip().lower(), raw_status)
            bugs.append({
                "summary": summary,
                "steps":    _cell("steps"),
                "actual":   _cell("actual"),
                "expected": _cell("expected"),
                "severity": _normalise_severity(_cell("severity")),
                "find_by":  _cell("find_by"),
                "status":   (status or "open"),
                "raw":      dict(zip(header, row)),
            })
        return bugs
    return []


# --- Node materialisation ------------------------------------------------

def _meta_jira(source_key, confidence=0.95):
    return {"_meta": {
        "extraction_source": "jira-rest",
        "confidence": confidence,
        "source_file": f"{config.JIRA_BASE_URL.rstrip('/')}/browse/{source_key}",
        "review_status": "verified",
    }}


def _upsert_epic_or_story(issue, project_id):
    """Upsert Epic (→UserStory node) or Story (→Requirement node) from a Jira issue.

    Returns (node_type, node_id, ref).
    """
    fields = issue.get("fields") or {}
    key = issue.get("key")
    issuetype = (fields.get("issuetype") or {}).get("name")
    node_type = _map_node_type(issuetype)
    if node_type is None:
        return (None, None, key)

    description_adf = fields.get("description")
    description_text = adf.to_pretty_text(description_adf) if description_adf else None

    props = {
        **_meta_jira(key),
        "title": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "priority": (fields.get("priority") or {}).get("name"),
        "jira_issuetype": issuetype,
        "assignee": (fields.get("assignee") or {}).get("displayName") or "Unassigned",
        "reporter": (fields.get("reporter") or {}).get("displayName"),
        "description": description_text,
    }
    parent = fields.get("parent") or {}
    if parent.get("key"):
        props["jira_parent_ref"] = parent["key"]

    node_id = db.upsert_node_by_ref(node_type, key, props, project_id=project_id)
    return (node_type, node_id, key)


def _upsert_testrun(subtask_key, subtask_summary, env, project_id):
    """Upsert a TestRun node from a test-env subtask."""
    # Basic subtask info comes from the parent-issue GET; fetch details only if
    # we care about status/comments — for TestRun we don't (yet).
    node_id = db.upsert_node_by_ref("TestRun", subtask_key, {
        **_meta_jira(subtask_key),
        "environment": env,
        "summary": subtask_summary,
        "status": "pending",
    }, project_id=project_id)
    return node_id


def _upsert_bugs_from_table(container_key, container_summary, parent_story_key,
                            bugs, project_id, component_ids):
    """Materialise N Bug nodes from parsed table rows.

    Ref pattern <container_key>-<row_idx>. Edges:
      Bug -affects-> each Component in component_ids
      find_by=Testcase → TR (self-test of parent story) -finds-> Bug  (best-effort;
        we don't know exact TR — leave optional linking to orchestrator)
    Returns list[(row_idx, node_id, ref)].
    """
    out = []
    for idx, bug in enumerate(bugs, start=1):
        ref = f"{container_key}-{idx}"
        node_id = db.upsert_node_by_ref("Bug", ref, {
            **_meta_jira(container_key),
            "summary": bug["summary"],
            "severity": bug["severity"] or "medium",
            "status": bug["status"] or "open",
            "find_by": bug["find_by"],
            "origin": "testing",
            "jira_container_ref": container_key,
            "jira_container_summary": container_summary,
            "jira_parent_ref": parent_story_key,
            "description": {
                "steps": bug["steps"],
                "actual": bug["actual"],
                "expected": bug["expected"],
            },
        }, project_id=project_id)
        for cid in component_ids:
            db.ensure_edge(node_id, "affects", cid)
        out.append((idx, node_id, ref))
    return out


# --- LLM AC extraction ---------------------------------------------------

_AC_EXTRACT_SYSTEM = """You are an information extraction engine for a QE (Quality Engineering) agent.

You receive the full text of one section of a Product Requirements Document (PRD).
Extract every Acceptance Criterion — the specific, testable conditions that must
hold for the feature to be considered correct.

Output shape (JSON, no prose, no markdown fences):
{
  "acceptance_criteria": [
    {"desc": "<one-sentence AC in the same language as the PRD (Vietnamese OK)>"},
    ...
  ]
}

Rules:
- Preserve original language.
- Split compound sentences into atomic ACs when possible ("both A and B" → 2 ACs).
- Focus on behaviour the QE could write a testcase for. Skip pure UI polish notes.
- Return valid JSON. No prose outside the object.
"""

_MAX_AC_INPUT_CHARS = 12000  # cap prompt so a huge PRD doesn't blow the LLM window


def _slice_section(body_text, section_anchor):
    """Best-effort: return only the text starting at the section that matches
    section_anchor, ending at the next heading of same-or-higher level.

    section_anchor is a Confluence URL slug like "15.-Assign-new-creator-...".
    Confluence slugifies headings by lowercasing + replacing whitespace with `-`
    + stripping most punctuation. We reverse-match by de-slugging.
    """
    if not section_anchor or not body_text:
        return body_text
    # Turn "15.-Assign-new-creator-for-a-booking-script-phase-2--ready" into
    # a fuzzy pattern that matches "15. Assign new creator ...".
    slug_tokens = [t for t in section_anchor.replace("-", " ").split() if t]
    if not slug_tokens:
        return body_text
    # Look for a heading line that contains ALL slug tokens in order.
    lines = body_text.splitlines()
    start_idx = None
    heading_level = None
    for i, line in enumerate(lines):
        m = re.match(r"^(#+)\s+(.*)", line)
        if not m:
            continue
        heading_text = m.group(2).lower()
        # Fuzzy: every slug token appears somewhere in the heading, in order.
        pos = 0
        ok = True
        for tok in slug_tokens:
            found = heading_text.find(tok.lower(), pos)
            if found < 0:
                ok = False
                break
            pos = found + len(tok)
        if ok:
            start_idx = i
            heading_level = len(m.group(1))
            break
    if start_idx is None:
        return body_text  # fall back to whole doc
    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        m = re.match(r"^(#+)\s+", lines[j])
        if m and len(m.group(1)) <= heading_level:
            end_idx = j
            break
    return "\n".join(lines[start_idx:end_idx])


def extract_acs_via_llm(brd_text, section_anchor=None):
    """Ask the ingestion LLM (Anthropic or Ollama, per LLM_PROVIDER) to extract
    ACs from a PRD section. Returns list[{desc: str}]. Empty list on any error —
    caller decides whether to warn.
    """
    from .llm import complete_json  # lazy import: LLM providers pull deps
    section_text = _slice_section(brd_text, section_anchor)
    if len(section_text) > _MAX_AC_INPUT_CHARS:
        section_text = section_text[:_MAX_AC_INPUT_CHARS] + "\n\n[...truncated for LLM window...]"
    if not section_text.strip():
        return []
    try:
        data = complete_json(section_text, system=_AC_EXTRACT_SYSTEM,
                             max_tokens=2000, temperature=0.1)
    except Exception:
        return []
    acs = data.get("acceptance_criteria") or []
    # Sanity filter: drop empty strings, cap length per AC
    out = []
    for i, ac in enumerate(acs):
        desc = (ac.get("desc") if isinstance(ac, dict) else str(ac)) or ""
        desc = desc.strip()
        if desc:
            out.append(desc[:500])
    return out


# --- Orchestrator ---------------------------------------------------------

def ingest_jira_ticket(issue_key, project_id, extract_acs=True, on_step=None):
    """Fetch a Jira ticket + its subtasks + every Confluence page it references,
    materialise everything in the graph and Chroma. Idempotent.

    Args:
      issue_key:   Jira key of the top-level Story (or Epic).
      project_id:  multi-tenant scope. All nodes upserted under this project.
      extract_acs: run LLM to pull ACs from BRD section. Default True.
      on_step:     optional progress callback (see tieukiwi.progress). Called
                   with sub-step events during the 6-stage pipeline.

    Returns:
      summary dict listing what was upserted / fetched / created.
    """
    from . import llm  # noqa

    def _sub(detail):
        if on_step is None:
            return
        try:
            on_step({"phase": "sub", "name": "ingest_jira_ticket", "detail": detail})
        except Exception:
            pass

    summary = {
        "tool": "ingest_jira_ticket",
        "issue_key": issue_key,
        "project_id": project_id,
        "epic": None,
        "requirement": None,
        "subtasks": {"testruns": [], "bug_containers": [], "skipped": []},
        "bugs": [],
        "confluence_pages": [],
        "acs_extracted": [],
        "warnings": [],
    }

    # 1. Fetch main issue
    _sub(f"Fetch Jira issue {issue_key}…")
    try:
        issue = fetch_jira_issue(issue_key)
    except (httpx.HTTPError, RuntimeError) as e:
        summary["status"] = "error"
        summary["error"] = str(e)
        return summary

    fields = issue.get("fields") or {}

    # 2. Upsert parent Epic (if any) → UserStory node
    parent = fields.get("parent") or {}
    parent_key = parent.get("key")
    epic_node_id = None
    if parent_key:
        parent_type = ((parent.get("fields") or {}).get("issuetype") or {}).get("name", "").lower()
        if parent_type == "epic":
            # We don't have full parent fields — fetch it for proper metadata.
            _sub(f"Fetch parent Epic {parent_key}…")
            try:
                parent_issue = fetch_jira_issue(parent_key)
                _, epic_node_id, _ = _upsert_epic_or_story(parent_issue, project_id)
                summary["epic"] = {"key": parent_key, "node_id": epic_node_id}
            except (httpx.HTTPError, RuntimeError) as e:
                summary["warnings"].append(f"Failed to fetch parent Epic {parent_key}: {e}")

    # 3. Upsert main Story / Requirement
    _sub(f"Upsert Story/Requirement {issue_key} vào graph…")
    node_type, req_node_id, req_key = _upsert_epic_or_story(issue, project_id)
    if node_type is None:
        summary["status"] = "error"
        summary["error"] = f"Unsupported issuetype for main issue {issue_key}"
        return summary
    summary["requirement"] = {"key": req_key, "node_id": req_node_id, "type": node_type}

    # Epic -has-> Requirement
    if epic_node_id and node_type == "Requirement":
        db.ensure_edge(epic_node_id, "has", req_node_id)

    # 4. Extract Confluence URLs from description → fetch each page
    description_adf = fields.get("description")
    urls = adf.extract_urls(description_adf) if description_adf else []
    confluence_targets = []
    for url in urls:
        parsed = adf.parse_confluence_url(url)
        if parsed:
            confluence_targets.append((parsed["page_id"], parsed["section_anchor"], url))

    if confluence_targets:
        _sub(f"Tải {len(confluence_targets)} Confluence page từ description…")
    for page_id, section_anchor, orig_url in confluence_targets:
        _sub(f"Fetch Confluence page {page_id}…")
        cf_result = confluence.fetch_confluence(
            page_id, project_id=project_id, section_anchor=section_anchor,
        )
        summary["confluence_pages"].append({
            "page_id": page_id,
            "section_anchor": section_anchor,
            "status": cf_result.get("status"),
            "node_id": cf_result.get("node_id"),
            "title": cf_result.get("title"),
            "chars": cf_result.get("chars"),
            "chunks_indexed": cf_result.get("chunks_indexed"),
        })
        if cf_result.get("status") in ("ok", "cached") and cf_result.get("node_id"):
            # Requirement -derivedFrom-> BRD (with section_anchor prop on the edge)
            db.ensure_edge(req_node_id, "derivedFrom", cf_result["node_id"],
                           props={"section_anchor": section_anchor} if section_anchor else None)

            # 5. Optional: LLM extract ACs for this section
            if extract_acs and cf_result.get("status") == "ok":
                brd_node = db.get_node_by_ref("BRD", f"CFL-{page_id}", project_id=project_id)
                if brd_node:
                    # rag stored the full text as chunks; we don't have raw text.
                    # Re-derive by asking Chroma or refetch. Cheapest: refetch once.
                    _sub(f"LLM tách Acceptance Criteria từ BRD (page {page_id})…")
                    ac_texts = _extract_acs_for_page(page_id, section_anchor)
                    for i, ac_desc in enumerate(ac_texts, start=1):
                        ac_ref = f"AC-{req_key}-{i}"
                        ac_node_id = db.upsert_node_by_ref(
                            "AcceptanceCriterion", ac_ref, {
                                "desc": ac_desc,
                                "_meta": {
                                    "extraction_source": "llm",
                                    "confidence": 0.75,
                                    "source_file": cf_result.get("url"),
                                    "review_status": "draft",
                                },
                            }, project_id=project_id,
                        )
                        db.ensure_edge(req_node_id, "has", ac_node_id)
                        summary["acs_extracted"].append({"ref": ac_ref, "desc": ac_desc[:80]})

    # 6. Route subtasks — TestRun / bug container / skip.
    # Do TestRuns first (need self-test TR id to link find_by=Testcase bugs).
    subtask_stubs = fields.get("subtasks") or []
    if subtask_stubs:
        _sub(f"Phân loại {len(subtask_stubs)} subtask (TestRun / Bug table)…")
    testrun_by_env = {}
    bug_container_stubs = []
    for st in subtask_stubs:
        st_key = st.get("key")
        st_summary = ((st.get("fields") or {}).get("summary") or "").strip()
        kind, env = route_subtask(st_summary)
        if kind == "testrun":
            tr_id = _upsert_testrun(st_key, st_summary, env, project_id)
            testrun_by_env[env] = tr_id
            summary["subtasks"]["testruns"].append({"key": st_key, "env": env, "node_id": tr_id})
        elif kind == "bug_container":
            bug_container_stubs.append((st_key, st_summary))
        else:
            summary["subtasks"]["skipped"].append({"key": st_key, "summary": st_summary})

    # Now Bug containers — use self-test TR (if present) to link find_by=Testcase.
    self_test_tr_id = testrun_by_env.get("self")
    for st_key, st_summary in bug_container_stubs:
        _sub(f"Parse bảng bug trong subtask {st_key}…")
        try:
            subtask_full = fetch_jira_issue(st_key)
        except (httpx.HTTPError, RuntimeError) as e:
            summary["warnings"].append(f"Failed to fetch subtask {st_key}: {e}")
            continue
        desc_adf = (subtask_full.get("fields") or {}).get("description")
        bugs_raw = parse_bug_subtask_table(desc_adf)
        bug_nodes = _upsert_bugs_from_table(
            st_key, st_summary, req_key, bugs_raw, project_id, component_ids=[],
        )
        # Team convention: find_by=Testcase means QE's self-test caught it →
        # add TR_self -finds-> Bug so classify_bug returns caught_by_test.
        if self_test_tr_id:
            for idx, bug_node_id, _ref in bug_nodes:
                if (bugs_raw[idx - 1]["find_by"] or "").strip().lower() == "testcase":
                    db.ensure_edge(self_test_tr_id, "finds", bug_node_id)
        summary["subtasks"]["bug_containers"].append({
            "key": st_key, "rows_parsed": len(bugs_raw),
        })
        for idx, node_id, ref in bug_nodes:
            summary["bugs"].append({
                "ref": ref, "node_id": node_id,
                "find_by": bugs_raw[idx-1]["find_by"],
                "severity": bugs_raw[idx-1]["severity"],
                "status": bugs_raw[idx-1]["status"],
            })

    summary["status"] = "ok"
    return summary


def _extract_acs_for_page(page_id, section_anchor):
    """Helper: re-fetch page body (cheap, already cached by Confluence side or
    we hit REST once more) and hand to LLM for AC extraction. Kept private
    because it needs the raw body text which we don't persist in Postgres."""
    try:
        page = confluence._confluence_get(
            f"/api/v2/pages/{page_id}?body-format=atlas_doc_format"
        )
    except (httpx.HTTPError, RuntimeError):
        return []
    body_val = ((page.get("body") or {}).get("atlas_doc_format") or {}).get("value")
    if isinstance(body_val, str):
        try:
            body_adf = json.loads(body_val)
        except json.JSONDecodeError:
            return []
    else:
        body_adf = body_val
    body_text = adf.to_pretty_text(body_adf) if body_adf else ""
    return extract_acs_via_llm(body_text, section_anchor)
