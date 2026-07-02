CREATE TABLE IF NOT EXISTS nodes (
  id          BIGSERIAL PRIMARY KEY,
  type        TEXT NOT NULL,        -- Requirement, AcceptanceCriterion, TestCase, ...
  ref         TEXT,                 -- external reference (JIRA-123, ...)
  props_json  JSONB DEFAULT '{}',
  created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS edges (
  id          BIGSERIAL PRIMARY KEY,
  src_id      BIGINT REFERENCES nodes(id),
  rel         TEXT NOT NULL,        -- has, coveredBy, executedBy, finds, affects, ...
  dst_id      BIGINT REFERENCES nodes(id),
  props_json  JSONB DEFAULT '{}'
);

-- KB rules (for Layer C, pre-created)
CREATE TABLE IF NOT EXISTS kb_rules (
  id          BIGSERIAL PRIMARY KEY,
  rule        TEXT NOT NULL,
  scope       TEXT,
  applies_to  TEXT,                 -- entity type per ontology
  status      TEXT DEFAULT 'active',
  provenance  JSONB DEFAULT '{}',
  created_at  TIMESTAMPTZ DEFAULT now()
);

-- KB promotion queue: candidate rules awaiting curator approval (Layer C)
CREATE TABLE IF NOT EXISTS promotion_queue (
  id          BIGSERIAL PRIMARY KEY,
  candidate   TEXT NOT NULL,        -- proposed rule / knowledge snippet
  source      JSONB DEFAULT '{}',   -- where it came from (thread, user, ...)
  status      TEXT DEFAULT 'pending',  -- pending / approved / rejected
  created_at  TIMESTAMPTZ DEFAULT now(),
  decided_at  TIMESTAMPTZ
);

-- Tier 2 memory: per-thread/artifact review state (Layer C feedback loop lives here)
CREATE TABLE IF NOT EXISTS thread_state (
  id          BIGSERIAL PRIMARY KEY,
  channel_id  TEXT NOT NULL,
  thread_ts   TEXT NOT NULL,
  state_json  JSONB DEFAULT '{}',
  updated_at  TIMESTAMPTZ DEFAULT now(),
  UNIQUE (channel_id, thread_ts)
);