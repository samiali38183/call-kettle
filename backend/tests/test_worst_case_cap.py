"""Worst-case loss cap (owner directive 2026-10-04): a customer who maxes out calls must never push cash profit more than
$100 below normal expected profit. These tests pin the code-side limits the model in marketing/cash_margin.py assumes:
a post-ceiling hard stop (<Reject>, unbilled), capped transfer legs after the ceiling, no repeat emergency dial loop,
a concurrency cap, an in-flight reserve in the guard, and safe defaults. Normal calls must stay byte-identical."""
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

CALLER = "+15555550100"
ROOT = Path(__file__).resolve().parents[2]


def _cfg(**over):
    from app.config import load_client_config

    return load_client_config("demo_hvac").model_copy(update=over)


@pytest.fixture
def S(app_client):
    client, main = app_client
    from app import storage

    return client, main, storage


def _over(monkeypatch, main, **cfg_over):
    from app import costing, ops

    cfg = _cfg(monthly_cost_ceiling_usd=10, **cfg_over)
    monkeypatch.setattr(main, "load_client_config", lambda cid: cfg)
    monkeypatch.setattr(costing, "month_usage", lambda cid, now=None: (10.5, 100))
    monkeypatch.setattr(ops, "alert_operator", lambda *a, **k: True)
    monkeypatch.setattr(ops.notify, "notify_owner", lambda *a, **k: None)
    return cfg


# ------------------------------------------------------------------ TwiML building blocks

def test_reject_twiml_makes_reject_the_first_verb():
    from app import twilio_utils

    xml = twilio_utils.reject_twiml()
    assert "<Reject" in xml and xml.index("<Response>") < xml.index("<Reject") and "<Say" not in xml and "<Gather" not in xml


def test_transfer_twiml_default_is_byte_identical_and_time_limit_is_opt_in():
    from app import twilio_utils

    base = twilio_utils.transfer_twiml(say_text="Hi", phone_number="+15555550100", action_url="https://x.test/r")
    assert "timeLimit" not in base and '<Dial timeout="25" action="https://x.test/r" method="POST">' in base
    limited = twilio_utils.transfer_twiml(say_text="Hi", phone_number="+15555550100", action_url="https://x.test/r", time_limit=300)
    assert 'timeLimit="300"' in limited
    assert limited.replace(' timeLimit="300"', "") == base


# ------------------------------------------------------------------ config + defaults

def test_new_config_fields_have_safe_defaults_and_bounds():
    from app.config import ClientConfig, load_client_config

    cfg = load_client_config("demo_hvac")
    assert cfg.post_ceiling_call_cap is None and cfg.ceiling_transfer_seconds == 300
    base = cfg.model_dump()
    for bad in ({"post_ceiling_call_cap": -1}, {"ceiling_transfer_seconds": 0}, {"ceiling_transfer_seconds": 99999}):
        with pytest.raises(Exception):
            ClientConfig.model_validate({**base, **bad})


def test_defaults_are_the_documented_safe_values(monkeypatch):
    from app import costing

    monkeypatch.delenv("MONTHLY_COST_CEILING_USD", raising=False)
    monkeypatch.delenv("POST_CEILING_CALL_CAP", raising=False)
    assert costing.DEFAULT_COST_CEILING_USD == 110.0 and costing.cost_ceiling(_cfg()) == 110.0
    assert costing.DEFAULT_POST_CEILING_CALL_CAP == 60 and costing.post_ceiling_cap(_cfg()) == 60
    assert costing.post_ceiling_cap(_cfg(post_ceiling_call_cap=5)) == 5
    monkeypatch.setenv("POST_CEILING_CALL_CAP", "9")
    assert costing.post_ceiling_cap(_cfg()) == 9
    assert costing.MAX_CONCURRENT_CALLS == 8 and costing.OPEN_CALL_RESERVE_USD == 0.25


