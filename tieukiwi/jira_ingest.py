"""Live Jira ingest — fetch a ticket and materialise its whole subtree in the graph.

Public entry:
    ingest_jira_ticket(issue_key, project_id) → dict summary

This is the orchestrator: fetch Jira → parse links → fetch Confluence → LLM
extract ACs → route subtasks (TestRun / Bug-table / skip). Idempotent: safe
to re-run; nodes upsert by (project_id, ref), edges by (src, rel, dst).

Freshness: content-based hash-gate (not TTL). See `_story_hash` and
`_bug_table_hash`. On re-fetch, if the canonical hash on the stored
Requirement matches the freshly-computed one, the whole pipeline short-
circuits with status='cached_fresh' — no Confluence fetch, no LLM AC pass.
Pass `force=True` (or user says "cập nhật"/"refresh") to bypass.

Building blocks (also exported):
    fetch_jira_issue         GET one Jira REST issue + subtask summaries
    parse_bug_subtask_table  ADF description of [Bug] subtask → list[dict]
    route_subtask            classify a subtask → TestRun / bug-container / skip
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import unquote

import httpx

from . import adf, config, confluence, db


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# --- Hash-gate helpers ---------------------------------------------------

def _story_hash(fields):
    """Canonical hash of the Story's DECISION-RELEVANT fields.

    Includes subtask stubs (key/summary/status) so a subtask being added,
    removed, or transitioning status invalidates the story cache. Does NOT
    include subtask descriptions — those get their own hash via
    `_bug_table_hash` (only fetched when the story hash is dirty, saving
    N unnecessary REST calls).

    Fields excluded on purpose to avoid churn:
      - fields.updated (Jira touches this on last-viewed too, unreliable)
      - custom fields, votes, watchers, comment count
    """
    subtasks_stub = fields.get("subtasks") or []
    description_adf = fields.get("description")
    canonical = {
        "summary": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "assignee": (fields.get("assignee") or {}).get("displayName"),
        "priority": (fields.get("priority") or {}).get("name"),
        "description_text": adf.to_pretty_text(description_adf) if description_adf else "",
        "confluence_urls": sorted(
            adf.extract_urls(description_adf) if description_adf else []
        ),
        "subtasks": sorted([
            {"key": st.get("key"),
             "summary": (st.get("fields") or {}).get("summary"),
             "status": ((st.get("fields") or {}).get("status") or {}).get("name")}
            for st in subtasks_stub
        ], key=lambda s: s["key"] or ""),
    }
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _check_brd_freshness(req_node_id, on_step=None):
    """For every BRD a Requirement derives from, hit Confluence's cheap metadata
    endpoint and compare `version.number` against what we stored.

    Returns [{ref, page_id, stored_version, current_version}, ...] for BRDs
    whose Confluence version has drifted (i.e. PRD edited without touching
    Jira). Empty list = all fresh (safe to short-circuit).

    Fail-safe: any HTTP / network error on the freshness check is treated as
    "assume fresh" — a Confluence outage should not force full re-ingest of
    every ticket. The specific BRD is just skipped from the drift check.
    """
    stale = []
    for brd in db.linked_brds(req_node_id):
        props = brd.get("props_json") or {}
        page_id = props.get("page_id")
        stored_version = props.get("version")
        if not page_id or stored_version is None:
            continue
        try:
            meta = confluence.get_page_metadata(page_id)
        except (httpx.HTTPError, RuntimeError):
            if on_step:
                try:
                    on_step(f"BRD {brd['ref']} freshness check bỏ qua (Confluence không phản hồi).")
                except Exception:
                    pass
            continue
        current_version = meta.get("version")
        if current_version is not None and current_version != stored_version:
            stale.append({
                "ref": brd["ref"],
                "page_id": page_id,
                "stored_version": stored_version,
                "current_version": current_version,
            })
    return stale


def _bug_table_hash(description_adf):
    """Hash the raw table rows in a [Bug] subtask description.

    Catches the case where the parent story hash is unchanged (subtask
    summary/status not touched) but a bug row was added/edited/removed
    inside the table — story hash misses this by design (it doesn't fetch
    each subtask's description).
    """
    tables = adf.extract_tables(description_adf) if description_adf else []
    canonical = tables[0] if tables else []
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


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


_HEADER_ANNOTATION_RE = re.compile(r"\s*\([^)]*\)\s*")


def _canonicalise_headers(header_row):
    """[('Bug','Step','Actual','Expected','Priority','Find by')] → dict {canonical: col_idx}.

    Also tolerates parenthetical annotations the Jira template adds
    ("Bug(Required)", "Root Cause(optional)") — the annotation carries no
    semantic value for parsing, so strip it before alias lookup.
    """
    mapping = {}
    for i, h in enumerate(header_row):
        key = _HEADER_ANNOTATION_RE.sub(" ", (h or "").lower()).strip()
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
        "jira_key": key,   # explicit identity — survives ref renames (VD TR- prefix)
        "title": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "priority": (fields.get("priority") or {}).get("name"),
        "jira_issuetype": issuetype,
        "assignee": (fields.get("assignee") or {}).get("displayName") or "Unassigned",
        "reporter": (fields.get("reporter") or {}).get("displayName"),
        "description": description_text,
        # Hash-gate: canonical fingerprint of decision-relevant fields.
        # Kept only on the STORY (Requirement) — Epic-level hash isn't used
        # to short-circuit, and the Story hash already includes Epic parent
        # transitively via jira_parent_ref lookup.
        "story_hash": _story_hash(fields) if node_type == "Requirement" else None,
        "jira_updated": fields.get("updated"),   # ISO-8601, cheap tie-breaker
        "last_ingested_at": _now_iso(),
    }
    parent = fields.get("parent") or {}
    if parent.get("key"):
        props["jira_parent_ref"] = parent["key"]

    node_id = db.upsert_node_by_ref(node_type, key, props,
                                     project_id=project_id, merge_props=True)
    return (node_type, node_id, key)


def _upsert_testrun(subtask_key, subtask_summary, env, project_id, parent_story_key=None):
    """Upsert a TestRun node from a test-env subtask.

    Ref = `TR-<subtask_key>` (TR- prefix avoids collision under (project_id, ref)
    unique index — see migration 007). `jira_key` and `jira_parent_ref` are
    stored explicitly so downstream queries don't need to parse the ref.
    """
    node_id = db.upsert_node_by_ref("TestRun", "TR-" + subtask_key, {
        **_meta_jira(subtask_key),
        "jira_key": subtask_key,               # actual Jira subtask key (bare)
        "jira_parent_ref": parent_story_key,   # parent Story — for "which ticket owns this TR"
        "title": subtask_summary,              # canonical field
        "environment": env,
        "status": "pending",
    }, project_id=project_id)
    return node_id


# Vietnamese diacritics — presence means the field is likely Vietnamese and
# needs translation. Skips the LLM call when a bug row is already in English.
_VN_DIACRITIC_RE = re.compile(
    r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ"
    r"ÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴĐ]"
)


def _maybe_translate_bug_field(text):
    """Translate a bug-table cell to English when it looks Vietnamese.

    Returns the input verbatim when: empty, non-string, or has no Vietnamese
    diacritics (heuristic — misses VN written without accents, which is rare
    in QE bug reports). LLM errors fall back to the original text and log a
    warning so a translate outage never blocks bug ingest.
    """
    if not text or not isinstance(text, str):
        return text
    if not _VN_DIACRITIC_RE.search(text):
        return text
    try:
        from .llm import translate_to_english
        return translate_to_english(text)
    except Exception as e:
        print(f"[warn] Bug field translate-to-English failed, "
              f"storing original text: {e}")
        return text


def _upsert_bugs_from_table(container_key, container_summary, parent_story_key,
                            bugs, project_id, component_ids):
    """Materialise N Bug nodes from parsed table rows.

    Ref pattern <container_key>-<row_idx>. Edges:
      Bug -affects-> each Component in component_ids
      find_by=Testcase → TR (self-test of parent story) -finds-> Bug  (best-effort;
        we don't know exact TR — leave optional linking to orchestrator)

    Free-text fields (summary, steps, actual, expected) are translated to
    English on the way in — Postgres storage contract is English-only for
    extracted artifacts.

    Returns list[(row_idx, node_id, ref)].
    """
    out = []
    for idx, bug in enumerate(bugs, start=1):
        ref = f"{container_key}-{idx}"
        translated_summary = _maybe_translate_bug_field(bug["summary"])
        node_id = db.upsert_node_by_ref("Bug", ref, {
            **_meta_jira(container_key),
            "jira_key": container_key,             # Jira container subtask (bugs share this)
            "jira_parent_ref": parent_story_key,   # parent Story — for "bug thuộc ticket nào"
            "title": translated_summary,           # canonical field
            "summary": translated_summary,         # keep for backward-compat renderers
            "severity": bug["severity"] or "medium",
            "status": bug["status"] or "open",
            "find_by": bug["find_by"],
            "origin": "testing",
            "jira_container_ref": container_key,
            "jira_container_summary": container_summary,
            "description": {
                "steps": _maybe_translate_bug_field(bug["steps"]),
                "actual": _maybe_translate_bug_field(bug["actual"]),
                "expected": _maybe_translate_bug_field(bug["expected"]),
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
- **Respect the PRD's own numbering.** If the section lists ACs with explicit
  markers (e.g. "1.", "2)", "AC1:", "AC-01", "- ", numbered bullets, or a
  table row per AC), extract EXACTLY ONE AC per marker — verbatim, no
  splitting, no merging. The count you return MUST equal the count of
  markers in the source. This is the common case.
- Only when the PRD has NO numbered/bulleted AC list (e.g. free-flowing
  prose describing behaviour) may you extract atomic behaviour statements
  yourself. Even then, prefer fewer, self-contained ACs over many fragments.
- Never split a single numbered AC into sub-ACs because it mentions multiple
  UI elements or branches — those are testcase steps inside one AC, not
  separate ACs.
- Focus on behaviour the QE could write a testcase for. Skip pure UI polish
  notes (colors, spacing, exact copy variants) unless the PRD lists them as
  their own AC.
- Return valid JSON. No prose outside the object.
"""

_MAX_AC_INPUT_CHARS = 60000  # cap prompt so a huge PRD doesn't blow the LLM window

# Explicit AC markers PMs use in Confluence PRDs. Matches:
# NOTE: The regex fast-path (`_AC_MARKER_RE` + `_extract_acs_by_regex`) was
# disabled 2026-07-14 — it extracted section headings as ACs on PRDs that
# didn't use disciplined AC1:/CC1: markers, poisoning coverage checks. AC
# extraction now runs LLM-only, matching master behaviour. Code is kept
# commented (not deleted) so it can be re-enabled if PRD marker discipline
# improves.
#
# _AC_MARKER_RE = re.compile(
#     # Optional bullet prefix ([-*•]) — PMs often nest CCs as bullets under
#     # a parent AC. We treat CC1..N as their own ACs so QE can trace coverage
#     # per corner case.
#     r"^\s*[-*•]?\s*(AC|CC)[- ]?(\d{1,3})\s*[:\.]\s*(.+?)\s*$",
#     re.MULTILINE | re.IGNORECASE,
# )
#
#
# def _extract_acs_by_regex(section_text):
#     """Deterministic AC extraction for PRDs that use explicit markers.
#
#     Each AC's description is the marker line ONLY — the text after `ACn:` /
#     `CCn:` on the same line. Sub-bullets, tables, and continuation lines
#     below the marker are intentionally NOT concatenated, so the AC stays a
#     single-line testable statement (matches how QE reads them in the doc).
#
#     Returns:
#       list[str]: extracted AC titles (empty when no markers found —
#                  caller may fall back to LLM extraction).
#     """
#     if not section_text:
#         return []
#     matches = list(_AC_MARKER_RE.finditer(section_text))
#     if not matches:
#         return []
#     return [m.group(3).strip()[:500] for m in matches]


def _slice_section(body_text, section_anchor):
    """Best-effort: return only the text of the section matching section_anchor.

    Returns (sliced_text, matched: bool, heading_text: str|None).
      - matched=False → anchor didn't resolve; sliced_text = whole body,
        heading_text = None.
      - No anchor supplied → matched=True, heading_text = None (caller has
        no section context).

    Matching strategy:
      1. URL-decode the anchor (Confluence encodes `&` as `%26`, etc).
      2. Strict pass — require ALL slug tokens to appear in order.
      3. Relaxed fallback — require the first 3 tokens only. Handles the
         common case of a stale anchor (heading was edited AFTER the link
         was copied into Jira; Confluence keeps the old anchor as a redirect
         but the heading text no longer matches trailing tokens).
    """
    if not section_anchor or not body_text:
        return (body_text, True, None)
    decoded_anchor = unquote(section_anchor)
    slug_tokens = [t for t in decoded_anchor.replace("-", " ").split() if t]
    if not slug_tokens:
        return (body_text, True, None)
    lines = body_text.splitlines()

    def _find(tokens):
        for i, line in enumerate(lines):
            m = re.match(r"^(#+)\s+(.*)", line)
            if not m:
                continue
            heading_text = m.group(2).lower()
            pos = 0
            ok = True
            for tok in tokens:
                found = heading_text.find(tok.lower(), pos)
                if found < 0:
                    ok = False
                    break
                pos = found + len(tok)
            if ok:
                return (i, len(m.group(1)), m.group(2).strip())
        return (None, None, None)

    start_idx, heading_level, heading_text = _find(slug_tokens)
    if start_idx is None and len(slug_tokens) > 3:
        # Anchor likely stale (heading text edited after link was copied).
        # Retry with just the section prefix — section-number + first 2 words
        # are enough to disambiguate in practice.
        start_idx, heading_level, heading_text = _find(slug_tokens[:3])
    if start_idx is None:
        return (body_text, False, None)
    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        m = re.match(r"^(#+)\s+", lines[j])
        if m and len(m.group(1)) <= heading_level:
            end_idx = j
            break
    return ("\n".join(lines[start_idx:end_idx]), True, heading_text)


def extract_acs_via_llm(brd_text, section_anchor=None):
    """Ask the ingestion LLM to extract ACs from a PRD section.

    Returns (acs: list[str], warnings: list[str], section_title: str|None,
             method: str|None).
    section_title = the actual heading line matched in the PRD, so callers
    can store it on AC nodes for query-by-feature. None when no anchor was
    supplied OR when the anchor didn't match any heading.
    method = "regex" (deterministic marker match) | "llm" (free-form section)
             | None (nothing extracted). Caller stamps this on each AC's
             _meta.extraction_source so provenance stays accurate.

    Warnings surface silent failure modes (anchor miss, truncation, LLM
    error, empty output).
    """
    from .llm import complete_json  # lazy import: LLM providers pull deps
    warnings = []
    section_text, matched, section_title = _slice_section(brd_text, section_anchor)
    if section_anchor and not matched:
        warnings.append(
            f"Section anchor '{section_anchor}' không match heading nào trong PRD; "
            "extract từ toàn bộ doc (có thể miss/nhiễu). Check heading text trên Confluence."
        )

    # Fast path (DISABLED 2026-07-14 — kept commented for future re-enable):
    # PRD uses explicit AC markers (AC1:, CC1:, ...) → deterministic regex
    # skip LLM. Turned off because most PRDs don't use disciplined markers and
    # the regex was pulling section headings ("Entry point") as ACs.
    #
    # regex_acs = _extract_acs_by_regex(section_text)
    # if regex_acs:
    #     warnings.append(
    #         f"[regex] Extracted {len(regex_acs)} AC(s) via explicit ACn:/CCn: markers — LLM skipped."
    #     )
    #     return (regex_acs, warnings, section_title, "regex")

    if len(section_text) > _MAX_AC_INPUT_CHARS:
        warnings.append(
            f"PRD section dài {len(section_text)} chars, truncate về {_MAX_AC_INPUT_CHARS} — "
            "phần cuối bị cắt. Cân nhắc split section hoặc tăng _MAX_AC_INPUT_CHARS."
        )
        section_text = section_text[:_MAX_AC_INPUT_CHARS] + "\n\n[...truncated for LLM window...]"
    if not section_text.strip():
        warnings.append("PRD section rỗng sau khi slice — không có gì để extract.")
        return ([], warnings, section_title, None)
    try:
        data = complete_json(section_text, system=_AC_EXTRACT_SYSTEM,
                             max_tokens=2000, temperature=0.1)
    except Exception as e:
        warnings.append(f"LLM extract AC fail: {e}")
        return ([], warnings, section_title, None)
    acs = data.get("acceptance_criteria") or []
    out = []
    for ac in acs:
        desc = (ac.get("desc") if isinstance(ac, dict) else str(ac)) or ""
        desc = desc.strip()
        if desc:
            out.append(desc[:500])
    if not out:
        warnings.append("LLM chạy xong nhưng trả về 0 AC — có thể section không có AC rõ ràng "
                        "hoặc anchor match sai heading.")
        return (out, warnings, section_title, None)
    return (out, warnings, section_title, "llm")


# --- Orchestrator ---------------------------------------------------------

def ingest_jira_ticket(issue_key, project_id=None, extract_acs=True, on_step=None,
                       force=False):
    """Fetch a Jira ticket + its subtasks + every Confluence page it references,
    materialise everything in the graph and Chroma. Idempotent.

    Args:
      issue_key:   Jira key of the top-level Story (or Epic).
      project_id:  Optional override. When omitted, project is derived from the
                   issue key prefix (`CDM-268` → `CDM`) — the Jira key IS the
                   source of truth for which project a ticket belongs to. If
                   both are provided and they disagree, the ticket wins and a
                   warning is emitted.
      extract_acs: run LLM to pull ACs from BRD section. Default True.
      on_step:     optional progress callback (see tieukiwi.progress). Called
                   with sub-step events during the 6-stage pipeline.
      force:       skip hash-gate and re-run the whole pipeline. Set true when
                   the user says "cập nhật"/"refresh" or when a Jira webhook
                   fires — any other time, leave false so unchanged tickets
                   short-circuit as status='cached_fresh'.

    Returns:
      summary dict listing what was upserted / fetched / created.
    """
    from . import llm  # noqa

    derived_project = issue_key.split("-", 1)[0] if "-" in issue_key else None
    project_mismatch = None
    if derived_project:
        if project_id and project_id != derived_project:
            project_mismatch = (
                f"Caller passed project_id='{project_id}' but ticket {issue_key} "
                f"is in Jira project '{derived_project}'. Storing under "
                f"'{derived_project}' (ticket wins)."
            )
        project_id = derived_project

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
    if project_mismatch:
        summary["warnings"].append(project_mismatch)

    # 1. Fetch main issue (always — this is the input to the hash check)
    _sub(f"Fetch Jira issue {issue_key}…")
    try:
        issue = fetch_jira_issue(issue_key)
    except (httpx.HTTPError, RuntimeError) as e:
        summary["status"] = "error"
        summary["error"] = str(e)
        return summary

    fields = issue.get("fields") or {}

    # 1b. Hash-gate: short-circuit when nothing decision-relevant has changed.
    # Only applies to Story (Requirement) — Epic-only ingests are rare and
    # cheap enough to always re-run.
    #
    # Trade-off: story hash includes subtask STUBS (key/summary/status) but
    # NOT their descriptions. So a new row appearing inside a [Bug] subtask's
    # description table WITHOUT the subtask's summary/status changing will
    # be missed until the next update that does bump those. In CDM's flow
    # this is rare — QE usually updates subtask status when adding bugs —
    # so we accept it. Users can force `refresh` to bypass.
    if not force:
        node_type = _map_node_type((fields.get("issuetype") or {}).get("name"))
        if node_type == "Requirement":
            new_hash = _story_hash(fields)
            existing = db.get_node_by_ref("Requirement", issue_key, project_id=project_id)
            if existing:
                old_hash = (existing.get("props_json") or {}).get("story_hash")
                if old_hash == new_hash:
                    stale = _check_brd_freshness(existing["id"], on_step=_sub)
                    if stale:
                        _sub(f"PRD update trên Confluence ({stale}) → full ingest.")
                        # PRD drift is the primary trigger for AC extract — we
                        # can't tell if the AC set changed without running it.
                        # Force extract_acs=True regardless of what the caller
                        # passed, so `_diff_and_upsert_acs` reconciles new /
                        # kept / obsolete ACs and the summary lands in Slack.
                        extract_acs = True
                        # fall through — do not return cached_fresh
                    else:
                        _sub("Hash unchanged + BRD fresh, skipping full ingest.")
                        db.upsert_node_by_ref("Requirement", issue_key, {
                            "last_seen_at": _now_iso(),
                        }, project_id=project_id, merge_props=True)
                        summary["status"] = "cached_fresh"
                        summary["requirement"] = {
                            "key": issue_key,
                            "node_id": existing["id"],
                            "type": "Requirement",
                        }
                        summary["story_hash"] = new_hash
                        return summary
                else:
                    _sub(f"Story hash changed ({old_hash} → {new_hash}) — full ingest.")
            else:
                _sub("First ingest — no cached hash.")

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
    # Accumulate ACs across ALL Confluence pages, then reconcile ONCE at the
    # end. If we called _diff_and_upsert_acs per-page, page N would obsolete
    # every AC from pages 1..N-1 (they aren't in page N's seen_refs), so only
    # the last page's ACs would survive.
    extracted_acs = []          # list of {desc, source_url, section_anchor, section_title}
    any_extract_ran = False
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

            # 5. Optional: LLM extract ACs for this section.
            # Re-run when:
            #   - BRD content just changed (status="ok" → fresh chunks indexed)
            #   - force=True (user explicitly refresh)
            #   - Requirement has 0 ACs FOR THIS ANCHOR (previous extraction
            #     failed silently, OR this anchor was never extracted — the
            #     latter matters when one Requirement links to multiple
            #     sections of the SAME page: without anchor-scoping the second
            #     section is skipped just because the first produced ACs).
            # Skip only when: content unchanged AND ACs already exist for
            # THIS anchor scope.
            should_extract = extract_acs and cf_result.get("status") in ("ok", "cached")
            if should_extract and cf_result.get("status") == "cached":
                gate_anchor = unquote(section_anchor) if section_anchor else None
                if not force and db.count_acs_by_anchor(req_node_id, gate_anchor) > 0:
                    should_extract = False
            if should_extract:
                brd_node = db.get_node_by_ref("BRD", f"CFL-{page_id}", project_id=project_id)
                if brd_node:
                    # rag stored the full text as chunks; we don't have raw text.
                    # Re-derive by asking Chroma or refetch. Cheapest: refetch once.
                    _sub(f"LLM tách Acceptance Criteria từ BRD (page {page_id})…")
                    ac_texts, ac_warnings, section_title, ac_method = _extract_acs_for_page(
                        page_id, section_anchor
                    )
                    summary["warnings"].extend(ac_warnings)
                    any_extract_ran = True
                    page_url = cf_result.get("url")
                    # URL-decode the anchor for storage (VD "3.2%20Login" → "3.2 Login")
                    # so `props_json->>'section_anchor' LIKE '%Login%'` works cleanly.
                    decoded_anchor = unquote(section_anchor) if section_anchor else None
                    for desc in ac_texts:
                        extracted_acs.append({
                            "desc": desc,
                            "source_url": page_url,
                            "section_anchor": decoded_anchor,
                            "section_title": section_title,
                            "extraction_method": ac_method,
                        })

    if any_extract_ran:
        diff = _diff_and_upsert_acs(
            extracted_acs,
            req_node_id=req_node_id,
            req_key=req_key,
            project_id=project_id,
        )
        summary["acs_extracted"] = diff["created"]
        summary["acs_kept"] = diff["kept_count"]
        summary["acs_obsoleted"] = diff["obsoleted"]

    # 5b. Prune stale `derivedFrom` edges — BRDs no longer referenced by the
    # current description. Cross-module policy: only remove the edge from
    # THIS Requirement; the BRD node + its Chroma chunks stay (may still be
    # useful to other Requirements semantically). Use deprecate_brd() (future)
    # for full removal of a wrong-page BRD.
    seen_brd_refs = {f"CFL-{pid}" for pid, _sec, _url in confluence_targets}
    existing_brds = db.linked_brds(req_node_id)
    orphan_brds = [b for b in existing_brds if b["ref"] not in seen_brd_refs]
    if orphan_brds:
        db.delete_edges_by_dst(
            req_node_id, "derivedFrom", [b["id"] for b in orphan_brds]
        )
        summary["brds_pruned"] = [
            {"ref": b["ref"],
             "title": (b.get("props_json") or {}).get("title")}
            for b in orphan_brds
        ]
        _sub(f"Đã prune {len(orphan_brds)} BRD link cũ khỏi {req_key}.")

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
            tr_id = _upsert_testrun(st_key, st_summary, env, project_id,
                                    parent_story_key=req_key)
            testrun_by_env[env] = tr_id
            summary["subtasks"]["testruns"].append({"key": st_key, "env": env, "node_id": tr_id})
        elif kind == "bug_container":
            bug_container_stubs.append((st_key, st_summary))
        else:
            summary["subtasks"]["skipped"].append({"key": st_key, "summary": st_summary})

    # Now Bug containers — use self-test TR (if present) to link find_by=Testcase.
    # Inner-loop hash-gate: for each [Bug] subtask, hash its description table
    # and skip re-parse when unchanged since last ingest. Saves a REST fetch
    # of the subtask AND the LLM-free but non-trivial parse work when the
    # story hash changed for OTHER reasons (e.g. TestRun status flip).
    existing_req = db.get_node_by_ref("Requirement", req_key, project_id=project_id) or {}
    old_bug_hashes = (existing_req.get("props_json") or {}).get("bug_container_hashes") or {}
    new_bug_hashes = dict(old_bug_hashes)   # copy — mutated when we (re-)parse

    # Bugs inherit their parent Story's `impacts` Components as `affects` — a
    # bug under Story X is by definition a defect in one of the Components X
    # is scoped to. If impacts_map.yml hasn't seeded the parent yet, this is
    # empty and bug_blast_radius will return P4 until the mapping is added.
    req_component_ids = []
    if existing_req.get("id"):
        with db.conn() as _c:
            req_component_ids = [
                r[0] for r in _c.execute(
                    "SELECT e.dst_id FROM edges e JOIN nodes c ON c.id=e.dst_id "
                    "WHERE e.src_id=%s AND e.rel='impacts' AND c.type='Component'",
                    (existing_req["id"],),
                ).fetchall()
            ]

    self_test_tr_id = testrun_by_env.get("self")
    for st_key, st_summary in bug_container_stubs:
        _sub(f"Parse bảng bug trong subtask {st_key}…")
        try:
            subtask_full = fetch_jira_issue(st_key)
        except (httpx.HTTPError, RuntimeError) as e:
            summary["warnings"].append(f"Failed to fetch subtask {st_key}: {e}")
            continue
        desc_adf = (subtask_full.get("fields") or {}).get("description")

        # Hash-gate for THIS bug container. Compare table hash before parsing.
        new_hash = _bug_table_hash(desc_adf)
        if not force and old_bug_hashes.get(st_key) == new_hash:
            # Skip re-parse; existing Bug nodes are already correct.
            summary["subtasks"]["bug_containers"].append({
                "key": st_key, "rows_parsed": 0, "cached": True,
            })
            _sub(f"  {st_key} bảng không đổi (cached).")
            continue
        new_bug_hashes[st_key] = new_hash

        bugs_raw = parse_bug_subtask_table(desc_adf)
        bug_nodes = _upsert_bugs_from_table(
            st_key, st_summary, req_key, bugs_raw, project_id,
            component_ids=req_component_ids,
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

    # Persist bug container hashes on the Requirement for next-run gating.
    if new_bug_hashes != old_bug_hashes:
        db.upsert_node_by_ref("Requirement", req_key, {
            "bug_container_hashes": new_bug_hashes,
        }, project_id=project_id, merge_props=True)

    summary["status"] = "ok"
    return summary


def _ac_content_hash(desc):
    """Stable 8-hex hash of an AC description, used to build hash-based refs
    and diff old/new AC sets across BRD updates.

    Normalises whitespace but keeps case + punctuation — minor rewording in
    the PRD ("Reviewer" → "Reviewers") will produce a NEW hash and create a
    new AC node. That's intentional: the old AC gets marked obsolete rather
    than silently mutated, preserving Bug -violates- history.
    """
    normalised = " ".join((desc or "").split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:8]


def _diff_and_upsert_acs(new_acs, req_node_id, req_key, project_id):
    """Reconcile freshly-extracted ACs with what's already in the graph.

    Args:
      new_acs: list of {"desc": str, "source_url": str} — the UNION of ACs
        extracted across every Confluence page linked from this ticket.
        Must be batched by the caller; per-page diffs would obsolete other
        pages' ACs (bug fixed 2026-07).

    For each new AC:
      - Compute hash-based ref: AC-<req_key>-<hash8>. Same desc → same ref
        → idempotent upsert (no dupes on re-fetch).
      - If a node with this ref already exists → keep as-is (skip re-upsert
        to preserve any human edits like review_status='verified').
      - Else create fresh with _meta.review_status='draft'.
    For each existing AC of this requirement NOT in the new set:
      - Mark _meta.review_status='obsolete' + _meta.obsoleted_at.
      - Do NOT delete — Bug -violates- edges pointing at it should remain
        so classify_bug's leaked_* categories keep giving history.

    Returns {"created": [{ref, desc}], "kept_count": N, "obsoleted": [refs]}.
    """
    with db.conn() as c:
        # Existing ACs for this Requirement (via `has` edge).
        sql = (
            "SELECT ac.id, ac.ref, ac.props_json FROM edges h "
            "JOIN nodes ac ON ac.id = h.dst_id AND ac.type='AcceptanceCriterion' "
            "WHERE h.src_id=%s AND h.rel='has'"
        )
        params = [req_node_id]
        if project_id is not None:
            sql += " AND ac.project_id=%s"
            params.append(project_id)
        rows = c.execute(sql, params).fetchall()

    # Map ref → props for quick lookup + delete tracking.
    existing_by_ref = {r[1]: (r[0], r[2] or {}) for r in rows}
    seen_refs = set()
    created = []

    for ac in new_acs:
        desc = ac["desc"]
        source_url = ac.get("source_url")
        section_anchor = ac.get("section_anchor")
        section_title = ac.get("section_title")
        # Extraction method comes from _extract_acs_for_page — "regex" for
        # explicit-marker docs (deterministic, confidence 1.0), "llm" for
        # free-form sections (confidence 0.75). Missing key = legacy default.
        method = ac.get("extraction_method") or "llm"
        h = _ac_content_hash(desc)
        ref = f"AC-{req_key}-{h}"
        if ref in seen_refs:
            # Same desc extracted from 2 pages this run — first wins.
            continue
        seen_refs.add(ref)
        if ref in existing_by_ref:
            # Already there — leave existing props (including any human edits).
            continue
        # section_anchor + section_title as top-level props so operators can
        # query by feature: `props_json->>'section_title' LIKE '%Login%'`.
        # Nulls omitted to keep props tidy for ACs from anchor-less URLs.
        new_props = {
            "desc": desc,
            "_meta": {
                "extraction_source": method,
                "confidence": 1.0 if method == "regex" else 0.75,
                "source_file": source_url,
                "review_status": "draft",
                "ingested_at": _now_iso(),
            },
        }
        if section_anchor:
            new_props["section_anchor"] = section_anchor
        if section_title:
            new_props["section_title"] = section_title
        ac_node_id = db.upsert_node_by_ref(
            "AcceptanceCriterion", ref, new_props, project_id=project_id,
        )
        db.ensure_edge(req_node_id, "has", ac_node_id)
        created.append({"ref": ref, "desc": desc[:80]})

    # Anything left in existing_by_ref that we didn't see → obsolete.
    obsoleted = []
    for ref, (node_id, props) in existing_by_ref.items():
        if ref in seen_refs:
            continue
        meta = props.get("_meta") or {}
        # Skip if already obsoleted (avoids clobbering obsoleted_at).
        if meta.get("review_status") == "obsolete":
            continue
        # Fetch → mutate → upsert, so we preserve other _meta fields (confidence,
        # source_file, extraction_source) instead of blowing them away.
        meta["review_status"] = "obsolete"
        meta["obsoleted_at"] = _now_iso()
        new_props = {**props, "_meta": meta}
        db.upsert_node_by_ref(
            "AcceptanceCriterion", ref, new_props, project_id=project_id,
        )
        obsoleted.append(ref)

    return {
        "created": created,
        "kept_count": len(existing_by_ref) - len(obsoleted),
        "obsoleted": obsoleted,
    }


def _extract_acs_for_page(page_id, section_anchor):
    """Helper: re-fetch page body and hand to LLM for AC extraction.

    Returns (acs: list[str], warnings: list[str], section_title: str|None,
             method: str|None). See extract_acs_via_llm for method semantics.
    Propagates warnings from extract_acs_via_llm plus surfaces fetch/parse
    failures. section_title = the matched PRD heading, for storing on the AC.
    """
    try:
        page = confluence._confluence_get(
            f"/api/v2/pages/{page_id}?body-format=atlas_doc_format"
        )
    except (httpx.HTTPError, RuntimeError) as e:
        return ([], [f"Confluence fetch page {page_id} fail: {e}"], None, None)
    body_val = ((page.get("body") or {}).get("atlas_doc_format") or {}).get("value")
    if isinstance(body_val, str):
        try:
            body_adf = json.loads(body_val)
        except json.JSONDecodeError:
            return ([], [f"Confluence page {page_id}: ADF body không parse được."], None, None)
    else:
        body_adf = body_val
    body_text = adf.to_pretty_text(body_adf) if body_adf else ""
    return extract_acs_via_llm(body_text, section_anchor)


# --- TestRun status sync + TC↔TR linker ---------------------------------

_JIRA_STATUS_MAP = {
    "done": "done", "closed": "done", "fixed": "done", "resolved": "done",
    "open": "open", "todo": "open", "to do": "open", "backlog": "open",
    "inprogress": "inprogress", "in progress": "inprogress",
    "in_progress": "inprogress", "doing": "inprogress",
    "blocked": "blocked",
}


def _normalise_jira_status(raw):
    if not raw:
        return None
    return _JIRA_STATUS_MAP.get(raw.strip().lower(), raw.strip().lower())


def sync_testruns_and_link_tcs(requirement_ref, project_id=None):
    """Refresh live Jira status for every TestRun of a Requirement, and when a
    TR flips to 'done' ensure `executedBy` edges from every TestCase covering
    that Requirement's ACs → the TR (ontology: TC -executedBy-> TR).

    Steps per TR (identified by `jira_parent_ref == requirement_ref`):
      1. Fetch Jira live via `fetch_jira_issue(jira_key)`.
      2. Normalise Jira status (Done/Closed/Fixed/Resolved → 'done', ...).
      3. Merge {status: <new>} into the TR node's props_json (partial update
         via upsert_node_by_ref merge_props — preserves environment/title/etc).
      4. If normalised status == 'done': for every TestCase reachable via
         Requirement -has-> AC -coveredBy-> TC, `ensure_edge(tc.id, 'executedBy',
         tr.id)` — idempotent (dedupes on re-run).

    Returns:
      {"requirement_ref": ..., "project_id": ..., "testruns": [{
          "ref": "TR-CDM-289", "jira_key": "CDM-289", "environment": "prod",
          "old_status": "pending", "new_status": "done",
          "edges_added": 3, "linked_tc_refs": ["CDM_Login_001", ...]
      }], "warnings": [...]}
    """
    warnings = []
    with db.conn() as c:
        # 1. Find every TestRun of this Requirement (via stored jira_parent_ref).
        sql = (
            "SELECT id, ref, props_json FROM nodes "
            "WHERE type='TestRun' AND props_json->>'jira_parent_ref'=%s"
        )
        params = [requirement_ref]
        if project_id is not None:
            sql += " AND project_id=%s"
            params.append(project_id)
        sql += " ORDER BY ref"
        tr_rows = c.execute(sql, params).fetchall()

        # 2. Find every TestCase covering any AC of this Requirement — done once
        #    per call so multiple TRs share the same TC set (no redundant lookups).
        tc_rows = c.execute(
            """
            SELECT DISTINCT tc.id, tc.ref FROM nodes tc
            JOIN edges cov ON cov.dst_id = tc.id AND cov.rel = 'coveredBy'
            JOIN nodes ac ON ac.id = cov.src_id AND ac.type = 'AcceptanceCriterion'
            JOIN edges h ON h.dst_id = ac.id AND h.rel = 'has'
            JOIN nodes req ON req.id = h.src_id AND req.type = 'Requirement' AND req.ref = %s
            WHERE tc.type = 'TestCase'
            ORDER BY tc.ref
            """,
            (requirement_ref,),
        ).fetchall()
    tc_pairs = [(tc_id, tc_ref) for tc_id, tc_ref in tc_rows]

    out_testruns = []
    for tr_id, tr_ref, tr_props in tr_rows:
        tr_props = tr_props or {}
        jira_key = tr_props.get("jira_key")
        environment = tr_props.get("environment")
        old_status = tr_props.get("status")
        if not jira_key:
            warnings.append(
                f"{tr_ref}: missing props.jira_key — cannot fetch live status."
            )
            out_testruns.append({
                "ref": tr_ref, "jira_key": None, "environment": environment,
                "old_status": old_status, "new_status": old_status,
                "edges_added": 0, "linked_tc_refs": [],
            })
            continue

        try:
            issue = fetch_jira_issue(jira_key, expand_subtasks=False)
        except (httpx.HTTPError, RuntimeError) as e:
            warnings.append(f"{tr_ref}: Jira fetch failed for {jira_key}: {e}")
            out_testruns.append({
                "ref": tr_ref, "jira_key": jira_key, "environment": environment,
                "old_status": old_status, "new_status": old_status,
                "edges_added": 0, "linked_tc_refs": [],
            })
            continue

        raw_status = ((issue.get("fields") or {}).get("status") or {}).get("name")
        new_status = _normalise_jira_status(raw_status) or old_status

        if new_status and new_status != old_status:
            db.upsert_node_by_ref(
                "TestRun", tr_ref, {"status": new_status},
                project_id=project_id, merge_props=True,
            )

        edges_added = 0
        linked_tc_refs = []
        if new_status == "done" and tc_pairs:
            tc_ids = [tc_id for tc_id, _ in tc_pairs]
            with db.conn() as c:
                existing = {
                    row[0] for row in c.execute(
                        "SELECT src_id FROM edges "
                        "WHERE rel='executedBy' AND dst_id=%s AND src_id = ANY(%s)",
                        (tr_id, tc_ids),
                    ).fetchall()
                }
                missing = [(tc_id, tc_ref) for tc_id, tc_ref in tc_pairs
                           if tc_id not in existing]
                if missing:
                    c.executemany(
                        "INSERT INTO edges(src_id, rel, dst_id, props_json) "
                        "VALUES (%s,'executedBy',%s,'{}'::jsonb)",
                        [(tc_id, tr_id) for tc_id, _ in missing],
                    )
            edges_added = len(missing)
            linked_tc_refs = [tc_ref for _, tc_ref in tc_pairs]

        out_testruns.append({
            "ref": tr_ref, "jira_key": jira_key, "environment": environment,
            "old_status": old_status, "new_status": new_status,
            "edges_added": edges_added, "linked_tc_refs": linked_tc_refs,
        })

    return {
        "requirement_ref": requirement_ref,
        "project_id": project_id,
        "testruns": out_testruns,
        "warnings": warnings,
    }
