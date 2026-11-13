# Architecture

A module-by-module walk-through of the backend. Paths are relative to `backend/`. The README has the overview and the reasons behind the main decisions. This file is meant to sit open next to the code.

This is a snapshot of the application code. Operator scripts that touch production, the Twilio voice-fallback function, the deployment config and private docs are left out. Where a module refers to a design doc under `docs/` that isn't in this repo, I used the module's own docstring.

## 1. Request lifecycle of one call

Default mode: Twilio does the speech recognition (`<Gather input="speech">`) and the server answers each utterance with TwiML.

1. **`POST /voice/incoming`** (`app/main.py: voice_incoming`)
   1. `_validated_form` rebuilds the public URL from the proxy headers and checks `X-Twilio-Signature` via `twilio_utils.validate_signature`. Failure returns 403 before anything else is read.
   2. The tenant is resolved from `?client_id=` through `config.load_client_config` (cached, validated). An unknown id gets a spoken apology, not a 500.
   3. Admission checks, cheapest first: trial window (`trial.admit`), concurrent calls per tenant (`costing.MAX_CONCURRENT_CALLS`), repeat caller limit (`MAX_CALLS_PER_NUMBER_10MIN`), then the monthly spend and call-count ceilings (`costing.status`). A call that fails these gets a `<Reject>` (not answered, so not billed by the carrier) or a degraded mode, never the model.
   4. Routing (`routing_mode`): AI first, owner phone first, or owner first only while open (`_owner_rings_first`). Numbers in `always_ring_owner` always ring the owner.
   5. `_ai_greeting` starts an in-memory `agent.CallSession`, plays the disclosure and opening line, and returns a `<Gather>` whose action is `/voice/gather`.
2. **`POST /voice/gather`** (`_voice_gather_turn`) runs one turn. `_once_per_delivery` uses Twilio's `I-Twilio-Idempotency-Token` so a retried delivery replays the stored TwiML instead of running the model twice. If the process restarted mid-call, `agent.recover_session` rebuilds what the database knows.
3. **`agent.run_turn`** holds a per-call lock, then the loop described in section 2.
4. The reply goes back as `<Say>` inside the next `<Gather>`, or as `<Dial>` (transfer) or `<Hangup>`.
5. **`POST /voice/status`** closes the call record when the caller hangs up, frees the session and queues the post-call summary as a background task.

Failure paths:

- Any unhandled exception on a `/voice/*` route is caught by `_voice_safety_net`, which returns TwiML that dials the owner (or apologises). Twilio never sees a bare 500.
- `POST /voice/transfer-result` handles an unanswered transfer: it records an escalation, then hands the caller back to the agent in a "take a message" state instead of dead air.
- Streaming mode (`/voice/stream`, section 4) falls back to the Gather path on any error.

## 2. The agent (`app/agent.py`)

| Piece | What it does |
|---|---|
| `TOOLS`, `tools_for(config)` | Eight tool schemas. A tool disabled by the tenant's `policy` is not sent to the model at all, and `_dispatch_tool` refuses it again if it is called anyway. `check_availability` is dropped when neither booking nor rescheduling is allowed. |
| `build_system_prompt` | Facts come from the validated config. "Open or closed right now" is computed in code (`_open_status_line`) and handed to the model as a sentence, not left for it to infer from a timestamp. `_policy_rules` turns policy switches into prompt rules. |
| `CallSession` | Message history, caller number, a `done` set of outcomes that really happened (`book`, `reschedule`, `cancel`, `lookup`), turn count, language, cost-evidence counters, and a lock so one call has one turn in flight. |
| `_run_turn` | Order of checks before the model is called: turn/duration limits (`cost_guard.check_limits`), control-character strip and 600-character cap on caller text, emergency regexes (English and Spanish), explicit "get me a person" regex, language detection. Only then does it call the model. |
| The tool loop | At most `cost_guard.MAX_TOOL_ITERATIONS_PER_TURN` (4) model calls per turn, each with `max_tokens=MAX_REPLY_TOKENS` (220), a 6 s client timeout and 1 retry, and a turn budget (`TURN_BUDGET_SECONDS`, 8 s) checked before each further model call, so a slow turn hands off instead of starting another round trip past Twilio's webhook deadline. |
| Claim guard | `claimed_actions`, `unsupported_claims`, `_guard_claims`, `_record_outcome`. See README, "Never claim an action that did not happen". |
| `_fix_dates` + `datefix.py` | Recomputes the weekday for any "Weekday, Month day" the model is about to say and corrects it when wrong. |
| Failure exits | Model API error, malformed response, or loop exhaustion: escalate to the owner, alert the operator (rate-limited), and either transfer the live call to the owner's phone or promise a callback, depending on policy and whether the number is dialable. |
| Versioning | `prompt_version`, `tool_version`, `config_version` fingerprint the prompt template, tool schema and tenant config. They are stored per call (`storage.set_call_versions`) so a behaviour change can be traced to what was deployed. |

