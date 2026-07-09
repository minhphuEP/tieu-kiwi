# CLAUDE.md — Tieu Kiwi

> Context file for Claude Code. Read this before modifying any code in the repo.
> Goal: keep every change aligned with the project architecture; do not change the design on your own.

## What the project is

Tieu Kiwi is an AI agent that supports QE (Quality Engineering). It reads requirements/design,
generates test cases & test plans, critiques PRD/Design, detects test-coverage gaps, and decides
whether a feature is ready to go live. It runs on the Anthropic Claude API.

Guiding principle: **each new task = add one tool to the same agent loop, never rewrite from scratch.**

## Architecture (target wireframe)

```
Connected apps:   Slack (/tieukiwi, results)   Jira (requirements, tickets)   GitLab (repo/MR, read_api)
                              |                        |                          |
                              +------------+-----------+--------------------------+
                                           v
Brain:                       Anthropic Claude - Agent core (tool-use loop, Layer A)
                                           |
QE tools:      gen testcase/test-plan  |  critic PRD/Design  |  coverage_gap/trace  |  go/no-go
                                           |
Data layer:        RAG - Chroma (glossary, rules, skills, templates, samples)
                   Knowledge graph - Postgres nodes/edges (Requirement->AC->TestCase->TestRun->Bug)
                                           |
Feedback loop:     Layer C - read replies in thread -> curator approves -> promote into KB
```

## Three layers (build agent-first: A -> B -> C)

- **Layer A - Agent core (runs on CLI, no Slack yet):** LLM tool-use loop + tools (fetch
  Jira/Confluence, search/write KB, gen testcase, critic, test-plan) + RAG. Tested via CLI + small eval.
- **Layer B - Slack wrapper:** Slack app in Socket Mode, scopes, `/tieukiwi` command, Bolt skeleton
  (ack <3s, dedup events), handler -> call Layer A agent -> return Block Kit.
- **Layer C - Loop & learning:** read replies in thread -> iterate; KB promotion + curator approval button.

## Data model & 3-tier memory

- **Tier 1 - Team shared KB (RAG):** review standards, conventions, glossary, agreed rules.
  This is the "gets better the more it runs" part.
- **Tier 2 - Thread/artifact memory:** context of each review, who accepted/rejected, final decision.
  Key: channel_id + thread_ts. Where the feedback loop lives.
- **Tier 3 - Per-user memory:** preferences/role/style. Key: user_id. Optional, do later.

## Ontology (knowledge graph)

Store the graph relationally in Postgres (do NOT use Neo4j). Query with SQL / recursive CTE.

```
nodes(id, type, ref, project_id, props_json, created_at)   -- project_id added by migration 002
edges(id, src_id, rel, dst_id, props_json)                 -- NO project_id: cross-project edges allowed

Sprint              -has->         UserStory
UserStory           -has->         Requirement
Requirement         -has->         AcceptanceCriterion
AcceptanceCriterion -coveredBy->   TestCase
TestCase            -inPlan->      TestPlan
TestCase            -executedBy->  TestRun
TestRun             -finds->       Bug
Bug                 -affects->     Component
Bug                 -violates->    AcceptanceCriterion
Requirement         -impacts->     Component            -- may be cross-project
Component           -dependsOn->   Component            -- may be cross-project
Feedback            -about->       Bug | Requirement | TestCase | AcceptanceCriterion
ReviewRule (KB)     -appliesTo->   <EntityType>
```

**Cross-project edges** are first-class (see `docs/db_schema.md`). Filter by project
at query time via `nodes.project_id` of src/dst.

**Feedback nodes** are candidate rules from Slack threads. They live in the graph
until a curator promotes them into the KB via `promotion_queue`.

## `props_json._meta` provenance (LLM-generated nodes)

Every node inserted by an ingestion pipeline that used an LLM to extract structure
MUST embed a `_meta` sub-object in `props_json`:

```json
{
  "detail": "...",
  "_meta": {
    "extraction_source": "llm:claude-sonnet-4-6",
    "confidence": 0.87,
    "source_file": "requirements/BRD-login.pdf",
    "source_chunk": 12,
    "review_status": "draft"
  }
}
```

Human-authored / excel-imported nodes: `_meta.extraction_source = "human" | "excel-import"`,
`confidence = 1.0`. Absent `_meta` = human by default.

