# How I used coding agents on this project

I wrote Call Kettle by hand and with two coding agents, Claude Code and Hermes Agent. This note covers how that worked, how the product's own agent is put together, and which files are worth reading. It's a sanitized snapshot, and production stays private.

## 1. The product's tool-use loop

`backend/app/agent.py` runs one conversation per call.

1. A `CallSession` holds the history. `build_system_prompt` puts in the config facts and whether the line is open right now.
2. Each utterance is cleaned and sent to Claude Haiku with a typed tool list from `tools_for(config)`. A disabled tool isn't offered.
3. While the response stops with `tool_use`, the loop runs the tool through `_dispatch_tool`, adds a `tool_result` and calls the model again. `cost_guard.MAX_TOOL_ITERATIONS_PER_TURN` caps the iterations per turn, and `MAX_REPLY_TOKENS` caps reply length.
4. Before any text is spoken, guards run. `unsupported_claims` and `_guard_claims` stop the agent from saying it booked, cancelled or rescheduled unless the matching tool succeeded. `_fix_dates` corrects spoken dates.

The model proposes and the code decides. Anything with consequences is enforced in code, and I treat rules that only live in the prompt as weaker.

## 2. Schema validation

`backend/app/config.py` defines the config schema with Pydantic. Hours, timezone, the AI disclosure in the opening line, webhook URLs and demo-only options are checked when the YAML loads, so a bad value fails at load and not in the middle of a call. The `Policy` model (`can_book`, `can_reschedule`, `can_cancel`, `can_transfer` and the wording switches) is `extra: forbid`, so a misspelled policy key is an error. The top-level `ClientConfig` is not `extra: forbid`, so an unknown top-level key is ignored. That is a known gap.

`can_book`, `can_reschedule`, `can_cancel` and `can_transfer` gate their tools both when the tools are offered and when they are called. The wording switches (`can_quote`, dispatch fee text, address collection) are prompt rules only.

`tools.py` checks tool inputs again (`clean_text`, slot parsing, ownership checks on existing bookings by call or caller ID), so a bad model argument can't reach storage.

## 3. Tests as the gate

I accept agent-written changes when `cd backend && python -m pytest -q` passes. The suite has 93 test files and covers failure modes as well as the happy path:

- `test_agent.py`, `test_failures.py`: loop behavior, model errors, fallback to the owner's phone
- `test_isolation.py`: one tenant's config can't read or book into another's data
- `test_frontdesk_concurrency.py`, `test_hardening.py`, `test_history_integrity.py`: races, malformed input, stored history consistency
- `test_config.py`: the schema rejects bad YAML
- `*_regressions.py`: each file pins a bug that was found and fixed

CI (`.github/workflows/ci.yml`) runs the same command on Python 3.12. When an agent fixes a bug, I expect a failing test first and then the fix.

This snapshot isn't a perfect copy of the private suite. `backend/tests/snapshot_known_failures.txt` lists the tests that fail here because the export dropped files they read or rewrote their fixtures, and `conftest.py` marks them `xfail` so they stay visible in the pytest summary. Before I added that, CI on this repository was red because two test files couldn't be collected at all. A test gate is only as good as the process that keeps it green.

## 4. Checking public text

`marketing/pricing_audit.py` scans public text for a configured private price and for template tokens or structured-data keys that would render one. The price comes from an environment variable and is never stored in the repository. I use it to check anything an agent writes for public pages, and `backend/tests/test_pricing_audit.py` covers it with made-up prices. The private tool that checked copy against a table of product facts, and the sales documents it read, are not in this snapshot.

## 5. Who did what

| Who | Role | Evidence |
|---|---|---|
| Sami Ali | Product decisions, the policy model (what the AI may and may not do on a call), review of anything touching bookings or caller data, deploys | `AGENTS.md` and `CLAUDE.md` rules, this note |
| Claude Code | Implementation in the repo, refactors under the existing suite, regression tests for reported bugs, working under the rules in `CLAUDE.md` | `CLAUDE.md` and `AGENTS.md`; the private commit history isn't in this snapshot |
| Hermes Agent | A local agent runtime for QA tasks, repo chores, scheduled health checks and status briefs, and the reviewer for integration and claim levels (what may be called implemented, tested or deployed). A board of lane claims kept parallel sessions from colliding | my private project notes and work board, which this repo can't reproduce |
| Claude Haiku | The model the product calls at runtime through the Anthropic API. It isn't a development agent | `backend/app/config.py` (`model` default), `agent.py` |

The agents helped most with test scaffolding, refactors under an existing suite, regression tests for reported bugs and repo chores. I use claim levels (proposed, implemented, unit tested, integration tested, deployed, production verified) because an agent saying something "works" isn't evidence. The status table in the README follows the same idea.

I'm not claiming that agents ran the product, that agent output was merged without review, or that you could reproduce the Hermes and Claude Code workflow from this snapshot, which leaves out the private tooling.

## 6. What to read

| Path | What's in it |
|---|---|
| `backend/app/agent.py` | The tool-use loop, session handling, claim guards, cost accounting |
| `backend/app/tools.py` | Server-side booking rules, ownership checks, escalation |
| `backend/app/config.py` | Pydantic tenant config with load-time validation and policy flags (only `Policy` is `extra: forbid`) |
| `backend/tests/` | Failure-mode tests, isolation tests, regression pins |
| `marketing/pricing_audit.py` | Scanner for a configured private price in public text |
