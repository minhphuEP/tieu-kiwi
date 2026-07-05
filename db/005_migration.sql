-- Migration 005: Layer C — curator / KB promotion flow
--
-- Per CLAUDE.md we never edit db/schema.sql; schema changes go in a numbered,
-- idempotent migration. kb_rules already ships with the required shape in
-- schema.sql (rule, scope, applies_to, status, provenance, created_at) — ensured
-- here for fresh DBs. promotion_queue shipped with (candidate, source, ...); this
-- migration reconciles it to (candidate_rule, scope, applies_to, evidence, ...)
-- used by the curator flow, without dropping anything.

-- kb_rules: no-op if it already exists (fresh-DB safety).
CREATE TABLE IF NOT EXISTS kb_rules (
  id          BIGSERIAL PRIMARY KEY,
  rule        TEXT NOT NULL,
  scope       TEXT,
  applies_to  TEXT,
  status      TEXT DEFAULT 'active',
  provenance  JSONB DEFAULT '{}',
  created_at  TIMESTAMPTZ DEFAULT now()
);

-- promotion_queue: create with the curator-flow shape if it does not exist yet.
CREATE TABLE IF NOT EXISTS promotion_queue (
  id             BIGSERIAL PRIMARY KEY,
  candidate_rule TEXT,
  scope          TEXT,
  applies_to     TEXT,
  evidence       JSONB DEFAULT '{}',
  status         TEXT DEFAULT 'pending',
  created_at     TIMESTAMPTZ DEFAULT now()
);

-- If an older promotion_queue exists (schema.sql shape), add the new columns.
ALTER TABLE promotion_queue ADD COLUMN IF NOT EXISTS candidate_rule TEXT;
ALTER TABLE promotion_queue ADD COLUMN IF NOT EXISTS scope          TEXT;
ALTER TABLE promotion_queue ADD COLUMN IF NOT EXISTS applies_to     TEXT;
ALTER TABLE promotion_queue ADD COLUMN IF NOT EXISTS evidence       JSONB DEFAULT '{}';

-- The legacy NOT NULL 'candidate' column would block inserts that only set
-- candidate_rule. Drop the constraint if that column is present.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'promotion_queue' AND column_name = 'candidate'
  ) THEN
    ALTER TABLE promotion_queue ALTER COLUMN candidate DROP NOT NULL;
  END IF;
END $$;
