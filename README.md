# Tieu Kiwi 🥝

**Tieu Kiwi** is an AI Quality-Engineering (QE) support agent that lives in Slack and runs on the
Anthropic Claude API. You @mention it (or use `/tieukiwi`) with a question about a ticket, and it
answers by reasoning over **two knowledge stores**:

- a **Postgres knowledge graph** — the *structured truth*: Requirements, Acceptance Criteria, Test
  Cases, Test Runs and Bugs, plus the relationships between them (`nodes` / `edges`); and
- a **Chroma vector store** — *semantic recall* of team rules, PRD/BRD text, glossaries and
  templates, retrieved via RAG.

From those it fetches Jira tickets into the graph, drafts test cases, flags requirement
ambiguities for the PO, detects coverage gaps, and makes a **GO / NO-GO** release call — routing
each result to the right owner (Delivery Manager / QE Lead / PO / Dev).

> **Open [`architecture.html`](architecture.html) in a browser** for the visual diagram + a card
> per demo function.

Guiding principle of the project: **each new capability = one more tool on the same agent loop —
never a rewrite.**

---

## Why two stores?

The two stores answer two different *kinds* of question, and Tieu Kiwi picks the right one per task:

| Question type | Example | Store | Tool(s) |
|---|---|---|---|
| **Structural / traceability** | "Is `CDM-268` ready to ship?", "Which ACs have no tests?", "Trace this requirement." | **Postgres graph** | `go_no_go`, `coverage_gap`, `trace`, `bug_blast_radius`, `classify_bug` |
| **Semantic / "what does the doc/rule say?"** | "What's our test-case naming rule?", "What does the PRD say about rollout?" | **Chroma RAG** | `search_kb`, and rule/template lookups inside `find_ambiguities` / `gen_testcase` |