Two deterministic backstops sit in front of the model: `_EMERGENCY_RE` (gas, fire, carbon monoxide, medical phrases) and `_HUMAN_REQUEST_RE` (an unambiguous request for a person). `is_identity_question` keeps "am I talking to a robot?" from being treated as a transfer request; the prompt requires an honest "I am an AI receptionist" answer.

### `app/cost_guard.py`

Per-turn and per-call limits, independent of the model: `MAX_TOOL_ITERATIONS_PER_TURN`, `MAX_HISTORY_MESSAGES`, `check_limits` (`max_turns`, `max_call_seconds` from the tenant config), `trim_history` and `repair_history`. `trim_history` only cuts on a caller's own words, never between a tool call and its result. `repair_history` guarantees every `tool_use` has a matching `tool_result`, drops orphans and empty messages, and runs before every model call and again in a `finally` after every turn, because the model API rejects an invalid history on every later turn of that call.

## 3. Tools and business rules (`app/tools.py`)

Every rule here runs on the server regardless of what the model said.

- `check_availability`: bookable windows come from `config.effective_booking_hours` (a tenant may answer 24/7 but only book in office hours), a slot grid of `slot_minutes`, a 30-minute minimum lead time, the service's own duration (a two-hour job needs two free hours and must end before closing), existing bookings from `storage.get_booked_intervals`, and busy time from connected calendars.
- `book_appointment`: re-validates everything the model proposed. Order: required name, date/time format, slot is on the grid, not in the past or inside the lead time, phone has at least 10 digits, service is one the tenant configured (name matched and duration taken from config), job fits before closing, slot still free on the owner's calendars (fresh read), per-call cap of 3 bookings. Then `storage.create_booking`.
- `find_my_appointments`, `cancel_appointment`, `reschedule_appointment`: ownership is checked by `_owned`. A caller can touch a booking only if it was made in this same call or with the phone number they are calling from (caller ID), never one they merely state.
- `escalate_to_human`: records the callback and notifies the owner through every channel independently. Reason, summary and phone are cleaned (`clean_text`); the summary is also passed through `redact.scrub_sensitive`. A caller ID the owner marked as spam does not page the owner, but a possible emergency always does.
- `_best_effort` wraps every side effect after a booking is saved (calendar write, SMS, owner notification). A failure there is logged and counted in `metrics`, and never turns a saved booking into a reported failure.
- If the database write itself fails, `book_appointment` returns an error that tells the model not to say it is booked, alerts the operator, and sends the owner the caller's details so the lead is not lost.

## 4. Telephony helpers

- `app/twilio_utils.py`: signature validation (`RequestValidator`; the check is skipped only when `CALLKETTLE_SKIP_SIGNATURE_CHECK=1`, and `admin/status` reports whether it is on), TwiML builders for gather, say, dial, reject, whisper, and the streaming variants, and phone-number-to-speech conversion. `is_dialable` guards against dialing a malformed number.
- `app/callhours.py`: counts calls inside and outside the tenant's own configured hours.
- `app/stream_stt.py` (optional, off by default): Twilio Media Streams audio into a streaming speech-to-text provider, with the provider interface, an HMAC token that binds a stream to one call and tenant, an end-of-utterance policy, and a kill switch. A tenant streams only if its config sets `stt_mode: stream` and the global env flag is on and the provider is configured. Any failure redirects the call to the Gather path. Tests drive it with a fake connection and recorded audio fixtures in `tests/fixtures/stream_stt/`.

## 5. Configuration (`app/config.py`)

- `ClientConfig` is the tenant: identity, hours, services, FAQs, escalation number, call limits, notification channels, calendar settings, routing mode, ceilings, webhook target.
- `Policy` is a separate model with `extra: forbid`: the typed switches for what the assistant may do (book, reschedule, cancel, transfer, quote prices, state a dispatch fee, collect an address, after-hours behaviour, emergency behaviour). A misspelled policy key is a validation error.
- Validators: hours are normalised to `HH:MM` with `mon..sun` keys and refused if opening is not before closing; the timezone must be a real IANA zone; `opening_line` must disclose that the caller is talking to an AI; `webhook_url` must be https with a secret of at least 16 characters; the iCal URL must be https; demo-only fields are rejected on a non-demo tenant.
- Loading: `load_client_config` is `lru_cache`d and reads `clients/<id>.yaml`. `client_id` must match `[A-Za-z0-9_]{1,60}` before it is turned into a file name, so it cannot walk out of the folder. `save_live_config` validates, writes atomically (`os.replace`) into a volume-backed folder (`CALLKETTLE_CLIENTS_DIR`) and clears the cache, so a new tenant is live without a redeploy.
- `app/certify.py` is a pure lint over a config (loops in transfer numbers, missing hours, fictional numbers) that returns BLOCKED, READY WITH WARNINGS or READY; `scripts/certify_client.py` adds the live part.

