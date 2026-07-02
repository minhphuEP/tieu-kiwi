# Tieu Kiwi — Roadmap (Layer B & C)

Layer A (agent core: tool-use loop + graph/RAG tools) is the current focus and is largely in
place. This document captures what remains for **Layer B (Slack wrapper)** and **Layer C
(feedback loop & learning)**. These are intentionally **not implemented yet** — skeletons/tables
exist where noted, but the behavior is TODO.

## Layer B — Slack wrapper

Goal: expose the Layer A agent through Slack without changing the agent loop.

- [ ] **Slack app (Socket Mode)** — create app, enable Socket Mode, add scopes:
      `app_mentions:read, chat:write, commands, channels:history, groups:history,
      im:history, files:read, users:read`.
- [ ] **`/tieukiwi` slash command** — register the command; route its text to the agent.
- [ ] **Bolt handler skeleton** — ack within 3s, dedup repeated event deliveries, then call the
      Layer A agent (`tieukiwi.agent.ask`) and return the result.
- [ ] **Block Kit response** — format the agent's answer (e.g. go/no-go decision + next_actions)
      as Block Kit instead of plain text.
- [ ] **Routing delivery** — wire `tieukiwi.routing.route_gap` to actually post action items to
      the owner role via `chat:write` (currently `route_gap` only returns a record; delivery is TODO).

## Layer C — Loop & learning

Goal: the system "gets better the more it runs" by capturing thread outcomes and promoting
validated knowledge into the shared KB.

- [ ] **Thread feedback loop** — read replies in a thread, iterate on the review, and persist
      per-thread context/decisions. Storage exists: `thread_state` table +
      `tieukiwi.memory.get_thread_state` / `save_thread_state` (Tier 2). TODO: the loop that
      reads replies and updates state.
- [ ] **KB promotion** — when a thread reaches an accepted decision, enqueue a candidate rule
      into the `promotion_queue` table. TODO: the enqueue logic + candidate extraction.
- [ ] **Curator approval button** — Block Kit button for a curator to approve/reject a queued
      candidate; on approve, promote it into the KB (RAG index + `kb_rules`). TODO.
- [ ] **Tier 3 per-user memory** — preferences/role/style keyed by `user_id`. Skeleton stubs in
      `tieukiwi.memory` raise `NotImplementedError`. TODO: choose storage + implement.

## Related gaps (data / config, tracked separately)

- [ ] `kb/templates/` and `kb/samples/` — content to index into RAG (empty today).
- [ ] `.env.example` — referenced in CLAUDE.md layout but not present.
- [ ] `config.py` — currently empty; CLAUDE.md expects it to read `.env`.
- [ ] Chroma collection name — CLAUDE.md says `"kb"`, but `rag.py` uses `"knowledge_base"`
      (installed chromadb rejects 2-char names). Reconcile the docs or the code.
