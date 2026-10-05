"""Offline trial policy acceptance; every DB is disposable."""
from datetime import datetime, timedelta, timezone
import importlib

import pytest

from app import storage
from app.config import load_client_config

START = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "trial.db"))
    storage.init_db()
    return tmp_path


def trial_module():
    return importlib.import_module("app.trial")


def safe_config():
    base = load_client_config("demo_hvac")
    return base.model_copy(update={
        "client_id": "trial_customer", "demo_mode": False, "demo_menu": None,
        "demo_private_codes": False, "portal_sample": False,
        "trial_enabled": True, "escalation_phone": "", "routing_mode": "ai_first",
        "always_ring_owner": [], "ceiling_mode": "message", "stt_mode": "gather",
        "policy": base.policy.model_copy(update={"can_transfer": False, "emergency_action": "message"}),
    })


def test_activation_is_explicit_and_does_not_touch_billing(db):
    trial = trial_module()
    cfg = safe_config()
    plan = trial.activate(cfg, START, approved_by="operator", owner_notified=True)
    assert plan["applied"] is False
    assert plan["ends_at"] == (START + timedelta(days=7)).isoformat()
    with storage._conn() as conn:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='customer_trials'").fetchone()
    applied = trial.activate(cfg, START, approved_by="operator", owner_notified=True, apply=True)
    assert applied["applied"] is True
    assert trial.admit(cfg, "CAone", now=START).allowed


def test_default_off_but_enabled_missing_corrupt_or_locked_evidence_fails_closed(db, monkeypatch):
    trial = trial_module()
    cfg = safe_config()
    assert trial.admit(cfg.model_copy(update={"trial_enabled": False}), "", now=START).allowed
    assert not trial.admit(cfg, "CAnew", now=START).allowed
    trial.activate(cfg, START, approved_by="operator", owner_notified=True, apply=True)
    with storage._conn() as conn:
        conn.execute("UPDATE customer_trials SET ends_at='broken'")
    assert not trial.admit(cfg, "CAnew", now=START).allowed
    monkeypatch.setattr(storage, "_conn", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("unavailable")))
    assert not trial.admit(cfg, "CAnew", now=START).allowed
    assert trial.admit(cfg.model_copy(update={"trial_enabled": False}), "CAnew", now=START).allowed


def test_boundaries_invalid_identifier_and_durable_stop(db):
    trial = trial_module()
    cfg = safe_config()
    trial.activate(cfg, START, approved_by="operator", owner_notified=True, apply=True)
    assert not trial.admit(cfg, "CAearly", now=START - timedelta(microseconds=1)).allowed
    assert not trial.admit(cfg, "", now=START).allowed
    assert not trial.admit(cfg, "x" * 129, now=START).allowed
    assert trial.admit(cfg, "CAvalid", now=START).allowed
    assert not trial.admit(cfg, "CAvalid", now=START + timedelta(days=7)).allowed
    with storage._conn() as conn:
        conn.execute("UPDATE customer_trials SET state='stopped'")
    assert not trial.admit(cfg, "CAvalid", now=START).allowed


@pytest.mark.parametrize("change", [
    {"demo_mode": True}, {"trial_enabled": False}, {"escalation_phone": "+15555550100"},
    {"routing_mode": "owner_first"}, {"always_ring_owner": ["+15555550100"]},
    {"max_turns": 13}, {"max_call_seconds": 361}, {"ceiling_mode": "transfer"},
    {"stt_mode": "stream"}, {"policy": load_client_config("demo_hvac").policy},
])
def test_activation_refuses_unbounded_or_demo_config(db, change):
    trial = trial_module()
    with pytest.raises(ValueError):
        trial.activate(safe_config().model_copy(update=change), START, approved_by="operator", owner_notified=True, apply=True)


@pytest.mark.parametrize("approver, notified", [("", True), ("operator", False)])
def test_activation_requires_operator_and_owner_notice(db, approver, notified):
    with pytest.raises(ValueError):
        trial_module().activate(safe_config(), START, approved_by=approver, owner_notified=notified, apply=True)


