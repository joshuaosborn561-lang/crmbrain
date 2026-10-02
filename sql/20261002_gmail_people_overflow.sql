-- Persist Gmail people overflow across Railway cycles (no local volume).
-- Applied to the campaignintelligence project (schema crmbrain).

CREATE TABLE IF NOT EXISTS crmbrain.gmail_people_overflow (
  email text PRIMARY KEY,
  external_id text,
  first_name text,
  last_name text,
  name text,
  domain text,
  company text,
  raw_subject text,
  summary text,
  occurred_at timestamptz,
  extra jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE crmbrain.gmail_people_overflow ENABLE ROW LEVEL SECURITY;
GRANT ALL ON crmbrain.gmail_people_overflow TO service_role;
