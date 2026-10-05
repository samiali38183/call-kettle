"""Cost-based spending ceiling: an estimate from recorded usage, warnings at 75/90/100%, and a degraded mode that takes a
message at no AI cost (or rings the owner) instead of silently burning margin."""
import sqlite3
from datetime import datetime, timezone

import pytest

CALLER = "+15555550100"


def _cfg(**over):
    from app.config import load_client_config

    return load_client_config("demo_hvac").model_copy(update=over)


def _add_call(storage, sid, client="demo_hvac", cost_tokens=0, tts=0, turns=0, minutes=1, started=None):
    started = started or datetime.now(timezone.utc)
    ended = started.replace() if minutes == 0 else datetime.fromtimestamp(started.timestamp() + minutes * 60, timezone.utc)
    with storage._conn() as conn:
        conn.execute(
            "INSERT INTO calls (call_sid, client_id, from_number, started_at, ended_at, turn_count, input_tokens, output_tokens, tts_chars) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (sid, client, "+15555550100", started.isoformat(), ended.isoformat(), turns, cost_tokens, tts))


@pytest.fixture
def S(app_client):
    client, main = app_client
    from app import storage

    return client, main, storage


# ---------------------------------------------------------------- the estimate

def test_a_typical_booking_call_is_estimated_near_the_measured_cost():
    from app import costing

    cost = costing.call_cost({"started_at": "2026-01-05T09:00:00+00:00", "ended_at": "2026-01-05T09:03:00+00:00", "turn_count": 12,
                              "input_tokens": 19_800, "output_tokens": 500, "tts_chars": 751})
    assert 0.19 <= cost <= 0.24                      # docs/ECONOMICS.md models ~$0.21 per 3-minute call


def test_a_call_with_missing_data_is_still_costed_not_free_and_not_a_crash():
    from app import costing

    assert costing.call_cost({}) > 0
    assert costing.call_cost({"started_at": "garbage", "ended_at": None, "turn_count": None}) > 0


def test_month_usage_counts_only_this_client_and_this_month(S):
    client, main, storage = S
    from app import costing

    now = datetime.now(timezone.utc)
    _add_call(storage, "CA_A1", tts=1000)
    _add_call(storage, "CA_A2", tts=1000)
    _add_call(storage, "CA_OTHER", client="demo_dental", tts=1000)
    _add_call(storage, "CA_OLD", started=now.replace(year=now.year - 1))
    cost, calls = costing.month_usage("demo_hvac")
    assert calls == 2 and 0.05 < cost < 0.2


@pytest.mark.parametrize("cost_pct,level", [(0, 0), (74, 0), (75, 75), (89, 75), (90, 90), (99, 90), (100, 100), (250, 100)])
def test_levels(S, monkeypatch, cost_pct, level):
    client, main, storage = S
    from app import costing

    monkeypatch.setattr(costing, "month_usage", lambda cid, now=None: (cost_pct * 1.0, 10))
    st = costing.status(_cfg(monthly_cost_ceiling_usd=100))
    assert st["level"] == level and st["over"] is (cost_pct >= 100)


def test_the_call_count_is_a_second_brake_even_when_each_call_is_cheap(S, monkeypatch):
    client, main, storage = S
    from app import costing

    monkeypatch.setattr(costing, "month_usage", lambda cid, now=None: (1.0, 5))
    st = costing.status(_cfg(monthly_call_ceiling=5, monthly_cost_ceiling_usd=100))
    assert st["over"] and st["reason"] == "calls"


def test_defaults_and_environment_override(monkeypatch):
    from app import costing

    assert costing.cost_ceiling(_cfg()) == 110.0   # lowered from 150 for the worst-case loss cap (docs/WORST_CASE_CAP.md)
    monkeypatch.setenv("MONTHLY_COST_CEILING_USD", "80")
    assert costing.cost_ceiling(_cfg()) == 80.0
    assert costing.cost_ceiling(_cfg(monthly_cost_ceiling_usd=200)) == 200.0


def test_config_rejects_nonsense_ceilings_and_modes():
    from app.config import ClientConfig, load_client_config

    base = load_client_config("demo_hvac").model_dump()
    for bad in ({"monthly_cost_ceiling_usd": 0}, {"monthly_cost_ceiling_usd": -5}, {"ceiling_mode": "ignore"}):
        with pytest.raises(Exception):
            ClientConfig.model_validate({**base, **bad})


# ---------------------------------------------------------------- warnings

def _alerts(monkeypatch):
    from app import ops

    seen, owner = [], []
    monkeypatch.setattr(ops, "alert_operator", lambda title, body, **kw: seen.append(body) or True)
    monkeypatch.setattr(ops.notify, "notify_owner", lambda cfg, **kw: owner.append(kw))
    return seen, owner


def test_each_level_warns_the_operator_once_per_month(S, monkeypatch):
    client, main, storage = S
    from app import costing, ops

    seen, owner = _alerts(monkeypatch)
    cfg = _cfg(monthly_cost_ceiling_usd=100, owner_email="owner@example.com")
    for pct in (50, 76, 76, 91, 91, 120, 130):
        monkeypatch.setattr(costing, "month_usage", lambda cid, now=None, p=pct: (float(p), 10))
        ops.check_cost_levels(cfg)
    assert [("75%" in m, "90%" in m, "100%" in m) for m in seen] == [(True, False, False), (False, True, False), (False, False, True)]
    assert len(owner) == 1 and "limit" in owner[0]["title"].lower() and "$" not in owner[0]["body"]      # the client never sees our costs


def test_jumping_straight_past_several_levels_sends_each_one(S, monkeypatch):
    client, main, storage = S
    from app import costing, ops

    seen, _ = _alerts(monkeypatch)
    monkeypatch.setattr(costing, "month_usage", lambda cid, now=None: (95.0, 10))
    ops.check_cost_levels(_cfg(monthly_cost_ceiling_usd=100))
    assert len(seen) == 2 and "75%" in seen[0] and "90%" in seen[1]


def test_housekeeping_checks_every_real_client(S, monkeypatch):
    client, main, storage = S
    from app import costing, ops

    seen, _ = _alerts(monkeypatch)
    monkeypatch.setattr(costing, "month_usage", lambda cid, now=None: (999.0, 999))
    result = ops.housekeeping()
    assert result["cost_warnings"] >= 1 and any("demo_hvac" in m or "demo_dental" in m for m in seen)
    assert not any("callkettle_sales" in m for m in seen)           # the operator's own line is not a customer


# ---------------------------------------------------------------- degraded modes

def _over(monkeypatch, main, **cfg_over):
    from app import costing

    cfg = _cfg(monthly_cost_ceiling_usd=10, **cfg_over)
    monkeypatch.setattr(main, "load_client_config", lambda cid: cfg)
    monkeypatch.setattr(costing, "month_usage", lambda cid, now=None: (10.5, 100))
    seen, owner = _alerts(monkeypatch)
    return cfg, seen, owner


def test_message_mode_takes_a_message_and_never_starts_the_model(S, monkeypatch):
    client, main, storage = S
    from app import agent

    _over(monkeypatch, main)
    monkeypatch.setattr(agent, "_anthropic_client", lambda: (_ for _ in ()).throw(AssertionError("the model must not be used")))
    r = client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_M1", "From": CALLER})
    assert "<Gather" in r.text and "/voice/ceiling-message" in r.text and "not available" in r.text and "<Dial" not in r.text
    assert agent.get_session("CA_M1") is None and storage.get_call("CA_M1")["outcome"] == "over_ceiling"


def test_a_message_is_recorded_for_the_owner_then_the_call_ends(S, monkeypatch):
    client, main, storage = S
    cfg, seen, owner = _over(monkeypatch, main)
    client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_M2", "From": CALLER})
    r = client.post("/voice/ceiling-message?client_id=demo_hvac&retry=0",
                    data={"CallSid": "CA_M2", "From": CALLER, "SpeechResult": "This is Pat, my number is+15555550100, the AC is out"})
    assert "<Hangup" in r.text and "call you back" in r.text
    conn = sqlite3.connect(storage.DB_PATH)
    reason, summary = conn.execute("SELECT reason, summary FROM escalations WHERE call_sid='CA_M2'").fetchone()
    conn.close()
    assert reason == "over_limit_message" and "AC is out" in summary


def test_an_emergency_in_message_mode_still_gets_911_and_a_transfer(S, monkeypatch):
    client, main, storage = S
    cfg, _, _ = _over(monkeypatch, main)
    r = client.post("/voice/ceiling-message?client_id=demo_hvac&retry=0",
                    data={"CallSid": "CA_M3", "From": CALLER, "SpeechResult": "there is a gas leak and I smell gas"})
    assert "911" in r.text and "<Dial" in r.text and cfg.escalation_phone.replace("+", "") in r.text.replace("+", "")


def test_silence_gets_one_retry_then_still_leaves_a_callback_record(S, monkeypatch):
    client, main, storage = S
    _over(monkeypatch, main)
    r1 = client.post("/voice/ceiling-message?client_id=demo_hvac&retry=0", data={"CallSid": "CA_M4", "From": CALLER})
    assert "<Gather" in r1.text and "retry=1" in r1.text
    r2 = client.post("/voice/ceiling-message?client_id=demo_hvac&retry=1", data={"CallSid": "CA_M4", "From": CALLER})
    assert "<Hangup" in r2.text
    conn = sqlite3.connect(storage.DB_PATH)
    assert conn.execute("SELECT caller_phone, summary FROM escalations WHERE call_sid='CA_M4'").fetchone()[0] == CALLER
    conn.close()


def test_transfer_mode_rings_the_owner_and_takes_a_message_if_unanswered(S, monkeypatch):
    client, main, storage = S
    _over(monkeypatch, main, ceiling_mode="transfer")
    r = client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_M5", "From": CALLER})
    assert "<Dial" in r.text and "/voice/transfer-result" in r.text


def test_hostile_speech_in_a_message_is_stored_as_data_not_markup(S, monkeypatch):
    client, main, storage = S
    _over(monkeypatch, main)
    r = client.post("/voice/ceiling-message?client_id=demo_hvac&retry=0",
                    data={"CallSid": "CA_M6", "From": CALLER, "SpeechResult": "<Hangup/><Dial>+15555550100</Dial>" + "x" * 5000})
    assert r.text.count("<Dial") == 0 and r.status_code == 200


def test_unknown_client_and_unsigned_requests_are_refused(S, monkeypatch):
    client, main, storage = S
    r = client.post("/voice/ceiling-message?client_id=does_not_exist", data={"CallSid": "CA_M7"})
    assert r.status_code == 200 and "<Hangup" in r.text
    monkeypatch.setattr("app.twilio_utils._SKIP_SIGNATURE_CHECK", False)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "x" * 32)
    assert client.post("/voice/ceiling-message?client_id=demo_hvac", data={"CallSid": "CA_M8"}).status_code == 403


def test_a_broken_cost_estimate_lets_the_call_through_instead_of_blocking_it(S, monkeypatch):
    client, main, storage = S
    from app import costing

    monkeypatch.setattr(costing, "status", lambda cfg: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    r = client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_M9", "From": CALLER})
    assert "<Gather" in r.text and "ceiling-message" not in r.text
