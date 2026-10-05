# Call Kettle: backend

An AI phone receptionist. It answers inbound calls, holds a natural conversation
about the business it's configured for, checks real availability, books
appointments, transfers to the owner on request, takes messages when nobody
picks up, and gives the owner a dashboard with bookings, callbacks, call
summaries and transcripts. A new client is a config file, not new code.

Production: `https://app.callkettle.com` (Fly.io, one always-on machine, 1 GB
encrypted volume with automatic daily snapshots).

## What it does today (and what it doesn't)

**Does**

- Answers 24/7 in a neural voice (Amazon Polly Neural via Twilio `<Say>`).
- Opens every call with an AI disclosure and a recording notice.
- Books only real slots: server-side enforcement of booking hours, slot grid, a
  30-minute lead time, and a database `UNIQUE(client_id, slot_start)` so two
  callers can never get the same slot.
- Uses caller ID so people don't have to dictate their number; gives dictated
  numbers longer to finish speaking; respells phone numbers for speech.
- Transfers to the owner's phone when asked (deterministic regex backstop plus
  the model). If nobody answers within 25 seconds it returns to the AI, takes a
  message, and alerts the owner. It never dead-ends.
- Notifies the owner of bookings and callbacks by email (with a calendar
  invite, `.ics`), ntfy push, and SMS once enabled.
- Writes a 2-3 sentence summary of every call (skipped for privacy-mode
  clients) and shows summaries and transcripts in the dashboard.
- If the AI itself errors mid-call, it rings the owner's phone. If the whole
  server is unreachable, Twilio's voice fallback (`../fallback`, hosted on
  separate infrastructure) rings the owner.
- Protects margin: per-call turn and duration caps, a repeat-caller limit
  (6 calls / 10 min per number), API timeouts, and public-endpoint rate limits.

**Does not**

- **No live Google/Outlook calendar API sync.** Bookings live in Call Kettle's own
  database and show in the dashboard; each one is also emailed as a calendar
  invite. True two-way sync would need per-client OAuth, so it isn't offered.
- **SMS is off until A2P 10DLC registration is approved.** Twilio silently
  blocks US SMS from unregistered numbers (error 30034), so `SMS_ENABLED`
  defaults to `0` and the AI is told not to promise texts. Register, then
  `fly secrets set SMS_ENABLED=1`.
