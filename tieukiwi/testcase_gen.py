"""Draft and refine TestCase records for a Requirement via LLM, following the
KB test case template + rubric. No Slack imports here — the Approve/Refine
Slack loop lives in tieukiwi/slack_app.py and calls these functions directly.

Interface:
  generate_draft(requirement_ref, project_id=None) -> dict
  refine_draft(state, comment) -> dict
  finalize_and_save(state, approved_by) -> list[node_id]

Draft schema (shared with tieukiwi/db.py and tieukiwi/testcase_export.py):
  {"ref", "ac_refs", "title", "type", "priority", "precondition", "steps": [...],
   "data_variants": [...], "api": {...}}
"""
import json

from . import db, llm, rag

_SYSTEM_PROMPT = """\
You are a QE test-case-writing engine. You draft or update TestCase records for a
software requirement, strictly following the provided template and rubric.

Output shape (JSON, no prose):
{
  "testcases": [
    {
      "ref": "<ProjectCode>_<AcronymOfFeature>_<NNN>, e.g. CDM_AssignCreator_001",
      "ac_refs": ["<AC ref this testcase covers, at least one>"],
      "title": "<[TC_ID] verb-first summary, <=100 chars>",
      "type": "<Normal|API|DataTable>",
      "priority": "<Highest|High|Medium|Low>",
      "precondition": "<numbered list as one string, or empty string>",
      "steps": [{"description": "<action>", "expected": "<observable outcome>"}],
      "data_variants": [],
      "api": {}
    }
  ],
  "deleted_refs": ["<ref of an existing testcase to remove entirely, if any>"],
  "summary": "<1-3 sentences: what you drafted or changed, and why>"
}

Rules:
- When you were given an "Existing testcases" list to update: `testcases` in your
  output must contain ONLY the testcases you are adding or changing — do NOT
  re-emit an existing testcase that needs no changes, it stays in place
  automatically. If a testcase should be removed entirely, put its `ref` in
  `deleted_refs` instead of (not in addition to) `testcases`. Leave
  `deleted_refs` as [] unless removal was explicitly requested or implied.
  When there is no "Existing testcases" list (fresh draft), `testcases` is the
  full list as usual and `deleted_refs` is always [].
- CRITICAL — renaming/migrating an existing testcase (e.g. fixing a legacy ID
  like `TC-CDM-268-A` into the `<ProjectCode>_<Acronym>_<NNN>` format, or
  replacing one testcase with a differently-refed one that covers the same
  scenario) is an ADD of the new ref PLUS a DELETE of the old ref — you MUST
  put the OLD ref in `deleted_refs` whenever its content moved to a new ref.
  Forgetting this leaves BOTH the old and new testcase in place, i.e. a
  duplicate of the same scenario under two refs. Before finishing, check every
  ref in `deleted_refs` against `testcases`/existing to confirm there is no
  surviving old-ref duplicate of anything you migrated or replaced.
- Write ALL testcase content (title, precondition, steps, data_variants, api fields)
  in English, regardless of the language of the source Requirement/AC text.
- Test case ID (`ref`) format: `<ProjectCode>_<AcronymOfFeature>_<NNN>`, where
  ProjectCode is given to you below, AcronymOfFeature is a short PascalCase name
  for the feature/scenario group under test (e.g. AssignCreator, DupScript), and
  NNN is a zero-padded 3-digit index starting at 001 within that acronym group.
  Every testcase for the same feature/scenario group MUST share the same acronym
  and increment NNN (e.g. CDM_AssignCreator_001, CDM_AssignCreator_002, ...).
- Every AC ref passed to you MUST be covered by at least one testcase in the output.
- `type`: "Normal" for a standard UI/manual testcase, "API" for a testcase
  exercising a REST/API endpoint (populate `api`, may leave `steps` minimal),
  "DataTable" for a testcase that must run the SAME step sequence against
  multiple distinct input sets (populate `data_variants`).
- `api` (only when type="API"): {"endpoint": str, "method": str,
  "request_headers": str, "request_body": str, "expected_status": str,
  "expected_response": str}. Leave as {} for non-API testcases.
- `data_variants` (only when type="DataTable"): each item
  {"label": str, "values": {col: val, ..., "Expected": val}}. Leave as [] otherwise.
- `steps` must always contain at least one step with a concrete `description` and
  `expected` — never leave a testcase with only an id/title and no steps.
- When a reviewer comment lists one scenario per line (bullets/dashes) to turn a
  testcase into a DataTable, create EXACTLY one data_variants item per line, in
  the same order, using that line's text verbatim as the `label` (do not reword
  it). NEVER fabricate specific column values (exact statuses, counts, setup
  details) that are not explicitly stated in the reviewer's comment or already
  established by the Requirement/AC text — an invented value is worse than a
  missing one, since it can mislead whoever executes the test. Only include a
  `values` column when its value is directly grounded in the comment or the
  AC text; otherwise omit that column for that row rather than guessing.
"""

