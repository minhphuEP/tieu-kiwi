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