- **Not HIPAA-covered.** Twilio (BAA only on Security/Enterprise editions),
  Anthropic (BAA on the first-party API, subject to approval, and "covered
  models" require 30-day retention) and Fly are not under BAAs here. Healthcare
  and legal clients run with `record_transcripts: false` (words are not stored
  or summarized) and the AI is told never to solicit medical details. That
  reduces exposure; it does not make the system HIPAA-compliant. Don't sell to
  businesses that handle PHI as a core workflow until BAAs are in place.
- English only.
- Call state is in memory: one machine, one worker. Move sessions to Redis
  before running more than one instance.

## Compliance notes (checked September 2026)

- **AI disclosure:** Maine's Chatbot Disclosure Act (in force Sept 2025)
  requires it when a consumer could be misled; Utah requires it on request
  (proactively for licensed professions). California SB 243 and Washington
  HB 2225 target companion chatbots and exclude customer-service bots.
  Call Kettle discloses on every call regardless; `ClientConfig` rejects any
  `opening_line` that doesn't say it's an AI.
- **Recording consent:** Virginia and DC are one-party; **Maryland is
  all-party** (as are CA, DE, FL, IL, MA, MT, NV, NH, PA, WA). Every call opens
  with "This call may be recorded and monitored for quality" (`main.CALL_DISCLOSURE`).
- None of this is legal advice. Have a lawyer review the service agreement
  before going live.

## Adding a config (about 15 minutes)

```bash
cd backend
python scripts/onboard_client.py                      # answer ~10 questions -> clients/<id>.yaml
python simulate_call.py <client_id>                   # talk to it first
python scripts/push_config.py <client_id>             # live in a second: no deploy, no restart
python scripts/provision_number.py <client_id> --area-code 703          # dry run
python scripts/provision_number.py <client_id> --area-code 703 --buy    # buys the number (~$1.15/mo)
python scripts/provision_number.py <client_id> --number +1703...        # or repoint one you own
python scripts/report_link.py <client_id>             # the client's private dashboard URL
```

`provision_number.py` sets the voice webhook, a status callback (closes out
calls when the caller hangs up) and a voice fallback that rings the owner if
the server is down. Then have the client forward their business line to the
new number (or publish the new number). Place a test call before going live.

`python scripts/report_link.py --all` lists every dashboard link plus the
operator page (`/admin?key=<REPORT_KEY>`). Each client's key opens only that
client's dashboard.

Optional client fields (see `clients/_template.yaml`): `booking_hours`,
`owner_email`, `ntfy_topic`, `record_transcripts`, `extra_instructions`.

## Operating it

**Check everything, any time:** `python scripts/selfcheck.py` runs ~20 live checks (server,
database, backups, AI key, webhook security, every phone number's settings, the
ring-the-owner fallback, transfer and 911 handling, private-page security, and the sales
documents) and prints PASS / WARN / FAIL. Run it before selling and after any deploy.

**You are told when something breaks.** A crash on a live call or a model-API failure sends
an ntfy push to the operator topic (rate-limited to one per 10 minutes per problem). The
caller in that moment is sent to the owner's phone, so nobody hears an error.

**Data:** SQLite on an encrypted Fly volume. Fly snapshots the volume daily; the server also
writes a consistent copy to `/data/backups/` daily (keeps 7). Call transcripts and summaries
are deleted automatically after 90 days (bookings and call counts stay). `python
scripts/export_data.py` saves everything to `Documents\CallKettle-Backups` on your PC.
`python scripts/offboard_client.py <id> --do-it` saves a copy, erases one client from the
server, and archives their config (the terms promise deletion on request).

**Deploys** restart the single server, so for a few seconds calls fall back to ringing the
owner's phone, and a call in progress loses its conversation state. Deploy when it's quiet.

**Capacity** (load-tested on production): 30 simultaneous callers, AI replies under 4 s for
95% of turns, worst case 5 s (Twilio allows 15 s), zero failures. One 512 MB machine is
plenty for this stack; move call state to Redis before running a second machine.

**Secrets to rotate together:** if you ever change the Twilio auth token, update it on Fly
(update the Fly secret `TWILIO_AUTH_TOKEN` and the fallback project on Vercel) or
webhook signatures and the fallback will start failing.

**Admin endpoints** (master key only): `/admin` (clients and intakes), `/admin/status`
(health, no secrets; add `&deep=1` to test the AI key), `/admin/export`,
`/admin/intakes`, `/admin/client/<id>/delete`.

## Configuration (Fly secrets / `.env`)

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | model access |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | telephony, webhook signature checks |
| `TWILIO_FROM_NUMBER` | SMS sender (must be A2P-registered) |
| `REPORT_KEY` | master secret; per-client dashboard keys derive from it |
| `SMS_ENABLED` | `1` only after A2P approval |
| `TTS_VOICE` | override the voice without a redeploy, e.g. `Polly.Matthew-Neural` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | booking/callback email (Gmail: smtp.gmail.com, 587, an app password) |
| `CALLKETTLE_DB_PATH` | SQLite path (`/data/callkettle.db` on Fly) |
| `CALLKETTLE_SKIP_SIGNATURE_CHECK` | `1` for local testing only, never in production |

## Development

```bash
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env
pytest -q                       # ~150 tests, no network, no API spend
uvicorn app.main:app --reload --port 8000
```

`simulate_call.py <client_id>` runs the real agent from the terminal (real
model calls, real cost).

## Layout

```
app/main.py         webhooks, dashboard, admin, public booking API
app/agent.py        prompt, tools, deterministic transfer detection, turn loop
app/tools.py        availability + booking rules, escalation
app/notify.py       email / ntfy / SMS fan-out, calendar invites
app/summary.py      post-call summaries (background)
app/twilio_utils.py TwiML (neural voice, hints, dictation timeouts), SMS
app/storage.py      SQLite (calls, bookings, escalations)
clients/*.yaml      one file per client
scripts/            onboard_client, provision_number, report_link
../fallback/        Vercel function: rings the owner if this server is down
```
