# SalesGlider CRM Brain

Daily grunt work for Josh's HubSpot. HubSpot only holds people he has actually engaged. Cold names stay in Supabase.

## What counts as engaged

- Cube ACR cell call (transcript already in Drive — no AssemblyAI)
- Fireflies meeting
- Positive SalesGlider SmartLead reply
- HeyReach / LinkedIn conversation
- Allo conversation Josh actually took
- RVM callback

Gmail updates someone already in HubSpot (PandaDoc, payment, replies). A Josh Calendly booking (SalesGlider Intro, etc.) creates the person if they are missing, then enriches through the waterfall MCP. If waterfall/Supabase is down, LeadMagic fills empty work emails and mobiles only (never overwrites).

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

HubSpot is engaged-only. Cold outbound stays in Supabase.

One brief per call, about two hours before, emailed to `joshua@salesglidergrowth.com`. Same shape as the Laura Klein brief. Never a week-ahead dump. Dedup is not hourly-cron-only: a Gmail Sent-folder check stops repeats even if a run fires twice.

Full cycle:

1. Reads today's Cube ACR folder transcripts
2. Pulls that day's Fireflies
3. Pulls positive SmartLead replies (SalesGlider key only)
4. Pulls HeyReach conversations and RVM callbacks
5. Creates / updates HubSpot contacts and notes
6. Moves deals only on evidence
7. Extracts personal details onto the contact
8. Queues a HeyReach LinkedIn request (campaign 530529) for anyone Josh called, emailed, or talked to on LinkedIn. Missing profile URLs come from the email-waterfall MCP.
9. Enrolls cold leads on a repeating 90-day ticker; Slack gets a draft, nothing sends
10. If a Josh meeting is about two hours out, emails one Laura-style brief to `joshua@salesglidergrowth.com`

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

- Mass-deleting old Appointment Scheduled junk
- Emailing prospects
- Creating HubSpot tasks
- Using the master SmartLead key