`review_status` values: `draft` (unreviewed LLM output) → `verified` (human OK) → `rejected`.
Downstream tools that make decisions (`go_no_go`) should consider only `verified` nodes when
strict mode is on. In current MVP everything is treated equal; strict mode is future work.

Graph tools the agent uses:
- `coverage_gap(requirement)` -> ACs with no TestCase (= coverage gap).
- `trace(requirement)` -> path Requirement->AC->TestCase->TestRun->Bug (tested & passing or not).
- `bug_blast_radius(bug)` -> number of ACs/Requirements depending on the affected Component -> bug priority.
- `go_no_go(requirement)` -> aggregate -> GO/NO-GO decision + next_actions.

## Tech stack

- **LLM:** Anthropic Python SDK (`anthropic`), model `claude-sonnet-4-6` / Opus. Embeddings for RAG.
- **Vector store:** Chroma (PersistentClient, path `./chroma_db`), collection `"knowledge_base"`.
- **DB:** Postgres (nodes/edges/kb_rules/promotion_queue/thread_state). Docker locally during dev.
- **Slack:** `slack_bolt` (Socket Mode). Scopes: app_mentions:read, chat:write, commands,
  channels:history, groups:history, im:history, files:read, users:read.
- **Integrations:** `httpx` for Jira/Confluence REST (Basic auth: email + API token),
  GitLab (PAT/Project token, scope read_api).

## Directory layout

