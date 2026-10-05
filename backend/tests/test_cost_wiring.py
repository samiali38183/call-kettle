"""Private cost observability wired into the live call path (local, synthetic, no provider calls)."""
import sqlite3
import time
from types import SimpleNamespace

import pytest

from app import agent, config as cfg, cost_observability as co, storage

CALLER_TEXT = "my number is+15555550100 and my secret"


def _usage(**kw):
    return SimpleNamespace(**kw)


def _response(**kw):
    return SimpleNamespace(usage=_usage(**kw))


@pytest.fixture
def env(app_client):
    client, main = app_client
    storage.log_call_start("CA1", "demo_dental", "+15555550100")
    storage.log_call_start("CA2", "other_tenant", "+15555550100")
    session = agent.start_session("CA1", cfg.load_client_config("demo_dental"), caller_number="+15555550100")
    yield client, main, session
    agent.end_session("CA1")


def _ev(sid="CA1", tenant="demo_dental"):
    return co.read_evidence(storage.DB_PATH, tenant, sid)


def test_init_db_creates_ledger_idempotently(app_client):
    storage.init_db()
    storage.init_db()
    with sqlite3.connect(storage.DB_PATH) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='private_cost_usage'").fetchone()


def test_usage_recorded_as_measured_cumulative_not_delta(env):
    agent.record_usage("CA1", _response(input_tokens=100, output_tokens=10), time.perf_counter())
    agent.record_usage("CA1", _response(input_tokens=50, output_tokens=5), time.perf_counter())
    ev = _ev()
    assert ev["input_tokens"]["value"] == 150 and ev["input_tokens"]["status"] == "measured"
    assert ev["output_tokens"]["value"] == 15
    assert ev["input_tokens"]["source"] == "anthropic_usage"


def test_missing_cache_fields_stay_unknown_and_none_is_not_zero(env):
    agent.record_usage("CA1", _response(input_tokens=7, output_tokens=3,
                                        cache_read_input_tokens=None), time.perf_counter())
    ev = _ev()
    assert "cache_read_tokens" not in ev and "cache_write_tokens" not in ev


def test_reported_cache_fields_recorded_cumulatively(env):
    agent.record_usage("CA1", _response(input_tokens=7, output_tokens=3, cache_read_input_tokens=4,
                                        cache_creation_input_tokens=0), time.perf_counter())
    agent.record_usage("CA1", _response(input_tokens=7, output_tokens=3, cache_read_input_tokens=6,
                                        cache_creation_input_tokens=2), time.perf_counter())
    ev = _ev()
    assert ev["cache_read_tokens"]["value"] == 10 and ev["cache_write_tokens"]["value"] == 2


def test_no_usage_object_records_nothing(env):
    agent.record_usage("CA1", SimpleNamespace(usage=None), time.perf_counter())
    assert _ev() == {}


def test_observability_failure_never_breaks_usage_logging(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ledger down")
    monkeypatch.setattr(co, "record_snapshot", boom)
    agent.record_usage("CA1", _response(input_tokens=9, output_tokens=2), time.perf_counter())
    with sqlite3.connect(storage.DB_PATH) as conn:
        row = conn.execute("SELECT input_tokens, output_tokens, model_calls FROM calls WHERE call_sid='CA1'").fetchone()
    assert row == (9, 2, 1)  # existing behaviour intact


def test_tenant_isolation(env):
    agent.record_usage("CA1", _response(input_tokens=9, output_tokens=2), time.perf_counter())
    assert _ev("CA1", "other_tenant") == {}
    assert _ev("CA2", "other_tenant") == {}


def test_gather_records_counts_and_never_changes_response(env, monkeypatch):
    client, main, session = env
    monkeypatch.setattr(main.agent, "run_turn", lambda s, t: ("Connecting you now.", True, "+15550000111"))
    body = {"CallSid": "CA1", "SpeechResult": CALLER_TEXT, "From": "+15555550100"}
    plain = client.post("/voice/gather?client_id=demo_dental&retry=0", data=body)
    ev = _ev()
    assert "gather_count" not in ev  # superseded: gathers are counted where emitted (test_cost_wiring_gaps.py); a transfer reply emits none
    assert ev["tts_chars"]["value"] == len("Connecting you now.")
    assert ev["transfer_count"] == {"value": 1, "status": "measured", "source": "twilio_gather_callback"}

    storage.log_call_start("CA3", "demo_dental", "+15555550100")
    agent.start_session("CA3", cfg.load_client_config("demo_dental"))
    monkeypatch.setattr(co, "record_snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    broken = client.post("/voice/gather?client_id=demo_dental&retry=0", data={**body, "CallSid": "CA3"})
    assert broken.status_code == plain.status_code == 200
    assert broken.text == plain.text


def test_ledger_has_no_caller_text_or_phone(env, monkeypatch):
    client, main, session = env
    monkeypatch.setattr(main.agent, "run_turn", lambda s, t: ("Okay.", False, None))
    client.post("/voice/gather?client_id=demo_dental&retry=0",
                data={"CallSid": "CA1", "SpeechResult": CALLER_TEXT, "From": "+15555550100"})
    agent.record_usage("CA1", _response(input_tokens=1, output_tokens=1), time.perf_counter())
    with sqlite3.connect(storage.DB_PATH) as conn:
        dump = repr(conn.execute("SELECT * FROM private_cost_usage").fetchall())
    assert "703" not in dump and "secret" not in dump and "5550199" not in dump


def test_status_callback_records_carrier_seconds_only_from_twilio_field(env):
    client, main, session = env
    r = client.post("/voice/status", data={"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "61"})
    assert r.status_code == 200
    ev = _ev()
    assert ev["carrier_seconds"] == {"value": 61, "status": "measured", "source": "twilio_status_callback"}

    storage.log_call_start("CA4", "demo_dental", "+1")
    client.post("/voice/status", data={"CallSid": "CA4", "CallStatus": "completed"})  # no CallDuration
    assert _ev("CA4") == {}
    client.post("/voice/status", data={"CallSid": "CA4", "CallStatus": "completed", "CallDuration": "abc"})
    assert _ev("CA4") == {}


def test_status_callback_survives_ledger_failure(env, monkeypatch):
    client, main, session = env
    monkeypatch.setattr(co, "record_snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    r = client.post("/voice/status", data={"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "5"})
    assert r.status_code == 200
    assert storage.get_call("CA1")["outcome"] == "caller_hung_up"


def test_transfer_result_records_dial_seconds_from_twilio(env):
    client, main, session = env
    r = client.post("/voice/transfer-result?client_id=demo_dental",
                    data={"CallSid": "CA1", "DialCallStatus": "completed", "DialCallDuration": "42"})
    assert r.status_code == 200
    ev = _ev()
    assert ev["transfer_seconds"]["value"] == [42] and ev["transfer_seconds"]["status"] == "measured"
