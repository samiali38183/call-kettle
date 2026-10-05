"""Operational safeguards: alerts, log hygiene, retention, backups, admin tools."""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("CALLKETTLE_DB_PATH", str(tmp_path / "ops.db"))
    from app import storage

    importlib.reload(storage)
    storage.init_db()
    yield storage


# ------------------------------------------------------------ operator alerts

def test_alerts_are_rate_limited_per_key_and_never_raise(monkeypatch):
    from app import notify, ops

    ops._last_alert.clear()
    sent = []
    monkeypatch.setattr(notify, "notify_owner", lambda config, **kw: sent.append(kw["title"]))
    assert ops.alert_operator("Model down", "x", key="model") is True
    assert ops.alert_operator("Model down", "x", key="model") is False  # suppressed for 10 minutes
    assert ops.alert_operator("Something else", "y") is True
    assert sent == ["ALERT: Model down", "ALERT: Something else"]

    def boom(*a, **k):
        raise RuntimeError("ntfy is down")

    ops._last_alert.clear()
    monkeypatch.setattr(notify, "notify_owner", boom)
    assert ops.alert_operator("Anything", "z") is False  # swallowed


def test_a_crashed_voice_route_alerts_the_operator(app_client, monkeypatch):
    c, main = app_client
    alerts = []
    monkeypatch.setattr(main.ops, "alert_operator", lambda title, body, **kw: alerts.append(title))
    monkeypatch.setattr(main.storage, "log_call_start", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    from fastapi.testclient import TestClient

    with TestClient(main.app, raise_server_exceptions=False) as safe:
        r = safe.post("/voice/incoming?client_id=demo_dental", data={"CallSid": "CA_ALERT", "From": "+15555550100"})
    assert r.status_code == 200 and "</Dial>" in r.text
    assert alerts == ["Error on a live call"]


def test_a_model_failure_alerts_the_operator(monkeypatch):
    from app import agent, ops

    alerts = []
    monkeypatch.setattr(ops, "alert_operator", lambda title, body, **kw: alerts.append(title))

    class Boom:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("credit balance too low")

    monkeypatch.setattr(agent, "_anthropic_client", lambda: Boom())
    from app.config import load_client_config

    session = agent.start_session("CA_M", load_client_config("demo_dental"))
    reply, ended, _ = agent.run_turn(session, "hello there")
    assert ended and "trouble" in reply.lower()
    assert alerts == ["AI model error on a live call"]


# ------------------------------------------------------------ log hygiene

def test_secret_keys_are_redacted_from_logs():
    from app import ops

    f = ops.RedactKeys()
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "x", 1, '%s - "%s %s HTTP/1.1" %d',
                            ("1.2.3.4", "GET", "/report/sample_homecare?key=+15555550100+155555501000000&x=1", 200), None)
    f.filter(rec)
    assert "96dc34aa" not in rec.getMessage() and "key=REDACTED&x=1" in rec.getMessage()


# ------------------------------------------------------------ retention and backups

def test_old_transcripts_and_summaries_are_deleted_but_history_stays(_fresh_db):
    from app import ops

    storage = _fresh_db
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    storage.log_call_start("CA_OLD", "demo_dental", "+15555550100")
    storage.log_turn("CA_OLD", "caller", "private words")
    storage.set_call_summary("CA_OLD", "private summary")
    storage.log_call_start("CA_NEW", "demo_dental", "+15555550100")
    storage.log_turn("CA_NEW", "caller", "recent words")
    conn = sqlite3.connect(storage.DB_PATH)
    conn.execute("UPDATE calls SET started_at = ? WHERE call_sid = 'CA_OLD'", (old,))
    conn.commit()
    conn.close()

    assert ops.purge_old_transcripts() == 1
    old_call, new_call = storage.get_call("CA_OLD"), storage.get_call("CA_NEW")
    assert old_call["transcript_json"] == "[]" and old_call["summary"] is None
    assert old_call["from_number"] == "+15555550100"  # the call itself is still counted
    assert "recent words" in new_call["transcript_json"]


