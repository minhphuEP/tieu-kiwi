# Tieu Kiwi 🥝

**Tieu Kiwi** is an AI agent for **Quality Engineering (QE)**, built on the Anthropic Claude API.
It reads requirements and design docs, generates test cases and test plans, critiques PRDs,
detects test‑coverage gaps, and decides whether a feature is ready to go live — by reasoning over
a **Postgres knowledge graph** (Requirement → AcceptanceCriterion → TestCase → TestRun → Bug) and a
**Chroma RAG** knowledge base of team rules, glossaries, and skills.

The guiding principle of the project: **each new capability = one more tool on the same agent
loop — never a rewrite.**

---

## ⚠️ Current status

Tieu Kiwi is built agent‑first, in three layers. **Layers A and B run today**:

| Layer | Scope | Status |
|-------|-------|--------|
| **A — Agent core** | CLI tool‑use loop + graph/RAG tools (Claude API) | ✅ Working |
| **B — Slack wrapper** | Slack app (Socket Mode), `/tieukiwi` command, Bolt handler | ✅ Working |
| **C — Loop & learning** | Thread feedback loop, KB promotion, curator approval | 🚧 Planned |

Live tools: `search_kb`, `coverage_gap`, `trace`, `bug_blast_radius`, `go_no_go`, and
**`fetch_jira`** (reads a Jira issue and writes it into the graph). The content‑generation tools
`gen_testcase`, `gen_test_plan` are still **skeletons with clear TODOs**. The
remaining feedback/learning loop (Layer C) is on the roadmap — see
[`docs/ROADMAP.md`](docs/ROADMAP.md). This README marks planned pieces explicitly so you always
know what actually works.

---

## Features

- 🤖 **Claude‑powered reasoning** — an agentic tool‑use loop on `claude-sonnet-4-6` (Anthropic Python SDK).
- 🧠 **Postgres knowledge graph** — Requirements, Acceptance Criteria, Test Cases, Test Runs, Bugs and Components stored as `nodes`/`edges`, queried with plain parameterized SQL (no Neo4j).
- 📚 **Chroma RAG** — team rules, glossary, and QE skills indexed into a local Chroma vector store for retrieval.
- ✅ **QE decision tools** — coverage‑gap detection, requirement tracing, bug blast‑radius, and an aggregate **GO / NO‑GO** call with concrete next actions.
- 🔌 **Jira integration** — the `fetch_jira` tool reads a Jira Cloud issue (REST v3) and upserts it into the graph as a `Requirement` node.
- 💬 **Slack integration** — Socket‑Mode app with a `/tieukiwi` slash command (acks <3s, dedups, replies in Block Kit).

---

## Architecture

Users reach the agent via the **CLI** or the **Slack `/tieukiwi` command**; the agent reads Jira
through the `fetch_jira` tool.

```mermaid
flowchart TD
    user([User])
    cli[CLI  python -m tieukiwi.cli]
    slack[/Slack  /tieukiwi<br/>slack_app.py/]

    user --> cli
    user --> slack
    slack --> agent

    cli --> agent

    subgraph brain [Agent core - Layer A]
      agent[Claude tool-use loop<br/>agent.py]
      tools[TOOLS + run_tool<br/>tools.py]
      agent <--> tools
    end

    agent <--> claude[[Anthropic Claude API]]

    tools --> rag[(Chroma RAG<br/>./chroma_db)]
    tools --> pg[(Postgres<br/>knowledge graph)]
    tools --> jira[[Jira REST v3]]
```

Plain‑text view:

```
User ─▶ CLI ───────▶ Agent loop (Claude) ─▶ tools ─┬─▶ Chroma RAG  (rules/glossary/skills)
     ▶ Slack /tieukiwi ▲                            ├─▶ Postgres    (Requirement→AC→TestCase→TestRun→Bug)
       (slack_app.py) ─┘                            └─▶ Jira REST   (fetch_jira → upsert Requirement)
```

**Knowledge‑graph ontology** (stored relationally in `nodes`/`edges`):

