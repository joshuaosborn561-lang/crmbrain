-- Nurture ticker context. Schema only. Do NOT run from this PR.
-- Apply via the normal migration path after dry-run review.

alter table crmbrain.ticker
  add column if not exists source             text,
  add column if not exists source_ref         text,
  add column if not exists signal_at          timestamptz,
  add column if not exists campaign           text,
  add column if not exists campaign_id        text,
  add column if not exists industry           text,
  add column if not exists industry_basis     text,
  add column if not exists last_touch_snippet text,
  add column if not exists stop_reason        text,
  add column if not exists stopped_at         timestamptz,
  add column if not exists nurture_state      text,
  add column if not exists nurture_action     text,
  add column if not exists gmail_thread_id    text,
  add column if not exists in_reply_to        text,
  add column if not exists slack_channel      text,
  add column if not exists slack_ts           text,
  add column if not exists draft_subject      text,
  add column if not exists draft_body         text,
  add column if not exists last_sent_at       timestamptz;

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'ticker_source_chk'
  ) then
    alter table crmbrain.ticker
      add constraint ticker_source_chk
      check (source is null or source in ('smartlead','hubspot','gmail'));
  end if;
  if not exists (
    select 1 from pg_constraint
    where conname = 'ticker_snippet_len_chk'
  ) then
    alter table crmbrain.ticker
      add constraint ticker_snippet_len_chk
      check (last_touch_snippet is null or char_length(last_touch_snippet) <= 280);
  end if;
end $$;

create index if not exists ticker_due_idx on crmbrain.ticker (next_fire_at) where status = 'active';
create index if not exists ticker_source_ref_idx on crmbrain.ticker (source_ref);

-- legacy cleanup (run only in the approved apply step, after dry-run review):
-- update crmbrain.ticker set status='stopped', stop_reason='legacy_reset', stopped_at=now()
--  where status='active' and source is null;