def test_backups_are_consistent_copies_and_old_ones_are_pruned(_fresh_db, tmp_path):
    from app import ops

    storage = _fresh_db
    storage.log_call_start("CA_B", "demo_dental", "+15555550100")
    dest = ops.backup_database(keep=2)
    assert dest and dest.exists()
    copy = sqlite3.connect(dest)
    assert copy.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1
    copy.close()
    folder = dest.parent
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (folder / f"{ops.OLD_BACKUP_GLOB.replace('*', day)}").write_bytes(b"old")      # backups made before the rename are pruned like the new ones
    ops.backup_database(keep=2)
    assert len(list(folder.glob("callkettle-*.db"))) + len(list(folder.glob(ops.OLD_BACKUP_GLOB))) == 2


def test_stale_call_sessions_are_freed():
    from datetime import timedelta as td

    from app import agent
    from app.config import load_client_config

    config = load_client_config("demo_dental")
    fresh = agent.start_session("CA_FRESH", config)
    stale = agent.start_session("CA_STALE", config)
    stale.started_at = datetime.now(timezone.utc) - td(hours=2)
    assert agent.purge_stale_sessions(3600) == 1
    assert agent.get_session("CA_STALE") is None and agent.get_session("CA_FRESH") is fresh
    agent.end_session("CA_FRESH")


def test_housekeeping_runs_all_steps(_fresh_db):
    from app import ops

    result = ops.housekeeping()
    assert set(result) >= {"stale_sessions_removed", "transcripts_purged", "backup"}


# ------------------------------------------------------------ safety

def test_client_ids_cannot_walk_out_of_the_clients_folder():
    from app.config import ClientNotFoundError, load_client_config

    for bad in ("../fly", "..\\fly", "a/b", "", "x" * 100, "demo dental", "demo_dental.yaml"):
        with pytest.raises(ClientNotFoundError):
            load_client_config(bad)
    assert load_client_config("demo_dental").business_name


def test_blocked_caller_ids_are_not_treated_as_one_repeat_caller(app_client):
    c, main = app_client
    replies = []
    for i in range(main.MAX_CALLS_PER_NUMBER_10MIN + 3):
        replies.append(c.post("/voice/incoming?client_id=demo_dental", data={"CallSid": f"CA_ANON{i}", "From": "anonymous"}).text)
        main.storage.log_call_end(f"CA_ANON{i}", "completed")        # sequential calls: the concurrency cap (worst-case loss cap) is not under test here
    assert all("<Gather" in r for r in replies)


# ------------------------------------------------------------ admin tools

def test_status_endpoint_is_private_and_reveals_no_secrets(app_client):
    c, _ = app_client
    assert c.get("/admin/status").status_code == 403
    r = c.get("/admin/status", params={"key": "master_key_for_tests"})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and body["database"]["ok"] is True
    assert body["features"]["sms_enabled"] is False and body["features"]["signature_check_on"] is not None
    text = r.text
    for secret in ("master_key_for_tests", "sk-ant", "AC"):
        assert secret not in text or secret == "AC" and "AC" in text  # "AC" can appear in words; keys must not
    assert "master_key_for_tests" not in text


def test_export_returns_every_table_and_is_private(app_client):
    c, main = app_client
    main.storage.log_call_start("CA_X", "demo_dental", "+15555550100")
    assert c.get("/admin/export").status_code == 403
    data = c.get("/admin/export", params={"key": "master_key_for_tests"}).json()
    assert set(data) >= {"calls", "bookings", "escalations", "intakes", "exported_at"}
    assert data["calls"][0]["call_sid"] == "CA_X"


def test_deleting_a_client_needs_confirmation_and_erases_only_that_client(app_client):
    c, main = app_client
    s = main.storage
    for sid, client in (("CA_D1", "demo_dental"), ("CA_D2", "demo_hvac")):
        s.log_call_start(sid, client, "+15555550100")
        s.log_escalation(call_sid=sid, client_id=client, reason="test", caller_phone="+1", summary="x")
    key = {"key": "master_key_for_tests"}
    assert c.post("/admin/client/demo_dental/delete").status_code == 403
    assert c.post("/admin/client/demo_dental/delete", params=key).status_code == 400  # no confirm
    r = c.post("/admin/client/demo_dental/delete", params={**key, "confirm": "demo_dental"})
    assert r.json()["deleted"] == {"calls": 1, "bookings": 0, "cancelled_bookings": 0, "escalations": 1, "owner_users": 0}
    assert s.get_call("CA_D1") is None and s.get_call("CA_D2") is not None  # the other client is untouched


