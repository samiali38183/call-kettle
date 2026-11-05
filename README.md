# Call Kettle

**A phone receptionist that picks up when a home-service shop can't, and is built so the model can't improvise.**

It answers the call, books, moves or cancels appointments inside the hours the owner set, takes a message or transfers to the owner, and gives the owner a portal with every booking and transcript. Live product: [callkettle.com](https://callkettle.com), running on Fly.io. This repo is a cleaned-up public copy of the code: no secrets, production data or private docs.

The interesting part isn't the voice, it's the guardrails:

- **The model suggests, the code decides.** Claude proposes tool calls; code validates them and runs them. Hours, slots, lead time and one-booking-per-slot are enforced in Python, not in the prompt.
- **It can't claim what didn't happen.** A caller is never told they're booked unless the booking tool actually succeeded.
- **Tenant isolation is tested.** Each business is a strict, validated config, and tests check that one business can't read another's data.
- **Cost is a first-class concern.** Per-call cost tracking and limits live in the code (`cost_guard.py`, `cost_observability.py`).

It was built with a multi-agent workflow: Hermes Agent and Claude Code work on separate parts of the codebase in parallel, and nothing merges unless the automated test suite (3,400+ tests across the private codebase) passes.

Write-up: [samiali38183.github.io](https://samiali38183.github.io/#also). Built by Sami Ali.

## How it's put together

- FastAPI backend (`backend/app/main.py`) exposing Twilio voice webhooks (`webhooks.py`, `twilio_utils.py`) with request signature validation, plus a streaming speech-to-text path (`stream_stt.py`).
- Claude Haiku agent (`agent.py`): one session per call, a limit on tool calls per turn, capped reply length, and code checks on what the model can say. For example, it can't tell a caller they're booked unless the booking tool actually succeeded.
- Tools (`tools.py`): `check_availability`, `book_appointment`, `find_my_appointments`, `cancel_appointment`, `reschedule_appointment`, `escalate_to_human`. Booking rules (hours, time slots, lead time, one booking per slot) are enforced in code, not just in the prompt.
- Per-business YAML configs (`backend/clients/`), validated with strict Pydantic models (`config.py`, `extra: forbid`). Settings control which tools are offered, and they're checked again when a tool runs.
- SQLite storage (`storage.py`). Tests in `tests/test_isolation.py` check that one business can't see another's data.
- Owner portal (`portal.py`, `owner_auth.py`): bookings, callbacks, summaries, transcripts.
- Notifications and calendar: email with `.ics` invites, push, optional SMS, iCal busy-time and Google Calendar integration.
- Cost limits and cost tracking (`cost_guard.py`, `cost_observability.py`, `qa/`).
- `marketing/audit.py`: checks public-facing docs against `marketing/facts.json` and flags claims that aren't backed up or private numbers that leaked. Some of its input documents are private and not included here, so it will not run to completion in this snapshot.

## Tools

| Piece | Role |
|---|---|
| Claude Code | In-repo implementation and refactors against the test suite |
| Hermes Agent | Task-driven QA, repo work, orchestration |
| Claude Haiku | Runtime call agent (structured tool-use) |
| pytest + GitHub Actions | Gate on agent-authored changes |
| Python 3.12, FastAPI, Twilio, SQLite, Fly.io | Stack and deployment |

The model only suggests tool calls. The code validates them and decides whether to run them.

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

## License

MIT. See [LICENSE](LICENSE).
