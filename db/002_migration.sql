-- ==========================================================================
-- Migration 002: cross-project support + users table (routing target)
--
-- Purpose:
--   1) Enable multi-project graphs and cross-project edges
--      (e.g. Component[PROJ_AUTH] --dependsOn--> Component[PROJ_NOTIF])
--   2) Add a users directory so ask-routing can @mention real Slack users
--
-- Design notes:
--   - project_id is NULLABLE on purpose so legacy rows (from seed_graph.py or
--     early ingests) still validate. New ingest paths should always set it.
--   - We do NOT add project_id to edges. Cross-project cardinality is a first
--     class feature; filter by project at query time via nodes.project_id.
--   - users is intentionally NOT FK-linked to nodes.props_json.owner_slack_id;
--     ownership override is a soft link so re-assignments don't require FK cascades.
--
-- Idempotency: safe to re-run (uses IF NOT EXISTS).
-- ==========================================================================

-- ---------- nodes.project_id ----------
ALTER TABLE nodes
  ADD COLUMN IF NOT EXISTS project_id TEXT;

-- Helpful indexes for project-scoped traversal
CREATE INDEX IF NOT EXISTS idx_nodes_project_type ON nodes (project_id, type);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes (type);
CREATE INDEX IF NOT EXISTS idx_nodes_props ON nodes USING GIN (props_json);

CREATE INDEX IF NOT EXISTS idx_edges_src_rel ON edges (src_id, rel);
CREATE INDEX IF NOT EXISTS idx_edges_dst_rel ON edges (dst_id, rel);


-- ---------- users (routing target) ----------
CREATE TABLE IF NOT EXISTS users (
  id               BIGSERIAL PRIMARY KEY,
  slack_id         TEXT UNIQUE,
  jira_account_id  TEXT,
  email            TEXT,
  display_name     TEXT,
  role             TEXT NOT NULL,   -- PO | BA | QE_LEAD | QE_EXECUTOR | DEV | TECH_LEAD
  project_id       TEXT             -- NULL = global default role
);

CREATE INDEX IF NOT EXISTS idx_users_role_project ON users (role, project_id);


-- ---------- Optional indexes on existing tables ----------
CREATE INDEX IF NOT EXISTS idx_kb_applies ON kb_rules (applies_to) WHERE status = 'active';