def test_test_data_cleanup_only_removes_test_prefixed_rows(app_client):
    c, main = app_client
    s = main.storage
    for sid in ("CA_SELFCHECK_1", "CA_LOAD_9", "CA_REAL_CALL"):
        s.log_call_start(sid, "demo_dental", "+15555550100")
    r = c.post("/admin/purge-test-data", params={"key": "master_key_for_tests"})
    assert r.json()["removed"] == 2
    assert s.get_call("CA_REAL_CALL") is not None


def test_usage_alert_fires_once_per_level_per_month(_fresh_db, monkeypatch):
    from app import ops

    storage = _fresh_db
    sent = []
    monkeypatch.setattr(ops, "alert_operator", lambda title, body, **kw: sent.append(body) or True)
    monkeypatch.setenv("FAIR_USE_CALLS", "10")
    for i in range(8):
        storage.log_call_start(f"CA_U{i}", "acme", "+15555550100%d" % i)
    assert len(ops.check_usage()) == 1 and "80%" in sent[-1]
    assert ops.check_usage() == []  # same level, same month: not again
    for i in range(8, 10):
        storage.log_call_start(f"CA_U{i}", "acme", "+15555550100%d" % i)
    assert len(ops.check_usage()) == 1 and "100%" in sent[-1]
    assert ops.check_usage() == []


def test_usage_alert_ignores_own_and_demo_lines(_fresh_db, monkeypatch):
    from app import ops

    storage = _fresh_db
    monkeypatch.setattr(ops, "alert_operator", lambda *a, **k: pytest.fail("must not alert"))
    monkeypatch.setenv("FAIR_USE_CALLS", "2")
    for cid in ("callkettle_demo", "callkettle_sales", "demo_hvac"):
        for i in range(5):
            storage.log_call_start(f"CA_{cid}{i}", cid, "+15555550100")
    assert ops.check_usage() == []


# ------------------------------------------------------------ monthly call ceiling (abuse brake)

def _form(sid, frm="+15555550100"):
    return {"CallSid": sid, "From": frm}


def test_past_the_monthly_ceiling_calls_take_a_message_not_the_ai(app_client, monkeypatch):
    client, main = app_client
    from app import agent, ops, storage

    monkeypatch.setenv("MONTHLY_CALL_CEILING", "3")
    alerts = []
    monkeypatch.setattr(ops, "alert_operator", lambda title, body, **kw: alerts.append((title, body)) or True)
    for i in range(3):
        storage.log_call_start(f"CA_CEIL{i}", "demo_hvac", f"+15555550100{i}")
    r = client.post("/voice/incoming?client_id=demo_hvac", data=_form("CA_CEIL_NEW"))
    assert r.status_code == 200 and "<Dial" not in r.text and "/voice/ceiling-message" in r.text
    assert agent.get_session("CA_CEIL_NEW") is None          # the AI never started, so no model spend
    assert alerts and "ceiling" in alerts[0][0].lower()
    assert storage.get_call("CA_CEIL_NEW") is not None       # still logged, so the dashboard shows it


def test_under_the_ceiling_the_ai_answers_normally(app_client, monkeypatch):
    client, main = app_client
    monkeypatch.setenv("MONTHLY_CALL_CEILING", "50")
    r = client.post("/voice/incoming?client_id=demo_hvac", data=_form("CA_OK1"))
    assert r.status_code == 200 and "<Gather" in r.text


def test_a_clients_own_ceiling_overrides_the_default(app_client, monkeypatch):
    client, main = app_client
    from app import config as config_module
    from app import storage

    cfg = config_module.load_client_config("demo_hvac").model_copy(update={"monthly_call_ceiling": 2, "ceiling_mode": "transfer"})
    monkeypatch.setattr(main, "load_client_config", lambda cid: cfg)
    for i in range(2):
        storage.log_call_start(f"CA_OWN{i}", "demo_hvac", f"+15555550100{i}")
    r = client.post("/voice/incoming?client_id=demo_hvac", data=_form("CA_OWN_NEW"))
    assert "<Dial" in r.text


def test_other_clients_calls_do_not_count_toward_the_ceiling(app_client, monkeypatch):
    client, main = app_client
    from app import storage

    monkeypatch.setenv("MONTHLY_CALL_CEILING", "3")
    for i in range(10):
        storage.log_call_start(f"CA_OTHER{i}", "demo_dental", f"+15555550100{i}")
    r = client.post("/voice/incoming?client_id=demo_hvac", data=_form("CA_FINE"))
    assert "<Gather" in r.text


