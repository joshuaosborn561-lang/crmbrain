# CRM Brain nurture ticker rebuild: spec

Status: approved by Josh 2026-10-02. Spec only. Implementation goes in a new PR stacked on draft PR #19 (`cursor/dry-run-63-backfill-gates-b994`, agent bc-4963015b) and its deal_terms follow-up.
Fixtures: `fixtures/nurture_fixtures.json`. Each test ID below (T-xx) maps to the `id` field of one fixture case.
Audit evidence for each problem: see the prior audit (2026-10-02). Line refs are to `main` @ 616a9c8 unless noted.

## 0. Scope and non-goals

In scope: ticker enrollment sources, ticker schema, pre-draft gates, draft content, cadence, the backfill script, and rollout.

Out of scope (owned elsewhere, do NOT duplicate):
- PR #19 already handles these. Reuse them, do not reimplement:
  - nameless rows rejected at enroll time (`memory.enroll_ticker`, `ticker.plan_enrollments` skip `no_name`, `ticker.enroll`)
  - HeyReach never queued for booked/held/open-deal contacts
  - `CRMBRAIN_LOOKBACK_START` / `lookback_override`
  - `CLIENT_HINTS` + Smartlead `client_campaign` tag
  - `config.non_deal_emails()` / `is_non_deal_person()`
  - silent-meeting detection
- deal_terms / amounts are owned by #19 and the stacked deal_terms PR. Deal amount = total contract value (TCV), never one instalment. The ticker never writes `amount`. It may only *read* the deal amount for context.
- **No Allo enrollment.** Allo is dropped. Josh's calls live in Cube ACR recordings in Google Drive (only 5 day-folders so far, upload gap). Cube and Fireflies are an **optional context source** for `last_touch_snippet` only. They are never required, and their absence never blocks enrollment.

## 1. Enrollment sources and signal dates

