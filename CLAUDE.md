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
nodes(id, type, ref, props_json, created_at)
edges(id, src_id, rel, dst_id, props_json)

Requirement         -has->        AcceptanceCriterion
AcceptanceCriterion -coveredBy->  TestCase
TestCase            -inPlan->      TestPlan
TestCase            -executedBy->  TestRun
TestRun             -finds->       Bug
Bug                 -affects->     Component
Bug                 -violates->    AcceptanceCriterion
Requirement         -impacts->     Component
Feedback            -about->       Bug | Requirement
ReviewRule (KB)     -appliesTo->   <EntityType>
```

Graph tools the agent uses:
- `coverage_gap(requirement)` -> ACs with no TestCase (= coverage gap).
- `trace(requirement)` -> path Requirement->AC->TestCase->TestRun->Bug (tested & passing or not).
- `bug_blast_radius(bug)` -> number of ACs/Requirements depending on the affected Component -> bug priority.
- `go_no_go(requirement)` -> aggregate -> GO/NO-GO decision + next_actions.

## Tech stack

- **LLM:** Anthropic Python SDK (`anthropic`), model `claude-sonnet-4-6` / Opus. Embeddings for RAG.
- **Vector store:** Chroma (PersistentClient, path `./chroma_db`), collection `"kb"`.
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
|-- db/schema.sql                # nodes, edges, kb_rules, promotion_queue, thread_state
|-- kb/                          # docs indexed into RAG: glossary, templates/, samples/
|-- skills/                      # rubrics (SKILL.md borrowed from agent-skills)
|-- seed.py                      # index skills/ + kb/ into Chroma
|-- seed_graph.py                # sample graph data for testing
`-- tieukiwi/
    |-- config.py                # reads .env
    |-- db.py                    # Postgres connection + graph tools
    |-- rag.py                   # Chroma: index_docs, search
    |-- memory.py                # 3-tier memory
    |-- routing.py               # ask routing: entity type -> owner role
    |-- tools.py                 # TOOLS definitions + run_tool
    |-- agent.py                 # tool-use loop
    `-- cli.py                   # CLI entry point
```

## Working conventions (IMPORTANT)

- **Do not change the architecture** described above unless explicitly asked.
- **Do not change signatures of functions already in use** (e.g. `coverage_gap()`) to avoid breaking other code.
- **Do not modify the nodes/edges schema** unless asked.
- **Always parameterize SQL**, never concatenate strings (avoid injection).
- **Secrets live only in .env**, never committed, never hardcode keys/tokens/passwords.
- Use the model string `claude-sonnet-4-6` for the agent loop.
- Chroma: collection `"kb"`, path `"./chroma_db"`. Always run seed & CLI from the repo root.
- When adding a new tool: add an entry to `TOOLS` (clear input_schema + description) and a branch in
  `run_tool`; do NOT rewrite the agent loop.
- Test with `python -c "import tieukiwi.<module>"` instead of running `python -m tieukiwi.cli`
  (the CLI needs a real API key + Postgres).

## Current state vs target

Already present: config, db (coverage_gap), rag, agent loop, cli, basic tools (search_kb,
coverage_gap), Postgres via Docker, RAG indexing of skills.

Still to add for a complete wireframe: trace / bug_blast_radius / go_no_go; gen_test_plan /
gen_testcase / gen_critic; fetch_jira; memory.py (tiers 2, 3); routing.py; tables kb_rules /
promotion_queue / thread_state; kb/templates + kb/samples; Layer B (Slack app); Layer C (feedback
loop + KB promotion). Parts belonging to later deadlines may be left as skeleton + TODO.