def test_backend_defaults_pass_the_marketing_worst_case_model_in_both_modes(monkeypatch):
    from app import costing, config

    monkeypatch.syspath_prepend(str(ROOT))
    from marketing import cash_margin as cm

    assert Decimal(str(costing.DEFAULT_COST_CEILING_USD)) == cm.BACKEND_DEFAULT_CEILING
    assert costing.DEFAULT_POST_CEILING_CALL_CAP == cm.BACKEND_POST_CEILING_CALL_CAP
    assert costing.MAX_CONCURRENT_CALLS == cm.BACKEND_CONCURRENCY
    assert Decimal(str(costing.OPEN_CALL_RESERVE_USD)) == cm.BACKEND_RESERVE_PER_OPEN_CALL
    assert config.ClientConfig.model_fields["ceiling_transfer_seconds"].default == cm.CEILING_TRANSFER_SECONDS
    for mode in ("message", "transfer"):
        assert cm.worst_case_scenarios()["defaults"][mode]["status"] == "pass"


# ------------------------------------------------------------------ post-ceiling hard stop

def _add_over_ceiling_metrics(storage, n, client="demo_hvac"):
    for _ in range(n):
        storage.record_metric("over_ceiling_call", client)


def test_post_ceiling_calls_counts_this_clients_metrics_this_month(S):
    client, main, storage = S
    from app import costing

    _add_over_ceiling_metrics(storage, 3)
    _add_over_ceiling_metrics(storage, 2, client="demo_dental")
    with storage._conn() as conn:
        conn.execute("INSERT INTO metrics (at, name, client_id, value) VALUES (?, 'over_ceiling_call', 'demo_hvac', 1)", ("2020-01-01T00:00:00+00:00",))
    assert costing.post_ceiling_calls("demo_hvac") == 3


@pytest.mark.parametrize("mode", ["message", "transfer"])
def test_below_the_post_ceiling_cap_degraded_modes_behave_exactly_as_before(S, monkeypatch, mode):
    client, main, storage = S
    _over(monkeypatch, main, ceiling_mode=mode, post_ceiling_call_cap=3)
    _add_over_ceiling_metrics(storage, 2)
    r = client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_W1", "From": CALLER})
    assert "<Reject" not in r.text and ("<Dial" in r.text if mode == "transfer" else "<Gather" in r.text)
    assert storage.get_call("CA_W1")["outcome"] == "over_ceiling"


@pytest.mark.parametrize("mode", ["message", "transfer"])
def test_at_the_post_ceiling_cap_the_line_rejects_the_call_unbilled_with_no_ai_gather_or_dial(S, monkeypatch, mode):
    client, main, storage = S
    from app import agent

    _over(monkeypatch, main, ceiling_mode=mode, post_ceiling_call_cap=3)
    monkeypatch.setattr(agent, "_anthropic_client", lambda: (_ for _ in ()).throw(AssertionError("the model must not be used")))
    _add_over_ceiling_metrics(storage, 3)
    r = client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_W2", "From": CALLER})
    assert r.text.count("<Reject") == 1 and "<Say" not in r.text and "<Gather" not in r.text and "<Dial" not in r.text
    assert storage.get_call("CA_W2") is None                        # no row: a flood must not grow the database either
    with storage._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM metrics WHERE name = 'post_ceiling_rejected'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM metrics WHERE name = 'over_ceiling_call'").fetchone()[0] == 3


def test_cap_zero_rejects_every_post_ceiling_call(S, monkeypatch):
    client, main, storage = S
    _over(monkeypatch, main, post_ceiling_call_cap=0)
    r = client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_W3", "From": CALLER})
    assert "<Reject" in r.text


def test_a_normal_call_below_every_ceiling_is_untouched_by_the_hard_stop(S, monkeypatch):
    client, main, storage = S
    from app import costing

    monkeypatch.setattr(costing, "month_usage", lambda cid, now=None: (20.0, 80))
    _add_over_ceiling_metrics(storage, 500)                             # stale counter must not matter when not over
    r = client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_W4", "From": CALLER})
    assert "<Reject" not in r.text and "<Gather" in r.text


def test_if_the_counter_cannot_be_read_the_call_is_still_handled_not_dropped(S, monkeypatch):
    client, main, storage = S
    from app import costing

    _over(monkeypatch, main)
    monkeypatch.setattr(costing, "post_ceiling_calls", lambda cid, now=None: (_ for _ in ()).throw(RuntimeError("db")))
    r = client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_W5", "From": CALLER})
    assert "<Reject" not in r.text and "<Gather" in r.text


