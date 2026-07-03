-- ==========================================================================
-- Migration 003: enforce (project_id, ref) uniqueness for idempotent ingest.
--
-- The team's original schema.sql leaves `ref` NULL-able (some nodes are
-- unnamed, e.g. anonymous Feedback). We enforce uniqueness only when ref is
-- present — a partial unique index — so ingest_*.py can safely use ON CONFLICT
-- to upsert without duplicating rows.
--
-- Idempotent: uses CREATE UNIQUE INDEX IF NOT EXISTS.
-- ==========================================================================

CREATE UNIQUE INDEX IF NOT EXISTS unique_node_ref_per_project
  ON nodes (project_id, ref)
  WHERE ref IS NOT NULL;
