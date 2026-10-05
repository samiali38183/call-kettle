"""Who answers first: the assistant (default), the owner's phone with the assistant as backup, after hours only, or VIP numbers."""
from datetime import datetime
from xml.etree import ElementTree

import pytest

CALLER = "+15555550100"
VIP = "+15555550100"


def _cfg(**over):
    from app.config import load_client_config

    return load_client_config("demo_hvac").model_copy(update=over)


def _use(monkeypatch, main, cfg):
    monkeypatch.setattr(main, "load_client_config", lambda cid: cfg)


def _incoming(client, sid="CA_R1", frm=CALLER):
    return client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": sid, "From": frm})


def test_default_the_assistant_answers(app_client):
    client, main = app_client
    r = _incoming(client)
    assert "<Gather" in r.text and "<Dial" not in r.text


def test_owner_first_rings_the_owner_with_no_greeting_and_a_fallback_url(app_client, monkeypatch):
    client, main = app_client
    from app import agent, storage

    _use(monkeypatch, main, _cfg(routing_mode="owner_first", owner_ring_seconds=18))
    r = _incoming(client, "CA_R1_first")
    root = ElementTree.fromstring(r.text)
    dial = root.find(".//Dial")
    assert dial.get("timeout") == "18" and (dial.text or "").strip() == _cfg().escalation_phone
    assert dial.get("action").endswith("/voice/owner-first-result?client_id=demo_hvac")
    assert root.find(".//Say") is None and root.find(".//Gather") is None            # silence while it rings
    assert agent.get_session("CA_R1_first") is None and storage.get_call("CA_R1_first") is not None


@pytest.mark.parametrize("status", ["no-answer", "busy", "failed", "canceled"])
def test_if_the_owner_does_not_answer_the_assistant_takes_the_call(app_client, monkeypatch, status):
    client, main = app_client
    from app import agent

    _use(monkeypatch, main, _cfg(routing_mode="owner_first"))
    _incoming(client, "CA_R2")
    r = client.post("/voice/owner-first-result?client_id=demo_hvac", data={"CallSid": "CA_R2", "From": CALLER, "DialCallStatus": status})
    assert "<Gather" in r.text and "recorded and monitored" in r.text and "AI receptionist" in r.text
    assert agent.get_session("CA_R2") is not None


def test_if_the_owner_answers_the_call_is_theirs_and_is_closed_out(app_client, monkeypatch):
    client, main = app_client
    from app import agent, storage

    _use(monkeypatch, main, _cfg(routing_mode="owner_first"))
    _incoming(client, "CA_R3")
    r = client.post("/voice/owner-first-result?client_id=demo_hvac",
                    data={"CallSid": "CA_R3", "From": CALLER, "DialCallStatus": "completed", "DialCallDuration": "75"})
    assert "<Gather" not in r.text and "<Say" not in r.text
    assert agent.get_session("CA_R3") is None and storage.get_call("CA_R3")["outcome"] == "owner_answered"


def test_with_screening_a_leg_that_hung_up_quickly_counts_as_unanswered(app_client, monkeypatch):
    client, main = app_client
    _use(monkeypatch, main, _cfg(routing_mode="owner_first", transfer_screening=True))
    r = _incoming(client, "CA_R4")
    assert ElementTree.fromstring(r.text).find(".//Dial/Number").get("url").startswith("http")
    short = client.post("/voice/owner-first-result?client_id=demo_hvac",
                        data={"CallSid": "CA_R4", "From": CALLER, "DialCallStatus": "completed", "DialCallDuration": "4"})
    assert "<Gather" in short.text


def test_after_hours_mode_rings_the_owner_only_while_open(app_client, monkeypatch):
    client, main = app_client
    from app import tools

    _use(monkeypatch, main, _cfg(routing_mode="after_hours"))
    monkeypatch.setattr(tools, "_local_now", lambda c: datetime(2026, 1, 5, 10, 0))        # Monday 10:00, open 07-19
    assert "<Dial" in _incoming(client, "CA_R5").text
    monkeypatch.setattr(tools, "_local_now", lambda c: datetime(2026, 1, 5, 22, 0))        # Monday 22:00, closed
    r = _incoming(client, "CA_R6")
    assert "<Gather" in r.text and "<Dial" not in r.text
    monkeypatch.setattr(tools, "_local_now", lambda c: datetime(2026, 1, 11, 10, 0))       # Sunday: closed all day
    assert "<Gather" in _incoming(client, "CA_R7").text


def test_vip_numbers_always_ring_the_owner_first(app_client, monkeypatch):
    client, main = app_client
    _use(monkeypatch, main, _cfg(always_ring_owner=[VIP]))
    assert "<Dial" in _incoming(client, "CA_R8", frm=VIP).text
    assert "<Gather" in _incoming(client, "CA_R9", frm=CALLER).text


def test_the_ceiling_still_takes_priority(app_client, monkeypatch):
    client, main = app_client
    from app import costing

    _use(monkeypatch, main, _cfg(routing_mode="owner_first", monthly_cost_ceiling_usd=10))
    monkeypatch.setattr(costing, "month_usage", lambda cid, now=None: (50.0, 99))
    r = _incoming(client, "CA_R10")
    assert "ceiling-message" in r.text


def test_owner_first_result_rejects_unsigned_requests_and_unknown_clients(app_client, monkeypatch):
    client, main = app_client
    r = client.post("/voice/owner-first-result?client_id=nope", data={"CallSid": "CA_X"})
    assert r.status_code == 200 and "<Hangup" in r.text
    monkeypatch.setattr("app.twilio_utils._SKIP_SIGNATURE_CHECK", False)
    monkeypatch.setattr("app.twilio_utils._AUTH_TOKEN", "t" * 32)
    assert client.post("/voice/owner-first-result?client_id=demo_hvac", data={"CallSid": "CA_X"}).status_code == 403


def test_config_validation():
    from app.config import ClientConfig

    base = _cfg().model_dump()
    assert ClientConfig.model_validate({**base, "routing_mode": "owner_first", "owner_ring_seconds": 25, "always_ring_owner": [VIP]})
    for bad in ({"routing_mode": "sometimes"}, {"owner_ring_seconds": 5}, {"owner_ring_seconds": 90},
                {"always_ring_owner": ["+15555550100"]}, {"always_ring_owner": ["+1703555"]}, {"always_ring_owner": [VIP] * 60}):
        with pytest.raises(Exception):
            ClientConfig.model_validate({**base, **bad})


def test_is_open_now_follows_the_clients_clock(monkeypatch):
    from app import tools

    cfg = _cfg()
    for when, expected in [(datetime(2026, 1, 5, 6, 59), False), (datetime(2026, 1, 5, 7, 0), True), (datetime(2026, 1, 5, 18, 59), True),
                           (datetime(2026, 1, 5, 19, 0), False), (datetime(2026, 1, 11, 12, 0), False), (datetime(2026, 1, 10, 9, 0), True)]:
        monkeypatch.setattr(tools, "_local_now", lambda c, w=when: w)
        assert tools.is_open_now(cfg) is expected, when