The backfill and the live cycle both build `TickerCandidate`s from the three sources below. Each candidate carries `source`, `signal_at` (the real moment of the prospect's last positive signal), `campaign`, `last_touch_snippet`, and a `source_ref` (stable id for idempotency).

| # | Source | Selection | signal_at | snippet | source_ref |
|---|---|---|---|---|---|
| S1 | Smartlead | Every campaign for client **345263**, any status (ACTIVE, PAUSED, COMPLETED, STOPPED, DRAFTED). Today `scan()` only reads ACTIVE ones (`sources/smartlead.py:172-176`), and that drops 50 of 85 positives. Leads in categories Interested (1), Meeting Request (2), Information Request (5), plus any category whose `sentiment_type` is positive (today that is 131482 "Positive Reply"). | Latest inbound message time from `campaigns/{cid}/leads/{lead_id}/message-history` (type REPLY, RECEIVED or INBOUND). If no inbound timestamp exists, skip with `no_signal_date` and add to the review list. **Never** use epoch 0. Today `_engagement_from_row` does `fromtimestamp(0)`, which makes the row instantly due. | Body of that latest inbound reply, quoted text stripped, first 280 chars | `smartlead:{cid}:{lead_id}` |
| S2 | HubSpot | (a) Every deal in stage **Nurture** (`3486952153`). Tonight's rebuild gives 35 deals, $45.1k TCV, each with a source note. (b) Deals in Discovery Completed (`presentationscheduled`) or Proposal Sent (`decisionmakerboughtin`) with no activity for 30+ days. "Activity" = max of `notes_last_contacted`, `notes_last_updated`, `hs_last_sales_activity_timestamp`, latest associated meeting/email engagement. Today (b) has 0 deals, but it must still be supported. | (a) Newest of: the source note's date, last associated email or meeting engagement, last inbound email. **Not** `hs_lastmodifieddate`. Today `backfill_nurture_ticker.py:160-165` uses that, and any edit resets the clock (that is why Joel and Jackie fired Sep 30). (b) The activity timestamp above. | The source note body (first 280 chars). If there is none, the latest associated email or meeting summary. If there is none of those either, use optional Cube/Fireflies context. | `hubspot:deal:{deal_id}:contact:{contact_id}` |
| S3 | Gmail | Threads where the prospect replied positively to Josh. Reuse the `gmail_scan` people scan and its intent classifier from #17/#19. A positive is intent in {interested, meeting_request, info_request, timing_later}. Exclude system addresses, calendar notifications, billing and signature mail. | Date of the prospect's latest positive inbound message | That message, quoted text stripped, first 280 chars | `gmail:{thread_id}` |

Precedence: when one person appears in several sources, merge into one candidate keyed by lowercase email (else hs_contact_id). Take `signal_at` from the newest signal. Take the snippet from the same source as that signal. Fill `campaign` and `industry` from the first non-empty value in this order: S1, S2, S3.

**Re-enrollment.** When a new signal arrives (signal_at newer than the row's signal_at):
- **Active row exists:** update signal_at, snippet, campaign and source, and set `next_fire_at = signal_at + 90d`.
- **Stopped row with a soft `stop_reason`** (`booked`, `emailed_recently`, `deal_archived`, `legacy_reset`, `manual_snooze`): insert a new active row. The partial unique index allows one active row per email.
- **Stopped row with a hard `stop_reason`** (`client`, `non_deal`, `unsubscribed`, `won`, `do_not_contact`): never re-enroll.
- Today `already_enrolled` (`ticker.py:76-94`) treats stopped rows as enrolled forever. Change it to consider active rows only, plus hard-stopped rows.

Live cycle: keep enrolling from the 36h lookback, with the same rules. The backfill script uses `CRMBRAIN_LOOKBACK_START` from #19 to bound S3. S1 and S2 are full scans.

## 2. Schema: new ticker fields + migration

New columns: `campaign`, `industry`, `last_touch_snippet`, `source`, `signal_at`. Supporting columns: `source_ref`, `campaign_id`, `stop_reason`, `stopped_at`, `industry_basis`.

```sql
-- migration: 2026XXXX_nurture_ticker_context.sql  (apply via the normal migration path; NOT run by this spec)
alter table crmbrain.ticker
  add column if not exists source             text,          -- smartlead | hubspot | gmail
  add column if not exists source_ref         text,          -- smartlead:{cid}:{lead_id} | hubspot:deal:{id}:contact:{id} | gmail:{thread_id}
  add column if not exists signal_at          timestamptz,   -- real last positive signal; drives next_fire_at
  add column if not exists campaign           text,          -- Smartlead campaign name or HubSpot source-note campaign
  add column if not exists campaign_id        text,
  add column if not exists industry           text,          -- VERTICALS key (hvac, roofing, staffing, msp, ...) or null
  add column if not exists industry_basis     text,          -- campaign | domain | website | hubspot | null
  add column if not exists last_touch_snippet text,          -- <=280 chars, what THEY said
  add column if not exists stop_reason        text,          -- see section 1 (soft vs hard)
  add column if not exists stopped_at         timestamptz;

alter table crmbrain.ticker
  add constraint ticker_source_chk check (source is null or source in ('smartlead','hubspot','gmail')),
  add constraint ticker_snippet_len_chk check (last_touch_snippet is null or char_length(last_touch_snippet) <= 280);

create index if not exists ticker_due_idx on crmbrain.ticker (next_fire_at) where status = 'active';
create index if not exists ticker_source_ref_idx on crmbrain.ticker (source_ref);
-- existing: ticker_email_active_uidx (lower(email)) where status='active' and email is not null  -> keep

-- legacy cleanup (run only in the approved apply step, after dry-run review):
-- update crmbrain.ticker set status='stopped', stop_reason='legacy_reset', stopped_at=now()
--  where status='active' and source is null;   -- all 57 pre-spec rows; the backfill re-enrolls the valid ones with real context
```

Code: `backfill_row` / `enroll` / `TickerCandidate` carry the new fields. `memory.enroll_ticker` writes them. Rows without `signal_at` are invalid after the migration.

**Industry resolution** (`infer_industry`). The first basis that returns a known vertical wins. Record which basis in `industry_basis`.
1. **campaign**: the campaign name (e.g. "SalesGlider Roofers" → roofing, "SalesGlider MSPs" → msp, "SalesGlider Staffing Airpods Only" → staffing, "SalesGlider HVAC Sports Offer" → hvac). Add keywords: `msp`/`msps`, `roofers`, `financial advisor(s)` (new vertical `financial_advisors`, no case study yet).
2. **domain**: split the email or website domain on `-`, `.`, and known suffix tokens, then keyword-match. `kellyroofing.com` → roofing. `talentedrecruiting.com` → staffing. `thechillbrothers.com` → no match.
3. **website**: if the HubSpot contact or company has `industry`, use it. Otherwise fetch the company homepage `<title>` and meta description once (5s timeout, cached in `crmbrain.relationship_facts`) and keyword-match. Website fetch is skipped in unit tests and fixtures supply `website_text`.
4. Otherwise `industry = null` and the general copy is used.

## 3. Pre-draft gates (evaluated at fire time, in `_fire_ticker`, before drafting)

Every gate returns a `skip_reason`. A gate either stops the row (hard or soft per section 1) or snoozes it (pushes next_fire_at). Each skip goes into `report.ticker_skipped` with counts. Nothing is posted for a gated row.

| Gate | Check | Action |
|---|---|---|
| G1 emailed_recently | Josh emailed this address in the last 60 days. Check **Gmail Sent** (`in:sent to:{email} newer_than:60d`) **or** Smartlead message-history has a SENT message within 60d in any client-345263 campaign | soft stop `emailed_recently`, `next_fire_at = last_sent + 90d` (stays active, snoozed) |
| G2 client / non-deal | `is_non_deal_person()` (NON_DEAL_EMAILS, seeded names), `CLIENT_HINTS` match on company/name, source campaign tagged `client_campaign` (e.g. "SalesGlider Acquire MSPOwner Infonaligy", "Insight ... Sports"), candidate-recruiting campaigns ("CANDIDATES"), or any associated deal in Closed Won (`closedwon`) / Contract Sent (`4391699184`) | hard stop `client` / `non_deal` |
| G3 deal deleted/archived | Row has `hs_deal_id` and HubSpot returns 404 or `archived=true` | soft stop `deal_archived` (re-enrolls on new signal) |
| G4 booked meeting | Contact has a future meeting (`contact_has_future_meetings`), or an open deal in Discovery Scheduled (`qualifiedtobuy`), or a non-stalled Discovery Completed/Proposal Sent (activity < 30d). Reuse #19's booked/held/open-deal helper | soft stop `booked` |
| G5 no identity | `name` empty **and** `email` empty. These are legacy rows: PR #19 already blocks new nameless rows at enroll time | hard stop `no_identity` |
| G6 unsubscribed | Smartlead lead is unsubscribed/blocklisted or category is Not Interested/Do Not Contact after the signal | hard stop `unsubscribed` |
| G7 copy validator | Draft fails section 4 validation | do not post; add to review with reason |

Order: G5, G2, G6, G3, G4, G1, then draft, then G7. The cheap local checks run before any API calls.

## 4. Draft content rules

`ticker.draft_email(row)` takes the full row (with snippet, industry, campaign). Structure, max ~90 words in the body:

1. **Opener: what they actually said.** One sentence that references `last_touch_snippet`.
   - Generated by Gemini (already configured) with the prompt: "Paraphrase what the prospect said in <=20 words, second person, no new facts, no numbers not in the snippet".
   - Fallback when the snippet is empty or the model fails validation: a template built from campaign and source. Example: `Hey {first}, you replied a while back when we reached out about {campaign_topic}.`
   - Never fabricate a conversation. If there is no snippet and no campaign, use `Hey {first}, it's been a few months since we connected.`
2. **Proof.**
   - When industry is known and `CASE_STUDIES[industry]` exists, use a short industry case study with ROI.
   - Seed table, figures limited to what is in the live approved copy:
     - roofing: "one of our roofers closed $100K in his first 3 months with us"
     - hvac/trades: "$2M in pipeline last quarter across our trades clients, one closed $100K in their first 3 months"
   - Other verticals (staffing, msp, construction, plumbing, electrical, solar, financial_advisors) use the general version until Josh approves a figure.
   - General version: "$2M in pipeline last quarter, one client closed $100K in their first 3 months, averaging 14+ replies per month."
3. **Soft CTA = meeting guarantee:** `We guarantee meetings, or we keep working until you hit them.` (`MEETING_GUARANTEE`).
   - AirPods / tickets: Josh rule is no AirPods or tickets. `AIRPODS_OFFER_LIVE = False`. Do not append a gift offer.
   - Remove the "I can send a Loom" variant unless Josh confirms it.
4. Close: `Worth a look?` then a blank line, then `Josh Osborn`.
5. Subject: industry subject (e.g. "Roofing?", "HVAC update"), else `{first}?`. Never "Quick update" for a named contact.

Hard validator (G7). A failing draft is never posted:
- no `has_free_poc_offer()` phrase (free POC, proof of concept, free 10K, test list, free campaign)
- no em/en dash characters (— – −). `_no_dashes` replaces them with "..." and the validator re-checks
- body <= 110 words
- ends with `Josh Osborn`
- contains `MEETING_GUARANTEE`
- first name is not "there" (G5 should already have caught that)
- no `{` placeholders left
- opener contains no number that is absent from snippet/case study

Slack post format stays as-is ("90-day ticker (approve before send)"), plus three lines:
- `Source: {source} / {campaign}`
- `Signal: {signal_at Chicago date}`
- `They said: "{snippet first 120 chars}"`

This lets Nurture approve with context.

## 5. Cadence

- `next_fire_at = signal_at + 90d`. After a fire, `next_fire_at = fired_at + 90d` until a newer signal resets it (section 1).
- **Backfill spreading.** Candidates whose `signal_at + 90d` is already in the past are scheduled oldest-signal first into weekday slots, starting on the first weekday after approval.
  - Cap: `NURTURE_MAX_PER_WEEKDAY = 5` posts per weekday (Chicago), no weekends.
  - Candidates whose `signal_at + 90d` is in the future keep that date, rolled forward to the next weekday with a free slot.
  - The live `_fire_ticker` also enforces the cap: due rows beyond 5 that day roll to the next free weekday (oldest signal first).
- **Empty means empty.** `memory.due_ticker` and `list_ticker` currently do `return rows or local` (`memory.py:330`, `:307`), so an empty Supabase result silently falls back to the container-local JSON.
  - Change: when Supabase is configured and the call succeeds, return the Supabase result even when it is `[]`.
  - Fall back to local only on an exception, and in that case fire **nothing** (log `ticker_supabase_unavailable`). A ticker must never post from local state in production.

## 6. Rollout

1. Merge order: #19 → deal_terms PR → this PR. This PR is rebased on them and does not touch their code paths except to call their helpers.
2. Apply the migration (schema only). Do not run the legacy cleanup yet.
3. **Dry-run backfill:** `python scripts/backfill_nurture_ticker.py --dry-run` (default) with `CRMBRAIN_DRY_RUN=1`.
   - Writes nothing to Supabase, HubSpot, Smartlead or Slack.
   - Outputs a report file plus a short Slack-free summary containing:
     - candidates by source (S1/S2/S3) and merged unique count
     - would-enroll / would-reenroll / would-skip counts by gate and skip reason
     - industry hit rate by basis
     - proposed weekly schedule (posts per week)
     - **10 sample drafts** (mix: at least 3 industry, 3 general, 2 HubSpot Nurture, 1 Gmail) rendered exactly as they would post
   - Josh's hard rule applies to the report: counts plus at most 10 samples, never full lists in output. The full list goes to a file only.
4. Nurture/Josh review the sample drafts and counts. Copy tweaks (AirPods flag, case-study figures) happen here.
5. **Only after explicit approval:** run the legacy cleanup, then `--apply` (writes ticker rows only), then set `CRMBRAIN_DRY_RUN=0` on Railway (owner's call). First live fire posts at most 5 that weekday.
6. Rollback: `update crmbrain.ticker set status='stopped', stop_reason='manual_snooze' where source is not null`. Restore legacy rows by flipping `legacy_reset` back to active if needed.

Estimate for the dry run (from the 2026-10-02 audit pulls):
- S1: 85 positives (excluding 1 internal test address)
- S2: 39 Nurture contacts, 0 stalled
- Overlap: 11, so ~113 merged before gates
- Gate losses: ~5 to client/candidate campaigns (G2), ~5 to clients/Paid/NON_DEAL, ~15-30 to emailed-in-60d (G1, the main unknown), a few to booked (G4)
- S3 (Gmail) adds an unmeasured ~5-20
- **Expected: ~75-95 active enrollments**, vs 57 legacy rows today of which 0 get industry copy. Roughly 50-60 would already be past due, so about 2-3 weeks of backlog at 5/weekday.

## 7. Acceptance tests (pytest, `tests/test_nurture_rebuild.py`, all offline with fixtures + fakes)

| ID | Area | Assertion |
|---|---|---|
| T-01 | S1 all statuses | Positives from PAUSED and COMPLETED campaigns are collected, not only ACTIVE |
| T-02 | S1 signal date | `signal_at` = latest inbound message-history time; next_fire_at = signal_at + 90d |
| T-03 | S1 no date | No inbound timestamp → skipped `no_signal_date`, never epoch 0 |
| T-04 | S1 client campaign | `client_campaign` campaign (Infonaligy) → skipped G2 |
| T-04b | S1 candidate campaign | CANDIDATES campaign → skipped G2 non_deal |
| T-05 | S2 Nurture | Nurture deal with source note → enrolled with snippet from note; signal_at from note date, not hs_lastmodifieddate |
| T-06 | S2 stalled | Proposal Sent, last activity 45d ago → enrolled; 10d ago → not enrolled |
| T-07 | S3 Gmail | Positive Gmail reply → enrolled, snippet from inbound body with quote stripped |
| T-08 | Merge | Same email in S1 + S2 → one candidate, newest signal wins, campaign from S1 |
| T-09 | Re-enroll soft | Stopped `booked` row + newer signal → new active row |
| T-10 | Re-enroll hard | Stopped `client` row + newer signal → no row |
| T-11 | Active refresh | Active row + newer signal → signal_at/snippet updated, next_fire_at moved |
| T-12 | Industry campaign | "SalesGlider Roofers" → roofing, basis=campaign |
| T-13 | Industry domain | jackie@kellyroofing.com with no company/campaign → roofing, basis=domain (regression for the Sep 30 "Quick update") |
| T-14 | Industry website | thechillbrothers.com with website_text "HVAC heating and cooling" → hvac, basis=website (regression for Joel) |
| T-15 | Industry none | No match anywhere → null → general copy |
| T-16 | G1 Gmail Sent | Sent 20d ago → skipped emailed_recently, next_fire_at = sent + 90d |
| T-17 | G1 Smartlead sent | Smartlead SENT 30d ago → skipped |
| T-18 | G2 Paid/NON_DEAL | Paid deal or NON_DEAL_EMAILS → hard stop |
| T-19 | G3 deleted deal | Deal 404 → soft stop deal_archived (legacy Joel/Jackie case) |
| T-20 | G4 booked | Future meeting → soft stop booked |
| T-21 | G5 legacy no identity | name='' and email='' (phone-only RVM legacy row) → hard stop, nothing posted |
| T-22 | Draft opener | Draft's first sentence references the snippet; no numbers not in snippet |
| T-23 | Draft industry | Roofing draft contains the roofing case study + MEETING_GUARANTEE, subject "Roofing?" |
| T-24 | Draft general | Unknown industry draft contains "$2M", "$100K", "14+" and subject "{first}?" |
| T-25 | Draft validator | Snippet containing "free 10K" / em dash: output has no POC phrase, no dashes ("..." instead); POC in model output → G7 reject |
| T-26 | Draft shape | Body <= 110 words, ends "Josh Osborn", no "{" left, no AirPods/gift line unless AIRPODS_OFFER_LIVE |
| T-27 | Cadence spread | 12 past-due candidates → 5,5,2 across 3 consecutive weekdays, no weekend slots, oldest signal first |
| T-28 | Cadence future | signal_at + 90d in the future keeps its date (rolled to weekday) |
| T-29 | Empty means empty | Supabase returns [] → due_ticker returns [] even when local JSON has due rows |
| T-30 | Supabase error | Supabase raises → fire nothing, error `ticker_supabase_unavailable` |
| T-31 | Dry-run report | Backfill dry-run writes nothing (fake memory records 0 writes), report has counts by source + exactly <=10 sample drafts |
| T-32 | Live cap | 8 rows due today → 5 posted, 3 rolled to next weekday |