_TEMPLATE_FALLBACK = (
    "Title: [TC_ID] verb-first summary. Priority: Highest|High|Medium|Low. "
    "Precondition: numbered list. Steps: Step_Description + Step_ExpectedResult per row."
)
_RUBRIC_FALLBACK = (
    "Cover happy path, negative path, and edge cases for every acceptance criterion. "
    "Keep steps atomic and independently verifiable. Write everything in English."
)

_REQUIRED_TC_KEYS = ("ref", "ac_refs", "title", "priority", "steps")

_ALLOWED_PRIORITIES = ("Highest", "High", "Medium", "Low")
_PRIORITY_ALIASES = {"critical": "Highest", "blocker": "Highest", "urgent": "Highest"}
_ALLOWED_TYPES = ("Normal", "API", "DataTable")

_LLM_MAX_TOKENS_BASE = 4096
# Testcases now carry more content per item (type, api, precondition, multiple
# English-language steps) than the original estimate assumed — undershooting
# this causes the LLM's JSON to be cut off mid-string (json.JSONDecodeError:
# "Unterminated string"). Sized with headroom; the retry-with-bigger-budget
# fallback below (_call_llm_json) also covers any remaining underestimate.
_LLM_MAX_TOKENS_PER_TESTCASE = 700
_LLM_MAX_TOKENS_CEILING = 24000


def _ac_gap_refs(prd, existing_testcases):
    """AC refs from `prd['acs']` not covered by any testcase's ac_refs."""
    covered = {ac_ref for tc in existing_testcases for ac_ref in tc.get("ac_refs", [])}
    return [ac["ref"] for ac in prd.get("acs", []) if ac["ref"] not in covered]


def _merge_returned(existing, returned, deleted_refs=None):
    """Merge an LLM's delta output (new/changed testcases only, per the
    "only include what you're adding or changing" rule in _SYSTEM_PROMPT)
    back onto the testcases it left untouched. Matched by `ref`. This is what
    lets update prompts skip re-emitting every unchanged testcase — without
    it, every draft/refine call would have to regenerate the full existing
    backlog's content just to leave it as-is, which is slow and gets slower
    as the backlog grows."""
    deleted = set(deleted_refs or ())
    returned_by_ref = {tc["ref"]: tc for tc in returned}
    merged = []
    for tc in existing:
        if tc["ref"] in deleted:
            continue
        merged.append(returned_by_ref.pop(tc["ref"], tc))
    merged.extend(returned_by_ref.values())
    return merged


def _normalize_priority(value):
    if not isinstance(value, str):
        raise ValueError(f"priority must be a string, got {type(value).__name__}")
    mapped = _PRIORITY_ALIASES.get(value.strip().lower(), value.strip().title())
    if mapped not in _ALLOWED_PRIORITIES:
        raise ValueError(f"priority must be one of {_ALLOWED_PRIORITIES}, got {value!r}")
    return mapped


_TYPE_BY_LOWER = {t.lower(): t for t in _ALLOWED_TYPES}


def _normalize_type(value):
    if value is None:
        return "Normal"
    if not isinstance(value, str):
        raise ValueError(f"type must be a string, got {type(value).__name__}")
    mapped = _TYPE_BY_LOWER.get(value.strip().lower())
    if mapped is None:
        raise ValueError(f"type must be one of {_ALLOWED_TYPES}, got {value!r}")
    return mapped


