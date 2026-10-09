-- Backfill ticker rows that Gmail already sent but the sent PATCH missed.
-- Slack cards: Mike Dolan + Lionel Francis Thu Oct 8 ~5:23pm CT;
-- Josh Pugmire + Jonathan Matthews Fri Oct 9 7:15am CT.
-- Thread ids from Gmail Sent (joshua@salesglidergrowth.com).
-- Do NOT run from the scheduled cycle. Review, then uncomment the UPDATEs
-- after applying migrations/20261009_ticker_gmail_message_id.sql.

SELECT id, name, email, nurture_state, nurture_action, last_sent_at,
       nurture_thread_id, gmail_thread_id
FROM crmbrain.ticker
WHERE id IN (
  'fe1dba0a-7afb-4243-81ae-ca24af4d1653',  -- Mike Dolan
  'f0e03b45-f62f-4ca1-9b19-d09e72d6bacf',  -- Lionel Francis
  '10e590f2-d4fc-43b1-828e-a0add6f51795',  -- Josh Pugmire
  '4619b45f-d379-4932-bf86-b1afe3681951'   -- Jonathan Matthews
);

-- UPDATE crmbrain.ticker SET
--   nurture_state = 'sent',
--   nurture_action = 'approve',
--   stop_reason = 'emailed_recently',
--   last_sent_at = '2026-10-08T22:23:06+00:00',
--   next_fire_at = '2027-01-06T22:23:06+00:00',
--   nurture_thread_id = '1a11d9cefa92102f',
--   gmail_thread_id = '1a11d9cefa92102f',
--   gmail_message_id = '1a11d9cefa92102f',
--   nurture_thread_subject = 'Roof River City follow up',
--   original_subject = 'Roof River City follow up',
--   draft_subject = 'Roof River City follow up',
--   thread_kind = 'new_thread',
--   updated_at = now()
-- WHERE id = 'fe1dba0a-7afb-4243-81ae-ca24af4d1653';
--
-- UPDATE crmbrain.ticker SET
--   nurture_state = 'sent',
--   nurture_action = 'approve',
--   stop_reason = 'emailed_recently',
--   last_sent_at = '2026-10-08T22:23:13+00:00',
--   next_fire_at = '2027-01-06T22:23:13+00:00',
--   nurture_thread_id = '1a11d9d0b07a9124',
--   gmail_thread_id = '1a11d9d0b07a9124',
--   gmail_message_id = '1a11d9d0b07a9124',
--   nurture_thread_subject = 'Empire Roofing follow up',
--   original_subject = 'Empire Roofing follow up',
--   draft_subject = 'Empire Roofing follow up',
--   thread_kind = 'new_thread',
--   updated_at = now()
-- WHERE id = 'f0e03b45-f62f-4ca1-9b19-d09e72d6bacf';
--
-- UPDATE crmbrain.ticker SET
--   nurture_state = 'sent',
--   nurture_action = 'approve',
--   stop_reason = 'emailed_recently',
--   last_sent_at = '2026-10-09T12:15:07+00:00',
--   next_fire_at = '2027-01-07T12:15:07+00:00',
--   nurture_thread_id = '1a12096acb2664e6',
--   gmail_thread_id = '1a12096acb2664e6',
--   gmail_message_id = '1a12096acb2664e6',
--   nurture_thread_subject = 'Following up',
--   original_subject = 'Following up',
--   draft_subject = 'Following up',
--   thread_kind = 'new_thread',
--   updated_at = now()
-- WHERE id = '10e590f2-d4fc-43b1-828e-a0add6f51795';
--
-- UPDATE crmbrain.ticker SET
--   nurture_state = 'sent',
--   nurture_action = 'approve',
--   stop_reason = 'emailed_recently',
--   last_sent_at = '2026-10-09T12:15:11+00:00',
--   next_fire_at = '2027-01-07T12:15:11+00:00',
--   nurture_thread_id = '1a12096bacd6dfef',
--   gmail_thread_id = '1a12096bacd6dfef',
--   gmail_message_id = '1a12096bacd6dfef',
--   nurture_thread_subject = 'Following up',
--   original_subject = 'Following up',
--   draft_subject = 'Following up',
--   thread_kind = 'new_thread',
--   updated_at = now()
-- WHERE id = '4619b45f-d379-4932-bf86-b1afe3681951';