# ------------------------------------------------------------------ capped transfer legs after the ceiling

def test_transfer_mode_dial_is_time_limited(S, monkeypatch):
    client, main, storage = S
    _over(monkeypatch, main, ceiling_mode="transfer", ceiling_transfer_seconds=240)
    r = client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_T1", "From": CALLER})
    assert 'timeLimit="240"' in r.text


def test_emergency_dial_in_message_mode_is_time_limited(S, monkeypatch):
    client, main, storage = S
    _over(monkeypatch, main)
    r = client.post("/voice/ceiling-message?client_id=demo_hvac&retry=0",
                    data={"CallSid": "CA_T2", "From": CALLER, "SpeechResult": "there is a gas leak and I smell gas"})
    assert "<Dial" in r.text and 'timeLimit="300"' in r.text and "911" in r.text


def test_over_ceiling_emergency_dial_never_loops_back_into_another_message_prompt(S, monkeypatch):
    """transfer mode: owner unanswered -> message prompt -> emergency phrase -> ONE capped Dial with no action URL, so the
    call cannot loop Dial -> Gather -> Dial (each pass would cost carrier minutes and an owner SMS)."""
    client, main, storage = S
    _over(monkeypatch, main, ceiling_mode="transfer")
    client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_T3", "From": CALLER})
    r = client.post("/voice/transfer-result?client_id=demo_hvac", data={"CallSid": "CA_T3", "From": CALLER, "DialCallStatus": "no-answer"})
    assert "/voice/ceiling-message" in r.text and "<Dial" not in r.text
    r2 = client.post("/voice/ceiling-message?client_id=demo_hvac&retry=0",
                     data={"CallSid": "CA_T3", "From": CALLER, "SpeechResult": "there is a gas leak and I smell gas"})
    assert r2.text.count("<Dial") == 1 and 'timeLimit="300"' in r2.text and "action=" not in r2.text.split("<Dial")[1].split(">")[0]


def test_ceiling_message_urls_are_unchanged(S, monkeypatch):
    client, main, storage = S
    _over(monkeypatch, main)
    r = client.post("/voice/ceiling-message?client_id=demo_hvac&retry=0", data={"CallSid": "CA_T4", "From": CALLER})
    assert "retry=1" in r.text and "xfer" not in r.text


def test_normal_transfers_keep_twilios_default_time_limit(S, monkeypatch):
    client, main, storage = S
    from app import costing

    monkeypatch.setattr(costing, "month_usage", lambda cid, now=None: (1.0, 1))
    cfg = _cfg(routing_mode="owner_first")
    monkeypatch.setattr(main, "load_client_config", lambda cid: cfg)
    r = client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_T5", "From": CALLER})
    assert "timeLimit" not in r.text


# ------------------------------------------------------------------ concurrency cap + in-flight reserve

def _open_call(storage, sid, client="demo_hvac", age_seconds=10, ended=False):
    started = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - age_seconds, timezone.utc)
    with storage._conn() as conn:
        conn.execute("INSERT INTO calls (call_sid, client_id, from_number, started_at, ended_at) VALUES (?, ?, ?, ?, ?)",
                     (sid, client, "+15555550100", started.isoformat(), started.isoformat() if ended else None))


def test_open_call_count_ignores_ended_stale_other_client_and_excluded_calls(S):
    client, main, storage = S
    _open_call(storage, "CA_O1")
    _open_call(storage, "CA_O2", ended=True)
    _open_call(storage, "CA_O3", age_seconds=3600)
    _open_call(storage, "CA_O4", client="demo_dental")
    _open_call(storage, "CA_O5")
    assert storage.open_call_count("demo_hvac", within_seconds=480) == 2
    assert storage.open_call_count("demo_hvac", within_seconds=480, exclude_sid="CA_O5") == 1