def _validate_testcases(raw):
    if not isinstance(raw, list):
        raise ValueError("testcases must be a list")
    normalized = []
    for i, tc in enumerate(raw):
        if not isinstance(tc, dict):
            raise ValueError(f"testcase[{i}] must be an object, got {type(tc).__name__}")
        missing = [k for k in _REQUIRED_TC_KEYS if k not in tc]
        if missing:
            raise ValueError(f"testcase[{i}] missing required keys: {missing}")
        if not isinstance(tc["ac_refs"], list):
            raise ValueError(f"testcase[{i}].ac_refs must be a list, got {type(tc['ac_refs']).__name__}")
        if not isinstance(tc["steps"], list):
            raise ValueError(f"testcase[{i}].steps must be a list, got {type(tc['steps']).__name__}")
        if tc.get("api") is not None and not isinstance(tc["api"], dict):
            raise ValueError(f"testcase[{i}].api must be an object, got {type(tc['api']).__name__}")
        normalized.append({
            "ref": tc["ref"],
            "ac_refs": list(tc["ac_refs"]),
            "title": tc["title"],
            "type": _normalize_type(tc.get("type")),
            "priority": _normalize_priority(tc["priority"]),
            "precondition": tc.get("precondition", ""),
            "steps": tc["steps"],
            "data_variants": tc.get("data_variants") or [],
            "api": tc.get("api") or {},
        })
    return normalized


def _looks_like_full_replacement(comment):
    """If `comment` parses as JSON matching the draft testcase list shape,
    return the normalized list; otherwise return None (free-text feedback)."""
    try:
        parsed = json.loads(comment)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    try:
        return _validate_testcases(parsed)
    except ValueError:
        return None


def _max_tokens_for(existing_count):
    """Scale the LLM output budget with how many testcases it must echo back
    (Branch B's 'return the FULL updated list' prompt) — a flat ceiling
    reproduces the truncation bug this was designed to fix once the existing
    testcase backlog grows past what the base budget covers."""
    return min(
        _LLM_MAX_TOKENS_BASE + existing_count * _LLM_MAX_TOKENS_PER_TESTCASE,
        _LLM_MAX_TOKENS_CEILING,
    )


def _call_llm_json(llm_fn, prompt, system, max_tokens):
    """Call llm_fn and parse JSON, retrying once with a doubled token budget
    (capped at the ceiling) if the response was cut off mid-JSON. Guards
    against underestimating _max_tokens_for for a given draft's actual size."""
    try:
        return llm_fn(prompt, system=system, max_tokens=max_tokens)
    except json.JSONDecodeError:
        retry_tokens = min(max_tokens * 2, _LLM_MAX_TOKENS_CEILING)
        if retry_tokens <= max_tokens:
            raise
        return llm_fn(prompt, system=system, max_tokens=retry_tokens)


def _fetch_kb_context(project_id=None):
    template_hits = rag.search("test case template format", k=1, project_id=project_id,
                                doc_type="template", include_global=True)
    # Rubrics live under skills/ (e.g. skills/gen-testcase.md), which never carries a
    # `role` field in Chroma metadata (scripts/seed/kb.py only assigns `role` to docs
    # under kb/<project>/<ROLE>/..., not skills/) — a role filter here would always
    # exclude them and silently fall back to the constant.
    rubric_hits = rag.search("test case writing rubric conventions", k=1,
                              project_id=project_id, include_global=True)
    template = template_hits[0][1] if template_hits else _TEMPLATE_FALLBACK
    rubric = rubric_hits[0][1] if rubric_hits else _RUBRIC_FALLBACK
    return f"# Test case template\n{template}\n\n# Test case rubric\n{rubric}"


def _project_code_for(requirement_ref):
    return requirement_ref.split("-")[0] if "-" in requirement_ref else requirement_ref


