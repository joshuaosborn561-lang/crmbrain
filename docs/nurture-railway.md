# #nurture Slack interactivity (separate Railway service)

The weekday cron (`python -m crmbrain cycle` in `railway.toml`) stays unchanged.
Slack button clicks need a public HTTPS endpoint, so deploy **one extra service**
in the same Railway project (`crmbrain`) and the same environment (`production`).

## Slack app

- App id: `A0AS2JUNFM3` (renamed **Nurture**)
- Bot: `B0AT08NHMMW`
- Channel: `#nurture` `C0BHBDTMRFY`
- Reuse the existing bot token (`SLACK_BOT_TOKEN`)

Interactivity Request URL:

```
https://<this-service-public-domain>/slack/interactions
```

Event Subscriptions are not required. Only Interactivity + a bot that can post
in `#nurture`.

## Railway

1. In project **crmbrain**, environment **production**, add a new service
   (do not edit the existing cron service).
2. Same GitHub repo / same branch the cron uses.
3. **Start command** (no cron schedule on this service):

```
uvicorn crmbrain.nurture_app:app --host 0.0.0.0 --port $PORT
```

4. Railway assigns a public domain. Paste `https://<domain>/slack/interactions`
   into the Slack app Interactivity Request URL and save.
5. Copy env vars from the cron service, then add:

| Variable | Default | Purpose |
|---|---|---|
| `SLACK_SIGNING_SECRET` | (required) | HMAC-SHA256 `v0` verification |
| `SLACK_BOT_TOKEN` | existing | `chat.postMessage` / `chat.update` / `views.open` |
| `SLACK_NURTURE_CHANNEL` | `C0BHBDTMRFY` | #nurture |
| `NURTURE_POST_ENABLED` | off | Cron posts Block Kit cards when `1` |
| `NURTURE_SEND_ENABLED` | off | Approve/Edit actually send Gmail when `1` |
| `NURTURE_MAX_PER_WEEKDAY` | `5` | Live fire cap (Chicago weekdays) |
| `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` | existing | Send as `joshua@salesglidergrowth.com` |
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | existing | Ticker claim / cooldown |
| `CRMBRAIN_MANUAL_FREEZE_AT` | existing | Send/post respect the freeze |

Leave `NURTURE_SEND_ENABLED` and `NURTURE_POST_ENABLED` unset (off) until Josh
approves a dry-run. With posting off, the cycle writes would-be cards to
`report.nurture_cards` and `artifacts/nurture_sample_cards.json`.

The cron service must **not** get this start command. Its `railway.toml` remains:

```
startCommand = "python -m crmbrain cycle"
cronSchedule = "0 12,22 * * *"
```

## Health

`GET /health` → `{"ok":"nurture"}`

## Scopes

See `docs/nurture-scopes.md`.
