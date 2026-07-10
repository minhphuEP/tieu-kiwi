# Tieu Kiwi — Changelog

## 2026-07-10 — Post-storage-refactor delta

New capabilities landed since the 2026-07-03 entry below. Backward compat
preserved throughout (every scoping param stays optional).

### Ingest pipeline

- ✅ `ingest_jira_ticket(issue_key, project_id=None, force=False)` orchestrator
  in `tieukiwi/jira_ingest.py` — fetches Jira Story + subtasks + Confluence
  PRDs → materialises Requirement / AC / TestRun / Bug nodes in one pass.
- ✅ 2-tier content hash-gate: `_story_hash` (fields + subtask stubs) and
  `_bug_table_hash` (per bug container). Unchanged tickets short-circuit as
  `status='cached_fresh'`.
- ✅ BRD-drift detection: on cached_fresh, compare stored version vs
  Confluence `version.number` — auto-elevate to full ingest when PRD moved.
- ✅ Regex fast-path for explicit `AC1:`/`CC1:` markers, LLM fallback for
  free-flowing prose.
- ✅ **Batched AC diff across pages** (fix): earlier per-page diff was
  obsoleting AC of previously-processed pages; now accumulated then
  reconciled once. AC ref stays `AC-{req_key}-{hash8}` (content-stable).
- ✅ **`section_anchor` + `section_title` on AC props** — human-readable
  section context stored alongside hash-based ref, so operators can:
  ```sql
  SELECT ref FROM nodes WHERE type='AcceptanceCriterion' AND project_id='CDM'
    AND props_json->>'section_title' ILIKE '%login%';
  ```
  Then copy the ref into Excel `ac_refs` column.
- ✅ VN→EN translate for bug-table cells with diacritics
  (`_maybe_translate_bug_field`) — Postgres contract is English-only.
- ✅ `[Bug]` subtask table parser (`parse_bug_subtask_table`): 1 row = 1 Bug
  node, `find_by=Testcase` auto-creates `TestRun_self -finds-> Bug`.
- ✅ TestCase Excel ingest reads `ac_refs` column → auto
  `AC -coveredBy-> TC` edges (`scripts/ingest/testcases.py`).

### Slack (Layer B)

- ✅ Unified turn handler `_handle_mention_turn` for both `@mention` and
  non-mention thread reply — same pipeline (refuse-switch → discard →
  sticky-resolve → pre-flight ingest → intent routing).
- ✅ Sticky Jira ticket per thread (`_save_thread_ref`, `_resolve_thread_ref`),
  first-wins — a different ref in a follow-up message is treated as ad-hoc.
- ✅ Refuse thread-reassignment attempts (`_SWITCH_RE`,
  `_detect_switch_target`, `_switch_refusal_text`) — "thread này giờ là
  CDM-500" is blocked with a message asking to open a new thread.
- ✅ Force-refresh intent (`_FORCE_REFRESH_RE`) — "cập nhật CDM-268", "PRD đã
  update", "chạy lại đi" → `force=True` on `ingest_jira_ticket`.
- ✅ List-AC intent (`_LIST_AC_RE`, `_do_list_acs`) — deterministic AC dump,
  skips LLM.
- ✅ Clarify-BRD interview (`_CLARIFY_RE`, `_do_clarify`, modal flow) — Slack
  Block Kit equivalent of the `brd-clarifier` interview workflow.
- ✅ Live in-thread progress (`_make_progress_callback` + `chat_update` in
  place) — replaces the "Processing…" ack with per-step labels then the final
  answer, one tidy message per question.
- ✅ Pre-flight `ingest_jira_ticket` before every question in a ticket thread
  (`_ensure_ticket_fresh`) — costs ~500ms when cached, keeps the graph in sync
  with Jira/Confluence for downstream tools.

### Agent / tools

- ✅ Polymorphic `get_ticket(ref)` — handles Requirement/Bug/TestRun/UserStory
  /BRD in one call, agent no longer needs to guess the type.
- ✅ `mark_reviewed` — TestCase review state machine (`draft` →
  `qe_reviewed`).
- ✅ `find_ambiguities` — LLM tool for the clarify flow.
- ✅ `code_impact`, `feature_blast_radius` — code-graph impact analysis
  (backend graph ingest via `scripts/ingest/code_graph.py`).
- ✅ `mention_for(role, project_id)` in `db.py` — single mention path
  through users table → `ROLE_<ROLE>` env → non-crashing `@role (unconfigured)`
  fallback.

### DB / migrations

- ✅ `db/005_migration.sql`, `db/006_migration.sql` — additional indexes +
  channel_project_map fields.
- ❌ `db/007_migration.sql` withdrawn — was data-fix for legacy `CDM_TEAM` →
  `CDM`. Fresh installs don't need it. See STORAGE_GUIDE §6.2.6.

### Config housekeeping

