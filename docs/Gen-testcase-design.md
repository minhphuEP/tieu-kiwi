# Design: `gen_testcase` — Draft → Review Loop → Export

> Status: approved. Scope: the `gen_testcase` tool only (Layer A tool + Slack wiring
> needed to drive its review loop). Does not touch other tools, the agent loop
> contract, or unrelated Slack flows.

## Goal

`gen_testcase` currently a skeleton in `tieukiwi/tools.py` (returns `not_implemented`).
Implement it to:

1. Fetch the PRD (Requirement + its AcceptanceCriteria) from the graph, plus any
   existing TestCase nodes already covering that Requirement.
2. Draft (or update) test cases with an LLM, following the KB test case template
   and rubric.
3. Run an Approve/Refine review loop on Slack (buttons + modal), looping until the
   user approves.
4. On approval: persist final TestCase nodes to Postgres and export an Excel file
   (Testomat.io import format) attached to the Slack thread.

## Architecture

```
Slack: "gen test case cho CDM-268"
  │  (regex intent, same pattern as _golive_intent — bypasses agent.ask())
  ▼
tieukiwi/testcase_gen.py :: generate_draft(ref, project_id)
  │  fetch Requirement+AC (tieukiwi/db.py) + existing TestCase (if any)
  │  fetch template + rubric (rag.search over kb/ + skills/)
  │  call LLM → draft TestCase list (JSON)
  ▼
thread_state (channel_id, thread_ts) = {ref, version, testcases, draft_message_ts}
  ▼
Slack post: rendered draft + [Approve] [Refine] buttons
  │
  ├─ Refine → open Modal (textarea) → submit
  │      testcase_gen.refine_draft(state, comment) → LLM edits/adds → version+1
  │      → update thread_state → post new draft + buttons (loop)
  │
  └─ Approve → testcase_gen.finalize_and_save(state, approved_by)
         → upsert TestCase nodes + coveredBy edges (tieukiwi/db.py)
         → testcase_export.export_excel(testcases) → .xlsx bytes
         → client.files_upload_v2() attach to thread
```

The agent tool-use loop (`agent.ask()`) is synchronous and cannot hold state across
a Slack button click that may arrive minutes or hours later. So the interactive
review loop (Refine/Approve) is driven entirely from `slack_app.py` action/view
handlers, independent of `agent.ask()` — the same separation the codebase already
uses for the go-live Approve/Reject flow (`_golive_intent` → `_do_golive`, not
routed through the agent tool loop).

## Components

### 1. `tieukiwi/db.py` — 3 new functions (no changes to existing signatures)

- `requirement_with_acs(ref, project_id=None) -> {ref, title, detail, acs: [{ref, desc}]}`
  Structured PRD: the Requirement node plus every AcceptanceCriterion linked via
  the `has` edge.
- `testcases_for_requirement(ref, project_id=None) -> list[dict]`
  Existing TestCase nodes reachable via AC → `coveredBy` → TestCase, deduplicated
  by `ref`. Empty list when the Requirement has no test cases yet — this is what
  distinguishes branch 2.1 (generate fresh) from 2.2 (update existing).
- `save_testcases(requirement_ref, testcases, approved_by, project_id=None) -> list[node_id]`
  Upserts each test case as a TestCase node (props = the draft schema below, see
  "Draft schema") with `props_json._meta` set to
  `{"extraction_source": "llm:<model>", "confidence": <llm confidence or 0.9 default>,
  "review_status": "verified", "approved_by": <slack user id>}`, and ensures a
  `coveredBy` edge from each `ac_ref` to the TestCase.

### 2. `tieukiwi/testcase_gen.py` (new module)

Pure generation/refinement logic — no Slack imports, no DB writes except through
`db.py` helpers, testable in isolation.

