# gen_testcase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the `gen_testcase` tool end-to-end: fetch PRD + existing test cases from the graph, draft/update test cases with an LLM, run an Approve/Refine loop on Slack, then persist and export the approved test cases to Excel.

**Architecture:** Pure generation logic lives in `tieukiwi/testcase_gen.py` (no Slack/DB side effects beyond `db.py` calls, LLM calls injectable for testing). Excel writing lives in `tieukiwi/testcase_export.py` (pure, no DB). Slack wiring in `tieukiwi/slack_app.py` uses the same regex-intent + button pattern already used for go-live, bypassing the synchronous `agent.ask()` tool loop. State between Slack round-trips uses the existing `thread_state` table via `tieukiwi/memory.py`.

**Tech Stack:** Python, psycopg (Postgres), Anthropic/Ollama via `tieukiwi/llm.py`, `openpyxl` for Excel, `slack_bolt` Block Kit + modals.

**Design doc:** `docs/Gen-testcase-design.md`

---

### Task 1: `db.py` — `requirement_with_acs` and `testcases_for_requirement`

**Files:**
- Modify: `tieukiwi/db.py` (append at end of file)

These two are read-only queries needed before any LLM call. Verified against the
real local Postgres + the `CDM_TEAM` demo fixture already seeded by
`scripts/seed/cdm_demo.py` (run `python scripts/seed/cdm_demo.py` first if not
already seeded — it is idempotent).

