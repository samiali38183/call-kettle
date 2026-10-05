"""Launch hardening (2026-10-03): defects that would hit the first paying customer. Each test failed before its fix.
Offline and deterministic: temp SQLite, fake model client, no network."""
import sqlite3
from datetime import datetime

import pytest
import yaml
from pydantic import ValidationError

from tests.test_agent import FakeAnthropicClient, FakeAnthropicClientRaises, FakeResponse, FakeTextBlock, FakeToolUseBlock, temp_db  # noqa: F401
from tests.test_ceiling import CALLER, S, _over  # noqa: F401

CALLER_ID = "+15555550100"


def _raw(**over) -> dict:
    from app.config import CLIENTS_DIR

    raw = yaml.safe_load((CLIENTS_DIR / "demo_hvac.yaml").read_text(encoding="utf-8"))
    raw.update(over)
    return raw


def _validate(**over):
    from app.config import ClientConfig

    return ClientConfig.model_validate(_raw(**over))


def _policy_cfg(**policy):
    from app.config import load_client_config

    base = load_client_config("demo_hvac")
    return base.model_copy(update={"policy": base.policy.model_copy(update=policy)})


def _escalations(storage, call_sid):
    conn = sqlite3.connect(storage.DB_PATH)
    try:
        return conn.execute("SELECT reason, caller_phone FROM escalations WHERE call_sid = ?", (call_sid,)).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- 1. business hours and timezone are validated at config time
WEEKDAYS_8_TO_5 = {d: ["8:00", "17:00"] for d in ("mon", "tue", "wed", "thu", "fri")}


def test_unpadded_hours_are_normalized_so_open_and_closed_are_right():
    """'8:00' used to be stored as typed. Open/closed is a string comparison, and '8:00' > '09:15' as text, so a shop open 8-5 was
    reported CLOSED all day: the AI told callers it was closed, after-hours routing skipped the owner, and an after-hours
    'message' policy refused every booking during business hours."""
    from app import agent, tools

    cfg = _validate(business_hours={**WEEKDAYS_8_TO_5, "sat": "closed", "sun": "closed"})
    assert cfg.business_hours["mon"] == ["08:00", "17:00"]
    assert agent.is_open_now(cfg, datetime(2026, 1, 5, 9, 15))            # a Monday morning
    assert tools.is_open_now(cfg)                                          # frozen clock: Monday 09:00
    assert "Right now: OPEN" in agent._open_status_line(cfg, datetime(2026, 1, 5, 10, 30))
    assert not agent.is_open_now(cfg, datetime(2026, 1, 5, 17, 0))         # closing time is exclusive


def test_booking_hours_are_normalized_too():
    cfg = _validate(booking_hours={"mon": ["9:30", "12:00"]})
    assert cfg.booking_hours == {"mon": ["09:30", "12:00"]}


def test_full_or_capitalised_day_names_are_understood_not_silently_closed():
    cfg = _validate(business_hours={"Monday": ["08:00", "17:00"], "TUE": ["08:00", "17:00"]})
    assert set(cfg.business_hours) == {"mon", "tue"}


@pytest.mark.parametrize("hours", [
    {"mnday": ["08:00", "17:00"]},                    # a typo would silently close the business every day
    {"mon": ["17:00", "08:00"]},                      # inverted (overnight hours are not supported by the booking grid)
    {"mon": ["08:00", "08:00"]},
    {"mon": ["08:00"]},
    {"mon": ["08:00", "12:00", "13:00", "17:00"]},
    {"mon": ["25:00", "26:00"]},
    {"mon": ["8am", "5pm"]},
    {"mon": ["08:00", "17:00"], "Monday": ["09:00", "17:00"]},
])
def test_hours_that_cannot_be_served_are_rejected(hours):
    with pytest.raises(ValidationError):
        _validate(business_hours=hours)
    with pytest.raises(ValidationError):
        _validate(booking_hours=hours)


