---
name: gen-testcase
description: Rubric for drafting or updating QE test cases from a PRD/Requirement + Acceptance Criteria. Use when generating TestCase records via the gen_testcase tool.
---

# Test Case Generation Rubric

## Coverage
- Every AcceptanceCriterion passed in MUST be covered by at least one testcase.
- Prefer one testcase per distinct scenario (happy path, negative path, edge case)
  rather than cramming multiple scenarios into one testcase's steps.

## Language
Write ALL testcase content (title, precondition, steps, data_variants, api
fields) in English, even when the source Requirement/AC text is in another
language (e.g. Vietnamese).

## Legacy testcase migration (standing rule, not a one-time fix)
When updating existing testcases (the "existing testcases found" branch), ALWAYS
rewrite any testcase that does not already conform to this rubric — wrong
language, legacy ID format, disallowed priority value, missing/incorrect
`type`. This applies on EVERY generation run that encounters a non-conforming
testcase, not just the first time one is spotted. Never leave a legacy-format
testcase as-is just because it already exists.

## Test Case ID
Format: `<ProjectCode>_<AcronymOfFeature>_<NNN>`, e.g. `CDM_AssignCreator_001`,
`CDM_AssignCreator_002`, `CDM_DupScript_001`. `ProjectCode` comes from the
Requirement ref (e.g. `CDM-268` -> `CDM`). `AcronymOfFeature` is a short
PascalCase name shared by every testcase in the same scenario group. `NNN` is
a zero-padded 3-digit index, incrementing within that group.

## Title
Format: `[TC_ID] <verb-first summary>`, e.g.
`[CDM_AssignCreator_001] Verify Assign button visibility per script status`.
Max 100 chars.

## Type
Allowed values only: `Normal`, `API`, `DataTable`.
- `Normal`: a standard UI/manual testcase — describe via `steps`.
- `API`: exercises a REST/API endpoint — describe via the `api` object
  (`endpoint`, `method`, `request_headers`, `request_body`, `expected_status`,
  `expected_response`); `steps` may still describe request/response actions.
- `DataTable`: the SAME step sequence must run against multiple distinct input
  sets — describe the shared steps via `steps`, then the input sets via
  `data_variants`.

## Priority
Allowed values only: `Highest`, `High`, `Medium`, `Low`.
- `Highest`: blocks the core flow described by the Requirement.
- `High`: a primary business flow (create/assign/submit/pay).
- `Medium`/`Low`: UX polish, rare edge cases, cosmetic checks.

## Precondition
A numbered list as a single string (`"1. ...\n2. ..."`), or an empty string when
there is no setup required.

## Steps
One step per row: `description` (the action) + `expected` (an observable outcome,
not an internal implementation detail). Keep each step atomic — independently
verifiable without needing to re-read prior steps. Every testcase MUST have at
least one concrete step — never output only an id/title with no steps.

## data_variants
Only for `type: DataTable` — the SAME step sequence must run against multiple
distinct input sets. Each item:
`{"label": "<short name for this data set>", "values": {<column>: <value>, ...,
"Expected": "<expected result for this row>"}}`.
Leave `data_variants` as an empty list for `Normal`/`API` testcases.

### Converting a reviewer's line-list into data_variants (no fabrication)
When a reviewer comment lists one scenario per line (bullets/dashes) asking to
turn a testcase into `DataTable`:
- One line = one `data_variants` item, in the same order. Do not merge, split,
  reorder, or drop lines.
- Use the reviewer's line text VERBATIM as `label` — do not reword, rephrase,
  or "clean up" their wording.
- NEVER invent specific column values (exact statuses, counts, setup details,
  IDs) that are not explicitly stated in the reviewer's comment or already
  established by the Requirement/AC text. A fabricated-but-plausible value is
  worse than an omitted one, since it can mislead whoever executes the test.
- Only add a `values` column when every row's value for it is verifiable from
  the comment or AC text. If you cannot verify a value for a given row, leave
  that column out of that row's `values` rather than guessing — a sparse but
  accurate data table beats a complete but invented one.

## api
Only for `type: API`:
`{"endpoint": "<path>", "method": "<GET|POST|...>", "request_headers": "...",
"request_body": "...", "expected_status": "<code>", "expected_response": "..."}`.
Leave `api` as an empty object for `Normal`/`DataTable` testcases.
