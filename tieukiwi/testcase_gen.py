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
        missing = [k for k in _REQUIRED_TC_KEYS if k not in tc]
        if missing:
            raise ValueError(f"testcase[{i}] missing required keys: {missing}")
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
    return "ok"


if __name__ == "__main__":
    print(_selftest())
