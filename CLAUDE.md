# Claude Code instructions: Call Kettle public snapshot

This is a sanitized public snapshot of Call Kettle (FastAPI + Twilio + Claude Haiku tool-use agent + YAML tenants + SQLite). Treat it as a portfolio and practice repo.

## Rules

- **Never commit `.env`** or any real key, token, phone number, email or database. Use `backend/.env.example` placeholders only.
- **Never fabricate customers**, testimonials, metrics, uptime figures, certifications or revenue. If asked to write marketing copy, describe only what the code in this tree does.
- **Do not add pricing or dollar figures** anywhere (docs, YAML, tests, copy). Private business terms are not part of this snapshot.
- **Keep sample tenants fictional.** Files in `backend/clients/` use invented business names; do not replace them with real businesses, real phone numbers or real addresses.
- **Run tests after every edit:** `cd backend && python -m pytest -q`. Do not report work as done while tests fail; report the failing output instead.
- Do not delete existing code or tests to make a run pass.

## Layout

- `backend/app/agent.py`: per-call session, tool-use loop, claim guards
- `backend/app/tools.py`: booking, availability, escalation tools (server-side rules)
- `backend/app/config.py`: strict Pydantic tenant schema
- `backend/clients/*.yaml`: tenant configs
- `backend/tests/`: pytest suite (the gate)
- `marketing/audit.py`: claim and leak scanner (some inputs are private and absent)
- `docs/AGENT_WORK.md`: how agents were used on this project
