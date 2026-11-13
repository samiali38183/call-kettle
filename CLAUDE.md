# Claude Code instructions: Call Kettle public snapshot

This is a sanitized public snapshot of Call Kettle (FastAPI, Twilio, a Claude Haiku tool-use agent, YAML tenants, SQLite). Treat it as a portfolio and practice repo.

## Rules

- Never commit `.env` or any real key, token, phone number, email or database. Use the placeholders in `backend/.env.example`.
- Don't make up customers, testimonials, metrics, uptime figures, certifications or revenue. If you write marketing copy, describe only what the code in this tree does.
- Don't add pricing or dollar figures anywhere (docs, YAML, tests, copy). Private business terms are not part of this snapshot.
- Keep the sample tenants fictional. Files in `backend/clients/` use invented business names, and they must not be replaced with real businesses, phone numbers or addresses.
- Run the tests after every edit with `cd backend && python -m pytest -q`. Don't report work as done while tests fail; report the failing output instead.
- Don't delete code or tests to get a passing run.

## Layout

- `backend/app/agent.py`: per-call session, tool-use loop, claim guards
- `backend/app/tools.py`: booking, availability and escalation tools (server-side rules)
- `backend/app/config.py`: Pydantic tenant schema (only `Policy` is `extra: forbid`)
- `backend/clients/*.yaml`: tenant configs
- `backend/tests/`: the pytest suite
- `marketing/pricing_audit.py`: checks public text for a configured private price
- `docs/AGENT_WORK.md`: how the coding agents were used on this project
