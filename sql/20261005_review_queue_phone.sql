-- Live Supabase (schema crmbrain): add the phone column that enqueue_review writes.
-- Monday Oct 5 cycle dropped ~64 review_queue rows because POST included `phone`
-- and crmbrain.review_queue had no such column (PGRST204 / schema cache).
--
-- Apply this on the campaignintelligence project as the service_role (SQL editor
-- or psql). After it succeeds, reload the PostgREST schema cache if rows still
-- 400:  NOTIFY pgrst, 'reload schema';
--
-- Safe to re-run.

ALTER TABLE crmbrain.review_queue
  ADD COLUMN IF NOT EXISTS phone text;