```
Requirement          -has->        AcceptanceCriterion
AcceptanceCriterion  -coveredBy->  TestCase
TestCase             -executedBy-> TestRun
TestRun              -finds->      Bug
Bug                  -affects->    Component
Bug                  -violates->   AcceptanceCriterion
Requirement          -impacts->    Component
```

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| **Python 3.11+** | Developed and tested on CPython 3.11. |
| **Docker + Docker Compose** | For the local Postgres instance (`docker-compose.yml`). |
| **Anthropic API key** | Required — the agent won't start without it. Get one at <https://console.anthropic.com>. |
| **Jira account + API token** | *Optional* — enables the `fetch_jira` tool. Token: <https://id.atlassian.com/manage-profile/security/api-tokens>. |
| **Slack tokens** (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET`) | *Optional* — needed only to run the Slack app (`python -m tieukiwi.slack_app`); the CLI works without them. |

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/minhphuEP/tieu-kiwi.git
cd tieu-kiwi

# 2. Create and activate a virtual environment
python3.11 -m venv .venv
source .venv/bin/activate          # macOS/Linux
# .venv\Scripts\activate           # Windows (PowerShell)

# 3. Install dependencies
pip install -r requirements.txt
```

Dependencies (`requirements.txt`): `anthropic`, `chromadb`, `psycopg[binary]`, `python-dotenv`,
`httpx`, `slack_bolt`.

> ℹ️ On first RAG use, Chroma downloads a small embedding model (`all-MiniLM-L6-v2`, ~80 MB) into
> a local cache — this is a one‑time download.

---

## Environment Configuration

All configuration is read from a `.env` file at the repo root (loaded centrally by
`tieukiwi/config.py`). Copy the template and fill it in:

```bash
cp .env.example .env
```

`.env.example`:

```dotenv
# ── Core (required to run the agent today) ───────────────────────────────
# Anthropic Claude API key — powers the agent's reasoning / tool-use loop.
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx

# Postgres connection string for the knowledge graph.
# Matches the credentials in docker-compose.yml for local dev.
DATABASE_URL=postgresql://tieukiwi_app:devpass@localhost:5432/tieukiwi

# ── Jira — consumed by the fetch_jira tool ───────────────────────────────
JIRA_BASE_URL=          # e.g. https://your-org.atlassian.net
JIRA_EMAIL=             # Jira account email (Basic-auth username)
JIRA_API_TOKEN=         # Jira API token

# ── Slack (Layer B, Socket Mode) — run: python -m tieukiwi.slack_app ──────
SLACK_BOT_TOKEN=        # Bot User OAuth token (xoxb-...)
SLACK_APP_TOKEN=        # App-level token for Socket Mode (xapp-...)
SLACK_SIGNING_SECRET=   # Signing secret (app Basic Information page)
```

| Variable | Used by | Required? |
|----------|---------|-----------|
| `ANTHROPIC_API_KEY` | `agent.py` (Claude client), `config.py` | ✅ Yes |
| `DATABASE_URL` | `config.py` → `db.py` (all graph tools) | ✅ Yes (for graph tools) |
| `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | `fetch_jira` tool | ⚙️ For Jira |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET` | `slack_app.py` (Layer B) | ⚙️ For Slack |

> 🔒 `.env` is git‑ignored. Never commit real keys. Jira vars are only needed for `fetch_jira`, and
> the Slack vars only to run the Slack app — the CLI runs with just `ANTHROPIC_API_KEY` + `DATABASE_URL`.

---

## Running Postgres with Docker Compose

The repo ships a minimal Postgres service (`docker-compose.yml`):

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: tieukiwi
      POSTGRES_USER: tieukiwi_app
      POSTGRES_PASSWORD: devpass
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
volumes:
  pgdata:
```

**Steps:**

```bash
# 1. Start Postgres in the background
docker compose up -d

# 2. Verify it's healthy / accepting connections
docker compose exec postgres pg_isready -U tieukiwi_app -d tieukiwi

# 3. Apply the schema (creates nodes, edges, kb_rules, promotion_queue, thread_state)
psql "$DATABASE_URL" -f db/schema.sql
# No psql client? Run it through the container instead:
#   docker compose exec -T postgres psql -U tieukiwi_app -d tieukiwi < db/schema.sql
```

The connection string in `.env` (`DATABASE_URL`) must match the compose credentials above.

---

## Running the Agent Locally

With `.env` filled in, Postgres up, and the schema applied:

```bash
# (optional) load sample graph + KB so the tools have data to reason over
python3 scripts/seed/kb.py           # index skills/ + kb/ into Chroma (RAG)
python3 scripts/seed/graph.py        # insert a sample requirement graph (graph data only)

# reset the users table to EXACTLY the 4 real demo users (routing targets).
# This is the single source of truth for users; safe to re-run (idempotent).
python3 scripts/seed/users_real.py

# start the interactive agent
python3 -m tieukiwi.cli
```

> **Reproducible users / role routing.** `scripts/seed/users_real.py` is the ONLY script that
> writes the `users` table — it wipes any leftover/placeholder rows and inserts the 4 real demo
> users (`delivery_manager` / `qe_lead` / `po` / `dev`, project `CDM`). Run it on every machine to
> get identical routing. `graph.py` and `cdm_demo.py` seed graph data only and never touch `users`.

You should see:

```
Tieu Kiwi CLI — type a question (Ctrl+C to exit)

>
```

Type a question and the agent will reason and call tools as needed. To **verify wiring without
spending API calls**, import‑check the modules:

```bash
python -c "import tieukiwi.tools, tieukiwi.db, tieukiwi.memory, tieukiwi.routing; print('OK')"
python -c "from tieukiwi.db import go_no_go; print('config OK')"
```

### Running the Slack app (Layer B)

With `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, and `SLACK_SIGNING_SECRET` set in `.env` (and the app
installed with Socket Mode + the `/tieukiwi` command registered):

```bash
python -m tieukiwi.slack_app
# → "Tieu Kiwi Slack app starting (Socket Mode). Ctrl+C to stop."
```

Then in Slack: `/tieukiwi is JIRA-101 ready to go live?` — you'll get an immediate “Processing…”
ack, followed by the agent's answer as a formatted message. If tokens are missing the app exits
with a clear message listing which ones.

---

## Integrations Setup

### Jira
1. Create an API token at <https://id.atlassian.com/manage-profile/security/api-tokens>.
2. Set `JIRA_BASE_URL` (e.g. `https://your-org.atlassian.net`), `JIRA_EMAIL`, `JIRA_API_TOKEN` in `.env`.
3. The `fetch_jira` tool calls `GET /rest/api/3/issue/{key}` with HTTP Basic auth (`email:token`)
   over `httpx`. It parses key/summary/description/issuetype/status and **upserts** a `Requirement`
   node into the graph (keyed by `ref`, so re‑fetching the same issue updates rather than duplicates).
   If the Jira env vars are missing it returns a clear error instead of crashing.

### Slack (Layer B — Socket Mode)
1. Create a Slack app and **enable Socket Mode**.
2. Add bot scopes: `commands`, `chat:write` (plus `app_mentions:read`, `channels:history`,
   `groups:history`, `im:history`, `files:read`, `users:read` for future features).
3. Create the `/tieukiwi` slash command; generate an **App‑Level Token** (`connections:write`) for
   Socket Mode.
4. Put `SLACK_BOT_TOKEN` (xoxb‑), `SLACK_APP_TOKEN` (xapp‑), and `SLACK_SIGNING_SECRET` in `.env`.
5. Run `python -m tieukiwi.slack_app`. The Bolt handler acks within 3 s, dedups retries/duplicate
   invocations, calls the Layer A agent (`agent.ask`), and replies in Block Kit `mrkdwn`.

> Note: routing action items *to specific owners* via Slack (`routing.py`) is still a Layer C TODO —
> the `/tieukiwi` command itself is fully working.

---

## RAG / Knowledge Base

Tieu Kiwi uses a **3‑tier memory** model (`tieukiwi/memory.py`); the RAG layer is Tier 1.

**Chroma RAG (Tier 1 — team shared knowledge):**
- Store: local `chromadb.PersistentClient(path="./chroma_db")`, collection `knowledge_base`
  (see `tieukiwi/rag.py`).
- Ingestion: `python scripts/seed/kb.py` walks every `.md` in `skills/` and `kb/`, and indexes each file as a
  document (`id` = filename, metadata `source` + `applies_to`). The three QE rubrics in `skills/`
  (`test-driven-development`, `code-review-and-quality`, `spec-driven-development`) are tagged to
  `TestCase` / `Bug` / `Requirement` respectively.
- Retrieval: the `search_kb` tool calls `rag.search(query)` (top‑k semantic search).

**Postgres knowledge graph (`nodes`/`edges`):**
- Populate manually via `tieukiwi.db.add_node` / `add_edge`, or run `python scripts/seed/graph.py` for a
  ready‑made sample (`JIRA-101` with covered/uncovered ACs, a failing test, and an open bug).
- The agent queries it through graph tools:

| Tool | What it answers |
|------|-----------------|
| `coverage_gap` | Acceptance Criteria with no covering TestCase |
| `trace` | Full Requirement → AC → TestCase → TestRun → Bug path |
| `bug_blast_radius` | How many Requirements/ACs a bug's component impacts → priority |
| `go_no_go` | Aggregate GO/NO‑GO decision + `next_actions` |

Tables `kb_rules`, `promotion_queue`, and `thread_state` support the (planned) Layer C learning loop.

---

## Usage Examples

**Interactive CLI:**

```
$ python -m tieukiwi.cli
Tieu Kiwi CLI — type a question (Ctrl+C to exit)

> Is JIRA-101 ready to go live?
```

Behind the scenes the agent calls the `go_no_go` tool, which returns (for the seeded sample):

```json
{
  "requirement": "JIRA-101",
  "decision": "NO-GO",
  "coverage_gaps": ["AC-2"],
  "failing_tests": [{"testrun": "TR-3", "testcase": "TC-3"}],
  "open_bugs": [{"bug": "BUG-1", "severity": "high"}],
  "next_actions": [
    "Write a testcase for AC-2",
    "Fix failing testcase TC-3 (run TR-3)",
    "Close bug BUG-1 (high)"
  ]
}
```

…and Tieu Kiwi replies in natural language, e.g. *“NO‑GO. AC‑2 has no test coverage, TC‑3 is
failing, and BUG‑1 (high) is still open. Next: write a test for AC‑2, fix TC‑3, and close BUG‑1.”*

**Other prompts to try:** *“Which acceptance criteria have no tests?”* (`coverage_gap`),
*“Trace JIRA‑101”* (`trace`), *“What's the blast radius of BUG‑1?”* (`bug_blast_radius`),
*“Fetch PROJ‑123 from Jira”* (`fetch_jira`), *“Search the KB for our code‑review standards”* (`search_kb`).

**Via Slack:**

```
/tieukiwi is JIRA-101 ready to go live?
```

Slack immediately shows *“Processing…”*, then Tieu Kiwi posts the GO/NO‑GO answer back into the
channel as a formatted message.

**Pulling a requirement from Jira** (writes it into the graph as a `Requirement` node):

```bash
python -c "from tieukiwi.tools import fetch_jira; print(fetch_jira('PROJ-123'))"
# → {'tool': 'fetch_jira', 'status': 'ok',
#    'issue': {'key': 'PROJ-123', 'summary': '...', 'issuetype': 'Story', 'status': 'In Progress'},
#    'node_id': 42}
```

---

## Project Structure

```
tieu-kiwi/
├── docker-compose.yml       # Local Postgres (postgres:16) service
├── requirements.txt         # Python dependencies
├── .env.example             # Template for .env (copy & fill in)
├── seed.py                  # Index skills/ + kb/ into Chroma (RAG)
├── scripts/seed/graph.py            # Insert a sample requirement graph for testing
├── db/
│   └── schema.sql           # Tables: nodes, edges, kb_rules, promotion_queue, thread_state
├── skills/                  # QE rubrics (Markdown) indexed into RAG
│   ├── test-driven-development.md
│   ├── code-review-and-quality.md
│   └── spec-driven-development.md
├── kb/                      # Extra knowledge docs to index (glossary, templates, samples)
├── docs/
│   └── ROADMAP.md           # Layer B (Slack) & Layer C (feedback loop) plan
└── tieukiwi/                # The Python package
    ├── config.py            # Loads .env; exposes ANTHROPIC/DATABASE/model/JIRA_*/SLACK_* settings
    ├── db.py                # Postgres connection + graph tools (coverage_gap, trace, go_no_go, upsert_node_by_ref, …)
    ├── rag.py               # Chroma: index_docs() / search()
    ├── memory.py            # 3-tier memory (Tier 2 = thread_state; Tier 3 TODO)
    ├── routing.py           # Entity-type → owner-role routing (Slack delivery TODO)
    ├── tools.py             # TOOLS definitions + run_tool dispatcher (incl. fetch_jira)
    ├── agent.py             # Claude tool-use loop (model configurable via config)
    ├── slack_app.py         # Slack Socket-Mode app (python -m tieukiwi.slack_app)
    └── cli.py               # CLI entry point (python -m tieukiwi.cli)
```

---

## Troubleshooting

| Symptom | Cause & Fix |
|---------|-------------|
| `RuntimeError: DATABASE_URL is not set. Add it to .env …` | `.env` missing or not at the repo root. Run `cp .env.example .env`, set `DATABASE_URL`, and run from the repo root. |
| `KeyError: 'ANTHROPIC_API_KEY'` on startup | `ANTHROPIC_API_KEY` isn't set in `.env`. Add it and restart. |
| `psycopg.OperationalError: connection refused` / could not connect | Postgres isn't running or the URL is wrong. `docker compose up -d`, check `docker compose ps`, and confirm `DATABASE_URL` host/port/creds match `docker-compose.yml`. |
| `relation "nodes" does not exist` | Schema not applied. Run `psql "$DATABASE_URL" -f db/schema.sql`. |
| First `search_kb`/`scripts/seed/kb.py` is slow or downloads a file | Chroma is fetching the `all-MiniLM-L6-v2` embedding model (~80 MB) once. Ensure network access; subsequent runs use the cache. |
| Anthropic `RateLimitError` / `429` | You've hit API rate limits. Back off and retry, reduce request frequency, or check your plan/limits in the Anthropic console. |
| `chromadb ... InvalidArgumentError: name ... 3-512 characters` | Chroma collection names must be ≥3 chars — this repo uses `knowledge_base` (not `kb`). Keep the name in `rag.py` as‑is. |
| `fetch_jira` returns `{"status": "error", "error": "Jira is not configured…"}` | Set `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` in `.env`. An HTTP 401/403 means bad email/token; 404 means the issue key doesn't exist or isn't visible to that account. |
| `SystemExit: Slack is not configured. Missing: …` | The Slack app needs `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` (and ideally `SLACK_SIGNING_SECRET`). Add them to `.env`. |
| `/tieukiwi` shows a Slack timeout / "failed" | The handler must ack within 3 s. It does by default; if you see this, the app process isn't running (`python -m tieukiwi.slack_app`) or Socket Mode / the slash command isn't configured in the Slack app. |

---

## Contributing / Roadmap

Layers A (agent core) and B (Slack wrapper) are in place. The next milestone is the
**feedback/learning loop (Layer C)** — thread feedback, KB promotion with a curator approval step,
and routing action items to owners via Slack. See [`docs/ROADMAP.md`](docs/ROADMAP.md). When adding
a capability, follow the project convention: **add one entry to `TOOLS` + one branch in `run_tool`
— don't rewrite the agent loop.**
