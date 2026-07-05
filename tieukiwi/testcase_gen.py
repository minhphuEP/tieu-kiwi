"""Draft and refine TestCase records for a Requirement via LLM, following the
KB test case template + rubric. No Slack imports here — the Approve/Refine
Slack loop lives in tieukiwi/slack_app.py and calls these functions directly.

Interface:
  generate_draft(requirement_ref, project_id=None) -> dict
  refine_draft(state, comment) -> dict
  finalize_and_save(state, approved_by) -> list[node_id]

Draft schema (shared with tieukiwi/db.py and tieukiwi/testcase_export.py):
  {"ref", "ac_refs", "title", "priority", "precondition", "steps": [...],
   "data_variants": [...]}
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
      "ref": "<short id, e.g. TC-<REQ>-01, unique within the response>",
      "ac_refs": ["<AC ref this testcase covers, at least one>"],
      "title": "<[TC_ID] verb-first summary, <=100 chars>",
      "priority": "<Critical|High|Medium|Low>",
      "precondition": "<numbered list as one string, or empty string>",
      "steps": [{"description": "<action>", "expected": "<observable outcome>"}],
      "data_variants": []
    }
  ],
  "summary": "<1-3 sentences: what you drafted or changed, and why>"
}

Rules:
- Preserve the original language of the AC text (Vietnamese stays Vietnamese).
- Every AC ref passed to you MUST be covered by at least one testcase in the output.
- Use `data_variants` only when the same steps must run against multiple distinct
  input sets (each item: {"label": str, "values": {col: val, ..., "Expected": val}});
  otherwise leave it as an empty list.
"""

_TEMPLATE_FALLBACK = (
    "Title: [TC_ID] verb-first summary. Priority: Critical|High|Medium|Low. "
    "Precondition: numbered list. Steps: Step_Description + Step_ExpectedResult per row."
)
_RUBRIC_FALLBACK = (
    "Cover happy path, negative path, and edge cases for every acceptance criterion. "
    "Keep steps atomic and independently verifiable."
)

_REQUIRED_TC_KEYS = ("ref", "ac_refs", "title", "priority", "steps")


def _ac_gap_refs(prd, existing_testcases):
    """AC refs from `prd['acs']` not covered by any testcase's ac_refs."""
    covered = {ac_ref for tc in existing_testcases for ac_ref in tc.get("ac_refs", [])}
    return [ac["ref"] for ac in prd.get("acs", []) if ac["ref"] not in covered]


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
        normalized.append({
            "ref": tc["ref"],
            "ac_refs": list(tc["ac_refs"]),
            "title": tc["title"],
            "priority": tc["priority"],
            "precondition": tc.get("precondition", ""),
            "steps": tc["steps"],
            "data_variants": tc.get("data_variants") or [],
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

    if not existing:
        prompt = (
            f"{context}\n\n"
            f"Requirement {prd['ref']}: {prd.get('title', '')}\n{prd.get('detail', '')}\n\n"
            f"Acceptance Criteria:\n{ac_lines}\n\n"
            "Draft testcases covering every AC above."
        )
    else:
        existing_text = json.dumps(existing, ensure_ascii=False, indent=2)
        prompt = (
            f"{context}\n\n"
            f"Requirement {prd['ref']}: {prd.get('title', '')}\n{prd.get('detail', '')}\n\n"
            f"Acceptance Criteria:\n{ac_lines}\n\n"
            f"Existing testcases:\n{existing_text}\n\n"
            "Update any testcase whose steps/expected no longer match the AC text "
            "above, and add new testcases for any AC not yet covered. Return the "
            "FULL updated list."
        )

    raw = llm_fn(prompt, system=_SYSTEM_PROMPT, max_tokens=8192)
    testcases = _validate_testcases(raw["testcases"])
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
        prompt = (
            f"{context}\n\nCurrent draft testcases:\n{current_text}\n\n"
            f"Reviewer comment:\n{comment}\n\n"
            "Apply the reviewer's comment and return the FULL updated testcase list."
        )
        raw = llm_fn(prompt, system=_SYSTEM_PROMPT, max_tokens=8192)
        testcases = _validate_testcases(raw["testcases"])
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
    return "ok"


if __name__ == "__main__":
    print(_selftest())
