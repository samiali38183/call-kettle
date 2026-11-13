"""The adversarial evaluation (qa/security_eval.py) as a regression gate.

Every case drives the real agent loop with a scripted model that does what an attacker wants. Two kinds of test:
  * each case must produce the expected decision, enforced by the expected control, with its state invariant intact;
  * for each boundary, a mutation test removes that boundary and requires the cases that depend on it to FAIL. A control that
    can be deleted without any case noticing is not covered, and this is where that would show up.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import agent, cost_guard, storage, toolguard, tools
from qa import security_eval as ev


@pytest.fixture(scope="module")
def results():
    saved = storage.DB_PATH
    try:
        yield {r["id"]: r for r in ev.run_all()}
    finally:
        storage.DB_PATH = saved


@pytest.fixture(autouse=True)
def _restore_db_path():
    saved = storage.DB_PATH
    yield
    storage.DB_PATH = saved


@pytest.mark.parametrize("case_id", [c.id for c in ev.CASES])
def test_case(results, case_id):
    r = results[case_id]
    assert r["error"] == ""
    assert (r["actual_decision"], r["actual_control"]) == (r["expected_decision"], r["expected_control"]), r["attack"]
    assert r["invariant_ok"], f"state check failed: {r['invariant']}"
    assert r["network_attempts"] == 0


def test_the_evaluation_makes_no_network_connection_and_leaks_nothing_into_the_audit_log(results):
    assert sum(r["network_attempts"] for r in results.values()) == 0
    assert sum(r["audit_leak"] for r in results.values()) == 0


def test_every_gate_control_has_at_least_one_case():
    gate = {toolguard.SESSION_BINDING, toolguard.UNKNOWN_TOOL, toolguard.NOT_OFFERED, toolguard.POLICY_DISABLED, toolguard.AFTER_HOURS,
            toolguard.ARG_SCHEMA, toolguard.CALL_BUDGET}
    assert gate <= {c.expect[1] for c in ev.CASES}


def test_benign_controls_are_not_refused(results):
    for cid in ("BN-01", "BN-02", "BN-03", "BN-04"):
        assert results[cid]["actual_decision"] in ("allowed", "scoped")


# ------------------------------------------------------------------------------------------------ mutations
_orig_authorize = agent.authorize_tool_call
_orig_slot_grid = tools._slot_grid
_orig_get_call = storage.get_call


def _skip_unknown_tool_check(session, name, tool_input):
    d = _orig_authorize(session, name, tool_input)
    return toolguard.Decision(True, toolguard.ALLOWED) if d.control == toolguard.UNKNOWN_TOOL else d


def _skip_binding_check(session, name, tool_input):
    view = SimpleNamespace(client_id=session.config.client_id, config=session.config, tool_calls=session.tool_calls)
    return _orig_authorize(view, name, tool_input)


def _own_nothing_check(config, booking_id, call_sid, caller_id):
    return storage.get_booking(int(booking_id)) if str(booking_id).isdigit() else None


_orig_book = tools.book_appointment


def _book_without_caller_id(**kw):
    return _orig_book(**{**kw, "caller_id": None})


MUTATIONS = {
    "policy switches ignored": ([patch.object(agent, "tool_allowed", lambda c, n: True), patch.object(agent, "tools_for", lambda c: list(agent.TOOLS))],
                                {"PI-01", "PI-03", "PI-05"}),
    "after-hours rule ignored": ([patch.object(agent, "is_open_now", lambda c, now=None: True)], {"PI-04"}),
    "unknown tool names accepted": ([patch.object(agent, "authorize_tool_call", _skip_unknown_tool_check)], {"PI-06"}),
    "tenant binding not checked": ([patch.object(agent, "authorize_tool_call", _skip_binding_check)], {"XT-03"}),
    "argument schema not checked": ([patch.object(toolguard, "check_args", lambda schema, tool_input: None)], {"IN-01", "TA-06", "PX-01"}),
    "per-call budgets not checked": ([patch.object(toolguard, "check_budget", lambda counts, name: None)], {"LE-02", "LE-03"}),
    "booking ownership not checked": ([patch.object(tools, "_owned", _own_nothing_check)], {"PI-02", "XT-01", "XT-02", "XT-05"}),
    "false-claim guard removed": ([patch.object(agent, "_guard_claims", lambda session, text: text)], {"CF-01", "CF-02", "CF-03", "CF-04"}),
    "card/SSN scrubbing removed": ([patch.object(tools, "scrub_sensitive", lambda t: t), patch.object(storage, "scrub_sensitive", lambda t: t),
                                    patch.object(toolguard, "scrub_sensitive", lambda t: t)], {"PX-03", "PX-04"}),
    "control characters not stripped from tool text": ([patch.object(tools, "clean_text", lambda v, n: str(v or "")[:n])], {"IN-03"}),
    "audit log stores raw arguments": ([patch.object(toolguard, "redact_args", lambda i: dict(i) if isinstance(i, dict) else {})], {"PX-05"}),
    "caller text not length-capped": ([patch.object(agent, "MAX_CALLER_CHARS", 10**6)], {"SAN-01"}),
    "recovered session not tenant-checked": ([patch.object(storage, "get_call", lambda sid: {**(_orig_get_call(sid) or {}), "client_id": "tenant_a"})], {"XT-06"}),
    "tool loop uncapped": ([patch.object(cost_guard, "MAX_TOOL_ITERATIONS_PER_TURN", 50)], {"LE-01"}),
    "per-call booking cap removed": ([patch.object(tools, "_call_booking_cap_reached", lambda *a: False), patch.dict(toolguard.PER_CALL_CAPS, {"book_appointment": 99})], {"CF-05"}),
    "confirmation text sent to any number": ([patch.object(tools, "book_appointment", _book_without_caller_id)], {"PX-06"}),
    "lead-time rule removed": ([patch.object(tools, "MIN_LEAD_MINUTES", -10**6)], {"TA-03"}),
    "off-grid times accepted": ([patch.object(tools, "_slot_grid", lambda c, d: _orig_slot_grid(c, d) + [d.replace(hour=3, minute=0)])], {"TA-02"}),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_removing_a_boundary_makes_its_cases_fail(name):
    patches, must_fail = MUTATIONS[name]
    for p in patches:
        p.start()
    try:
        failed = {r["id"] for r in ev.run_all(only=sorted(must_fail)) if not r["passed"]}
    finally:
        for p in reversed(patches):
            p.stop()
    assert failed == must_fail, f"with '{name}' the cases that failed were {sorted(failed)}, wanted {sorted(must_fail)}"


# ------------------------------------------------------------------------------------------------ the pieces
SCHEMA = {"properties": {"booking_id": {"type": "integer"}, "note": {"type": "string"}}, "required": ["booking_id"]}


@pytest.mark.parametrize("value,ok", [
    ({"booking_id": 3}, True), ({"booking_id": "3"}, False), ({"booking_id": True}, False), ({}, False),
    ({"booking_id": 3, "extra": 1}, False), ({"booking_id": 3, "note": "x" * 1001}, False), ("booking_id=3", False), (None, False),
])
def test_check_args(value, ok):
    assert (toolguard.check_args(SCHEMA, value) is None) is ok


def test_check_budget_caps_a_tool_and_the_whole_call():
    assert toolguard.check_budget({"escalate_to_human": 2}, "escalate_to_human") is None
    assert toolguard.check_budget({"escalate_to_human": 3}, "escalate_to_human")
    assert toolguard.check_budget({"check_availability": toolguard.MAX_TOOL_CALLS_PER_CALL}, "check_availability")


def test_redact_args_keeps_scheduling_values_and_only_the_length_of_personal_ones():
    out = toolguard.redact_args({"caller_name": "Pat Quincy", "caller_phone": "+15555550177", "date": "2026-01-12", "booking_id": 4,
                                 "service": "card 4111 1111 1111 1111"})
    assert out["caller_name"] == "<10 chars>" and out["caller_phone"] == "<12 chars>"
    assert out["date"] == "2026-01-12" and out["booking_id"] == 4 and "4111" not in out["service"]
    assert toolguard.redact_args("not a dict") == {"_invalid": "str"}


def test_tenant_erase_also_removes_the_audit_rows(app_client):
    client, main = app_client
    storage.log_tool_call(client_id="gone_tenant", call_sid="CA1", tool="cancel_appointment", decision="refused", control="policy_disabled",
                          reason="x", args_json="{}")
    r = client.post("/admin/client/gone_tenant/delete?key=master_key_for_tests&confirm=gone_tenant")
    assert r.json()["deleted"]["tool_audit"] == 1