def test_validated_config_trial_marker_defaults_off_and_rejects_demo():
    from app.config import ClientConfig
    raw = load_client_config("demo_hvac").model_dump()
    assert ClientConfig.model_validate(raw).trial_enabled is False
    with pytest.raises(ValueError):
        ClientConfig.model_validate({**raw, "trial_enabled": True})


def test_activation_cannot_restart_or_extend_trial(db):
    trial = trial_module()
    cfg = safe_config()
    trial.activate(cfg, START, approved_by="operator", owner_notified=True, apply=True)
    with pytest.raises(ValueError):
        trial.activate(cfg, START + timedelta(days=1), approved_by="operator", owner_notified=True, apply=True)


def test_signed_initial_gate_rejects_before_any_paid_pipeline(app_client, monkeypatch):
    c, main = app_client
    trial = trial_module()
    cfg = safe_config()
    monkeypatch.setattr(main, "load_client_config", lambda _: cfg)
    alerts = []
    monkeypatch.setattr(main.ops, "alert_operator", lambda *args, **kwargs: alerts.append(args))
    def forbidden(*args, **kwargs):
        raise AssertionError("Rejected trial reached a paid pipeline")
    monkeypatch.setattr(main, "_ai_greeting", forbidden)
    monkeypatch.setattr(main.costing, "status", forbidden)
    monkeypatch.setattr(main.storage, "log_call_start", forbidden)
    response = c.post("/voice/incoming?client_id=trial_customer", data={"CallSid": "CAmissing"})
    assert "<Reject" in response.text
    assert "<Dial" not in response.text and "<Gather" not in response.text and "<Say" not in response.text
    assert alerts
    # Missing/invalid signatures still fail before trial evidence and alerts.
    monkeypatch.setattr(main, "_validated_form", _invalid_form)
    assert c.post("/voice/incoming?client_id=trial_customer", data={"CallSid": "CAunsigned"}).status_code == 403
    assert len(alerts) == 1


async def _invalid_form(request):
    return None


def test_initial_admission_then_retry_is_rejected_not_restarted(app_client, monkeypatch):
    c, main = app_client
    trial = trial_module()
    cfg = safe_config()
    now = datetime.now(timezone.utc)
    trial.activate(cfg, now - timedelta(seconds=1), approved_by="operator", owner_notified=True, apply=True)
    monkeypatch.setattr(main, "load_client_config", lambda _: cfg)
    monkeypatch.setattr(main.ops, "alert_operator", lambda *args, **kwargs: None)
    greeted = []
    monkeypatch.setattr(main, "_ai_greeting", lambda *args: greeted.append(args) or main.Response(content="<Response/>", media_type="application/xml"))
    assert "<Reject" not in c.post("/voice/incoming?client_id=trial_customer", data={"CallSid": "CAstart"}).text
    assert "<Reject" in c.post("/voice/incoming?client_id=trial_customer", data={"CallSid": "CAstart"}).text
    assert len(greeted) == 1


def test_drifted_trial_config_fails_closed(db):
    trial = trial_module()
    cfg = safe_config()
    trial.activate(cfg, START, approved_by="operator", owner_notified=True, apply=True)
    assert not trial.admit(cfg.model_copy(update={"escalation_phone": "+15555550100"}), "CAdrift", now=START).allowed


def test_status_expiry_stop_and_explicit_conversion_without_billing(db):
    trial = trial_module()
    cfg = safe_config()
    assert trial.status(cfg.client_id, now=START)["state"] == "not_configured"
    trial.activate(cfg, START, approved_by="operator", owner_notified=True, apply=True)
    assert trial.status(cfg.client_id, now=START)["remaining_calls"] == 30
    assert trial.status(cfg.client_id, now=START + timedelta(days=7))["state"] == "expired"
    assert trial.transition(cfg.client_id, "stopped", approved_by="operator", agreement_confirmed=False)["applied"] is False
    assert trial.status(cfg.client_id, now=START)["state"] == "active"
    trial.transition(cfg.client_id, "stopped", approved_by="operator", agreement_confirmed=False, apply=True)
    assert not trial.admit(cfg, "CAstopped", now=START).allowed
    with pytest.raises(ValueError):
        trial.transition(cfg.client_id, "converted", approved_by="operator", agreement_confirmed=False, apply=True)
    trial.transition(cfg.client_id, "converted", approved_by="operator", agreement_confirmed=True, apply=True)
    assert trial.status(cfg.client_id, now=START)["state"] == "converted"
    assert trial.admit(cfg, "CApaid", now=START + timedelta(days=8)).allowed
    with storage._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM trial_admissions").fetchone()[0] == 0
        assert conn.execute("SELECT state, changed_by FROM customer_trials").fetchone() == ("converted", "operator")