Note on strictness: `extra: forbid` is set on `Policy`, not on `ClientConfig` itself, so an unknown top-level key in a tenant YAML is currently ignored rather than rejected. See Limitations in the README.

## 6. Storage (`app/storage.py`)

SQLite in WAL mode, one file. Tables include `calls`, `bookings`, `cancelled_bookings`, `booking_events`, `metrics`, `escalations`, `tool_audit`, `intakes`, `private_demos`, `spam_numbers`, `owner_users`, `owner_sessions`, `owner_login_failures`.

- Double-booking protection is two layers. `bookings` has `UNIQUE(client_id, slot_start)`, and `create_booking` takes `BEGIN IMMEDIATE` and rejects any confirmed booking whose interval overlaps the new one, which catches a 60-minute job at 09:00 against a 09:30 request that the unique index alone would miss. `move_booking` applies the same overlap check.
- Live-call writes (`log_turn`, outcome, versions, metrics) use a short busy timeout (`LIVE_BUSY_SECONDS`) and a breaker (`live_path_open`, `trip_live_breaker`), so a locked database costs a measurement rather than holding a caller on the line.
- `booking_events` is an append-only history of created, moved and cancelled bookings with the call that did it.
- `record_transcripts: false` tenants store a placeholder instead of caller words.

`app/frontdesk_storage.py` adds the owner follow-up workspace tables on top, initialised after the core schema and never at import time.

## 7. Cost controls and observability

These are two separate mechanisms, and they are not coupled on purpose.

