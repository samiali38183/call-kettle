# Agent workflow

Call Kettle was built by Sami Ali with a mix of hand-written code and LLM coding agents (Claude Code, Hermes Agent). This note describes the workflow and the product's own agent design, and points a reviewer at the code worth reading. This repo is a sanitized snapshot; production stays private.

## 1. The product's tool-use loop

`backend/app/agent.py` runs one conversation per call:

1. A `CallSession` holds history for a call SID. The system prompt is built per tenant and per moment (`build_system_prompt`): business facts, policy rules, and whether the business is open right now.
2. Each caller utterance is sanitized (control characters stripped, length capped) and sent to Claude Haiku with a tool list from `tools_for(config)`. A tool the tenant has disabled is not offered at all.
3. While the response stops with `tool_use`, the loop executes the tool via `_dispatch_tool`, appends a `tool_result`, and calls the model again. Iterations per turn are bounded by `cost_guard.MAX_TOOL_ITERATIONS_PER_TURN`, and replies are capped by `MAX_REPLY_TOKENS`.
4. Before text is spoken, guards run: `unsupported_claims` / `_guard_claims` stop the agent from saying it booked, cancelled or transferred unless the matching tool succeeded, and `_fix_dates` corrects spoken dates.

The design principle is that the model proposes and the code disposes. Anything with consequences is enforced in code; prompt-only rules are treated as weaker.

## 2. Schema validation

`backend/app/config.py` defines the tenant schema with Pydantic (`extra: forbid`, field and model validators). A typo in a YAML file fails at load time rather than mid-call. Per-tenant policy switches (`can_book`, `can_reschedule`, `can_cancel`, transfer) gate tools both when offering them and when they are called. Wording switches (for example quotes or fees) are prompt rules and are only as strong as the model; the code comments say so.

Tool inputs are checked again in `tools.py` (`clean_text`, slot parsing, ownership checks on existing bookings by call or caller ID) so a bad model argument cannot reach storage.

## 3. Tests as the gate

Agent-written changes are accepted when `cd backend && python -m pytest -q` passes. The suite (about 95 files) is organized around failure modes as well as happy paths:

- `test_agent.py`, `test_failures.py`: loop behavior, model errors, fallback to the owner's phone
- `test_isolation.py`: one tenant cannot read or book into another's data
- `test_frontdesk_concurrency.py`, `test_hardening.py`, `test_history_integrity.py`: races, malformed input, stored history consistency
- `test_config.py`: schema rejects bad tenant files
- `*_regressions.py` files: each pins a bug that was found and fixed

CI (`.github/workflows/ci.yml`) runs the same command on Python 3.12. When an agent fixes a bug, the expected pattern is a failing test first, then the fix.

## 4. Leak and claim audit

`marketing/audit.py` checks public-facing documents against `marketing/facts.json` and flags claims the product cannot back up; it also calls `marketing/pricing_audit.py` to scan for leaks of private figures. It is a second gate for agent-written copy. In this snapshot some of its source documents are intentionally absent, so read it as code rather than expecting a clean run.

## 5. Where agents helped

Agents were most useful for test scaffolding, refactors under an existing suite, regression tests for reported bugs, and repo chores. Product decisions, the policy model (what the AI may and may not do on a call), and review of anything touching bookings or caller data stayed with the founder.

## 6. What to read

| Path | Why |
|---|---|
| `backend/app/agent.py` | The tool-use loop, session handling, claim guards, cost accounting |
| `backend/app/tools.py` | Server-side booking rules, ownership checks, escalation |
| `backend/app/config.py` | Strict tenant schema and per-tenant policy |
| `backend/tests/` | Failure-mode coverage, isolation tests, regression pins |
| `backend/clients/sample_*.yaml` | Fictional tenants for local multi-tenant experiments |
| `marketing/audit.py` | Claim and leak scanning |
