# Call Kettle

Call Kettle is an AI voice receptionist for small service businesses (HVAC, plumbing, dental, auto, cleaning and similar). It answers inbound phone calls, holds a conversation about the business it is configured for, checks real availability, books, reschedules and cancels appointments, transfers to the owner on request, takes messages when nobody picks up, and gives the owner a portal with bookings, callbacks, call summaries and transcripts. Onboarding a new business is a YAML config file, not new code.

- Live marketing site: https://callkettle.com
- Live app: https://app.callkettle.com
- Portfolio write-up: https://samiali38183.github.io/projects/call-kettle.html

**This repository is a sanitized public snapshot.** It contains no secrets, no real customer data, no production database and no private sales documents. See [Security note](#security-note).

Founder and engineer: Sami Ali. Built with a mix of hands-on engineering and LLM coding agents (Claude Code, Hermes Agent). See [docs/AGENT_WORK.md](docs/AGENT_WORK.md).

## Architecture

- **FastAPI** backend (`backend/app/main.py`) exposing Twilio voice webhooks (`webhooks.py`, `twilio_utils.py`) with request signature validation, plus a streaming speech-to-text path (`stream_stt.py`).
- **Claude Haiku tool-use agent loop** (`agent.py`): per-call session, bounded tool iterations per turn, capped reply length, and code-level guards on what the model may claim (for example, it cannot say it booked something unless a booking tool actually succeeded).
- **Tools** (`tools.py`): `check_availability`, `book_appointment`, `find_my_appointments`, `cancel_appointment`, `reschedule_appointment`, `escalate_to_human`. Booking rules (hours, slot grid, lead time, one booking per slot) are enforced server-side, not only in the prompt.
- **YAML tenant configs** (`backend/clients/`), validated by strict Pydantic models (`config.py`, `extra: forbid`). Per-tenant policy switches decide which tools are offered to the model and are re-checked when a tool is called.
- **SQLite** storage (`storage.py`) with a `UNIQUE(client_id, slot_start)` constraint so two callers cannot receive the same slot; tenant isolation is tested in `tests/test_isolation.py`.
- **Owner portal** (`portal.py`, `owner_auth.py`): bookings, callbacks, summaries, transcripts.
- **Notifications and calendar**: email with `.ics` invites, push, optional SMS, iCal busy-time and Google Calendar integration.
- **Cost guards and observability** (`cost_guard.py`, `cost_observability.py`, `qa/`).
- **`marketing/audit.py`**: a consistency and leak scanner that checks public-facing documents against `marketing/facts.json` and flags unsupported claims and private-figure leaks. Some of its input documents are private and not included here, so it will not run to completion in this snapshot.

## How agents were used

- **Claude Code** wrote and refactored much of the backend alongside hand-written code, working against the existing test suite.
- **Hermes Agent** was used for task-driven work such as scripted QA passes and repo chores.
- **pytest is the gate**: agent-authored changes are accepted only when the suite passes (about 95 test files under `backend/tests/`, including isolation, concurrency, hardening and regression tests).
- **Audit as a second gate**: `marketing/audit.py` catches claims the product cannot back up and leaks of private figures before anything is published.

Details, including what to read first, are in [docs/AGENT_WORK.md](docs/AGENT_WORK.md).

## Sample tenants (fictional)

The snapshot includes 10 fictional sample tenant configs so hiring managers and LLM agents can exercise multi-tenant isolation locally. All business names are invented.

| File | Vertical |
|---|---|
| `backend/clients/sample_auto.yaml` | Auto repair |
| `backend/clients/sample_cleaning.yaml` | Cleaning |
| `backend/clients/sample_dental.yaml` | Dental |
| `backend/clients/sample_electrical.yaml` | Electrical |
| `backend/clients/sample_garage.yaml` | Garage doors and gates |
| `backend/clients/sample_hvac_east.yaml` | HVAC |
| `backend/clients/sample_hvac_north.yaml` | HVAC |
| `backend/clients/sample_lawn.yaml` | Lawn care |
| `backend/clients/sample_plumbing.yaml` | Plumbing |
| `backend/clients/sample_salon.yaml` | Salon |

Also present: `sample_portal_hvac.yaml` (a seeded owner-portal demo fixture), `demo_*.yaml` (demo-line fixtures), `callkettle_*.yaml` (the product's own demo and sales-line configs) and `_template.yaml` (a blank starting point). None represent real customers.

## Run locally

Requires Python 3.12 (as used in CI).

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # then fill in values locally; never commit .env
uvicorn app.main:app --reload
```

`.env.example` lists `ANTHROPIC_API_KEY`, the Twilio credentials, `CALLKETTLE_DB_PATH` and `LOG_LEVEL`. `CALLKETTLE_SKIP_SIGNATURE_CHECK=1` is for local development only. `backend/simulate_call.py` can drive a conversation without a phone line; it needs an Anthropic key.

## Tests

```bash
cd backend && python -m pytest -q
```

## Security note

The production repository stays private. This snapshot exists for portfolio review and for agent practice. It contains no credentials, no production database, no real caller or customer data, and no private sales or pricing documents. Please do not submit issues or PRs containing secrets or personal data.

## License

MIT. See [LICENSE](LICENSE).