def test_an_unknown_timezone_is_rejected_instead_of_crashing_every_call():
    with pytest.raises(ValidationError):
        _validate(timezone="America/NewYork")
    assert _validate(timezone="America/Chicago").timezone == "America/Chicago"


def test_every_shipped_client_config_still_validates():
    from app.config import CLIENTS_DIR, ClientConfig

    for path in sorted(CLIENTS_DIR.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        ClientConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def test_an_intake_with_inverted_hours_cannot_go_live():
    from app import onboarding

    intake = {"business_name": "Test Heating", "owner_name": "Pat", "owner_phone": "+15555550100", "trade": "HVAC",
              "hours": {"mon": "17:00-08:00"}, "services": [{"name": "Repair visit", "minutes": 60}]}
    with pytest.raises(ValidationError):
        onboarding.config_from_intake(intake, client_id="test_heating")


# ---------------------------------------------------------------- 2. a refused transfer_call must not dial anyway
def test_a_transfer_the_policy_forbids_is_not_dialed_even_if_the_model_calls_it(temp_db, monkeypatch):
    """Policy says can_transfer is enforced in code. The dispatcher refused the tool, but run_turn still ended the call and
    returned the owner's number, so a business that does not take live transfers got its phone rung."""
    from app import agent

    fake = FakeAnthropicClient([
        FakeResponse(content=[FakeToolUseBlock(id="t1", name="transfer_call", input={"reason": "wants owner", "handoff_message": "Connecting you now."})],
                     stop_reason="tool_use"),
        FakeResponse(content=[FakeTextBlock(text="I can't connect you live, but I can take a message for the team.")], stop_reason="end_turn"),
    ])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)
    session = agent.start_session("CA_LH_2", _policy_cfg(can_transfer=False), caller_number=CALLER_ID)
    reply, should_end, transfer_to = agent.run_turn(session, "my furnace is making a grinding noise")
    assert transfer_to is None and not should_end and "take a message" in reply
    results = [b["content"] for m in session.messages if m["role"] == "user" and isinstance(m["content"], list) for b in m["content"]]
    assert any("not available" in r for r in results)                    # the model was told the transfer was refused


def test_an_allowed_transfer_still_dials(temp_db, monkeypatch):
    from app import agent

    fake = FakeAnthropicClient([FakeResponse(content=[FakeToolUseBlock(id="t1", name="transfer_call", input={"reason": "x", "handoff_message": "Connecting you now."})],
                                             stop_reason="tool_use")])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)
    cfg = _policy_cfg()
    reply, should_end, transfer_to = agent.run_turn(agent.start_session("CA_LH_2b", cfg, caller_number=CALLER_ID), "my furnace is making a grinding noise")
    assert should_end and transfer_to == cfg.escalation_phone and reply == "Connecting you now."


# ---------------------------------------------------------------- 3. every callback alert carries the caller's number
@pytest.fixture
def owner_alerts(monkeypatch):
    from app import notify

    seen = []
    monkeypatch.setattr(notify, "notify_owner", lambda config, *, title, body, ics=None: seen.append((title, body)))
    return seen


def test_a_call_that_hits_its_limit_tells_the_owner_who_to_call_back(temp_db, owner_alerts):
    """The turn/time-limit hand-off said 'they'll follow up shortly' but sent the owner 'Caller: unknown' with no number."""
    from app import agent

    cfg = _policy_cfg()
    session = agent.start_session("CA_LH_3a", cfg, caller_number=CALLER_ID)
    session.turn_count = cfg.max_turns
    _reply, should_end, _ = agent.run_turn(session, "one more thing")
    assert should_end
    assert _escalations(temp_db, "CA_LH_3a") == [("max_turns_reached", CALLER_ID)]
    assert CALLER_ID in owner_alerts[-1][1]


