-- ==========================================================================
-- Migration 004: channel_project_map
--
-- Purpose:
--   Slack channels are the natural boundary for project isolation. Each channel
--   handles one project's questions; the agent should scope ALL tool calls to
--   that project.
--
--   Layer B (Slack) resolves `channel_id -> project_id` via this table at the
--   entry point of every event handler, then passes project_id into
--   `agent.ask(..., project_id=...)`. From there, `run_tool()` propagates it
--   to Postgres queries + RAG search.
--
--   This table is NOT FK-linked to `nodes.project_id` on purpose — a channel
--   can be provisioned for a project before any node with that project_id
--   exists (e.g. Kiwi's very first indexing).
--
-- Idempotent: uses IF NOT EXISTS.
-- ==========================================================================

CREATE TABLE IF NOT EXISTS channel_project_map (
  channel_id  TEXT PRIMARY KEY,     -- Slack channel id, e.g. "C0123XYZ"
  project_id  TEXT NOT NULL,        -- project code used in nodes.project_id
  team_id     TEXT,                 -- optional Slack workspace id (multi-workspace)
  note        TEXT,                 -- free-form (who wired this up, when, why)
  created_at  TIMESTAMPTZ DEFAULT now(),
  updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_channel_project_by_project
  ON channel_project_map (project_id);
