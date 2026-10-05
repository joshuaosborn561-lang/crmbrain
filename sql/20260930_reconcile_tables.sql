-- Review queue, source freshness, Cube ACR cache, and ingest cursors.
-- Applied to the campaignintelligence project (schema crmbrain). Soft-archive
-- only; these tables never write to HubSpot.

CREATE TABLE IF NOT EXISTS crmbrain.review_queue (
  id bigserial PRIMARY KEY,
  person_key text NOT NULL,
  email text,
  phone text,
  name text,
  company text,
  intent text,
  confidence numeric,
  reason text,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'open',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS review_queue_status_idx
  ON crmbrain.review_queue (status, created_at DESC);
CREATE INDEX IF NOT EXISTS review_queue_person_idx
  ON crmbrain.review_queue (person_key);

CREATE TABLE IF NOT EXISTS crmbrain.source_freshness (
  source text PRIMARY KEY,
  last_item_at timestamptz,
  last_success_at timestamptz,
  last_error text,
  item_count integer,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crmbrain.source_cursors (
  source text PRIMARY KEY,
  cursor text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crmbrain.cube_acr_calls (
  id text PRIMARY KEY,
  occurred_at timestamptz,
  name text,
  phone text,
  transcript text,
  summary text,
  raw_subject text,
  extra jsonb NOT NULL DEFAULT '{}'::jsonb,
  synced_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cube_acr_calls_occurred_idx
  ON crmbrain.cube_acr_calls (occurred_at DESC);

ALTER TABLE crmbrain.review_queue ENABLE ROW LEVEL SECURITY;
ALTER TABLE crmbrain.source_freshness ENABLE ROW LEVEL SECURITY;
ALTER TABLE crmbrain.source_cursors ENABLE ROW LEVEL SECURITY;
ALTER TABLE crmbrain.cube_acr_calls ENABLE ROW LEVEL SECURITY;

GRANT ALL ON crmbrain.review_queue TO service_role;
GRANT ALL ON crmbrain.source_freshness TO service_role;
GRANT ALL ON crmbrain.source_cursors TO service_role;
GRANT ALL ON crmbrain.cube_acr_calls TO service_role;
GRANT USAGE, SELECT ON SEQUENCE crmbrain.review_queue_id_seq TO service_role;