def generate_draft(requirement_ref, project_id=None, llm_fn=None):
    """Branch A (no existing testcases): draft fresh testcases covering every AC.
    Branch B (existing testcases found): update mismatched ones + add missing.
    Returns {requirement_ref, project_id, version: 1, testcases, summary}.
    """
    llm_fn = llm_fn or llm.complete_json
    prd = db.requirement_with_acs(requirement_ref, project_id=project_id)
    if not prd.get("found"):
        raise ValueError(f"Requirement not found: {requirement_ref}")
    existing = db.testcases_for_requirement(requirement_ref, project_id=project_id)
    context = _fetch_kb_context(project_id)
    ac_lines = "\n".join(f"- {ac['ref']}: {ac['desc']}" for ac in prd["acs"])
    project_code = _project_code_for(requirement_ref)

    if not existing:
        prompt = (
            f"{context}\n\n"
            f"Project code: {project_code}\n\n"
            f"Requirement {prd['ref']}: {prd.get('title', '')}\n{prd.get('detail', '')}\n\n"
            f"Acceptance Criteria:\n{ac_lines}\n\n"
            "Draft testcases covering every AC above."
        )
    else:
        existing_text = json.dumps(existing, ensure_ascii=False, indent=2)
        prompt = (
            f"{context}\n\n"
            f"Project code: {project_code}\n\n"
            f"Requirement {prd['ref']}: {prd.get('title', '')}\n{prd.get('detail', '')}\n\n"
            f"Acceptance Criteria:\n{ac_lines}\n\n"
            f"Existing testcases:\n{existing_text}\n\n"
            "Update any testcase whose steps/expected no longer match the AC text "
            "above, and add new testcases for any AC not yet covered. Also rewrite "
            "(migrate) any existing testcase that does not already conform to the "
            "rubric above — wrong language, legacy ID format, disallowed priority "
            "value, missing/incorrect type — into the current conventions. This is "
            "a standing rule applied on every run, not a one-time fix. Per the "
            "output-shape rule, only include testcases you added or changed."
        )

    raw = _call_llm_json(llm_fn, prompt, _SYSTEM_PROMPT, _max_tokens_for(len(existing)))
    testcases = _validate_testcases(raw["testcases"])
    if existing:
        testcases = _merge_returned(existing, testcases, raw.get("deleted_refs"))
    gaps = _ac_gap_refs(prd, testcases)
    if gaps:
        raise ValueError(f"LLM draft still leaves AC(s) uncovered: {gaps}")
    return {
        "requirement_ref": requirement_ref,
        "project_id": project_id,
        "version": 1,
        "testcases": testcases,
        "summary": raw.get("summary", ""),
    }


def refine_draft(state, comment, llm_fn=None):
    """Apply a reviewer comment to the current draft and return version+1.
    If `comment` parses as a full replacement testcase list, use it directly
    (no LLM call)."""
    llm_fn = llm_fn or llm.complete_json
    replacement = _looks_like_full_replacement(comment)
    if replacement is not None:
        testcases = replacement
        summary = "Replaced draft with the exact testcase list provided by the reviewer."
    else:
        context = _fetch_kb_context(state.get("project_id"))
        current_text = json.dumps(state["testcases"], ensure_ascii=False, indent=2)
        project_code = _project_code_for(state["requirement_ref"])
        prompt = (
            f"{context}\n\nProject code: {project_code}\n\n"
            f"Current draft testcases:\n{current_text}\n\n"
            f"Reviewer comment:\n{comment}\n\n"
            "Apply the reviewer's comment. Per the output-shape rule, only include "
            "testcases you added or changed — not the full list. "
            "If the comment lists one scenario per line to convert a testcase to "
            "DataTable, create exactly one data_variants row per line (same order, "
            "label = that line's text verbatim) and do NOT invent column values "
            "that aren't stated in the comment or the AC text — omit a value you "
            "can't verify rather than guessing one."
        )
        raw = _call_llm_json(llm_fn, prompt, _SYSTEM_PROMPT, _max_tokens_for(len(state["testcases"])))
        testcases = _validate_testcases(raw["testcases"])
        testcases = _merge_returned(state["testcases"], testcases, raw.get("deleted_refs"))
        summary = raw.get("summary", "")
    return {
        "requirement_ref": state["requirement_ref"],
        "project_id": state.get("project_id"),
        "version": state["version"] + 1,
        "testcases": testcases,
        "summary": summary,
    }


def finalize_and_save(state, approved_by):
    return db.save_testcases(
        state["requirement_ref"], state["testcases"], approved_by,
        project_id=state.get("project_id"),
    )