def test_operator_cli_dry_run_apply_readback_stop_and_convert(db, capsys):
    from pathlib import Path
    import yaml
    path = Path(__file__).resolve().parents[1] / "scripts" / "manage_trial.py"
    spec = importlib.util.spec_from_file_location("manage_trial", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    config_path = db / "reviewed.yaml"
    config_path.write_text(yaml.safe_dump(safe_config().model_dump()), encoding="utf-8")
    common = ["trial_customer", "--db", storage.DB_PATH]
    args = ["activate", *common, "--config", str(config_path), "--go-live", START.isoformat(), "--approved-by", "operator", "--owner-notified"]
    assert cli.main(args) == 0
    assert '"applied": false' in capsys.readouterr().out
    assert cli.main(["status", *common, "--as-of", START.isoformat()]) == 0
    assert "not_configured" in capsys.readouterr().out
    assert cli.main([*args, "--apply"]) == 0
    assert '"applied": true' in capsys.readouterr().out
    assert cli.main(["status", *common, "--as-of", (START + timedelta(days=7)).isoformat()]) == 0
    assert '"state": "expired"' in capsys.readouterr().out
    assert cli.main(["convert", *common, "--approved-by", "operator", "--apply"]) == 2
    assert "agreement" in capsys.readouterr().err
    assert cli.main(["stop", *common, "--approved-by", "operator", "--apply"]) == 0
    capsys.readouterr()
    assert cli.main(["convert", *common, "--approved-by", "operator", "--agreement-confirmed", "--apply"]) == 0
    assert '"state": "converted"' in capsys.readouterr().out


def test_corrupt_extended_window_cannot_bypass_seven_day_limit(db):
    trial = trial_module()
    cfg = safe_config()
    trial.activate(cfg, START, approved_by="operator", owner_notified=True, apply=True)
    with storage._conn() as conn:
        conn.execute("UPDATE customer_trials SET ends_at=?", ((START + timedelta(days=30)).isoformat(),))
    assert not trial.admit(cfg, "CAextended", now=START + timedelta(days=8)).allowed


def test_database_lock_declines_within_webhook_budget(db):
    import sqlite3
    import time
    trial = trial_module()
    cfg = safe_config()
    trial.activate(cfg, START, approved_by="operator", owner_notified=True, apply=True)
    locked = sqlite3.connect(storage.DB_PATH)
    try:
        locked.execute("BEGIN IMMEDIATE")
        before = time.monotonic()
        assert not trial.admit(cfg, "CAlocked", now=START).allowed
        assert time.monotonic() - before < 3
    finally:
        locked.rollback()
        locked.close()
    assert trial.status(cfg.client_id, now=START)["calls_reserved"] == 0


def test_quota_is_atomic_retry_safe_tenant_bound_and_never_resets(db):
    from concurrent.futures import ThreadPoolExecutor
    trial = trial_module()
    cfg = safe_config()
    trial.activate(cfg, START, approved_by="operator", owner_notified=True, apply=True)
    assert trial.admit(cfg, "CAfirst", now=START).allowed
    assert trial.admit(cfg, "CAfirst", now=START).allowed
    other = cfg.model_copy(update={"client_id": "other_customer"})
    trial.activate(other, START, approved_by="operator", owner_notified=True, apply=True)
    assert not trial.admit(other, "CAfirst", now=START).allowed
    next_month = START + timedelta(days=4)
    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(pool.map(lambda i: trial.admit(cfg, f"CA{i}", now=next_month).allowed, range(50)))
    assert sum(admitted) == 29
    assert not trial.admit(cfg, "CAoverflow", now=next_month).allowed
    assert trial.admit(cfg, "CAfirst", now=next_month).allowed
    assert trial.admit(other, "CAother", now=next_month).allowed
    with storage._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM trial_admissions WHERE client_id=?", (cfg.client_id,)).fetchone()[0] == 30
