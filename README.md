# SalesGlider CRM Brain

Daily grunt work for Josh's HubSpot. HubSpot only holds people with a **meeting held or scheduled**. Cold replies, LinkedIn chats, and RVM callbacks stay in Supabase (90-day Slack ticker). The CRM agent sits on top and QAs; this repo must not invent pipeline.

## What counts as engaged (HubSpot contact)

Create or keep a HubSpot contact **only** when:

- Fireflies meeting held
- Calendly / GCal booked or past (Josh SalesGlider intros)
- Cube ACR **business discovery** call with a real `*-transcript.docx` from Drive (call-transcriber output — not an HTML `.txt` scrape, not personal/family)
- Allo **discovery** conversation Josh actually took

**Never** create a HubSpot contact or deal from:

- Smartlead Interested / Info Request (or any positive reply alone)
- HeyReach / LinkedIn chat
- RVM callback
- Random Gmail person scan (`gmail_person` counterparts)

Those sources may still enroll the 90-day Slack ticker. Gmail only **updates** contacts/deals that already exist in HubSpot, except Calendly `create_new` for real Josh SalesGlider Intro bookings.

Clients (`CLIENT_HINTS`): notes only, never a new SalesGlider deal.

Personal phones/names are skipped (Sarah, Jeremy Ciotola, Dad, Cayden, Diana Burns, plus first-name aliases). **Jeremy Ciotola is personal** except an explicit SalesGlider Intro meeting — then he may be in CRM.

## Deal stages (forecasting)

Only move a deal when evidence warrants it. Never open a deal from Smartlead / HeyReach / RVM.

**Forward**

- Calendly / GCal booked → Discovery Scheduled (`qualifiedtobuy`)
- Meeting held (Fireflies / Cube disco) → Discovery Completed (`presentationscheduled`)
- Proposal sent (PandaDoc / Gmail) → Proposal Sent (`decisionmakerboughtin`)
- Signed → Signed (`closedwon`)
- Paid (Stripe / payment) → Paid (`3482933986`)

**Back** (explicit signal only — never a default)

- No-show → No Show (`3557889773`)
- Disco flake / never booked after a meeting → Nurture (`3486952153`) — not fake Replied
- Clear not interested → Closed Lost (`closedlost`)

The cycle will not regress a more advanced open stage to Replied or Nurture without that back-signal. If a contact has meeting-held evidence and only a Replied deal exists, it is moved to Discovery Completed.

Fireflies and Cube `*-transcript.docx` always run extract → `merge_contact_props` so relational notes (family, school, hooks) land on the contact. If the transcript clearly states **this deal's** price (monthly retainer, proposal $, package), the cycle PATCHes HubSpot deal `amount` when that field is empty. It never invents an amount and never copies Josh's case-study stats ($2M pipeline, $100K closed, free 10K leads).

Each cycle also **prunes** junk:

- Archives (or closed-lost fallback) deals stuck in Appointment Scheduled (`appointmentscheduled`) with no Calendly / Fireflies / GCal meeting evidence
- Does **not** treat a HubSpot email association as a meeting (that was promoting Replied junk to Discovery Scheduled)
- Cleans leftover `Name - Replied` deal titles when the stage is corrected
- Does not delete contacts that have meeting evidence
- Soft-archives blank / no-identity contacts when that is safe

`circle back` / `next quarter` is a ticker reason only. It does not open a Nurture deal or move Discovery Completed (or later) backward.

```bash
python scripts/prune_junk.py
```

To backfill existing HubSpot gaps:

```bash
python scripts/backfill_contact_fields.py
```

To reprocess recent Fireflies / Cube transcripts onto **existing** HubSpot contacts (notes + empty deal amounts; dry-run by default):

```bash
python scripts/backfill_notes_and_amounts.py
python scripts/backfill_notes_and_amounts.py --apply --days 14
```

To enroll historical 90-day nurture ticker rows (dry-run by default; never emails):

```bash
python scripts/backfill_nurture_ticker.py
python scripts/backfill_nurture_ticker.py --apply
```

## Railway

Start command: `python -m crmbrain cycle`

Recommended cron: `0 12,22 * * *` UTC — 7am and 5pm America/Chicago.

HubSpot is meeting-held/scheduled only. Cold outbound stays in Supabase.

One brief per call, about two hours before, emailed to `joshua@salesglidergrowth.com`. Same shape as the Laura Klein brief. Never a week-ahead dump. Dedup is not hourly-cron-only: a Gmail Sent-folder check stops repeats even if a run fires twice.

Full cycle:

1. Reads today's Cube ACR folder — prefers `*-transcript.docx`
2. Pulls that day's Fireflies
3. Pulls positive SmartLead replies (SalesGlider key only) — ticker only unless already in HubSpot
4. Pulls HeyReach conversations and RVM callbacks — ticker only unless already in HubSpot
5. Creates / updates HubSpot contacts only for meetings booked or held
6. Moves deals only on evidence; never opens Replied from chat/RVM
7. Prunes Appointment Scheduled junk with no meeting evidence
8. Extracts relational notes onto the contact (Fireflies / Cube every cycle, including a notes refresh if the transcript was already processed)
9. Fills empty deal `amount` when the transcript states a retainer / proposal / package price
10. Queues a HeyReach LinkedIn request (campaign 530529) for anyone Josh called, emailed, or talked to on LinkedIn. Missing profile URLs come from the email-waterfall MCP.
11. Enrolls cold leads on a repeating 90-day ticker; Slack gets a draft, nothing sends
12. If a Josh meeting is about two hours out, emails one Laura-style brief to `joshua@salesglidergrowth.com`

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill values
python -m crmbrain cycle
```

## Model

Gemini 2.5 Flash when `GEMINI_API_KEY` is set. Heuristics still run without it.

## Not this job

- Emailing prospects
- Creating HubSpot tasks
- Using the master SmartLead key
