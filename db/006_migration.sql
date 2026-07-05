-- Migration 006: Layer C — go-live release sign-off decisions
--
-- Records the human final sign-off (Delivery Manager) for a release, separate
-- from KB rule promotion. nodes/edges unchanged; this is a new table.

CREATE TABLE IF NOT EXISTS go_live_decisions (
  id              BIGSERIAL PRIMARY KEY,
  requirement_ref TEXT NOT NULL,
  decision        TEXT NOT NULL,        -- approved | rejected
  approved_by     TEXT,                 -- Slack user id of the approver
  reason          TEXT,
  provenance      JSONB DEFAULT '{}',
  created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_golive_req ON go_live_decisions (requirement_ref);