def test_a_model_failure_tells_the_owner_who_to_call_back(temp_db, owner_alerts, monkeypatch):
    from app import agent

    monkeypatch.setattr(agent, "_anthropic_client", lambda: FakeAnthropicClientRaises())
    agent.run_turn(agent.start_session("CA_LH_3b", _policy_cfg(can_transfer=False), caller_number=CALLER_ID), "my AC stopped cooling")
    assert ("agent_error", CALLER_ID) in _escalations(temp_db, "CA_LH_3b")
    assert any(CALLER_ID in body for title, body in owner_alerts if "agent_error" in title)


def test_a_model_escalation_without_a_number_falls_back_to_caller_id(temp_db, owner_alerts):
    from app import agent

    session = agent.start_session("CA_LH_3c", _policy_cfg(), caller_number=CALLER_ID)
    agent._dispatch_tool(session, "escalate_to_human", {"reason": "callback_requested", "summary": "Wants a quote on a new furnace."})
    assert _escalations(temp_db, "CA_LH_3c") == [("callback_requested", CALLER_ID)]
    agent._dispatch_tool(session, "escalate_to_human", {"reason": "callback_requested", "caller_phone": "+15555550100", "summary": "Use the office line."})
    assert ("callback_requested", "+15555550100") in _escalations(temp_db, "CA_LH_3c")       # a number the caller gave still wins


# ---------------------------------------------------------------- 4. over the spending ceiling, an unanswered transfer must not start the AI
def test_over_the_ceiling_an_unanswered_transfer_takes_a_message_without_the_model(S, monkeypatch):  # noqa: F811
    """ceiling_mode 'transfer' promises: ring the owner; if unanswered, take a message at no AI cost. The transfer-result
    handler started a full AI session instead, which is exactly the spend the ceiling exists to stop."""
    client, main, storage = S
    from app import agent

    _over(monkeypatch, main, ceiling_mode="transfer")
    monkeypatch.setattr(agent, "_anthropic_client", lambda: (_ for _ in ()).throw(AssertionError("the model must not be used")))
    r = client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_LH_4", "From": CALLER})
    assert "<Dial" in r.text
    r = client.post("/voice/transfer-result?client_id=demo_hvac", data={"CallSid": "CA_LH_4", "From": CALLER, "DialCallStatus": "no-answer"})
    assert "/voice/ceiling-message" in r.text and "<Gather" in r.text
    assert agent.get_session("CA_LH_4") is None
    assert ("transfer_unanswered", CALLER) in _escalations(storage, "CA_LH_4")
    r = client.post("/voice/ceiling-message?client_id=demo_hvac&retry=0", data={"CallSid": "CA_LH_4", "From": CALLER, "SpeechResult": "Pat, furnace is out"})
    assert "<Hangup" in r.text


def test_under_the_ceiling_an_unanswered_transfer_still_offers_the_assistant(S, monkeypatch):  # noqa: F811
    client, main, storage = S
    from app import agent

    storage.log_call_start("CA_LH_4b", "demo_hvac", CALLER)
    r = client.post("/voice/transfer-result?client_id=demo_hvac", data={"CallSid": "CA_LH_4b", "From": CALLER, "DialCallStatus": "no-answer"})
    assert "/voice/gather" in r.text and agent.get_session("CA_LH_4b") is not None


# ---------------------------------------------------------------- 5. a silent caller is always handed to the owner
def test_silence_without_a_live_session_still_tells_the_owner(S):  # noqa: F811
    """The caller is told "I'll have someone from the team follow up", but the callback record and alert were only made when an
    in-memory call session existed. After a restart mid-call (sessions are in memory) nobody was told to follow up."""
    client, main, storage = S
    from app import agent

    storage.log_call_start("CA_LH_5", "demo_hvac", CALLER)
    agent.end_session("CA_LH_5")
    r = client.post("/voice/gather?client_id=demo_hvac&retry=1", data={"CallSid": "CA_LH_5", "From": CALLER})
    assert "<Hangup" in r.text and "follow up" in r.text
    assert ("no_speech_detected", CALLER) in _escalations(storage, "CA_LH_5")
