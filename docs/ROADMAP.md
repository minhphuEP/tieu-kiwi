# Tieu Kiwi — Roadmap

Updated 2026-07-10.

Layer A (agent core: tool-use loop + graph/RAG + Jira/Confluence ingest) and most
of Layer B (Slack wrapper) are **now in place**. Layer C (feedback loop → KB
promotion) is the current focus.

## ✅ Layer A — Agent core (done, ongoing polish)

- Tool-use loop (`tieukiwi/agent.py`) with rich `DEFAULT_SYSTEM` prompt:
  get_ticket first, echo warnings verbatim, never invent data.
- Live tools registered in `tieukiwi/tools.py::TOOLS`:
  - `search_kb`, `coverage_gap`, `trace`, `bug_blast_radius`, `go_no_go`
  - `get_ticket` (polymorphic: Requirement/Bug/TestRun/UserStory/BRD)
  - `ingest_jira_ticket` — fetch Jira issue + subtasks + Confluence PRDs, extract ACs
  - `fetch_confluence` — for standalone Confluence URLs outside Jira context
  - `gen_testcase` — LLM draft + AC↔TC matcher
  - `mark_reviewed` — TestCase review state machine
  - `find_ambiguities` — clarify PRD interview
  - `code_impact`, `feature_blast_radius` — code-graph impact analysis
- Multi-tenant scoping: `project_id` + `role` propagate through every tool
  via `run_tool(name, args, context)`.
- Ingest hash-gate: story-level hash + BRD version + bug-container hash short-
  circuit unchanged tickets (~500ms) — see `jira_ingest.ingest_jira_ticket`
  and the dedicated `docs/HASH_GATE.md` for the 3-tier decision grid, known
  gaps, and per-event cost.

## ✅ Layer B — Slack wrapper (largely done)

- [x] **Slack app (Socket Mode)** — running via `python -m tieukiwi.slack_app`.
      Scopes wired: `app_mentions:read, chat:write, commands, channels:history,
      groups:history, im:history, files:read, users:read`.
- [x] **`/tieukiwi` slash command** — with `curator-test` demo shortcut.
- [x] **Bolt handler skeleton** — ack <3s + retry/event dedup (`_seen_before`).
- [x] **Block Kit response** — `_mrkdwn_blocks`, `_testcase_draft_blocks`,
      `_golive_approval_blocks`, `_clarify_summary_blocks` cover go-live,
      testcase draft/approve, clarify interview.
- [x] **Unified turn handler** — `_handle_mention_turn` (in `slack_app.py`)
      routes both `@mention` and non-mention thread replies through the same
      intent pipeline: refuse-switch → discard → sticky ticket resolve →
      pre-flight ingest → go-live / list-AC / clarify / gen-testcase / fallback.
- [x] **Sticky ticket per thread** — first ref persisted; refuse re-assignment
      attempts ("thread này giờ là CDM-500") — user must open a new thread.
- [x] **Force-refresh intent** — user says "cập nhật", "refresh", "PRD đã update"
      → agent re-ingests with `force=True`, bypassing hash-gate.
- [x] **Live progress display** — interim ack `chat_update` in-place with
      per-step labels (`_make_progress_callback`), swap for final answer.
- [x] **Routing via `db.mention_for(role, project_id)`** — single mention path
      through users table → optional `ROLE_<ROLE>` env → non-crashing fallback.

Still open in Layer B:

- [ ] **Wire more channels** — `db.bind_channel("C0123XYZ", "CDM", …)` once per
      channel. Currently only CDM is wired end-to-end.
- [ ] **Runtime alerts on gap detection** — currently gaps get answered in-thread;
      no push notification when a critical AC becomes uncovered.

## 🟨 Layer C — Loop & learning (partial, main focus)

- [x] **Thread state** — `thread_state` table + `memory.get_thread_state /
      save_thread_state` (Tier 2 — thread-scoped memory). Sticky ticket lives here.
- [x] **Curator demo** — `/tieukiwi curator-test` posts a candidate rule with
      Approve/Edit/Reject buttons; approver routed via role lookup.
- [ ] **Thread feedback loop** — read replies in a thread, iterate on the review,
      persist decisions. `promotion_queue` table exists; enqueue logic + candidate
      extraction from thread outcomes still TODO.
- [ ] **KB promotion path** — when a thread reaches an accepted decision, INSERT
      into `kb_rules` and re-index into Chroma. TODO end-to-end.
- [ ] **Tier 3 per-user memory** — `user_id`-keyed preferences/role/style.
      Skeleton stubs raise `NotImplementedError`.

## 📦 Data / config housekeeping

- [x] `.env.example` present in repo root.
- [x] `tieukiwi/config.py` reads `DATABASE_URL`, `ANTHROPIC_API_KEY`, LLM
      provider settings from `.env` (was empty in earlier snapshot).
- [x] Chroma collection unified — `rag.py` uses `"knowledge_base"` everywhere;
      CLAUDE.md matches.
- [ ] `kb/templates/` and `kb/samples/` — sample content exists (`kb/_global/QE/
      templates/testcase_template.md`); expand for other roles.

## 🧭 Slide-deck story arc (for presentation)

Suggested narrative, aligning with what's actually live:

1. **Problem** — QE handoff friction: PRD drift, coverage gaps, undocumented
   go-live decisions. What if the agent watched Jira/Confluence + the graph?
2. **Architecture** — Layer A/B/C, ontology diagram, 3-tier memory.
3. **Live demo** — `@Tieu Kiwi thông tin CDM-268` → sticky ticket → pre-flight
   ingest → PRD drift auto-detect → coverage_gap → go/no-go.
4. **What the agent actually does** — walk through `TOOLS` list; show 1 tool
   per bullet: ingest_jira_ticket, coverage_gap, gen_testcase, find_ambiguities.
5. **Layer C roadmap** — thread feedback → curator approval → KB promotion.
6. **Q&A** — invite specific team to bind their channel via `db.bind_channel`.