def _selftest():
    prd = {"ref": "REQ-1", "acs": [{"ref": "AC-1", "desc": "x"}, {"ref": "AC-2", "desc": "y"}]}
    existing = [{"ac_refs": ["AC-1"]}]
    assert _ac_gap_refs(prd, existing) == ["AC-2"]

    valid = _validate_testcases([{
        "ref": "TC-1", "ac_refs": ["AC-1"], "title": "t", "priority": "High",
        "steps": [{"description": "d", "expected": "e"}],
    }])
    assert valid[0]["precondition"] == ""
    assert valid[0]["data_variants"] == []
    assert valid[0]["type"] == "Normal"
    assert valid[0]["api"] == {}

    try:
        _validate_testcases([{"ref": "TC-1"}])
        raise AssertionError("expected ValueError for missing keys")
    except ValueError:
        pass

    full = json.dumps([{
        "ref": "TC-1", "ac_refs": ["AC-1"], "title": "t", "priority": "High",
        "steps": [{"description": "d", "expected": "e"}],
    }])
    assert _looks_like_full_replacement(full) is not None
    assert _looks_like_full_replacement("please add a negative case") is None

    try:
        _validate_testcases([{"ref": "TC-1", "ac_refs": "AC-1", "title": "t",
                               "priority": "High", "steps": []}])
        raise AssertionError("expected ValueError for non-list ac_refs")
    except ValueError:
        pass

    assert _looks_like_full_replacement(json.dumps([1, 2, 3])) is None
    assert _looks_like_full_replacement("[]") is None

    assert _max_tokens_for(0) == _LLM_MAX_TOKENS_BASE
    assert _max_tokens_for(10) == _LLM_MAX_TOKENS_BASE + 10 * _LLM_MAX_TOKENS_PER_TESTCASE
    assert _max_tokens_for(1000) == _LLM_MAX_TOKENS_CEILING

    assert _normalize_priority("Critical") == "Highest"
    assert _normalize_priority("  high ") == "High"
    try:
        _normalize_priority("Urgent-ish")
        raise AssertionError("expected ValueError for unknown priority")
    except ValueError:
        pass

    assert _normalize_type(None) == "Normal"
    assert _normalize_type("api") == "API"
    assert _normalize_type("datatable") == "DataTable"
    try:
        _normalize_type("Weird")
        raise AssertionError("expected ValueError for unknown type")
    except ValueError:
        pass

    api_tc = _validate_testcases([{
        "ref": "CDM_Login_001", "ac_refs": ["AC-1"], "title": "t", "priority": "Critical",
        "type": "API", "steps": [{"description": "d", "expected": "e"}],
        "api": {"endpoint": "/login", "method": "POST"},
    }])[0]
    assert api_tc["priority"] == "Highest"
    assert api_tc["type"] == "API"
    assert api_tc["api"] == {"endpoint": "/login", "method": "POST"}

    assert _project_code_for("CDM-268") == "CDM"
    assert _project_code_for("STANDALONE") == "STANDALONE"

    existing_tcs = [{"ref": "TC-1", "title": "old"}, {"ref": "TC-2", "title": "old"}]
    merged = _merge_returned(existing_tcs, [{"ref": "TC-1", "title": "new"}])
    assert merged == [{"ref": "TC-1", "title": "new"}, {"ref": "TC-2", "title": "old"}]
    merged = _merge_returned(existing_tcs, [{"ref": "TC-3", "title": "brand new"}])
    assert merged == existing_tcs + [{"ref": "TC-3", "title": "brand new"}]
    merged = _merge_returned(existing_tcs, [], deleted_refs=["TC-1"])
    assert merged == [{"ref": "TC-2", "title": "old"}]

    calls = []

    def flaky_llm(prompt, system=None, max_tokens=None):
        calls.append(max_tokens)
        if len(calls) == 1:
            raise json.JSONDecodeError("Unterminated string", "{", 1)
        return {"testcases": [], "summary": "ok"}

    result = _call_llm_json(flaky_llm, "p", "s", 1000)
    assert result == {"testcases": [], "summary": "ok"}
    assert calls == [1000, 2000], calls

    def always_flaky_llm(prompt, system=None, max_tokens=None):
        raise json.JSONDecodeError("Unterminated string", "{", 1)

    try:
        _call_llm_json(always_flaky_llm, "p", "s", _LLM_MAX_TOKENS_CEILING)
        raise AssertionError("expected JSONDecodeError to propagate once at the ceiling")
    except json.JSONDecodeError:
        pass

    return "ok"


if __name__ == "__main__":
    print(_selftest())
