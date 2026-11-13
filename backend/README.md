# Call Kettle: backend

The backend of an AI phone receptionist. It answers inbound calls, talks about the business it is configured for, checks real availability, books appointments, transfers to the owner on request, takes a message when nobody picks up, and gives the owner a dashboard with bookings, callbacks, call summaries and transcripts. A new client is a config file and not new code.

It runs on Fly.io as one always-on machine with a persistent volume for the database. The repo-level [README](../README.md) has the overview, the architecture diagrams and the test results. This file is about running and operating the backend.

## What it does

- Answers in a neural voice (Amazon Polly Neural through Twilio `<Say>`; the default is `Polly.Joanna-Neural`).
- Opens every call with an AI disclosure and a recording notice.
- Books only real slots. The server enforces booking hours, the slot grid and a 30-minute lead time, and the database has `UNIQUE(client_id, slot_start)`, so two callers can't get the same slot.
- Uses caller ID so people don't have to dictate their number, gives dictated numbers longer to finish, and respells phone numbers for speech.
- Transfers to the owner's phone when asked, using a regex backstop plus the model. If nobody answers within 25 seconds, the call returns to the AI, which takes a message and alerts the owner.
- Tells the owner about bookings and callbacks by email (with a calendar invite as an `.ics` attachment), ntfy push, and SMS once it is enabled.
- Writes a 2-3 sentence summary of each call, except for privacy-mode clients, and shows summaries and transcripts in the dashboard.
- If the AI errors in the middle of a call, it rings the owner's phone. The provisioning script also sets a Twilio voice fallback that rings the owner if the whole server is down. That fallback function is not in this repo.
- Limits spend with per-call turn and duration caps, a repeat-caller limit (6 calls in 10 minutes per number), API timeouts and rate limits on public endpoints.

## What it doesn't do

- **No full two-way calendar sync.** Bookings live in Call Kettle's own database and show in the dashboard, and each one is also emailed as a calendar invite. A tenant can connect Google Calendar through a shared service account (free/busy read and event write, `app/gcal.py`) or a private iCal link (busy time, read only, `app/icalbusy.py`). There is no Outlook or Apple integration and no per-owner OAuth.
- **SMS is off until US carrier registration (A2P 10DLC) is approved.** Carriers block SMS from unregistered numbers, so `SMS_ENABLED` defaults to `0` and the AI is told not to promise texts.
- **Not HIPAA-covered.** There are no BAAs with Twilio, Anthropic or Fly here. Healthcare and legal clients can run with `record_transcripts: false`, so their words are not stored or summarized, and the AI is told not to ask for medical details. That reduces exposure and does not make the system HIPAA-compliant.
- **English by default.** Spanish is a beta that a tenant has to turn on with `spanish: true`.
- **Call state is in memory.** Run one machine and one worker. Move sessions to Redis before running a second instance.

## Compliance notes

None of this is legal advice, and a lawyer should review the terms before going live.

- **AI disclosure.** `ClientConfig` rejects any `opening_line` that doesn't say the caller is talking to an AI, so every call discloses it.
- **Recording notice.** Every call opens with "This call may be recorded and monitored for quality" (`main.CALL_DISCLOSURE`). Recording consent rules differ by state, so check the ones that apply to your callers.

## Adding a client

```bash
cd backend
python scripts/onboard_client.py                      # answer the questions -> clients/<id>.yaml
python simulate_call.py <client_id>                   # talk to it first
python scripts/push_config.py <client_id>             # live right away: no deploy, no restart
python scripts/provision_number.py <client_id> --area-code 703          # dry run
python scripts/provision_number.py <client_id> --area-code 703 --buy    # buys the number
python scripts/provision_number.py <client_id> --number +1703...        # or repoint one you own
python scripts/report_link.py <client_id>             # the client's private dashboard URL
```

`provision_number.py` sets the voice webhook, a status callback that closes out calls when the caller hangs up, and a voice fallback that rings the owner if the server is down. Then have the client forward their business line to the new number or publish the new number. Place a test call before going live.

`python scripts/report_link.py --all` lists every dashboard link and the operator page (`/admin?key=<REPORT_KEY>`). Each client's key opens only that client's dashboard.

Optional client fields are listed in `clients/_template.yaml`: `booking_hours`, `owner_email`, `ntfy_topic`, `record_transcripts`, `extra_instructions`.

## Operating it

**Checks.** `python scripts/selfcheck.py` runs live checks against a deployment and prints PASS, WARN or FAIL for each. They cover the server, the database, backups, the AI key, webhook security, each phone number's settings, the ring-the-owner fallback, transfer and 911 handling and private-page security. Run it after a deploy.

**Alerts.** A crash on a live call or a model API failure sends an ntfy push to the operator topic, rate-limited to one per 10 minutes per problem. The caller in that moment is sent to the owner's phone, so nobody hears an error.

**Data.** SQLite lives on the Fly volume. The server writes a consistent copy to `/data/backups/` daily and keeps 7. Call transcripts and summaries are deleted after 90 days, while bookings and call counts stay. `python scripts/export_data.py` saves everything to `Documents\CallKettle-Backups` on your PC. `python scripts/offboard_client.py <id> --do-it` saves a copy, erases one client from the server and archives their config.

**Deploys** restart the single server. For a few seconds calls fall back to ringing the owner's phone, and a call in progress loses its conversation state. Deploy when it's quiet.

**Capacity.** This repo publishes no latency or load figures. `scripts/load_test.py` exists but needs live credentials.

**Rotating secrets.** If you change the Twilio auth token, update `TWILIO_AUTH_TOKEN` on Fly and in the voice fallback project, or webhook signature checks and the fallback will start failing.

**Admin endpoints** need the master key: `/admin` (clients and intakes), `/admin/status` (health with no secrets; add `&deep=1` to test the AI key), `/admin/export`, `/admin/intakes` and `/admin/client/<id>/delete`.

## Configuration (Fly secrets or `.env`)

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | model access |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | telephony and webhook signature checks |
| `TWILIO_FROM_NUMBER` | SMS sender (must be A2P-registered) |
| `REPORT_KEY` | master secret; per-client dashboard keys derive from it |
| `SMS_ENABLED` | set to `1` only after A2P approval |
| `TTS_VOICE` | override the voice without a redeploy, e.g. `Polly.Matthew-Neural` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | booking and callback email |
| `CALLKETTLE_DB_PATH` | SQLite path (`/data/callkettle.db` on Fly) |
| `CALLKETTLE_SKIP_SIGNATURE_CHECK` | `1` for local testing only, never in production |

## Development

```bash
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env
pytest -q                       # offline, no network, no API spend (counts are in the top-level README)
uvicorn app.main:app --reload --port 8000
```

`simulate_call.py <client_id>` runs the real agent from the terminal. It makes real model calls and costs real money.

## Layout

```
app/main.py         webhooks, dashboard, admin, public booking API
app/agent.py        prompt, tools, deterministic transfer detection, turn loop
app/tools.py        availability and booking rules, escalation
app/notify.py       email / ntfy / SMS fan-out, calendar invites
app/summary.py      post-call summaries (background)
app/twilio_utils.py TwiML (neural voice, hints, dictation timeouts), SMS
app/storage.py      SQLite (calls, bookings, escalations)
clients/*.yaml      one file per client
scripts/            onboard_client, provision_number, report_link
```
