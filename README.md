# Call Kettle

An AI phone receptionist for small businesses. It answers calls, books and changes appointments, and gives the owner a portal to see bookings and call transcripts.

This is a cleaned-up public copy of the code, with no secrets, production data or private docs. Live product: [callkettle.com](https://callkettle.com). Write-up: [samiali38183.github.io/projects/call-kettle.html](https://samiali38183.github.io/projects/call-kettle.html). Code: this repo.

Built by Sami Ali.

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
| Python 3.12, FastAPI, Twilio, SQLite | Stack |

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
