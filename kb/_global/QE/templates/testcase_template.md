# Test Case Template — Testomat.io Import Format

> Extracted from: `data_ingestion/testcases/CDM Sample Preparing-Shipping - Test Suite 2026-06-30_import.xlsx`
> Format: Excel (`.xlsx`). Each sheet = one testcase (or one testcase type).

## Sheet types

The workbook contains three kinds of sheets.

### 1. `Normal_TestCases` — simple testcases (no data table)

| # | Column | Requirement |
|---|---|---|
| 1 | Title | REQUIRED* |
| 2 | Priority | REQUIRED* |
| 3 | Pre-condition | optional |
| 4 | Step_Description | REQUIRED |
| 5 | Test_Data | optional |
| 6 | Step_ExpectedResult | REQUIRED |

Layout per testcase:
- Row 1 of the testcase: Title + Priority + Pre-condition + first Step_Description + first Test_Data + first Step_ExpectedResult all filled.
- Subsequent rows: only Step_Description / Test_Data / Step_ExpectedResult filled; Title / Priority / Pre-condition left blank.
- If the testcase has no data variants, put `(single-action TC — no DT)` in `Test_Data`.

### 2. `API_TestCases` — API testcases

| # | Column | Requirement |
|---|---|---|
| 1 | Title | REQUIRED |
| 2 | Priority | REQUIRED |
| 3 | Pre-condition | optional |
| 4 | Endpoint | REQUIRED |
| 5 | Method | REQUIRED |
| 6 | Request_Headers | optional |
| 7 | Request_Body | optional |
| 8 | Expected_Status | optional |
| 9 | Expected_Response | optional |

### 3. `<TC_ID>` — data-driven testcase (one sheet per testcase)

Sheet name = the TestCase ID (e.g. `CDM_SPS_003`).

Column layout:

| # | Column | Requirement |
|---|---|---|
| 1 | Title | REQUIRED* |
| 2 | Priority | REQUIRED* |
| 3 | Pre-condition | optional |
| 4 | Step_Description | REQUIRED |
| 5 | Description | REQUIRED |
| 6..N-1 | DataCol_2, DataCol_3, DataCol_4, … | RENAME ↓ (rename to describe each varying input) |
| N | Expected | REQUIRED |

The sheet has three sections in order:

**Section A — Meta + Steps**
- First row: Title + Priority + Pre-condition + first Step_Description + first Expected filled.
- Subsequent step rows: only Step_Description + Expected filled; other cols blank.

**Section B — Data table separator** (single row)
- Text: `DATA TABLE  ▼  one row = one set of test data`

**Section C — Data table**
- Header row after the separator:
  - Cols 1..4: `← not used`
  - Col 5: `Description`
  - Cols 6..N-1: rename `DataCol_2`, `DataCol_3`, … to describe the varying variable (e.g. `Setup (script + offer)`, `Form inputs (variant / size / tracking / fee / note)`, `Field varied`, `Override value`, `Action context`, `Pre-state (SAMPLE_SHIPPED)`, `Action`).
  - Last col: `Expected`
- Data rows: one row per data variant. Fill cols 5..N; leave cols 1..4 blank.

## Conventions observed

- **Title format**: `[<TC_ID>] <verb-first summary>` — e.g. `[CDM_SPS_003] Verify Add tracking & ship sample happy E2E …`
- **Priority values seen**: `Critical`, `High` (others not present in the sample; confirm allowed set with QE lead).
- **Pre-condition**: numbered multi-line list, e.g. `1.\n2.\n3.\n`.
- **Steps**: one Step_Description per row; ordered by row.
- **Expected Result**: observable outcomes; inline UI copy quoted in backticks (e.g. `` `"Mark as preparing"` ``).
- **Cross-references** to other KB entries use double-bracket links, e.g. `[[cdm-sample-shipping]]`.