- ✅ `.env.example` present.
- ✅ `tieukiwi/config.py` populated (was empty in earlier snapshot).
- ✅ Chroma collection unified to `"knowledge_base"`.

### Deprecated

- ❌ `scripts/seed/cdm_demo.py`, `scripts/seed/cdm_demo_import_tcs.py` —
  removed. Real Jira via `ingest_jira_ticket` is the canonical seeding path;
  `scripts/seed/graph.py` remains as a lightweight generic fixture.
- ❌ `fetch_jira` tool hidden from `TOOLS[]` — LLM must go through
  `ingest_jira_ticket` (full pull) or `get_ticket` (read cache). Function
  itself kept for legacy scripts.

---

# Tieu Kiwi — Storage Layer Update (2026-07-03)

> Notice for the team. Storage layer got multi-tenant scoping (Slack channel →
> project), a real ingestion pipeline for BRD / testcase / bug docs, and a
> convention-driven KB folder structure. **No signature was broken**; every new
> scoping param is optional with a `None` default.

## TL;DR

- ✅ Multi-tenant: every Postgres query + RAG search can be scoped by
  `project_id` and `role`. Slack layer resolves `channel_id → project_id`.
- ✅ Ingestion pipelines for `.md` / `.pdf` / `.docx` / `.doc` / `.txt` / `.xlsx` / `.csv` / `.json`.
- ✅ KB folder convention: metadata auto-inferred from the path.
- ✅ Backward compat: `db.coverage_gap()` (no args) still works.

## 🆕 New files

### Storage schema
| File | Purpose |
|---|---|
| `db/002_migration.sql` | `nodes.project_id` + `users` table + indexes |
| `db/003_migration.sql` | Partial unique index `(project_id, ref)` — enables idempotent upsert |
| `db/004_migration.sql` | `channel_project_map` (Slack channel ↔ project) |

### Python modules
| File | Purpose |
|---|---|
| `tieukiwi/llm.py` | LLM abstraction (anthropic / ollama). Used by ingestion, NOT by agent loop |
| `tieukiwi/text_extract.py` | Shared text extractor for `.md/.pdf/.docx/.doc/.txt` |

### Scripts (under `scripts/`)
| File | What it does |
|---|---|
| `scripts/seed/kb.py` | Index `skills/` + `kb/` into Chroma (was `seed.py`) |
| `scripts/seed/users.py` | Seed the `users` directory (7 project + 6 global fallback) |
| `scripts/seed/graph.py` | Sample graph fixture (was `seed_graph.py`) |
| `scripts/seed/reset.py` | Dev-only: DELETE FROM edges/nodes/users |
| `scripts/ingest/requirements.py` | BRD → Requirement + AC + Component nodes via Claude |
| `scripts/ingest/testcases.py` | Excel / CSV → TestCase nodes (no LLM, direct parse) |
| `scripts/ingest/bugs.py` | Jira `.json` batch OR `.doc/.docx/.pdf` single → Bug nodes via Claude |

### Docs & sample KB
| File | Purpose |
|---|---|
| `docs/ontology.md` | 9 node types + 9 relations, Mermaid diagrams, routing map |
| `docs/db_schema.md` | ERD, cross-project semantics, `_meta` provenance convention |
| `docs/KB_GUIDE.md` | How to add / update KB content (see this file) |
| `data_ingestion/README.md` | Drop-zone layout + workflow |
| `kb/CDM/glossary.md` | Sample project-scoped glossary |
| `kb/_global/QE/templates/testcase_template.md` | Sample global QE template |

## 🔧 Modified files (backward compat)

### `tieukiwi/db.py`

Every graph query got an **optional** `project_id` kwarg. Default `None` = no
filter (i.e. exact old behavior). Set to a project code = multi-tenant scope.

```python
db.coverage_gap(project_id=None)
db.trace(req_ref, project_id=None)
db.bug_blast_radius(bug_ref, project_id=None)  # blast STILL cross-project
db.go_no_go(req_ref, project_id=None)          # returns decision='NOT_FOUND' if req not in project
db.failing_tests_for(req_ref, project_id=None)
db.open_bugs_for(req_ref, project_id=None)
```

New helpers for Slack integration:

```python
db.project_for_channel(channel_id) -> str | None
db.bind_channel(channel_id, project_id, team_id=None, note=None)
```

### `tieukiwi/rag.py`

`search()` extended with metadata filters (all optional):

```python
rag.search(
    query, k=4,
    project_id=None, role=None, doc_type=None,
    include_global=False,   # if True + project_id set, also match scope=global
)
```

Added `rag.wipe()` — drop and recreate the Chroma collection (use before
re-seed when source files were deleted).

### `tieukiwi/tools.py`

`run_tool` now takes ambient context:

```python
def run_tool(name, args, context=None):
    """context = {"project_id": ..., "role": ...} — set by the Slack layer
       (channel_id → project_id) via agent.ask(). NOT in input_schema, so the
       LLM cannot spoof it."""
```