- `generate_draft(requirement_ref, project_id=None) -> dict`
  - Fetches PRD via `db.requirement_with_acs`.
  - Fetches existing test cases via `db.testcases_for_requirement`.
  - Fetches the template (`kb/_global/QE/templates/testcase_template.md`) and the
    rubric (`skills/gen-testcase/SKILL.md`) via `rag.search(..., doc_type="template")`
    and `rag.search(..., role="QE")` — falls back to a bundled constant if the KB
    hasn't been seeded (so the tool degrades gracefully rather than crashing on a
    missing `VOYAGEAI_API_KEY`/empty Chroma collection).
  - Branch A (no existing test cases): asks the LLM to draft one test case per
    logical scenario per AC, covering every `ac_ref` in the PRD at least once.
  - Branch B (existing test cases found): sends the LLM the existing test cases +
    current AC text, asking it to (a) update any test case whose steps/expected no
    longer match the current AC wording, (b) add new test cases for any AC not yet
    covered. The LLM returns the full updated list, not a diff.
  - Returns `{requirement_ref, version: 1, testcases: [...], summary: "<what changed and why>"}`.
- `refine_draft(state, comment) -> dict`
  - `state` is the current thread_state blob (see "State management").
  - Sends the current draft + the user's comment to the LLM. If the comment looks
    like a full replacement test case list (heuristic: parses as a JSON/table
    structure resembling the draft schema), treat it as the new version directly
    instead of asking the LLM to interpret free text.
  - Returns `{..., version: state["version"] + 1, ...}`.
- `finalize_and_save(state, approved_by) -> list[node_id]`
  - Calls `db.save_testcases(state["requirement_ref"], state["testcases"], approved_by, project_id)`.

#### Draft schema (shared: thread_state, LLM I/O, DB props)

```json
{
  "ref": "TC-CDM-268-05",
  "ac_refs": ["AC-CDM-268-3"],
  "title": "[TC-CDM-268-05] Verify draft status persists after duplicate",
  "priority": "High",
  "precondition": "1. Script gốc đã tồn tại và ở trạng thái Published.",
  "steps": [
    {"description": "Mở script gốc, bấm Duplicate", "expected": "Script mới xuất hiện trong list"}
  ],
  "data_variants": []
}
```

`data_variants` (optional, default `[]`): when non-empty, each item is
`{"label": str, "values": {<column_name>: <value>}}` — one row of a data table.
Empty → exported to the shared `Normal_TestCases` sheet; non-empty → exported to
its own sheet named after `ref` (data-driven format), matching the two sheet types
already documented in `kb/_global/QE/templates/testcase_template.md`.

### 3. `tieukiwi/testcase_export.py` (new module)

Mirrors the column conventions `scripts/ingest/testcases.py` reads, but is not a
strict round-trip inverse — re-ingesting an exported data-driven sheet is not
currently supported by that script's parser. `export_excel(testcases: list[dict]) -> bytes`:
- Builds one `Normal_TestCases` sheet for every draft with empty `data_variants`,
  one row per step (title/priority/precondition only on the first row of each TC,
  matching the read-side convention already documented).
- Builds one sheet per TC with non-empty `data_variants`, named after `ref`
  (sanitized: invalid Excel characters stripped, truncated to 31 chars,
  de-duplicated with a numeric suffix on collision), following the Section A
  (steps) / separator / Section C (data table) layout.
- Raises `ValueError` on an empty `testcases` list rather than producing an
  invalid workbook.
- Returns the workbook as bytes (no temp file) for direct upload to Slack.

### 4. `skills/gen-testcase.md` (new)

Rubric for the LLM prompt: Title format (`[TC_ID] verb-first summary`), allowed
Priority values, Precondition/Steps/Expected conventions — mirrors the "Conventions
observed" section of `testcase_template.md`. Indexed into Chroma by the existing
`scripts/seed/kb.py` run (skills/ is already scanned), retrieved via `rag.search`
in `testcase_gen.py` alongside the template — no new ingestion plumbing needed.

### 5. `tieukiwi/tools.py`

`gen_testcase(requirement_ref)` becomes a thin wrapper calling
`testcase_gen.generate_draft(requirement_ref)`, keeping the existing tool contract
for the agent loop unchanged (Claude can still call this tool mid-conversation and
get draft text back — without Slack buttons — for plain-chat use outside the
Slack interactive flow).

### 6. `tieukiwi/slack_app.py` + `tieukiwi/slack_format.py`

