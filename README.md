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

Each cycle also **prunes** junk:

- Archives (or closed-lost fallback) deals stuck in Appointment Scheduled (`appointmentscheduled`) with no Calendly / Fireflies / GCal meeting evidence
- Does not delete contacts that have meeting evidence
- Soft-archives blank / no-identity contacts when that is safe

```bash
python scripts/prune_junk.py
```

To backfill existing HubSpot gaps:

```bash
python scripts/backfill_contact_fields.py
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
8. Extracts personal details onto the contact
9. Queues a HeyReach LinkedIn request (campaign 530529) for anyone Josh called, emailed, or talked to on LinkedIn. Missing profile URLs come from the email-waterfall MCP.
10. Enrolls cold leads on a repeating 90-day ticker; Slack gets a draft, nothing sends
11. If a Josh meeting is about two hours out, emails one Laura-style brief to `joshua@salesglidergrowth.com`

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