The graph gives *exact, joinable* facts (an AC either has a covering TestCase or it doesn't). RAG
gives *fuzzy recall* over prose where an exact key doesn't exist. `tieukiwi/rag.py` isolates the
vector backend behind one module — it uses a **local `all-MiniLM-L6-v2` ONNX embedding (no API key,
offline)** and the collection `"knowledge_base"`, so swapping embeddings never touches tool code.

---

## Architecture

Tieu Kiwi is built agent-first, in three layers. **All three run today.**

| Layer | What it is | Where |
|---|---|---|
| **A — Agent core** | Claude tool-use loop + the graph/RAG tools. Callable from the CLI with no Slack. | `agent.py`, `tools.py`, `db.py`, `rag.py`, `routing.py` |
| **B — Slack wrapper** | Socket-Mode Slack app: `/tieukiwi` command + `@mention` + in-thread replies; acks < 3 s, dedups events. Only calls `agent.ask(...)` / the tools — it never changes the loop. | `slack_app.py` |
| **C — Loop & learning** | Curator feedback loop: candidate rules → curator approval → **promotion** into the KB; go-live sign-off; test-case Approve/Refine; Tier-2 thread memory. | `slack_app.py`, `db.py`, `memory.py` |

```
User in Slack
   │  /tieukiwi  ·  @Tieu Kiwi …  ·  thread reply
   ▼
Layer B — Slack wrapper (Socket Mode, slack_app.py)
   │  resolves channel → project_id, routes intent, ack < 3s
   ▼
Layer A — Agent core (agent.py: Claude tool-use loop)  ◀──▶  Anthropic Claude API
   │  run_tool(name, args, context)
   ├────────────▶ Postgres knowledge graph   (structural truth)  ── db.py
   ├────────────▶ Chroma RAG "knowledge_base" (semantic recall)  ── rag.py
   └────────────▶ Jira REST v3 / Confluence   (fetch_jira → upsert Requirement)
   ▲
Layer C — feedback loop: candidate rule → curator Approve → kb_rules + Chroma (promotion)
          go-live sign-off · testcase Approve/Refine · thread memory (memory.py)
```

**Agent loop (Layer A) in brief** (`agent.ask`): send the user message + the `TOOLS` schema to
Claude (`claude-sonnet-4-6`) with a strict *anti-hallucination* system prompt; while Claude returns
`tool_use`, dispatch each call through `run_tool(name, args, context)` and feed results back;
return the final text. `context = {project_id, role}` is injected by the caller (the Slack layer) —
the LLM cannot spoof it.

---

## Data model (knowledge graph)

Stored **relationally** in Postgres — `nodes(id, type, ref, project_id, props_json)` and
`edges(id, src_id, rel, dst_id, props_json)` — and queried with plain parameterized SQL / recursive
CTEs (no Neo4j). Ontology:

```
Requirement          ─has─────────▶ AcceptanceCriterion
AcceptanceCriterion  ─coveredBy───▶ TestCase
TestCase             ─executedBy──▶ TestRun
TestRun              ─finds───────▶ Bug
Bug                  ─violates────▶ AcceptanceCriterion
Bug                  ─affects─────▶ Component
Requirement          ─impacts─────▶ Component        (may be cross-project)
```

- **`project_id`** is the multi-tenant scope, defined as the **Jira key prefix** (`CDM-268` → `CDM`;
  see `db.project_id_from_ref`). Nodes carry it; edges do **not** (cross-project edges are
  first-class, filtered at query time via the endpoints' `project_id`).
- LLM-generated nodes embed `props_json._meta` provenance (`extraction_source`, `confidence`,
  `review_status`) per the `_meta` contract in `CLAUDE.md`.

> ⚠️ **Known gap:** `fetch_jira` upserts the Requirement node **without** setting `project_id`
> (it lands `NULL`). `project_id_from_ref` *is* applied when `save_testcases` writes TestCase nodes.
> The deterministic Slack shortcuts still scope correctly because they pass the channel's
> `project_id` explicitly.

An AcceptanceCriterion with **no** `coveredBy` edge is a **coverage gap**. `go_no_go` combines
coverage gaps + failing `TestRun`s + open critical/high `Bug`s into a single decision.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.11+** | Developed/tested on CPython 3.11. |
| **Docker + Docker Compose** | Local Postgres (`docker-compose.yml`). |
| **Anthropic API key** | Required — the agent won't start without it. <https://console.anthropic.com>. |
| **Jira account + API token** | *Optional* — enables `fetch_jira` (and Confluence PRD expansion). |
| **Slack tokens** | *Optional* — only to run the Slack app; the CLI works without them. |

> **No embedding API key needed.** RAG embeds locally with `all-MiniLM-L6-v2` (downloaded once,
> ~80 MB, cached). The `VOYAGEAI_API_KEY` line in `.env.example` is vestigial and is **not** read
> by the code — leave it blank.

---

## Setup

```bash
# 1. Clone + venv
git clone https://github.com/minhphuEP/tieu-kiwi.git
cd tieu-kiwi
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # anthropic, chromadb, psycopg[binary], python-dotenv, httpx, slack_bolt, openpyxl

# 2. Config
cp .env.example .env                      # then fill ANTHROPIC_API_KEY + DATABASE_URL (Jira/Slack optional)

# 3. Postgres + schema + migrations (order matters)
docker compose up -d
for f in db/schema.sql db/002_migration.sql db/003_migration.sql \
         db/004_migration.sql db/005_migration.sql db/006_migration.sql; do
  docker compose exec -T postgres psql -U tieukiwi_app -d tieukiwi < "$f"
done

# 4. Seed (order matters)
python scripts/seed/users_real.py         # users table — the 4 real routing targets (DM/QE Lead/PO/Dev, project CDM)
python scripts/seed/kb.py                 # Tier-1 KB → Chroma (indexes skills/ + kb/)
python scripts/seed/graph.py              # sample requirement graph (dev fixture; or scripts/seed/cdm_demo.py)
```

`.env` essentials:

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
DATABASE_URL=postgresql://tieukiwi_app:devpass@localhost:5432/tieukiwi
ANTHROPIC_MODEL=claude-sonnet-4-6         # default agent model

# Jira (optional — for fetch_jira / Confluence PRD expansion)
JIRA_BASE_URL=            # https://your-org.atlassian.net
JIRA_EMAIL=
JIRA_API_TOKEN=

# Slack (optional — Layer B/C, Socket Mode)
SLACK_BOT_TOKEN=          # xoxb-...
SLACK_APP_TOKEN=          # xapp-...
SLACK_SIGNING_SECRET=
```

> **Reproducible role routing.** `scripts/seed/users_real.py` is the *only* script that writes the
> `users` table — it wipes leftovers and inserts the 4 real demo users (`delivery_manager` /
> `qe_lead` / `po` / `dev`, project `CDM`). Role → Slack user is resolved by
> `db.resolve_role_slack_id` / `db.mention_for` (users table → `ROLE_<ROLE>` env override →
> a non-crashing `@role (unconfigured)` label). Never hardcode Slack ids elsewhere.

### Run it

```bash
# CLI (Layer A only — needs ANTHROPIC_API_KEY + DATABASE_URL)
python -m tieukiwi.cli

# Slack app (Layer B/C — needs the Slack tokens too)
python -m tieukiwi.slack_app

# Wire a Slack channel to a project (so tool calls scope correctly)
python -c "from tieukiwi import db; db.bind_channel('C0123XYZ', 'CDM', note='wired by me')"

# Smoke-check wiring without spending API calls
python -c "import tieukiwi.tools, tieukiwi.db, tieukiwi.rag, tieukiwi.routing; print('OK')"
```

---

## Demo functions

Each row is a real, demoable capability. Commands are shown as `@Tieu Kiwi …`; the same intents
work via `/tieukiwi …`. See [`architecture.html`](architecture.html) for a card per function.

| # | Say in Slack | What happens | Store / tool | Result |
|---|---|---|---|---|
| 1 | `@Tieu Kiwi fetch CDM-268` | Agent loop calls `fetch_jira`; reads the Jira issue and **upserts a Requirement node** (summary, status, issuetype, priority, assignee/reporter, story points, description). **ACs are seeded separately — not parsed from Jira.** | Graph ← Jira REST v3 (`fetch_jira`) | Requirement node with ticket metadata |
| 2 | `@Tieu Kiwi CDM-268 đã đủ điều kiện go live chưa?` | Deterministic `go_no_go` over the graph. **GO** only if no coverage gaps **and** no failing tests **and** no open critical/high bug. On **GO** → @mentions the **Delivery Manager** with **Approve / Reject** sign-off buttons (recorded in `go_live_decisions`). On **NO-GO** → coverage %, failing tests, open bugs + `next_actions`. | **Graph** (`go_no_go`) | GO/NO-GO decision + owner routing |
| 3 | `@Tieu Kiwi generate testcase cho CDM-268` (also *"write testcase"*) | `testcase_gen` drafts test cases covering **every AC of the requirement** (not a single AC), exports an **Excel file (Testomat.io format)**, posts a draft + **Approve / Refine** buttons and @mentions the **QE Lead**. **Approve** → saves `TestCase` nodes + `coveredBy` edges (with `_meta` provenance). **Refine** → LLM re-draft from your comment (v+1). | Graph write + RAG (template/rubric) | Excel draft → approved TestCase nodes |
| 4 | `@Tieu Kiwi CDM-268 cần PO chốt: ...` | `find_ambiguities` reads the requirement (fetching Jira + linked Confluence PRD if needed), flags genuine ambiguities against **3 dimensions** (Behaviour & Edge Cases, Constraints, Conflicts), @mentions the **PO** and offers **"Open clarification form."** Submitted answers are written back to the Requirement node (`clarified_requirements`). | RAG rubric + LLM | PO questions → answers stored on the requirement |
| 5 | `@Tieu Kiwi curator-test` · `@Tieu Kiwi học rule mới: <rule>` | Enqueues a **candidate rule** in `promotion_queue` and @mentions the **QE Lead** with **Approve / Edit / Reject**. **Approve = promotion**: the rule is inserted into `kb_rules` (status `active`) **and indexed into Chroma**, so `search_kb` can retrieve it from then on. | Postgres queue → **Chroma** (promotion) | New team rule, retrievable via RAG |
| 6 | `@Tieu Kiwi CDM-268 có bug/test nào đang fail không?` | Agent answers the bug/failing-test question, then the Slack layer **@mentions the Dev owner**. (The `classify_bug` / `bug_blast_radius` tools also exist for deeper triage in free-form Q&A.) | Graph (`trace` / agent) + routing | Answer + Dev @mention |
| — | *(underpins 2–6)* | **Role routing.** A role is mapped to a real person via the `users` table: `routing.approver_role_for(...)` → `db.resolve_role_slack_id` / `db.mention_for`. 4 roles: **Delivery Manager, QE Lead, PO, Dev.** | `users` table | `<@Uxxx>` mention (or graceful fallback) |

> **"Promotion"** = moving a candidate rule from the human-in-the-loop `promotion_queue` into the
> live knowledge base: a row in `kb_rules` (system of record) **plus** a Chroma document (so it
> becomes searchable). Rejected candidates are marked `rejected` and never reach `kb_rules`.

### Verify without Slack

```bash
python -c "from tieukiwi.db import go_no_go; print(go_no_go('CDM-268'))"
python -c "from tieukiwi.tools import fetch_jira; print(fetch_jira('CDM-268'))"
```

---

## Agent tools (full list)

The agent decides which of these to call each turn:

| Tool | Store | Purpose |
|---|---|---|
| `search_kb` | Chroma | Semantic KB search (rules / glossary / templates / rubrics). |
| `coverage_gap` | Graph | ACs with no covering TestCase. |
| `trace` | Graph | Requirement → AC → TestCase → TestRun → Bug, with pass/fail. |
| `go_no_go` | Graph | Aggregate GO / NO-GO + `next_actions`. |
| `bug_blast_radius` | Graph | Requirements/ACs impacted by a bug's component → P1–P4. |
| `classify_bug` | Graph | How a bug was detected → which pipeline to improve (`caught_by_test` / `leaked_*`). |
| `find_ambiguities` | RAG + LLM | PO clarification questions from a requirement (3 dimensions). |
| `gen_testcase` | Graph + RAG | Draft/update test cases for a requirement (returns a draft; the interactive Approve/Refine loop is driven from Slack). |
| `fetch_jira` | Jira → Graph | Read a Jira issue and upsert it as a Requirement node. |
| `gen_test_plan` | — | **Skeleton (not implemented yet).** |

---

## 3-tier memory

- **Tier 1 — Team KB (RAG):** Chroma `knowledge_base`, seeded from `skills/` + `kb/` and grown by
  curator promotion. `tieukiwi/rag.py`.
- **Tier 2 — Thread/artifact memory:** per-thread state keyed by `channel_id + thread_ts` in the
  `thread_state` table — holds in-flight test-case drafts, the thread's ticket, and bot
  participation. `tieukiwi/memory.py`. (Test-case drafts live here; there is no separate
  `testcase_drafts` table.)
- **Tier 3 — Per-user memory:** preferences/role/style keyed by `user_id`. **TODO / later.**

---

## Project structure

```
tieu-kiwi/
├── docker-compose.yml           # local Postgres (postgres:16)
├── .env.example                 # copy → .env
├── db/
│   ├── schema.sql               # nodes, edges, kb_rules, promotion_queue, thread_state
│   ├── 002_migration.sql        # + nodes.project_id, users, indexes
│   ├── 003_migration.sql        # + partial unique (project_id, ref)
│   ├── 004_migration.sql        # + channel_project_map
│   ├── 005_migration.sql        # reshape promotion_queue for the curator flow
│   └── 006_migration.sql        # + go_live_decisions
├── kb/                          # RAG docs (path-based metadata)
├── skills/                      # QE rubrics indexed into RAG
├── scripts/
│   ├── seed/                    # users_real.py, kb.py, graph.py, cdm_demo.py, reset.py
│   └── ingest/                  # requirements.py, testcases.py, bugs.py
├── docs/                        # STORAGE_GUIDE, KB_GUIDE, ontology, Gen-testcase-design, ROADMAP…
├── architecture.html           # ← standalone visual architecture (open in a browser)
└── tieukiwi/
    ├── config.py                # loads .env; model/JIRA/SLACK settings
    ├── agent.py                 # Claude tool-use loop (Layer A)
    ├── tools.py                 # TOOLS + run_tool (incl. fetch_jira / Confluence expand)
    ├── db.py                    # Postgres graph + tools + role/curator/go-live helpers
    ├── rag.py                   # Chroma (local all-MiniLM-L6-v2 embedding)
    ├── routing.py               # role mapping (approver_role_for / route_gap / curator_role_for)
    ├── memory.py                # 3-tier memory (Tier 2 = thread_state)
    ├── testcase_gen.py          # LLM draft/refine/finalize test cases
    ├── testcase_export.py       # Excel export (Testomat.io format)
    ├── slack_app.py             # Layer B/C: Socket-Mode app, buttons, curator/go-live/gen flows
    └── cli.py                   # CLI entry point
```

---

## Conventions (from `CLAUDE.md`)

- **Don't change the architecture or the agent loop** unless asked. New capability = one `TOOLS`
  entry + one `run_tool` branch.
- **Never edit `db/schema.sql`** — add a numbered idempotent `db/NNN_migration.sql`.
- **Always parameterize SQL.** Secrets live only in `.env`.
- Agent model string: `claude-sonnet-4-6`. Chroma: collection `knowledge_base`, path `./chroma_db`.
  Run seed & CLI from the repo root.
- LLM-ingested nodes must embed `_meta` provenance in `props_json`.
- Role→person resolution is ONE path (`db.resolve_role_slack_id` / `db.mention_for`); role
  constants live only in `tieukiwi/routing.py`.
