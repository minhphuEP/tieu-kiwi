# data_ingestion/

Local drop-zone for **project-specific source documents** that populate the
Postgres knowledge graph (Tier 2). Content is git-ignored (may contain
sensitive BRD / Jira exports) — commit only illustrative fixtures.

> ⚠️ **`data_ingestion/` is NOT the same as `kb/`.**
> - `data_ingestion/` → Postgres graph (Requirement / TestCase / Bug nodes) via
>   `scripts/ingest/*.py`.
> - `kb/` → Chroma RAG (glossary, rules, templates) via `scripts/seed/kb.py`.
>
> Docs to read alongside: [`docs/KB_GUIDE.md`](../docs/KB_GUIDE.md) for RAG,
> [`docs/CHANGELOG.md`](../docs/CHANGELOG.md) for overall changes.

## Layout

```
data_ingestion/
├── requirements/    ← BRD / spec         (.md .markdown .txt .pdf .docx .doc)
├── testcases/       ← QE test bank        (.xlsx .csv)
└── bugs/            ← Jira export         (.json .doc .docx .pdf .txt .md)
```

Text extraction is handled by `tieukiwi.text_extract` — drop any supported
format directly, no conversion needed:

| Ext | Extractor | Notes |
|---|---|---|
| `.md` `.markdown` `.txt` | native | Best fidelity |
| `.pdf` | `pypdf` | Complex tables may lose cells |
| `.docx` | `python-docx` | Paragraphs + table cells preserved |
| `.doc` | `textutil` | macOS only — save as `.docx` on Linux/Windows |

## Prerequisites

```bash
# Repo root: DB up + all migrations applied
docker compose up -d
for f in db/schema.sql db/002_migration.sql db/003_migration.sql db/004_migration.sql; do
  docker exec -i tieu-kiwi-postgres-1 psql -U tieukiwi_app -d tieukiwi < "$f"
done

# Seed users (routing target — needed once)
python scripts/seed/users.py
```

## Workflow

Ingestion is per-file and idempotent (re-running the same file just upserts):

```bash
# Requirements (LLM extract)
python scripts/ingest/requirements.py data_ingestion/requirements/<file>.<md|pdf|docx> \
    --project=<PROJECT_ID> \
    --sprint=<SPRINT_REF> \
    --us=<USERSTORY_REF> \
    --us-title="<optional title>"

# Testcases (structured parse — no LLM)
python scripts/ingest/testcases.py data_ingestion/testcases/<file>.<xlsx|csv> \
    --project=<PROJECT_ID> \
    [--sprint=<SPRINT_REF>] \
    [--sheet=<sheet_name>]

# Bugs (LLM extract for .doc/.docx/.pdf/.txt; direct for .json batches)
python scripts/ingest/bugs.py data_ingestion/bugs/<file>.<doc|docx|pdf|json> \
    --project=<PROJECT_ID>
```

Verify after each ingest (scope to your project):

```bash
python -c "from tieukiwi import db; print(db.go_no_go('<REQ_REF>', project_id='<PROJECT_ID>'))"
```

To wipe and re-ingest cleanly during dev:

```bash
python scripts/seed/reset.py --yes    # nuke nodes/edges/users
python scripts/seed/users.py          # re-seed users
# then re-run scripts/ingest/*.py
```

## Format conventions per pipeline

### `requirements/` — `.md/.pdf/.docx/.doc/.txt`

Whole file goes to the LLM (see `tieukiwi/llm.py`). Claude / Ollama extracts:

- **1 Requirement** — `ref` (from any explicit ticket key like `CDM-198`, else
  derived from title), `title`, `detail`.
- **N AcceptanceCriteria** — `ref` (`AC-1`, `AC-2`, …), `title`, `detail`.
  ACs preserved with sub-bullets and tables inline.
- **Component names** — auto-created as `Component` nodes and linked via
  `impacts` edges to the Requirement.

Provenance stamped on every created node:
```json
"_meta": {
  "extraction_source": "llm:claude-sonnet-4-6",
  "confidence": 0.85,
  "source_file": "data_ingestion/requirements/<file>",
  "review_status": "draft"
}
```

Language preserved (Vietnamese stays Vietnamese, English stays English).

### `testcases/` — `.xlsx` (multi-sheet) or `.csv`

Column headers matched **case-insensitively** against these aliases:

| Canonical | Header aliases (any of) |
|---|---|
| `title` | `Title`, `Test Title` |
| `priority` | `Priority` |
| `preconditions` | `Pre-condition`, `Precondition`, `Pre_condition`, `Preconditions` |
| `step` | `Step_Description`, `Step Description`, `Steps`, `Step` |
| `data` | `Test_Data`, `Test Data`, `Data` |
| `expected` | `Step_ExpectedResult`, `Expected Result`, `Expected`, `Expected_Result` |

**Test ID** is extracted from the `Title` cell via regex `\[([A-Z][A-Z0-9_\-]+)\]`.
Example title: `[CDM_SPS_002] Verify Mark as preparing…` → ref = `CDM_SPS_002`.

**Layout convention** (per sheet in xlsx, or the whole file in csv):
- Row 1: column headers.
- Row 2 (xlsx only): schema hints like `REQUIRED*` / `optional` — auto-detected and **skipped**.
- Row 3+: data rows. First data row carries Title / Priority / Pre-condition
  plus its own step. Subsequent rows carry only Step_Description + Expected
  (later steps of the same testcase).

Extra columns (e.g. `DataCol_2`, `DataCol_3`, `Description`) are preserved
verbatim under `props_json.raw_rows` for auditability — the graph query tools
don't use them, but they're there when you need them.

Provenance: `_meta.extraction_source = "excel-import"` or `"csv-import"`.

**Note**: current code does NOT auto-link testcases to ACs (`coveredBy` edges).
If your source has an AC-reference column, we'd need to extend the ingestor —
ask in Slack. For now, add `coveredBy` edges manually or via a follow-up LLM step.

### `bugs/` — Jira `.json` batch OR single-issue export

**Batch JSON** (Jira REST-shaped):
```json
{
  "issues": [
    {"key": "CDM-287", "summary": "...", "priority": "Medium", "status": "Done", "assignee": "...", ...}
  ]
}
```
Each issue is passed to the LLM for normalisation.

**Single-issue export** (`.doc` from Jira "Export Word", `.docx`, `.pdf`, `.txt`, `.md`):
whole file → LLM → normalised JSON.

The LLM output aligns to this shape:
```json
{
  "ref": "CDM-287",
  "summary": "...",
  "severity": "critical|high|medium|low",   // mapped from Priority
  "status": "open|in_progress|done|closed",
  "reporter": "...",
  "assignee": "...",
  "sprint": "...",
  "parent_ref": "CDM-198",                  // parent story (auto-creates UserStory node)
  "description": {"bug", "steps", "actual", "expected", "root_cause", "find_by"},
  "violates_ac_refs":   ["AC-101-4", ...],   // creates `violates` edges (only if AC exists)
  "affects_components": ["COMP-AUTH", ...],  // creates `affects` edges (auto-creates Component)
  "found_by_testrun_ref": "RUN-XXX-1"        // creates `finds` edge (only if TestRun exists)
}
```

Provenance: `_meta.extraction_source = "llm:<model>"`, `review_status = "draft"`.

## Idempotency

All ingest scripts use `ON CONFLICT (project_id, ref) DO UPDATE` (backed by the
partial unique index from migration 003). Re-running the same file:
- Overwrites `props_json` on existing nodes (fresh data wins).
- Skips edge creation if the edge already exists.
- No duplicate nodes / edges are produced.

Safe to iterate: fix a typo in the source doc, re-run the same ingest command,
the node updates in place.

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `[skip] 'SheetName' — no valid testcase` | No bracketed `[ID]` in first row's Title | Add `[TC-XXX]` prefix to the title |
| Bug has no `affects` / `violates` edges after ingest | Source didn't mention Component / AC refs | Edit the source doc or add edges manually |
| Requirement extracted but ACs missing | LLM missed structure in complex PDF | Convert to `.md` first, or split into smaller files |
| `psycopg.errors.UniqueViolation` | You bypassed the ingest script and inserted raw | Use the ingest scripts; they know about the partial unique index |
| `Cannot read <file>: textutil not found` | Running on Linux/Windows with `.doc` | Convert to `.docx` first |

## What's OUT of scope here

- Chroma / RAG indexing → see [`docs/KB_GUIDE.md`](../docs/KB_GUIDE.md) and
  `kb/` folder convention. `data_ingestion/*` does NOT feed the RAG.
- Auto-linking `coveredBy` (AC ↔ TestCase) from ingestion — planned, currently manual.
- Watch-mode / auto-reingest on file change — not implemented; ingest on demand.
- Real-time Jira sync — future work under `fetch_jira` tool skeleton in `tieukiwi/tools.py`.