Every tool that goes to Postgres/RAG now reads `context["project_id"]` /
`context["role"]` and passes them down.

### `tieukiwi/agent.py`

`ask()` accepts two new kwargs and bundles them into the tool context:

```python
def ask(user_msg, system=..., project_id=None, role=None):
    context = {"project_id": project_id, "role": role}
    ...
    run_tool(block.name, block.input, context=context)
```

### `tieukiwi/routing.py`

Kept `owner_for(entity_type)` (legacy, class-level role name).

Added `resolve_owner_slack(node_id)` with 3-tier fallback:

1. `nodes.props_json.owner_slack_id` (instance override)
2. `users WHERE role=X AND project_id=<node.project_id>`
3. `users WHERE role=X AND project_id IS NULL` (global fallback)
4. `None` (log a gap, ask curator to add mapping)

Feedback nodes hop through `about` edge to resolve the target entity's owner.

### `scripts/seed/kb.py`

Auto-inference from folder path — no manual tagging:

- `kb/<PROJECT>/**` → `scope=project`, `project_id=<PROJECT>`
- `kb/_global/**` → `scope=global`
- `kb/*/<QE|PO|BO|DEV>/**` → `role=<...>`
- `templates/`, `samples/`, filename `*glossary*` → `doc_type=...`

Reads every extension supported by `text_extract` — drop `.pdf` / `.docx` /
`.doc` straight into `kb/` and it just works.

### `scripts/seed/graph.py`

Extended fixture: 17 nodes / 22 edges / 7 users, cross-project edges,
`_meta.extraction_source` provenance on LLM-simulated nodes.

### `tieukiwi/config.py` + `.env.example`

Added ingestion LLM settings: `LLM_PROVIDER`, `ANTHROPIC_MODEL`, `OLLAMA_HOST`,
`OLLAMA_LLM_MODEL`.

### `requirements.txt`

Added: `pandas`, `openpyxl`, `pypdf`, `python-docx`.

## 🔒 New conventions (please follow)

1. **Schema evolution**: never edit `db/schema.sql`. Add a new file
   `db/NNN_migration.sql` (idempotent, using `IF NOT EXISTS`).
2. **`props_json._meta` provenance**: every node inserted by an LLM extractor
   MUST embed `_meta`:
   ```json
   {"_meta": {
     "extraction_source": "llm:claude-sonnet-4-6",
     "confidence": 0.87,
     "source_file": "...",
     "review_status": "draft"
   }}
   ```
   Human-authored: `_meta.extraction_source = "human"`. Absent = human by default.
3. **Multi-tenant scoping**: Layer B (Slack) MUST resolve `channel_id → project_id`
   at the entry of every event handler:
   ```python
   proj = db.project_for_channel(event["channel"]) or DEFAULT_PROJECT
   answer = agent.ask(text, project_id=proj, role="QE")
   ```

## 🎯 What each other team needs to know

### Slack layer (Layer B)

Wire a new channel once at setup:
```python
db.bind_channel("C0123XYZ", "CDM", team_id="T01", note="wired by <you>")
```

Per event:
```python
proj = db.project_for_channel(event["channel"]) or DEFAULT_PROJECT
answer = agent.ask(text, project_id=proj, role="QE")
```

### Agent core

Nothing to change. `agent.ask()` already propagates `context` to every tool
call. Just make sure new tools you add read `context` when appropriate:

```python
if name == "your_tool":
    return your_tool(args["arg"], project_id=context.get("project_id"))
```

### Execution engine (test runner)

When creating a TestRun / Bug node, please stamp `project_id` in `props_json`
so it aligns with multi-tenant scoping:

```python
db.add_node("TestRun", ref="RUN-XXX-1",
            props={"status":"pass", "project_id":"CDM", ...})
```

(Or extend `db.add_node()` to take `project_id` as a kwarg — TODO.)

## ✅ Verified end-to-end

- 3 ingestion pipelines chạy với sample data thật (`.md` / `.xlsx` / `.doc`)
- Idempotent: chạy lại nhiều lần không tạo duplicate
- Backward compat: gọi tool không context → hành xử y như cũ
- Multi-tenant guard: wrong `project_id` → `decision=NOT_FOUND`, không leak data
- Cross-project blast: `bug_blast_radius` vẫn count qua project boundary

## 📝 Non-goals (future work)

- Auto-link `coveredBy` (AC ↔ TestCase) từ ingestion — hiện phải thủ công / LLM tune
- `_meta.review_status='verified'` strict mode cho `go_no_go`
- `promotion_queue` workflow (Layer C: Slack thread feedback → KB rule)
- Tier 3 per-user memory
- `db.add_node()` / `db.add_edge()` chưa nhận `project_id` kwarg trực tiếp
  (workaround: pass qua `props`)
