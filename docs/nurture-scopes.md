# Nurture send / Slack scopes

## Gmail

Sending a 1:1 thread reply uses `users.messages.send` with `threadId` plus
`In-Reply-To` / `References` MIME headers.

- **Required:** `https://www.googleapis.com/auth/gmail.send`
- **Not required:** `https://www.googleapis.com/auth/gmail.modify`

`gmail.send` is enough to send (including into an existing thread).
`gmail.modify` also lets the app change labels / trash; this code never does that.

The existing refresh token was granted for calendar/drive/gmail **readonly**.
`python -m crmbrain google-scopes` now lists `gmail.send`. If that line is
`MISSING`, re-consent the Josh Gmail OAuth client with `gmail.send` added
(keep readonly scopes) and store the new `GMAIL_REFRESH_TOKEN`.

This environment has no live OAuth secret, so the token's current grant cannot
be inspected here. Check production with `python -m crmbrain google-scopes`.

## Slack bot (`B0AT08NHMMW` / app `A0AS2JUNFM3`)

| Scope | Needed? | Why |
|---|---|---|
| `chat:write` | **yes** | `chat.postMessage` and `chat.update` in `#nurture` |
| `chat:write.public` | no | Bot is already in `C0BHBDTMRFY` |
| `users:read.email` | no | We never look up Slack users |
| (none extra for modals) | — | `views.open` uses the bot token + Interactivity URL |

Also set the Interactivity Request URL to the Railway nurture service
`POST /slack/interactions` and keep `SLACK_SIGNING_SECRET` on that service.
