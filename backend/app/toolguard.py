"""Server-side checks on a tool call the model asked for, and the audit record of what was decided.

The model is untrusted input here. Whatever it asks for (because a caller talked it into it, or for any other reason) is checked
against the tenant's policy, the tool's schema and per-call budgets by code that does not read the model's words. agent.py calls
`check_args` and `check_budget` from `authorize_tool_call`; every decision, allowed or refused, is written by `audit`.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from app import storage
from app.redact import scrub_sensitive

logger = logging.getLogger("callkettle.toolguard")

# Controls, as recorded in the audit log and reported by the evaluation (backend/qa/security_eval.py).
ALLOWED = "allowed"
SESSION_BINDING = "session_tenant_binding"
UNKNOWN_TOOL = "unknown_tool"
NOT_OFFERED = "tool_not_offered"
POLICY_DISABLED = "policy_disabled"
AFTER_HOURS = "after_hours_policy"
ARG_SCHEMA = "arg_schema"
CALL_BUDGET = "per_call_tool_budget"

MAX_ARG_CHARS = 1000            # no tool takes more than a short sentence; the model API would allow far more
MAX_TOOL_CALLS_PER_CALL = 40    # a 12-turn call with 4 model round trips per turn is already well under this
# Tools whose effect leaves the system (owner paged, calendar written, caller moved): a looping or manipulated model gets a few tries.
PER_CALL_CAPS = {"escalate_to_human": 3, "cancel_appointment": 3, "reschedule_appointment": 5, "transfer_call": 2, "book_appointment": 6}
# Arguments that hold what a caller said or their contact details. The audit log keeps their length, never the value.
_PERSONAL_ARGS = {"caller_name", "caller_phone", "summary", "closing_message", "handoff_message"}
_JSON_TYPES = {"string": str, "integer": int}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    control: str          # which check decided it (one of the constants above)
    reason: str = ""


def check_args(schema: dict, tool_input: object) -> str | None:
    """Why this input does not fit the tool's declared input_schema, or None. Unknown names, missing required names, wrong types
    (a bool is not an integer) and oversized strings are all refused."""
    if not isinstance(tool_input, dict):
        return "arguments are not an object"
    props = schema.get("properties", {})
    extra = sorted(set(tool_input) - set(props))
    if extra:
        return f"unexpected argument(s): {', '.join(map(str, extra))[:80]}"
    missing = [k for k in schema.get("required", []) if k not in tool_input]
    if missing:
        return f"missing argument(s): {', '.join(missing)}"
    for key, value in tool_input.items():
        want = _JSON_TYPES.get(props[key].get("type"))
        if want is None:
            continue
        if not isinstance(value, want) or (want is int and isinstance(value, bool)):
            return f"argument {key} must be a {props[key]['type']}"
        if isinstance(value, str) and len(value) > MAX_ARG_CHARS:
            return f"argument {key} is longer than {MAX_ARG_CHARS} characters"
    return None


def check_budget(counts: dict[str, int], name: str) -> str | None:
    """Why this call would pass a per-call budget, or None. `counts` is the number of calls already allowed on this phone call."""
    if sum(counts.values()) >= MAX_TOOL_CALLS_PER_CALL:
        return f"this call has already used {MAX_TOOL_CALLS_PER_CALL} tool calls"
    cap = PER_CALL_CAPS.get(name)
    if cap is not None and counts.get(name, 0) >= cap:
        return f"{name} was already used {cap} times on this call"
    return None


def redact_args(tool_input: object) -> dict:
    """What may be stored about a call's arguments: scheduling values as given (scrubbed of card and SSN shapes, truncated),
    personal values only as a length."""
    if not isinstance(tool_input, dict):
        return {"_invalid": type(tool_input).__name__}
    out: dict = {}
    for key, value in tool_input.items():
        key = str(key)[:40]
        if key in _PERSONAL_ARGS:
            out[key] = f"<{len(value)} chars>" if isinstance(value, str) else f"<{type(value).__name__}>"
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        else:
            out[key] = scrub_sensitive(re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)))[:80]
    return out


def outcome_of(result: object) -> str:
    """'ok' when the tool did what was asked, 'rejected_by_tool' when its own rules refused (a result with an error or success false)."""
    if isinstance(result, dict) and ("error" in result or result.get("success") is False):
        return "rejected_by_tool"
    return "ok"


def audit(client_id: str, call_sid: str, tool: str, tool_input: object, decision: Decision, outcome: str = "") -> None:
    """Record one tool-call decision. Never raises and never blocks the call (the live-path breaker applies, like the transcript)."""
    try:
        storage.log_tool_call(
            client_id=client_id, call_sid=call_sid, tool=str(tool)[:60], decision="allowed" if decision.allowed else "refused",
            control=decision.control, reason=decision.reason[:200], outcome=outcome, args_json=json.dumps(redact_args(tool_input), default=str),
        )
    except Exception:
        logger.exception("Could not write the tool audit record for %s", call_sid)