- [ ] **Step 1: Write the verification script (expected to fail — function doesn't exist yet)**

Create `scripts/dev/verify_requirement_with_acs.py`:

```python
"""Manual verification for db.requirement_with_acs / db.testcases_for_requirement.
Requires: Postgres up, migrations applied, scripts/seed/cdm_demo.py already run.
Run: python scripts/dev/verify_requirement_with_acs.py
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

from tieukiwi import db

prd = db.requirement_with_acs("CDM-268", project_id="CDM_TEAM")
assert prd["found"] is True, prd
assert prd["ref"] == "CDM-268"
assert prd["title"] == "Reviewer_Duplicate & assign Creator for a Script", prd["title"]
ac_refs = sorted(ac["ref"] for ac in prd["acs"])
assert ac_refs == ["AC-CDM-268-1", "AC-CDM-268-2", "AC-CDM-268-3", "AC-CDM-268-4"], ac_refs
assert all(ac["desc"] for ac in prd["acs"]), prd["acs"]

missing = db.requirement_with_acs("CDM-NOPE-999", project_id="CDM_TEAM")
assert missing["found"] is False, missing

existing = db.testcases_for_requirement("CDM-268", project_id="CDM_TEAM")
by_ref = {tc["ref"]: tc for tc in existing}
assert "TC-CDM-268-A" in by_ref, by_ref.keys()
assert sorted(by_ref["TC-CDM-268-A"]["ac_refs"]) == ["AC-CDM-268-1", "AC-CDM-268-2"], by_ref["TC-CDM-268-A"]

print("OK")
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `source .venv/bin/activate && python scripts/dev/verify_requirement_with_acs.py`
Expected: `AttributeError: module 'tieukiwi.db' has no attribute 'requirement_with_acs'`

- [ ] **Step 3: Implement `requirement_with_acs` and `testcases_for_requirement`**

Append to `tieukiwi/db.py`:

```python
def requirement_with_acs(ref, project_id=None):
    """Return the Requirement's PRD content plus its AcceptanceCriteria.

    Args:
      ref: Requirement ref (e.g. 'CDM-268').
      project_id: if given, requirement must belong to that project.

    Returns:
      {"ref": ref, "found": True, "title": str|None, "detail": str|None,
       "acs": [{"ref": str, "desc": str}]}
      or {"ref": ref, "found": False} if no such Requirement exists.
    """
    sql = "SELECT id, props_json FROM nodes WHERE type='Requirement' AND ref=%s"
    params = [ref]
    if project_id is not None:
        sql += " AND project_id=%s"
        params.append(project_id)
    with conn() as c:
        row = c.execute(sql, params).fetchone()
        if not row:
            return {"ref": ref, "found": False}
        req_id, props = row
        props = props or {}
        acs = c.execute(
            """
            SELECT ac.ref, ac.props_json->>'desc' FROM nodes ac
            JOIN edges h ON h.dst_id=ac.id AND h.rel='has'
            WHERE h.src_id=%s AND ac.type='AcceptanceCriterion'
            ORDER BY ac.ref
            """,
            (req_id,),
        ).fetchall()
    return {
        "ref": ref,
        "found": True,
        "title": props.get("title"),
        "detail": props.get("detail"),
        "acs": [{"ref": ac_ref, "desc": desc} for ac_ref, desc in acs],
    }


def testcases_for_requirement(ref, project_id=None):
    """Return existing TestCase nodes covering any AC of this Requirement.

    Dedup by TestCase ref (a TC can cover multiple AC of the same requirement).
    Each item includes `ac_refs`: every AC of this requirement that this
    TestCase covers. Returns [] if the requirement doesn't exist or has no
    covered ACs yet.
    """
    sql = "SELECT id FROM nodes WHERE type='Requirement' AND ref=%s"
    params = [ref]
    if project_id is not None:
        sql += " AND project_id=%s"
        params.append(project_id)
    with conn() as c:
        row = c.execute(sql, params).fetchone()
        if not row:
            return []
        req_id = row[0]
        rows = c.execute(
            """
            SELECT tc.ref, tc.props_json, ac.ref FROM nodes tc
            JOIN edges cov ON cov.dst_id=tc.id AND cov.rel='coveredBy'
            JOIN nodes ac ON ac.id=cov.src_id AND ac.type='AcceptanceCriterion'
            JOIN edges h ON h.dst_id=ac.id AND h.rel='has'
            WHERE h.src_id=%s AND tc.type='TestCase'
            ORDER BY tc.ref
            """,
            (req_id,),
        ).fetchall()
    by_ref = {}
    for tc_ref, props, ac_ref in rows:
        props = props or {}
        if tc_ref not in by_ref:
            by_ref[tc_ref] = {
                "ref": tc_ref,
                "title": props.get("title"),
                "priority": props.get("priority"),
                "precondition": props.get("precondition"),
                "steps": props.get("steps") or [],
                "data_variants": props.get("data_variants") or [],
                "ac_refs": [],
            }
        by_ref[tc_ref]["ac_refs"].append(ac_ref)
    return list(by_ref.values())
```

- [ ] **Step 4: Run the verification script again to confirm it passes**

Run: `source .venv/bin/activate && python scripts/dev/verify_requirement_with_acs.py`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add tieukiwi/db.py scripts/dev/verify_requirement_with_acs.py
git commit -m "feat(db): add requirement_with_acs and testcases_for_requirement"
```

---

### Task 2: `db.py` — `save_testcases`

**Files:**
- Modify: `tieukiwi/db.py` (append at end of file)

- [ ] **Step 1: Write the verification script (expected to fail)**

Create `scripts/dev/verify_save_testcases.py`:

```python
"""Manual verification for db.save_testcases. Requires Postgres up + migrations
applied + scripts/seed/cdm_demo.py already run (for AC-CDM-268-3 to exist).
Cleans up the test node it creates so it's safe to re-run.
Run: python scripts/dev/verify_save_testcases.py
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

from tieukiwi import db

TEST_REF = "TC-VERIFY-SAVE-001"
db.delete_node_by_ref(TEST_REF, type_="TestCase")  # start clean

draft = [{
    "ref": TEST_REF,
    "ac_refs": ["AC-CDM-268-3"],
    "title": "[TC-VERIFY-SAVE-001] Verify save_testcases upserts and links",
    "priority": "Medium",
    "precondition": "",
    "steps": [{"description": "do X", "expected": "see Y"}],
    "data_variants": [],
}]

ids_1 = db.save_testcases("CDM-268", draft, approved_by="U_TEST", project_id="CDM_TEAM")
assert len(ids_1) == 1

props = db.get_node_props(TEST_REF, type_="TestCase")
assert props["title"] == draft[0]["title"], props
assert props["_meta"]["review_status"] == "verified", props["_meta"]
assert props["_meta"]["approved_by"] == "U_TEST", props["_meta"]

ac_id = db.node_id_for("AC-CDM-268-3", type_="AcceptanceCriterion")
tc_id = db.node_id_for(TEST_REF, type_="TestCase")
gap_before = [ref for _, ref in db.coverage_gap(project_id="CDM_TEAM")]
assert "AC-CDM-268-3" not in gap_before, gap_before  # now covered

# Re-run with a changed title -> should update in place, not duplicate.
draft[0]["title"] = "[TC-VERIFY-SAVE-001] Updated title"
ids_2 = db.save_testcases("CDM-268", draft, approved_by="U_TEST", project_id="CDM_TEAM")
assert ids_2 == ids_1, (ids_2, ids_1)
props_2 = db.get_node_props(TEST_REF, type_="TestCase")
assert props_2["title"] == "[TC-VERIFY-SAVE-001] Updated title"

db.delete_node_by_ref(TEST_REF, type_="TestCase")  # cleanup
print("OK")
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `source .venv/bin/activate && python scripts/dev/verify_save_testcases.py`
Expected: `AttributeError: module 'tieukiwi.db' has no attribute 'save_testcases'`

- [ ] **Step 3: Implement `save_testcases`**

Append to `tieukiwi/db.py`:

```python
def save_testcases(requirement_ref, testcases, approved_by, project_id=None):
    """Upsert draft-schema testcases (tieukiwi/testcase_gen.py) as verified
    TestCase nodes, and ensure a coveredBy edge from each of their ac_refs.

    Args:
      requirement_ref: the Requirement these testcases belong to (context only;
                        edges are created from each testcase's own ac_refs).
      testcases: list of draft-schema dicts.
      approved_by: identifier (Slack user id) of the human approver.
      project_id: scope for resolving ac_refs to node ids and for the upsert
                  key (project_id, ref).

    Returns:
      list of TestCase node ids, in the same order as `testcases`.
    """
    node_ids = []
    with conn() as c:
        for tc in testcases:
            props = {
                "title": tc["title"],
                "priority": tc["priority"],
                "precondition": tc.get("precondition", ""),
                "steps": tc["steps"],
                "data_variants": tc.get("data_variants") or [],
                "_meta": {
                    "extraction_source": "llm:gen_testcase",
                    "confidence": 0.9,
                    "review_status": "verified",
                    "approved_by": approved_by,
                },
            }
            row = c.execute(
                """
                INSERT INTO nodes (type, ref, project_id, props_json)
                VALUES ('TestCase', %s, %s, %s)
                ON CONFLICT (project_id, ref) WHERE ref IS NOT NULL DO UPDATE
                  SET props_json = nodes.props_json || EXCLUDED.props_json
                RETURNING id
                """,
                (tc["ref"], project_id, psycopg.types.json.Json(props)),
            ).fetchone()
            tc_id = row[0]
            node_ids.append(tc_id)
            for ac_ref in tc.get("ac_refs", []):
                ac_sql = "SELECT id FROM nodes WHERE type='AcceptanceCriterion' AND ref=%s"
                ac_params = [ac_ref]
                if project_id is not None:
                    ac_sql += " AND project_id=%s"
                    ac_params.append(project_id)
                ac_row = c.execute(ac_sql, ac_params).fetchone()
                if not ac_row:
                    continue
                ac_id = ac_row[0]
                exists = c.execute(
                    "SELECT id FROM edges WHERE src_id=%s AND rel='coveredBy' AND dst_id=%s",
                    (ac_id, tc_id),
                ).fetchone()
                if not exists:
                    c.execute(
                        "INSERT INTO edges(src_id, rel, dst_id, props_json) VALUES (%s,'coveredBy',%s,%s)",
                        (ac_id, tc_id, psycopg.types.json.Json({})),
                    )
    return node_ids
```

- [ ] **Step 4: Run the verification script again to confirm it passes**

Run: `source .venv/bin/activate && python scripts/dev/verify_save_testcases.py`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add tieukiwi/db.py scripts/dev/verify_save_testcases.py
git commit -m "feat(db): add save_testcases (upsert TestCase + coveredBy edges)"
```

---

### Task 3: `tieukiwi/testcase_export.py` — Excel export (pure, no DB/LLM)

**Files:**
- Create: `tieukiwi/testcase_export.py`

This has no external dependencies (no DB, no LLM, no Slack) so it gets a real
self-test following the existing `_selftest()` convention used in
`tieukiwi/slack_format.py`.

- [ ] **Step 1: Write the failing self-test**

Create `tieukiwi/testcase_export.py` with only the self-test and imports (no
implementation yet):

```python
"""Write TestCase drafts to an Excel workbook following the Testomat.io import
format documented in kb/_global/QE/templates/testcase_template.md — the write
side inverse of scripts/ingest/testcases.py.

Interface:
  export_excel(testcases) -> bytes   # .xlsx workbook, ready to upload

Draft schema per testcase (see tieukiwi/testcase_gen.py):
  {"ref", "ac_refs", "title", "priority", "precondition", "steps": [...],
   "data_variants": [...]}
Empty `data_variants` -> row appended to the shared Normal_TestCases sheet.
Non-empty `data_variants` -> its own sheet named after `ref` (data-driven),
where each variant's `values` dict may include an "Expected" key for the
per-row expected result column (kept last).
"""
import io

import openpyxl

_NORMAL_SHEET = "Normal_TestCases"
_NORMAL_HEADERS = ["Title", "Priority", "Pre-condition", "Step_Description",
                   "Test_Data", "Step_ExpectedResult"]
_DATA_SEP_TEXT = "DATA TABLE  ▼  one row = one set of test data"


def _selftest():
    testcases = [
        {"ref": "TC-1", "ac_refs": ["AC-1"], "title": "[TC-1] Happy path", "priority": "High",
         "precondition": "1. Logged in.",
         "steps": [{"description": "Click submit", "expected": "See success"}],
         "data_variants": []},
        {"ref": "TC-2", "ac_refs": ["AC-2"], "title": "[TC-2] Field variants", "priority": "Medium",
         "precondition": "",
         "steps": [{"description": "Enter value", "expected": "Validated"}],
         "data_variants": [
             {"label": "empty", "values": {"Field": "", "Expected": "Error shown"}},
             {"label": "valid", "values": {"Field": "abc", "Expected": "Accepted"}},
         ]},
    ]
    data = export_excel(testcases)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert set(wb.sheetnames) == {"Normal_TestCases", "TC-2"}, wb.sheetnames
    normal = wb["Normal_TestCases"]
    assert [c.value for c in normal[1]] == _NORMAL_HEADERS
    assert normal["A2"].value == "[TC-1] Happy path"
    dd = wb["TC-2"]
    assert dd["A1"].value == "[TC-2] Field variants"
    header_row = [c.value for c in dd[3]]
    assert header_row[4] == "Description" and header_row[-1] == "Expected", header_row
    assert dd[4][4].value == "empty" and dd[4][5].value == "" and dd[4][-1].value == "Error shown"
    return "ok"


if __name__ == "__main__":
    print(_selftest())
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `source .venv/bin/activate && python -m tieukiwi.testcase_export`
Expected: `NameError: name 'export_excel' is not defined`

- [ ] **Step 3: Implement `export_excel` and its helpers**

Insert above `_selftest()` in `tieukiwi/testcase_export.py`:

```python
def export_excel(testcases):
    """testcases: list of draft-schema dicts. Returns the .xlsx workbook as bytes."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # drop the default blank sheet

    normal_tcs = [tc for tc in testcases if not tc.get("data_variants")]
    data_driven_tcs = [tc for tc in testcases if tc.get("data_variants")]

    if normal_tcs:
        _write_normal_sheet(wb, normal_tcs)
    for tc in data_driven_tcs:
        _write_data_driven_sheet(wb, tc)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _write_normal_sheet(wb, testcases):
    ws = wb.create_sheet(_NORMAL_SHEET)
    ws.append(_NORMAL_HEADERS)
    for tc in testcases:
        steps = tc["steps"] or [{"description": "", "expected": ""}]
        first = steps[0]
        ws.append([tc["title"], tc["priority"], tc.get("precondition", ""),
                   first["description"], "(single-action TC — no DT)", first["expected"]])
        for step in steps[1:]:
            ws.append(["", "", "", step["description"], "", step["expected"]])


def _write_data_driven_sheet(wb, tc):
    ws = wb.create_sheet(tc["ref"])
    steps = tc["steps"] or [{"description": "", "expected": ""}]
    first = steps[0]
    ws.append([tc["title"], tc["priority"], tc.get("precondition", ""),
               first["description"], "Description", first["expected"]])
    for step in steps[1:]:
        ws.append(["", "", "", step["description"], "", step["expected"]])
    ws.append([_DATA_SEP_TEXT])
    all_keys = {k for v in tc["data_variants"] for k in v["values"]}
    variant_cols = sorted(all_keys - {"Expected"})
    ws.append(["", "", "", "", "Description"] + variant_cols + ["Expected"])
    for variant in tc["data_variants"]:
        row = ["", "", "", "", variant.get("label", "")]
        row += [variant["values"].get(col, "") for col in variant_cols]
        row.append(variant["values"].get("Expected", ""))
        ws.append(row)
```

- [ ] **Step 4: Run it to confirm it passes**

Run: `source .venv/bin/activate && python -m tieukiwi.testcase_export`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add tieukiwi/testcase_export.py
git commit -m "feat(testcase_export): write Excel export in Testomat.io format"
```

---

### Task 4: `tieukiwi/testcase_gen.py` — pure helpers (schema validation, AC gap, refine heuristic)

**Files:**
- Create: `tieukiwi/testcase_gen.py`

Build the DB/LLM-free pieces first (validated by a real self-test), then wire in
`generate_draft`/`refine_draft`/`finalize_and_save` in Task 5 on top of them.

- [ ] **Step 1: Write the failing self-test**

Create `tieukiwi/testcase_gen.py`:

```python
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
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `source .venv/bin/activate && python -m tieukiwi.testcase_gen`
Expected: passes immediately for the pure helpers already defined above — this
step is actually GREEN on first write since Task 4 defines both the test and
implementation of these small pure functions together (they're too small to
split further; the file doesn't compile at all until they exist). Confirm by
running it: if any `assert` fails, fix the helper, not the test.

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add tieukiwi/testcase_gen.py
git commit -m "feat(testcase_gen): add pure draft-schema helpers + self-test"
```

---

### Task 5: `tieukiwi/testcase_gen.py` — `generate_draft`, `refine_draft`, `finalize_and_save`

**Files:**
- Modify: `tieukiwi/testcase_gen.py`

These call the LLM and the DB, so they take an injectable `llm_fn` parameter
(defaults to the real `llm.complete_json`) so they can be exercised with a stub
in a fast, deterministic test before touching the real Anthropic API.

- [ ] **Step 1: Write the failing test with a stub LLM**

Create `scripts/dev/verify_testcase_gen.py`:

```python
"""Manual verification for testcase_gen.generate_draft/refine_draft/finalize_and_save,
using a stub LLM (no API calls) but the real Postgres (CDM_TEAM fixture from
scripts/seed/cdm_demo.py must already be seeded).
Run: python scripts/dev/verify_testcase_gen.py
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

from tieukiwi import db, testcase_gen

TEST_REF = "TC-VERIFY-GEN-001"
db.delete_node_by_ref(TEST_REF, type_="TestCase")  # start clean


def stub_llm_generate(prompt, system=None):
    return {
        "testcases": [{
            "ref": TEST_REF,
            "ac_refs": ["AC-CDM-268-3"],
            "title": "[TC-VERIFY-GEN-001] Stub-generated testcase",
            "priority": "Medium",
            "precondition": "",
            "steps": [{"description": "do X", "expected": "see Y"}],
            "data_variants": [],
        }],
        "summary": "Stub draft for AC-CDM-268-3.",
    }


draft = testcase_gen.generate_draft("CDM-268", project_id="CDM_TEAM", llm_fn=stub_llm_generate)
assert draft["version"] == 1
assert draft["testcases"][0]["ref"] == TEST_REF
assert draft["requirement_ref"] == "CDM-268"


def stub_llm_refine(prompt, system=None):
    return {
        "testcases": [{
            "ref": TEST_REF,
            "ac_refs": ["AC-CDM-268-3"],
            "title": "[TC-VERIFY-GEN-001] Refined per reviewer comment",
            "priority": "High",
            "precondition": "",
            "steps": [{"description": "do X", "expected": "see Y"}],
            "data_variants": [],
        }],
        "summary": "Bumped priority to High per reviewer comment.",
    }


refined = testcase_gen.refine_draft(draft, "please bump priority to High", llm_fn=stub_llm_refine)
assert refined["version"] == 2
assert refined["testcases"][0]["priority"] == "High"

# Refine with a full-replacement comment (JSON list) — should NOT call the LLM.
def stub_llm_should_not_be_called(prompt, system=None):
    raise AssertionError("LLM should not be called for a full-replacement comment")

import json
replacement_comment = json.dumps(refined["testcases"])
refined_2 = testcase_gen.refine_draft(refined, replacement_comment, llm_fn=stub_llm_should_not_be_called)
assert refined_2["version"] == 3
assert refined_2["testcases"] == refined["testcases"]

node_ids = testcase_gen.finalize_and_save(refined_2, approved_by="U_TEST")
assert len(node_ids) == 1
props = db.get_node_props(TEST_REF, type_="TestCase")
assert props["title"] == "[TC-VERIFY-GEN-001] Refined per reviewer comment"
assert props["_meta"]["approved_by"] == "U_TEST"

db.delete_node_by_ref(TEST_REF, type_="TestCase")  # cleanup
print("OK")
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `source .venv/bin/activate && python scripts/dev/verify_testcase_gen.py`
Expected: `AttributeError: module 'tieukiwi.testcase_gen' has no attribute 'generate_draft'`

- [ ] **Step 3: Implement `generate_draft`, `refine_draft`, `finalize_and_save`**

Insert above `_selftest()` in `tieukiwi/testcase_gen.py`:

```python
def _fetch_kb_context(project_id=None):
    template_hits = rag.search("test case template format", k=1, project_id=project_id,
                                doc_type="template", include_global=True)
    rubric_hits = rag.search("test case writing rubric conventions", k=1,
                              project_id=project_id, role="QE", include_global=True)
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

    raw = llm_fn(prompt, system=_SYSTEM_PROMPT)
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
        raw = llm_fn(prompt, system=_SYSTEM_PROMPT)
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
```

- [ ] **Step 4: Run the verification script again to confirm it passes**

Run: `source .venv/bin/activate && python scripts/dev/verify_testcase_gen.py`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add tieukiwi/testcase_gen.py scripts/dev/verify_testcase_gen.py
git commit -m "feat(testcase_gen): implement generate_draft, refine_draft, finalize_and_save"
```

---

### Task 6: `skills/gen-testcase.md` — rubric for the LLM prompt

**Files:**
- Create: `skills/gen-testcase.md`

- [ ] **Step 1: Write the skill file**

```markdown
---
name: gen-testcase
description: Rubric for drafting or updating QE test cases from a PRD/Requirement + Acceptance Criteria. Use when generating TestCase records via the gen_testcase tool.
---

# Test Case Generation Rubric

## Coverage
- Every AcceptanceCriterion passed in MUST be covered by at least one testcase.
- Prefer one testcase per distinct scenario (happy path, negative path, edge case)
  rather than cramming multiple scenarios into one testcase's steps.

## Title
Format: `[TC_ID] <verb-first summary>`, e.g.
`[TC-CDM-268-05] Verify draft status persists after duplicate`. Max 100 chars.

## Priority
Allowed values only: `Critical`, `High`, `Medium`, `Low`.
- `Critical`: blocks the core flow described by the Requirement.
- `High`: a primary business flow (create/assign/submit/pay).
- `Medium`/`Low`: UX polish, rare edge cases, cosmetic checks.

## Precondition
A numbered list as a single string (`"1. ...\n2. ..."`), or an empty string when
there is no setup required.

## Steps
One step per row: `description` (the action) + `expected` (an observable outcome,
not an internal implementation detail). Keep each step atomic — independently
verifiable without needing to re-read prior steps.

## data_variants
Only use when the SAME step sequence must run against multiple distinct input
sets (a data-driven testcase). Each item:
`{"label": "<short name for this data set>", "values": {<column>: <value>, ...,
"Expected": "<expected result for this row>"}}`.
If there's no data variation, leave `data_variants` as an empty list — most
testcases should NOT use this field.
```

- [ ] **Step 2: Index it into Chroma and verify it's retrievable**

Run:
```bash
source .venv/bin/activate && python scripts/seed/kb.py
```
Expected: output includes a line like `- skills:gen-testcase` in the upserted-docs list.

Then verify retrieval:
```bash
python -c "
from tieukiwi.rag import search
hits = search('test case writing rubric conventions', k=1, role='QE', include_global=True)
assert hits, 'expected at least one hit'
assert 'gen-testcase' in hits[0][0], hits[0][0]
print('OK')
"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add skills/gen-testcase.md
git commit -m "docs(skills): add gen-testcase rubric for LLM prompt"
```

---

### Task 7: `tieukiwi/tools.py` — wire `gen_testcase` to the new module

**Files:**
- Modify: `tieukiwi/tools.py:14-20` (the `gen_testcase` skeleton)

- [ ] **Step 1: Write the failing check**

Run:
```bash
source .venv/bin/activate && python -c "
from tieukiwi import tools
result = tools.gen_testcase('CDM-268')
assert result.get('status') != 'not_implemented', result
"
```
Expected: `AssertionError` (current skeleton always returns `status: not_implemented`).

- [ ] **Step 2: Replace the skeleton**

In `tieukiwi/tools.py`, replace:

```python
def gen_testcase(requirement_ref):
    model = config.model_for("gen_testcase")  # TODO: pass into the Claude call when implemented
    # TODO: load requirement + ACs (db.trace) and relevant KB (rag.search),
    # then call the Claude API (model=model) to draft TestCase nodes; return proposed testcases.
    return _not_implemented(
        "gen_testcase", "Generate testcases via Claude from requirement + KB context."
    )
```

with:

```python
def gen_testcase(requirement_ref, project_id=None):
    """Draft (or update) testcases for a requirement. Returns the draft dict from
    testcase_gen.generate_draft — plain-chat use (no Slack Approve/Refine loop;
    that loop is driven directly from tieukiwi/slack_app.py, see docs/Gen-testcase-design.md)."""
    return testcase_gen.generate_draft(requirement_ref, project_id=project_id)
```

Add the import at the top of `tieukiwi/tools.py`:

```python
from . import config, db, rag, testcase_gen
```

(replacing the existing `from . import config, db, rag` line).

- [ ] **Step 3: Run the check again to confirm it passes**

This calls the real LLM + DB (no stubbing at the tool-dispatch layer, since this
is the integration point), so it requires `ANTHROPIC_API_KEY` set and the
`CDM_TEAM` fixture seeded.

Run:
```bash
source .venv/bin/activate && python -c "
from tieukiwi import tools
result = tools.gen_testcase('CDM-268', project_id='CDM_TEAM')
assert result.get('status') != 'not_implemented', result
assert result['testcases'], result
print('OK', len(result['testcases']), 'testcases drafted')
"
```
Expected: `OK <N> testcases drafted`

- [ ] **Step 4: Commit**

```bash
git add tieukiwi/tools.py
git commit -m "feat(tools): wire gen_testcase to testcase_gen.generate_draft"
```

---

### Task 8: `slack_format.py` — render a testcase draft as Block Kit text

**Files:**
- Modify: `tieukiwi/slack_format.py` (append near the other render/selftest functions)

- [ ] **Step 1: Write the failing self-test**

Add to `tieukiwi/slack_format.py`, just above the existing `if __name__ == "__main__":` block:

```python
def render_testcase_draft(draft):
    """Render a testcase_gen draft dict as Slack mrkdwn text (AC/TC/Priority table
    + condensed steps), for posting alongside Approve/Refine buttons."""
    lines = [
        f"*Draft test cases — {draft['requirement_ref']} (v{draft['version']})*",
        "",
    ]
    if draft.get("summary"):
        lines.append(f"> {draft['summary']}")
        lines.append("")
    lines.append("| TC | AC | Priority | Title |")
    lines.append("|----|----|----|-------|")
    for tc in draft["testcases"]:
        ac_list = ", ".join(tc["ac_refs"])
        lines.append(f"| {tc['ref']} | {ac_list} | {tc['priority']} | {tc['title']} |")
    return to_slack("\n".join(lines))


def _testcase_draft_selftest():
    draft = {
        "requirement_ref": "CDM-268",
        "version": 2,
        "summary": "Added AC-4 coverage per reviewer comment.",
        "testcases": [
            {"ref": "TC-1", "ac_refs": ["AC-1"], "priority": "High", "title": "[TC-1] Happy path"},
            {"ref": "TC-2", "ac_refs": ["AC-4"], "priority": "Medium", "title": "[TC-2] Archive block"},
        ],
    }
    out = render_testcase_draft(draft)
    assert "CDM-268" in out and "v2" in out, out
    assert "TC-1" in out and "TC-2" in out, out
    assert "Archive block" in out, out
    return out
```

Update the trailing block to also run this self-test:

```python
if __name__ == "__main__":
    print(_selftest())
    print()
    print(_testcase_draft_selftest())
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `source .venv/bin/activate && python -m tieukiwi.slack_format`
Expected: passes for `_selftest()` output then raises inside `_testcase_draft_selftest()`
only if `render_testcase_draft` has a bug — since we're writing test+impl together
here (the function is small and the self-test is the spec), run it once written
to confirm correctness rather than expecting a hard failure first. If any
`assert` fails, fix `render_testcase_draft`.

Expected final: no assertion errors, both self-tests print their output.

- [ ] **Step 3: Commit**

```bash
git add tieukiwi/slack_format.py
git commit -m "feat(slack_format): add render_testcase_draft"
```

---

### Task 9: `slack_app.py` — intent detection, thread_state, Approve/Refine buttons, modal

**Files:**
- Modify: `tieukiwi/slack_app.py`

This is the interactive wiring. It cannot be unit-tested without a live Slack
workspace, so verification here is a manual Slack smoke test (documented as the
final step) plus confirming the module still imports cleanly (the existing
convention per `CLAUDE.md`: "Test with `python -c "import tieukiwi.<module>"`
instead of running `python -m tieukiwi.cli`").

- [ ] **Step 1: Add the intent regex and thread_state key helper**

Near the existing `_GOLIVE_RE` / `_golive_intent` in `tieukiwi/slack_app.py`, add:

```python
# Test-case generation intent, e.g. "gen test case cho CDM-268", "tạo test case CDM-268".
_GEN_TC_RE = re.compile(r"gen(?:erate)?\s*test\s*case|t(ạ|a)o\s*test\s*case", re.I)


def _gen_testcase_intent(text):
    # Return the requirement ref if this looks like a "generate test cases" request, else None.
    if not text or not _GEN_TC_RE.search(text):
        return None
    m = _REQ_RE.search(text)
    return m.group(1).upper() if m else None
```

Add the new imports at the top of `tieukiwi/slack_app.py`:

```python
from . import agent, config, db, memory, routing, slack_format, testcase_export, testcase_gen
```

(replacing the existing `from . import agent, config, db, routing, slack_format` line).

- [ ] **Step 2: Verify the module still imports cleanly**

Run: `source .venv/bin/activate && python -c "import tieukiwi.slack_app"`
Expected: no output, exit code 0.

- [ ] **Step 3: Add the draft-posting flow and button/modal handlers**

Add these functions near `_do_golive` in `tieukiwi/slack_app.py`:

```python
def _testcase_draft_blocks(draft):
    text = slack_format.render_testcase_draft(draft)
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
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
        },
    ]


def _do_gen_testcase(say, requirement_ref, logger=None, thread_ts=None, channel_id=None):
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
    memory.save_thread_state(channel_id, anchor_ts, {"flow": "gen_testcase", **draft})
```

Add these handlers inside `build_app()`, alongside the existing `@app.action("golive_approve")` handlers:

```python
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
        user = body["user"]["id"]
        try:
            testcase_gen.finalize_and_save(state, approved_by=user)
            xlsx_bytes = testcase_export.export_excel(state["testcases"])
            client.files_upload_v2(
                channel=channel_id, thread_ts=thread_ts,
                filename=f"{state['requirement_ref']}_testcases.xlsx",
                content=xlsx_bytes,
                initial_comment=f":white_check_mark: Approved by <@{user}> "
                                 f"(v{state['version']}) — {len(state['testcases'])} testcase(s) saved.",
            )
        except Exception as e:
            logger.exception("finalize_and_save/export failed")
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                     text=slack_format.to_slack(f":warning: Error: {e}"))

    @app.action("tc_refine")
    def handle_tc_refine(ack, body, client, logger):
        ack()
        channel_id = body["channel"]["id"]
        thread_ts = body["message"].get("thread_ts") or body["message"]["ts"]
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
        try:
            refined = testcase_gen.refine_draft(state, comment)
        except Exception as e:
            logger.exception("refine_draft failed")
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                     text=slack_format.to_slack(f":warning: Error: {e}"))
            return
        memory.save_thread_state(channel_id, thread_ts, {"flow": "gen_testcase", **refined})
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                 blocks=_testcase_draft_blocks(refined),
                                 text=f"Draft test cases for {refined['requirement_ref']} (v{refined['version']})")
```

Add `import json` at the top of `tieukiwi/slack_app.py` if not already present (it already is, per the existing `import json` at line 15).

Wire the intent check into `handle_tieukiwi`, right after the existing go-live
intent check (`if ref: _do_golive(...); return`):

```python
        # Generate-testcase request -> draft + Approve/Refine buttons.
        tc_ref = _gen_testcase_intent(text)
        if tc_ref:
            _do_gen_testcase(say, tc_ref, logger, channel_id=command.get("channel_id"))
            return
```

- [ ] **Step 4: Verify the module still imports cleanly**

Run: `source .venv/bin/activate && python -c "import tieukiwi.slack_app"`
Expected: no output, exit code 0.

- [ ] **Step 5: Manual Slack smoke test (requires SLACK_BOT_TOKEN + SLACK_APP_TOKEN in .env)**

```bash
source .venv/bin/activate && python -m tieukiwi.slack_app
```
In the connected Slack workspace, in a channel bound to `CDM_TEAM`
(`db.bind_channel(...)`, see `docs/STORAGE_GUIDE.md` §7):
1. Run `/tieukiwi gen test case cho CDM-268` → expect a draft message with an
   AC/TC/Priority table and `[Approve test cases]` / `[Refine test cases]` buttons.
2. Click **Refine test cases** → expect a modal with a textarea; type a comment
   (e.g. "add a case for archived products") and submit → expect a new message
   with `(v2)` and updated table.
3. Click **Approve test cases** → expect an `.xlsx` file attached to the thread
   and a "Approved by @you" confirmation message.
4. Download the `.xlsx` and open it → expect a `Normal_TestCases` sheet (and any
   data-driven sheets) matching the approved draft.

- [ ] **Step 6: Commit**

```bash
git add tieukiwi/slack_app.py
git commit -m "feat(slack_app): wire gen_testcase Approve/Refine loop + Excel export"
```

---

## Plan Self-Review Notes

- **Spec coverage:** Task 1–2 cover PRD/existing-TC fetch + persistence (spec
  §"1. Input" and the save step). Task 4–5 cover branch 2.1/2.2 draft + refine
  loop logic. Task 3 covers the Excel export requirement. Task 6 covers "always
  follow the template" via the KB rubric. Task 7 covers the `tools.py` contract.
  Task 9 covers the full Slack Approve/Refine/Approve-again loop and thread state.
- **No placeholders:** every step has runnable code and an expected output.
- **Type consistency:** the draft schema (`ref`, `ac_refs`, `title`, `priority`,
  `precondition`, `steps`, `data_variants`) is identical across
  `testcase_gen.py`, `db.save_testcases`, and `testcase_export.py`.
- **Out of scope** (per design doc, unchanged): thread-reply-as-comment,
  concurrent-approval conflict handling, auto-rerunning `go_no_go` after approval.