- `_gen_testcase_intent(text)` — regex intent detector, same shape as
  `_golive_intent`, matching phrases like "gen test case cho CDM-268" / "tạo test
  case cho ...".
- On match: call `testcase_gen.generate_draft`, store the result in `thread_state`
  keyed by `(channel_id, thread_ts)` (the ts of the message Slack assigns when we
  post the draft), post the rendered draft with `[Approve]`/`[Refine]` buttons.
- `@app.action("tc_approve")` — calls `finalize_and_save`, then
  `testcase_export.export_excel`, then `client.files_upload_v2` to attach the
  `.xlsx` to the thread; edits the original message to remove the buttons and show
  "Approved by @X (version N)".
- `@app.action("tc_refine")` — opens a modal with a multi-line textarea.
- `@app.view("tc_refine_submit")` — reads the textarea, calls
  `testcase_gen.refine_draft`, updates `thread_state`, posts the new draft version
  + buttons (loop continues).
- `slack_format.render_testcase_draft(draft)` — renders the draft as a Slack
  mrkdwn bullet list (one line per testcase: ref, AC refs, priority, title),
  built directly rather than via `to_slack()` — see the function's docstring:
  `to_slack`'s AC-line parser (`_parse_ac_lines`) hijacks any line combining an
  AC-like ref with a common word like "fail"/"không có" and silently rewrites
  it as a fabricated go-live coverage report, so a pipe-table or `to_slack`-
  routed render is unsafe for this content.
- Each new draft version is posted as a NEW Slack message; the previous draft
  message has its buttons stripped (`chat_update`, best-effort) and is marked
  "Superseded by vN". Both `tc_approve` and `tc_refine` clicks are rejected
  with a warning if they come from a superseded message
  (`_is_stale_draft_click`, comparing against `state["draft_message_ts"]`).

## State management

`thread_state` table (already exists, migration 002) keyed by `(channel_id,
thread_ts)`. Blob shape (actual, as implemented):

```json
{
  "flow": "gen_testcase",
  "requirement_ref": "CDM-268",
  "project_id": "CDM_TEAM",
  "version": 2,
  "testcases": [ /* draft schema array, current version */ ],
  "summary": "what changed in this version and why",
  "draft_message_ts": "1720000000.123456"
}
```

No `history` array — only the current version is kept; superseded messages are
marked in-place in Slack instead. Read via `memory.get_thread_state`, written
via `memory.save_thread_state` — no schema changes needed.

## Error handling

- Requirement not found → `generate_draft` raises `ValueError`; the plain-chat
  tool path (`tools.gen_testcase`) catches `(ValueError, KeyError)` and returns
  a structured `{"status": "error", ...}` dict; the Slack path
  (`_do_gen_testcase`) catches the same and replies with an error message. No
  thread_state is created on failure.
- LLM returns invalid/incomplete JSON (e.g. truncated output) → propagates as a
  `ValueError`/`json.JSONDecodeError`, handled the same way as above — no
  automatic retry. `max_tokens` scales with the number of testcases being
  echoed back (`_max_tokens_for`, base 4096 + 400/testcase, capped at 16000) to
  avoid the truncation failure mode in the first place.
- Excel export fails (missing/malformed field, or empty testcase list) → the
  TestCase nodes are already saved (approval already happened); report the
  export error in-thread without rolling back the DB write.
- A click on a superseded draft message (an older Refine/Approve button after a
  newer version was posted) is rejected with a warning rather than acting on
  stale state (`_is_stale_draft_click`).

## Out of scope for this iteration

- Replying in-thread as a comment mechanism (Modal was chosen instead).
- Concurrent-approval conflict handling (last-write-wins is acceptable for now;
  this includes a narrow TOCTOU window where a Refine modal submission can land
  after a different newer draft was already posted — the submit itself does
  not re-check staleness, only the initial button clicks do).
- Automatically re-running `go_no_go` after approval.
- True atomicity against near-simultaneous double-clicks on Approve (buttons
  are removed after the DB save succeeds, which closes the common case, but
  two clicks arriving before the first `chat_update` completes could still
  both reach the export/upload step).
