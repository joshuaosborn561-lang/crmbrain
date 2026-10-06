-- Stop leftover Sep ticker rows that have no source and are not a HubSpot
-- Nurture-stage deal. Do NOT run from the scheduled cycle.
--
-- Dry-run (counts only):
SELECT count(*) AS legacy_active_no_source
FROM crmbrain.ticker
WHERE status = 'active'
  AND (source IS NULL OR source = '');

SELECT id, email, hs_deal_id, reason, next_fire_at
FROM crmbrain.ticker
WHERE status = 'active'
  AND (source IS NULL OR source = '')
ORDER BY next_fire_at;

-- Apply (after reviewing the dry-run counts). Keeps source-null rows whose
-- hs_deal_id is currently on dealstage 3486952153 if you join HubSpot yourself;
-- live Sep rows have no source and no Nurture deal id, so this UPDATE matches them.
-- UPDATE crmbrain.ticker
-- SET status = 'stopped',
--     stop_reason = 'legacy_reset',
--     stopped_at = now()
-- WHERE status = 'active'
--   AND (source IS NULL OR source = '');
