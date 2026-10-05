# Call Kettle

Production-style **AI voice receptionist**: inbound calls, a tool-use agent loop, server-side booking rules, and an owner portal.

Sanitized public snapshot of the engineering — no secrets, no production database, no private docs. Live product: [callkettle.com](https://callkettle.com). Write-up: [samiali38183.github.io/projects/call-kettle.html](https://samiali38183.github.io/projects/call-kettle.html). Code: this repo.

Built by **Sami Ali**. A large share of the implementation, tests, and refactors went through **Claude Code** and **Hermes Agent**, gated by pytest. Details: [docs/AGENT_WORK.md](docs/AGENT_WORK.md).

## Architecture

- **FastAPI** backend (`backend/app/main.py`) exposing Twilio voice webhooks (`webhooks.py`, `twilio_utils.py`) with request signature validation, plus a streaming speech-to-text path (`stream_stt.py`).
- **Claude Haiku tool-use agent loop** (`agent.py`): per-call session, bounded tool iterations per turn, capped reply length, and code-level guards on what the model may claim (for example, it cannot say it booked something unless a booking tool actually succeeded).
- **Tools** (`tools.py`): `check_availability`, `book_appointment`, `find_my_appointments`, `cancel_appointment`, `reschedule_appointment`, `escalate_to_human`. Booking rules (hours, slot grid, lead time, one booking per slot) are enforced server-side, not only in the prompt.
- **YAML configs** (`backend/clients/`), validated by strict Pydantic models (`config.py`, `extra: forbid`). Policy switches decide which tools are offered and are re-checked when a tool is called.
- **SQLite** storage (`storage.py`); isolation is tested in `tests/test_isolation.py`.
- **Owner portal** (`portal.py`, `owner_auth.py`): bookings, callbacks, summaries, transcripts.
- **Notifications and calendar**: email with `.ics` invites, push, optional SMS, iCal busy-time and Google Calendar integration.
- **Cost guards and observability** (`cost_guard.py`, `cost_observability.py`, `qa/`).
- **`marketing/audit.py`**: a consistency and leak scanner that checks public-facing documents against `marketing/facts.json` and flags unsupported claims and private-figure leaks. Some of its input documents are private and not included here, so it will not run to completion in this snapshot.

## What I used (agents + LLMs)

| Piece | Role |
|---|---|
| **Claude Code** | In-repo implementation and refactors against the test suite |
| **Hermes Agent** | Task-driven QA, repo work, orchestration |
| **Claude Haiku** | Runtime call agent (structured tool-use) |
| **pytest + GitHub Actions** | Gate on agent-authored changes |
| **Python 3.12, FastAPI, Twilio, SQLite** | Stack |

The runtime agent is schema-first: the model proposes a tool call; code validates and executes it.

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
