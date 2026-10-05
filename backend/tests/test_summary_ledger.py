"""Post-call summarizer tokens land in the private cost ledger under the same call_sid, tenant-scoped."""
import json
import os
import tempfile
from dataclasses import dataclass

import pytest

SECRET_TEXT = "my secret ssn 123-45-6789 and card"


@pytest.fixture(autouse=True)
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("CALLKETTLE_DB_PATH", path)
    monkeypatch.setenv("CALLKETTLE_SKIP_SIGNATURE_CHECK", "1")
    import importlib

    from app import storage

    importlib.reload(storage)
    storage.init_db()
    yield storage
    os.remove(path)


@dataclass
class _Block:
    text: str
    type: str = "text"


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


@dataclass
class _Resp:
    content: list
    usage: object
    stop_reason: str = "end_turn"


class _Client:
    def __init__(self, response):
        self.response = response
        self.messages = self

    def create(self, **kwargs):
        return self.response


def _call(storage, sid, client_id="demo_dental", text=SECRET_TEXT):
    storage.log_call_start(sid, client_id, "+1703****0100")
    storage.log_turn(sid, "caller", text)
    storage.log_turn(sid, "ai", "Okay.")


def _ev(storage, tenant, sid):
    from app import cost_observability as co

    return co.read_evidence(storage.DB_PATH, tenant, sid)


def test_summary_tokens_are_recorded_after_the_session_ended(monkeypatch, temp_db):
    from app import agent, summary

    _call(temp_db, "CS1")
    monkeypatch.setattr(agent, "_anthropic_client", lambda: _Client(_Resp([_Block("Caller asked something.")], _Usage(400, 60))))
    summary.summarize_call("CS1")
    ev = _ev(temp_db, "demo_dental", "CS1")
    assert ev["input_tokens"] == {"value": 400, "status": "measured", "source": "anthropic_usage"}
    assert ev["output_tokens"]["value"] == 60
    with temp_db._conn() as conn:
        assert tuple(conn.execute("SELECT input_tokens, output_tokens FROM calls WHERE call_sid='CS1'").fetchone()) == (400, 60)   # calls table unchanged


def test_summary_tokens_add_to_the_in_call_totals(monkeypatch, temp_db):
    from app import agent, summary

    _call(temp_db, "CS2")
    agent.observe("demo_dental", "CS2", {"input_tokens": {"value": 1000, "status": "measured", "source": "anthropic_usage"},
                                         "output_tokens": {"value": 100, "status": "measured", "source": "anthropic_usage"}}, 3)
    monkeypatch.setattr(agent, "_anthropic_client", lambda: _Client(_Resp([_Block("ok")], _Usage(400, 60))))
    summary.summarize_call("CS2")
    ev = _ev(temp_db, "demo_dental", "CS2")
    assert ev["input_tokens"]["value"] == 1400 and ev["output_tokens"]["value"] == 160


def test_no_double_count_when_a_session_is_still_live(monkeypatch, temp_db):
    from app import agent, summary
    from app.config import load_client_config

    _call(temp_db, "CS3")
    agent.start_session("CS3", load_client_config("demo_dental"))
    try:
        monkeypatch.setattr(agent, "_anthropic_client", lambda: _Client(_Resp([_Block("ok")], _Usage(400, 60))))
        summary.summarize_call("CS3")
        assert _ev(temp_db, "demo_dental", "CS3")["input_tokens"]["value"] == 400
    finally:
        agent.end_session("CS3")


def test_tenant_scoped_and_no_text_stored(monkeypatch, temp_db):
    from app import agent, summary

    _call(temp_db, "CS4")
    monkeypatch.setattr(agent, "_anthropic_client", lambda: _Client(_Resp([_Block("Summary with+15555550100")], _Usage(10, 5))))
    summary.summarize_call("CS4")
    assert _ev(temp_db, "someone_else", "CS4") == {}
    with temp_db._conn() as conn:
        dump = json.dumps([tuple(r) for r in conn.execute("SELECT * FROM private_cost_usage").fetchall()])
    assert "secret" not in dump and "703" not in dump and "Summary" not in dump


def test_ledger_failure_never_breaks_the_summary(monkeypatch, temp_db):
    from app import agent, cost_observability, summary

    _call(temp_db, "CS5")

    def boom(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(cost_observability, "add_token_usage", boom, raising=False)
    monkeypatch.setattr(cost_observability, "record_snapshot", boom)
    monkeypatch.setattr(agent, "_anthropic_client", lambda: _Client(_Resp([_Block("Still saved.")], _Usage(10, 5))))
    summary.summarize_call("CS5")
    assert temp_db.get_call("CS5")["summary"] == "Still saved."


def test_missing_usage_fields_stay_unknown_not_zero(monkeypatch, temp_db):
    from app import agent, summary

    _call(temp_db, "CS6")

    class NoUsage:
        content = [_Block("ok")]
        usage = None
        stop_reason = "end_turn"

    monkeypatch.setattr(agent, "_anthropic_client", lambda: _Client(NoUsage()))
    summary.summarize_call("CS6")
    assert _ev(temp_db, "demo_dental", "CS6") == {}