- Enforcement (`app/costing.py`, `app/usage_trigger.py`, `app/ops.py: check_cost_levels`): builds an estimate of a tenant's month from what each call recorded (model tokens, characters spoken, minutes, recognitions) and compares it to a per-tenant ceiling. Operator warnings at 75, 90 and 100 percent. At 100 percent new calls go to `ceiling_mode`: take a message with no model call, or ring the owner. After a further cap of degraded calls, `voice_incoming` returns `<Reject>`. The module says plainly that this is an estimate and a smoke alarm, not an invoice. `usage_trigger.py` receives Twilio account-level usage trigger callbacks (signature verified, idempotent) as a second alarm.
- Observability (`app/cost_observability.py`, `scripts/cost_report.py`, `scripts/reconcile_costs.py`): a separate ledger of measured provider evidence (token counts from the API's own usage fields, Twilio-reported seconds, counts of TwiML emitted). It never writes an unknown value as zero, records the source of each number, and does not enforce anything. Writes happen through `agent.observe`, which never raises and is skipped if the database is busy.
- `qa/token_benchmark.py` replays the real `agent.run_turn` against a scripted fake client and measures request size, so a prompt change can be compared before and after. It estimates tokens as characters divided by four and says so in its report. `qa/margin_benchmark.py` replays scripted turns offline. Neither contacts a model or measures voice quality.

## 8. Calendars

- `app/gcal.py`: optional Google Calendar integration through one service account. The owner shares their calendar with the service account's address. Reads free/busy before offering slots and re-checks right before booking; writes the event after the booking is saved, in a background thread. Fails soft: a Google error means the assistant falls back to its own schedule.
- `app/icalbusy.py`: reads the owner's private iCal feed as busy time. Read only, no login, no write path. All-day events are ignored unless `calendar_all_day_blocks` is set. Recurrence and time zones are expanded with `recurring-ical-events` and `x-wr-timezone`. It is availability awareness, not two-way sync.
- Bookings reach an owner's calendar through the `.ics` invite attached to the owner email (`notify.build_ics`), with `CANCEL` and `REQUEST` methods and sequence numbers for changes.

## 9. Notifications and outbound events

- `app/notify.py`: `notify_owner` fans out to SMS (only when `SMS_ENABLED=1`), email, and ntfy push, each independent; none can raise into a live call. Email goes through `app/mailer.py`.
- `app/mailer.py`: one place that sends and records every transactional email (`email_log`), supports SMTP and two API providers, suppresses addresses that hard-bounce or complain, and honours idempotency keys so a repeated key never sends twice. Provider events are accepted at `/email/events/{provider}`.
- `app/leadcard.py`: the one-screen text of an owner alert. The urgent flag comes from the reason code the system logged, never from the caller's words. Missing facts are written as missing.
- `app/webhooks.py`: outbound signed webhooks for tenants that configure `webhook_url`. A transactional outbox in SQLite (`webhook_outbox`), a background worker started in the app lifespan, retries with growing delays over about 31 hours, stable event ids for receiver-side de-duplication, HMAC-SHA256 signatures with a timestamp, https only, public addresses only (re-checked before every attempt), no redirects, bounded response size. It is not an inbound integration with any third-party system.
- `app/digest.py`: Monday email recap built only from database counts.
- `app/summary.py`: post-call summary written off the call path; skipped for tenants that do not record transcripts.

## 10. Owner portal and operator surfaces

- `app/portal.py`: server-rendered HTML with no JavaScript and no external requests. The tenant comes from the signed-in session and from nothing else; no URL or form field selects it, and every query is parameterised with the session's `client_id`. Pages: overview with a needs-attention queue, calendar, calls, settings, help, CSV exports (with spreadsheet-formula neutralisation).
- `app/owner_auth.py`: one account is bound to one tenant. scrypt password hashing with a per-user salt, constant-time compare, forced change of the temporary password, lockout after repeated failures stored in SQLite, the same response for unknown and locked accounts, random session tokens stored only as hashes, idle and absolute expiry, per-session CSRF token.
- `app/owner_activation.py`, `app/admin_onboarding.py`, `app/onboarding.py`, `app/provisioning.py`: operator-led onboarding. An intake form becomes a config, the operator reviews it, a phone number is attached (Twilio client injected so tests never hit the network), and the owner activates their account through a one-time invitation.
- `app/frontdesk.py`: owner follow-up workspace (notes and handled state on calls), with stale-form detection so an old browser tab cannot overwrite a newer note.
- `app/spamtag.py`: owner marks a caller ID as not a customer; later calls from it stop paging the owner. Scoped by tenant; emergencies are never suppressed.
- `app/outcomes.py`: classifies each call from recorded facts (booking saved, escalation reason, how the call ended), never from the model's own opinion.
- `/admin/*` routes need the master key; the interactive API docs are disabled; `SecurityHeadersMiddleware` sets frame, referrer and content-type headers.

## 11. Operations

- `app/ops.py`: operator alerts (rate-limited per problem), log filters that redact keys and personal data, 90-day transcript purge (bookings and counts stay), consistent database copies made from the housekeeping loop and checked by a restore test (7 kept), Twilio balance check, demo data reset, housekeeping, all run from a loop started in the app lifespan.
- `app/offsite.py`: encrypted offsite backups. Hybrid scheme: a random AES-256-GCM key encrypts the backup and is wrapped with an RSA public key, so the server can write backups but not read old ones. S3-compatible destination with SigV4 signing.
- `app/redact.py`: card-number and SSN scrubbing applied to stored transcripts and escalation summaries.
- `app/billing.py`: Stripe webhook receiver with signature verification and replay protection that records subscription state for the operator. It does not switch a phone line off on a lapsed payment; that is an operator decision by design.
- `scripts/selfcheck.py`: live checks against a running deployment. `scripts/load_test.py`, `scripts/live_*_check.py`: live and load tooling that needs real credentials and is not part of the offline test suite.
- `.github/workflows/ci.yml` runs the offline suite on Python 3.12. `.github/workflows/heartbeat.yml` checks the public site and `/health` every six hours.

## 12. Test layout

See the Tests section of the README for counts. By area:

| Area | Files |
|---|---|
| Agent loop and failure handling | `test_agent.py`, `test_failures.py`, `test_resilience_chaos.py`, `test_history_integrity.py`, `test_hardening.py` |
| Claim guard | `test_truth_invariant.py` |
| Booking rules and tools | `test_tools.py`, `test_changes.py`, `test_storage.py` |
| Tenant isolation | `test_isolation.py`, `test_owner_portal.py`, `test_frontdesk*.py` |
| Cost | `test_cost_guard.py`, `test_cost_observability.py`, `test_cost_wiring*.py`, `test_token_budget.py`, `test_ceiling.py` |
| Telephony and streaming | `test_twilio_utils.py`, `test_stream_stt*.py` |
| Calendars | `test_icalbusy.py` |
| Outbound events | `test_webhooks.py`, `test_webhook_event_ids.py`, `test_webhook_contract.py` |
| Config | `test_config.py`, `test_certify.py` |

`tests/conftest.py` pins the clock to a fixed Monday morning so slot logic is deterministic, disables push notifications, and waits for notification worker threads at the end of each test so a background thread cannot leak into the next test's database.