def test_status_adds_a_reserve_for_open_calls_and_trips_earlier(S, monkeypatch):
    client, main, storage = S
    from app import costing

    monkeypatch.setattr(costing, "month_usage", lambda cid, now=None: (99.9, 10))
    cfg = _cfg(monthly_cost_ceiling_usd=100)
    assert costing.status(cfg)["over"] is False and costing.status(cfg)["reserve"] == 0.0
    for i in range(2):
        _open_call(storage, f"CA_R{i}")
    st = costing.status(cfg)
    assert st["open_calls"] == 2 and st["reserve"] == 0.5 and st["over"] is True and st["reason"] == "cost"
    assert st["percent"] == 99 and st["level"] == 90                    # the warning levels still read the plain estimate


def test_status_is_byte_identical_for_a_quiet_line(S, monkeypatch):
    client, main, storage = S
    from app import costing

    monkeypatch.setattr(costing, "month_usage", lambda cid, now=None: (50.0, 10))
    st = costing.status(_cfg(monthly_cost_ceiling_usd=100))
    assert st["cost"] == 50.0 and st["percent"] == 50 and st["level"] == 0 and st["over"] is False and st["reason"] is None


def test_the_ninth_simultaneous_call_is_rejected_unbilled(S, monkeypatch):
    client, main, storage = S
    from app import costing

    monkeypatch.setattr(costing, "month_usage", lambda cid, now=None: (1.0, 1))
    for i in range(costing.MAX_CONCURRENT_CALLS):
        _open_call(storage, f"CA_C{i}")
    r = client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_C_NEW", "From": CALLER})
    assert r.text.count("<Reject") == 1 and "<Gather" not in r.text and storage.get_call("CA_C_NEW") is None


def test_seven_other_open_calls_do_not_block_the_eighth(S, monkeypatch):
    client, main, storage = S
    from app import costing

    monkeypatch.setattr(costing, "month_usage", lambda cid, now=None: (1.0, 1))
    for i in range(costing.MAX_CONCURRENT_CALLS - 1):
        _open_call(storage, f"CA_D{i}")
    r = client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_D_NEW", "From": CALLER})
    assert "<Reject" not in r.text and "<Gather" in r.text


def test_the_calls_own_open_row_never_counts_against_it(S, monkeypatch):
    client, main, storage = S
    from app import costing

    monkeypatch.setattr(costing, "month_usage", lambda cid, now=None: (1.0, 1))
    for i in range(costing.MAX_CONCURRENT_CALLS - 1):
        _open_call(storage, f"CA_E{i}")
    _open_call(storage, "CA_E_SELF")                                   # e.g. a redirect: the row already exists
    r = client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_E_SELF", "From": CALLER})
    assert "<Reject" not in r.text


# ------------------------------------------------------------------ reporting only: cost_report worst-case block

def test_cost_report_prints_worst_case_exposure_against_the_100_dollar_cap(tmp_path, monkeypatch):
    import importlib.util

    monkeypatch.syspath_prepend(str(ROOT))
    path = Path(__file__).resolve().parents[1] / "scripts" / "cost_report.py"
    spec = importlib.util.spec_from_file_location("cost_report_wc", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    block = mod.worst_case_block("demo_hvac")
    assert block["available"] is True
    text = mod.render_worst_case(block)
    assert "worst-case exposure" in text and "$100" in text and "MODELED" in text
    for mode in ("message", "transfer"):
        assert mode in text
    assert "PASS" in text and "497" not in text
    off = mod.render_worst_case({"available": False, "reason": "no config"})
    assert "UNKNOWN" in off


def test_cost_report_block_flags_a_client_whose_config_fails_the_cap(monkeypatch):
    import importlib.util

    monkeypatch.syspath_prepend(str(ROOT))
    path = Path(__file__).resolve().parents[1] / "scripts" / "cost_report.py"
    spec = importlib.util.spec_from_file_location("cost_report_wc2", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from app.config import load_client_config

    cfg = load_client_config("demo_hvac").model_copy(update={"monthly_cost_ceiling_usd": 150.0})
    block = mod.worst_case_block("demo_hvac", config=cfg)
    assert block["status"] == "fail" and "FAIL" in mod.render_worst_case(block)
    cfg = load_client_config("demo_hvac").model_copy(update={"monthly_cost_ceiling_usd": 100000})
    assert mod.worst_case_block("demo_hvac", config=cfg)["status"] == "fail"