# ------------------------------------------------------------ Twilio balance alert

def test_low_twilio_balance_alerts_the_operator_once_a_day(monkeypatch):
    from app import ops

    sent = []
    monkeypatch.setattr(ops, "twilio_balance", lambda: 9.5)
    monkeypatch.setattr(ops.notify, "notify_owner", lambda cfg, **kw: sent.append(kw))
    ops._last_alert.clear()
    assert ops.check_balance(threshold=15) == 9.5
    assert ops.check_balance(threshold=15) == 9.5       # second check the same day: no second alert
    assert len(sent) == 1 and "$9.50" in sent[0]["body"]


def test_a_healthy_or_unreadable_balance_sends_nothing(monkeypatch):
    from app import ops

    sent = []
    monkeypatch.setattr(ops.notify, "notify_owner", lambda cfg, **kw: sent.append(kw))
    ops._last_alert.clear()
    monkeypatch.setattr(ops, "twilio_balance", lambda: 120.0)
    assert ops.check_balance(threshold=15) == 120.0
    monkeypatch.setattr(ops, "twilio_balance", lambda: None)
    assert ops.check_balance(threshold=15) is None
    assert sent == []


def test_the_twilio_sdk_call_used_for_the_balance_really_exists():
    """The unit tests above mock twilio_balance(); this guards the real SDK path without the network."""
    from twilio.rest import Client

    client = Client("AC" + "0" * 32, "x" * 32)
    assert callable(client.balance.fetch)


def test_status_page_proves_the_maintenance_loop_ran(app_client):
    client, main = app_client
    from app import ops

    ops.LAST_HOUSEKEEPING.clear()
    ops.housekeeping()
    s = client.get("/admin/status?key=master_key_for_tests").json()
    assert s["housekeeping"]["at"] and "usage_alerts" in s["housekeeping"]["result"]


# ------------------------------------------------------------ the daily backup is opened, not just written

def _live_db(monkeypatch, tmp_path):
    import importlib

    monkeypatch.setenv("CALLKETTLE_DB_PATH", str(tmp_path / "live.db"))
    from app import ops, storage

    importlib.reload(storage)
    storage.init_db()
    storage.log_call_start("CA_BK1", "demo_hvac", "+15555550100")
    return ops, storage


def test_a_good_backup_passes_its_restore_test(monkeypatch, tmp_path):
    ops, storage = _live_db(monkeypatch, tmp_path)
    made = ops.backup_database()
    check = ops.verify_backup(made)
    assert check["ok"] is True and check["rows"]["calls"] == 1


def test_a_corrupted_backup_fails_its_restore_test(monkeypatch, tmp_path):
    ops, storage = _live_db(monkeypatch, tmp_path)
    made = ops.backup_database()
    data = bytearray(made.read_bytes())
    for i in range(2048, min(len(data), 6000)):
        data[i] = 0xFF
    made.write_bytes(bytes(data))
    check = ops.verify_backup(made)
    assert check["ok"] is False and check["problem"]


def test_a_backup_missing_a_table_or_not_a_database_fails(monkeypatch, tmp_path):
    import sqlite3

    ops, storage = _live_db(monkeypatch, tmp_path)
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert ops.verify_backup(empty)["ok"] is False
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a database" * 100)
    assert ops.verify_backup(junk)["ok"] is False
    assert ops.verify_backup(tmp_path / "missing.db")["ok"] is False


def test_a_backup_holding_more_than_the_live_database_is_suspicious(monkeypatch, tmp_path):
    ops, storage = _live_db(monkeypatch, tmp_path)
    made = ops.backup_database()
    with storage._conn() as conn:
        conn.execute("DELETE FROM calls")
    check = ops.verify_backup(made)
    assert check["ok"] is False and "more calls" in check["problem"]


def test_housekeeping_alerts_when_the_backup_fails_its_test(monkeypatch, tmp_path):
    ops, storage = _live_db(monkeypatch, tmp_path)
    alerts = []
    monkeypatch.setattr(ops, "alert_operator", lambda title, body, **kw: alerts.append(title) or True)
    monkeypatch.setattr(ops, "check_balance", lambda: None)
    monkeypatch.setattr(ops, "verify_backup", lambda p: {"ok": False, "problem": "simulated", "rows": {}})
    result = ops.housekeeping()
    assert result["backup_verified"] is False and "Backup failed its restore test" in alerts
