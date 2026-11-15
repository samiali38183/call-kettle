"""Authorization matrix for the tool-call gate (agent.authorize_tool_call).

The expected decision for every tool under every combination of tenant policy switches and open/closed hours is written out
below as a table that does not import the implementation's own policy map. If someone adds a tool, or changes which switch
controls a tool, this file has to change too, so a tool cannot become callable by accident. The gate must also refuse anything
that is not a known tool name (default deny).
"""
import itertools

import pytest

from app import agent, toolguard
from app.config import load_client_config

# tool -> the policy switch that must be on for it to be allowed (None: no switch)
SWITCH = {
    "check_availability": None,        # offered only while booking or rescheduling is on (see _expected)
    "book_appointment": "can_book",
    "find_my_appointments": None,
    "cancel_appointment": "can_cancel",
    "reschedule_appointment": "can_reschedule",
    "escalate_to_human": None,
    "end_call": None,
    "transfer_call": "can_transfer",
}
AFTER_HOURS_TOOLS = {"book_appointment", "reschedule_appointment"}   # refused while closed on a message-only tenant


def _valid_input(spec: dict) -> dict:
    schema = spec["input_schema"]
    sample = {"string": "x", "integer": 1}
    return {k: sample[schema["properties"][k]["type"]] for k in schema.get("required", [])}


def _expected(tool: str, policy: dict, closed_message_only: bool) -> tuple[bool, str]:
    switch = SWITCH[tool]
    if switch and not policy[switch]:
        return False, toolguard.POLICY_DISABLED
    if tool == "check_availability" and not (policy["can_book"] or policy["can_reschedule"]):
        return False, toolguard.NOT_OFFERED
    if tool in AFTER_HOURS_TOOLS and closed_message_only:
        return False, toolguard.AFTER_HOURS
    return True, toolguard.ALLOWED


def _session(policy: dict, message_only: bool):
    base = load_client_config("demo_hvac")
    config = base.model_copy(update={
        "client_id": "tenant_a",
        "policy": base.policy.model_copy(update={**policy, "after_hours_action": "message" if message_only else "book"}),
    })
    return agent.CallSession(call_sid="CA_MATRIX", client_id="tenant_a", config=config, caller_number="+15555550111")


def test_the_table_covers_exactly_the_tools_that_exist():
    assert set(SWITCH) == {t["name"] for t in agent.TOOLS}, "a tool was added or removed: decide its policy row here"


def test_the_implementation_policy_map_matches_the_table():
    assert agent._TOOL_POLICY == {t: s for t, s in SWITCH.items() if s}


@pytest.mark.parametrize("bits", list(itertools.product([True, False], repeat=4)))
@pytest.mark.parametrize("closed_message_only", [False, True])
def test_every_tool_under_every_policy_combination(monkeypatch, bits, closed_message_only):
    policy = dict(zip(("can_book", "can_reschedule", "can_cancel", "can_transfer"), bits))
    monkeypatch.setattr(agent, "is_open_now", lambda config, now=None: not closed_message_only)
    session = _session(policy, message_only=closed_message_only)
    for spec in agent.TOOLS:
        got = agent.authorize_tool_call(session, spec["name"], _valid_input(spec))
        want = _expected(spec["name"], policy, closed_message_only)
        assert (got.allowed, got.control) == want, f"{spec['name']} with {policy}, closed={closed_message_only}"


@pytest.mark.parametrize("name", ["export_all_calls", "", "BOOK_APPOINTMENT", "book_appointment ", None, 7, ["book_appointment"], "../etc/passwd"])
def test_anything_that_is_not_a_known_tool_is_refused(name):
    got = agent.authorize_tool_call(_session({}, False), name, {})
    assert (got.allowed, got.control) == (False, toolguard.UNKNOWN_TOOL)


def test_a_session_bound_to_another_tenant_is_refused_for_every_tool():
    session = _session({}, False)
    session.client_id = "tenant_b"
    for spec in agent.TOOLS:
        got = agent.authorize_tool_call(session, spec["name"], _valid_input(spec))
        assert (got.allowed, got.control) == (False, toolguard.SESSION_BINDING)


def test_every_tool_schema_declares_its_arguments_and_rejects_extra_ones():
    for spec in agent.TOOLS:
        assert "properties" in spec["input_schema"]
        got = agent.authorize_tool_call(_session({}, False), spec["name"], {**_valid_input(spec), "destination": "+15555550199"})
        assert (got.allowed, got.control) == (False, toolguard.ARG_SCHEMA), spec["name"]