```
tieu-kiwi/
|-- .env.example / .env (.env is NOT committed)
|-- docker-compose.yml           # local Postgres
|-- db/
|   |-- schema.sql               # nodes, edges, kb_rules, promotion_queue, thread_state
|   |-- 002_migration.sql        # + nodes.project_id, users, indexes
|   |-- 003_migration.sql        # + unique index for idempotent upsert
|   `-- 004_migration.sql        # + channel_project_map (Slack -> project)
|-- kb/                          # RAG docs — path-based metadata (see seed.py):
|   |-- <PROJECT_ID>/**          #   project-scoped: scope=project, project_id=<..>
|   `-- _global/<ROLE>/[templates|samples]/**  # global + role + doc_type
|-- skills/                      # rubrics (SKILL.md borrowed from agent-skills)
|-- data_ingestion/              # drop-zone for source docs (BRD, testcases, bugs)
|   |-- requirements/            # .md/.pdf/.docx/.txt -> scripts/ingest/requirements.py
|   |-- testcases/               # .xlsx/.csv          -> scripts/ingest/testcases.py
|   `-- bugs/                    # .json/.doc/.docx/.pdf -> scripts/ingest/bugs.py
|-- scripts/
|   |-- seed/
|   |   |-- kb.py                # index skills/ + kb/ into Chroma (was seed.py)
|   |   |-- users.py             # seed users directory (routing target)
|   |   |-- graph.py             # sample graph data (dev fixture)
|   |   `-- reset.py             # DELETE nodes/edges/users (dev only)
|   `-- ingest/
|       |-- requirements.py      # BRD -> Requirement + AC nodes (LLM extract)
|       |-- testcases.py         # Excel/CSV -> TestCase nodes (structured parse)
|       `-- bugs.py              # Jira -> Bug nodes (LLM extract)
`-- tieukiwi/
    |-- config.py                # reads .env (DATABASE_URL, ANTHROPIC_API_KEY, LLM_PROVIDER, ...)
    |-- db.py                    # Postgres connection + graph tools
    |-- rag.py                   # Chroma: index_docs, search, wipe
    |-- llm.py                   # LLM abstraction for ingestion (anthropic|ollama switch)
    |-- memory.py                # 3-tier memory
    |-- routing.py               # ask routing: owner_for + resolve_owner_slack (fallback)
    |-- tools.py                 # TOOLS definitions + run_tool
    |-- agent.py                 # tool-use loop
    `-- cli.py                   # CLI entry point
```

## Ingestion workflow

**Bắt đầu ở đây**: [`docs/STORAGE_GUIDE.md`](docs/STORAGE_GUIDE.md) — hướng dẫn
đầy đủ cho team về **cách lưu dữ liệu vào Tiểu Kiwi** (Postgres artifacts vs
Chroma knowledge) + concrete commands cho mọi tình huống (requirement mới,
testcase legacy, bug, rule, glossary, template, lesson).

Chi tiết chuyên sâu:
- [`docs/KB_GUIDE.md`](docs/KB_GUIDE.md) — Chroma folder convention
- [`data_ingestion/README.md`](data_ingestion/README.md) — Postgres ingest specs

Quick start (đầy đủ chi tiết trong STORAGE_GUIDE):
```bash
docker compose up -d
for f in db/schema.sql db/002_migration.sql db/003_migration.sql db/004_migration.sql; do
  docker exec -i tieu-kiwi-postgres-1 psql -U tieukiwi_app -d tieukiwi < "$f"
done
python scripts/seed/users.py       # routing target (Postgres)
python scripts/seed/kb.py          # Tier 1 knowledge (Chroma) — from kb/ + skills/
```

All ingest scripts are idempotent (ON CONFLICT DO UPDATE via the partial unique
index from migration 003). Re-running against the same file will not create
duplicate nodes.

## Multi-tenant scoping (Slack channel -> project)

Each Slack channel handles ONE project's questions. The Slack layer resolves
`channel_id -> project_id` via the `channel_project_map` table (migration 004),
then passes `project_id` into every agent call:

```python
# In the Slack Bolt handler (Layer B):
proj = db.project_for_channel(event["channel"]) or DEFAULT_PROJECT
answer = agent.ask(text, project_id=proj, role="QE")
```

`agent.ask()` bundles `{project_id, role}` into a `context` dict that
`run_tool(name, args, context)` propagates to every tool:

- **Postgres queries** (`coverage_gap`, `trace`, `go_no_go`, `bug_blast_radius`)
  each take an optional `project_id` kwarg. When set, the ENTRY entity
  (Requirement / Bug) must belong to that project; downstream traversal follows
  edges naturally, so cross-project impact is still counted.
- **RAG search** (`rag.search`) filters Chroma by `project_id` (with
  `include_global=True` for shared docs) and by `role` for persona isolation.

Backward compat: every scoping param is optional (default `None` = no filter).
Calling `db.coverage_gap()` with no args still works as before.

To wire up a new channel:

```python
db.bind_channel("C0123XYZ", "CDM_TEAM", note="wired by <you>")
```

## Working conventions (IMPORTANT)

- **Do not change the architecture** described above unless explicitly asked.
- **Do not change signatures of functions already in use** (e.g. `coverage_gap()`) to avoid breaking other code.
- **Schema evolution**: never edit `db/schema.sql`. Add a new file `db/NNN_migration.sql` (idempotent, using `IF NOT EXISTS`). Migration 002 added `nodes.project_id` and `users`.
- **Always parameterize SQL**, never concatenate strings (avoid injection).
- **Secrets live only in .env**, never committed, never hardcode keys/tokens/passwords.
- Use the model string `claude-sonnet-4-6` for the agent loop.
- Chroma: collection `"knowledge_base"`, path `"./chroma_db"`. Always run seed & CLI from the repo root.
- When adding a new tool: add an entry to `TOOLS` (clear input_schema + description) and a branch in
  `run_tool`; do NOT rewrite the agent loop.
- When ingesting via LLM: always embed `_meta` provenance in `props_json` (see convention above).
- Ask-routing: two APIs coexist. `routing.owner_for(entity_type)` = class-level role name (legacy),
  `routing.resolve_owner_slack(node_id)` = instance-level Slack user with 3-tier fallback (new).
  Prefer the second for anything that will actually @mention someone.
- Test with `python -c "import tieukiwi.<module>"` instead of running `python -m tieukiwi.cli`
  (the CLI needs a real API key + Postgres).

## Current state vs target

Already present: config, db (coverage_gap), rag, agent loop, cli, basic tools (search_kb,
coverage_gap), Postgres via Docker, RAG indexing of skills.

Still to add for a complete wireframe: gen_test_plan / gen_critic; memory.py (tiers 2, 3);
routing.py; kb/templates + kb/samples. Parts belonging to later deadlines may be left as
skeleton + TODO.

Present but deprecated for LLM use: `fetch_jira` (tools.py) — function kept for backward
compat with legacy scripts, but hidden from TOOLS[] to force LLM through
`ingest_jira_ticket` (full pull) or `get_ticket` (read cache).
