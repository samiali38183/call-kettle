# Security invariants

The properties the server is meant to keep no matter what the model says, where each one is enforced, and which evaluation case or test fails if it stops holding. `backend/tests/test_security_invariants_doc.py` checks that every case ID and test file named here exists, so this table cannot drift away from the suite.

All evidence below comes from offline runs with a scripted model and synthetic tenants ([SECURITY_EVALUATION.md](SECURITY_EVALUATION.md) explains what that does and does not show).

## Trust zones

| Zone | Contents | Treated as |
|---|---|---|
| Untrusted input | Caller speech after transcription; the model's reply text and tool arguments; text inside tool results (a calendar note, a stored field) | Data. It can suggest an action, never grant one. |
| Trusted policy | Tenant YAML validated at load (`config.py`, `Policy`), the tool schemas in `agent.TOOLS`, the per-call budgets in `toolguard.py` | Decides what is allowed. Not editable from a call. |
| Trusted call facts | The tenant bound to the session, the caller ID as Twilio reports it, the call id | Source of identity and ownership. Never taken from a tool argument. |
| Effects | SQLite, owner notifications, calendar writes, transfers, SMS | Reached only through `agent._dispatch_tool`, after `authorize_tool_call`. |

## Invariants

| ID | Invariant | Enforced in | Fails if broken |
|---|---|---|---|
| I-1 | A tool call runs only if the session's tenant matches its config, the tool exists, the tenant's policy allows it and offers it, the after-hours rule allows it, the arguments match the schema and the per-call budget is not used up. | `agent.authorize_tool_call`, `toolguard.check_args`, `toolguard.check_budget` | PI-01, PI-03, PI-04, PI-05, XT-03, TA-04, TA-05, TA-06, TA-07, TA-09; `tests/test_tool_authorization_matrix.py` |
| I-2 | Default deny. A tool name, argument or type the server does not know is refused, not ignored. | `authorize_tool_call`, `check_args` | PI-06, IN-01, TA-04, TA-05, TA-09; `tests/test_tool_authorization_matrix.py` |
| I-3 | Authority comes from the call, not from the model's arguments. A transfer goes to the tenant's configured number, a lookup uses the caller ID and takes no phone argument, a confirmation text goes only to the caller ID. | `_run_tool`, `tools.find_my_appointments`, `tools.book_appointment` | IN-01, IN-02, PX-01, PX-06 |
| I-4 | Tenant isolation: a call as one tenant cannot read or change another tenant's rows, even with the same phone number or slot. This is application-level (one SQLite file, every query filtered by `client_id`), not a database boundary. | `tools._owned`, `storage` queries, `recover_session` | XT-01, XT-02, XT-04, XT-06; `tests/test_tenant_isolation_tools.py`, `tests/test_isolation.py` |
| I-5 | A booking can be changed only by the call that made it or by a caller with the same caller ID. Guessing ids does not help. | `tools._owned` | PI-02, XT-05, BN-03, BN-04 |
| I-6 | Booking rules are enforced in code: service allow-list, slot grid, lead time, closing time, per-call booking cap, no overlapping bookings. | `tools.book_appointment`, `storage.create_booking` | TA-01, TA-02, TA-03, TA-08, CF-05, RT-02 |
| I-7 | The assistant may say booked, moved or cancelled only if a tool result recorded it on this call. Otherwise the reply is replaced and the owner is alerted. | `agent._record_outcome`, `agent._guard_claims` | CF-01, CF-02, CF-03, CF-04; `tests/test_truth_invariant.py` |
| I-8 | Work per call is bounded: four model round trips per turn, per-tool caps, 40 tool calls per call, 600 characters of caller text, 1000 characters per argument. | `cost_guard`, `toolguard`, `agent.MAX_CALLER_CHARS` | LE-01, LE-02, LE-03, SAN-01 |
| I-9 | The audit log and stored text hold no card or SSN shapes, and personal arguments appear only as a length. | `toolguard.redact_args`, `redact.scrub_sensitive` | PX-03, PX-04, PX-05 |
| I-10 | Tool results are data. Text inside a result, including text claiming to come from the owner or an administrator, cannot change what the server allows. | the gate runs on every call, whatever came before it | IN-04, IN-05 |
| I-11 | Retries are safe. Repeating the same booking in a call returns the existing booking, and a second caller cannot take a held slot. | `tools.book_appointment`, `storage.create_booking` | RT-01, RT-02 |

Every case ID above is in `backend/qa/security_eval.py`. Cases with a matching mutation test (a boundary removed, the case must fail) are listed in [SECURITY_EVALUATION.md](SECURITY_EVALUATION.md#the-regression-gate). I-11's double-booking half (RT-02) has no mutation test: the overlap check and the unique index both stop it, so removing one alone would not fail the case.

## Mapping to outside frameworks

This is a map from what the code does to the names other people use. It is not a compliance claim, and the evaluation covers only the rows marked as tested.

| Framework entry | Where it applies here | Invariants |
|---|---|---|
| OWASP LLM01:2025 Prompt Injection | Caller speech and tool-result text try to steer the model; the server decides what executes | I-1, I-3, I-10 |
| OWASP LLM02:2025 Sensitive Information Disclosure | Lookups scoped to the caller, redaction of card and SSN shapes, no raw personal values in the audit log | I-3, I-4, I-9 |
| OWASP LLM05:2025 Improper Output Handling | Model output (arguments, claims) is validated before it reaches storage, email or the caller | I-1, I-2, I-7 |
| OWASP LLM06:2025 Excessive Agency | The model has eight narrow tools, each switchable per tenant, with ownership and budget checks | I-1, I-5, I-8 |
| OWASP LLM10:2025 Unbounded Consumption | Loop, call and length caps; spend ceilings are in `costing.py` | I-8 |
| MITRE ATLAS AML.T0051.000 / .001 (LLM Prompt Injection: direct, indirect) | Cases PI-* (direct) and IN-* (indirect) | I-1, I-10 |
| MITRE ATLAS AML.T0053 (AI Agent Tool Invocation) | The abuse the gate is built to stop: a manipulated model calling a tool it should not | I-1, I-5 |
| MITRE ATLAS AML.T0057 (LLM Data Leakage) | PX-* cases | I-3, I-9 |
| MITRE ATLAS AML.T0029 / AML.T0034 (Denial of AI Service, Cost Harvesting) | LE-* cases and per-call limits | I-8 |

Not mapped, because nothing here tests it: LLM03 supply chain, LLM04 data and model poisoning, LLM07 system prompt leakage, LLM08 vector and embedding weaknesses, LLM09 misinformation beyond the booking-claim guard.

## What the invariants do not claim

- They hold for the server given any model output. They say nothing about how often a real model produces the dangerous output, and no live-model run exists for this project.
- Caller ID is trusted as Twilio reports it. A spoofed caller ID that matches a customer's number passes the ownership check (I-5).
- The booking phone number a caller states is not verified, so one caller can create a booking under another person's number. That person's own lookup would then list it.
- Redaction (I-9) is shape based, and prompt-only rules (quotes, fees, address collection) are not covered by any invariant here.
