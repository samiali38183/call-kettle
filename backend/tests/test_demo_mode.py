"""The public demo line resets itself: old bookings go, nothing else does, and only demo clients are touched."""
from datetime import datetime, timedelta, timezone

import pytest


def _row(storage, table, cols, vals):
    with storage._conn() as conn:
        conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(vals))})", vals)


def _booking(storage, client, slot, created):
    _row(storage, "bookings", ["call_sid", "client_id", "caller_name", "caller_phone", "service", "slot_start", "slot_end", "created_at"],
         ["CA", client, "Pat", "+15555550100", "Visit", slot, slot, created.isoformat()])


def _count(storage, table, client):
    with storage._conn() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE client_id = ?", (client,)).fetchone()[0]


@pytest.fixture
def env(app_client, monkeypatch):
    client, main = app_client
    from app import config as config_module
    from app import ops, storage

    real = config_module.load_client_config
    demo = real("demo_hvac").model_copy(update={"demo_mode": True})
    real_ids = ["demo_hvac", "demo_dental"]
    monkeypatch.setattr(ops, "list_client_ids", lambda: real_ids)
    monkeypatch.setattr(ops, "load_client_config", lambda cid: demo if cid == "demo_hvac" else real(cid))
    return ops, storage


def test_old_demo_bookings_are_deleted_and_recent_ones_stay(env):
    ops, storage = env
    now = datetime.now(timezone.utc)
    _booking(storage, "demo_hvac", "2026-01-12T09:00", now - timedelta(hours=30))
    _booking(storage, "demo_hvac", "2026-01-12T10:00", now - timedelta(hours=2))
    out = ops.reset_demo_data()
    assert out["bookings"] == 1 and _count(storage, "bookings", "demo_hvac") == 1


def test_a_paying_clients_bookings_are_never_touched(env):
    ops, storage = env
    _booking(storage, "demo_dental", "2026-01-12T09:00", datetime.now(timezone.utc) - timedelta(days=90))
    assert ops.reset_demo_data()["bookings"] == 0 and _count(storage, "bookings", "demo_dental") == 1


def test_cancellations_and_history_go_too_but_call_records_stay_for_the_spend_estimate(env):
    ops, storage = env
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    _row(storage, "cancelled_bookings", ["id", "client_id", "caller_name", "caller_phone", "service", "slot_start", "slot_end", "created_at", "cancelled_at"],
         [1, "demo_hvac", "Pat", "+15555550100", "Visit", "x", "x", old, old])
    _row(storage, "booking_events", ["booking_id", "client_id", "event", "at"], [1, "demo_hvac", "created", old])
    storage.log_call_start("CA_OLD_DEMO", "demo_hvac", "+15555550100")
    with storage._conn() as conn:
        conn.execute("UPDATE calls SET started_at = ? WHERE call_sid = 'CA_OLD_DEMO'", (old,))
    out = ops.reset_demo_data()
    assert out["cancelled"] == 1 and out["events"] == 1
    assert _count(storage, "calls", "demo_hvac") == 1


def test_slots_reopen_after_the_reset(env):
    ops, storage = env
    from app import tools
    from app.config import load_client_config

    cfg = load_client_config("demo_hvac")
    res = tools.book_appointment(call_sid="CA_D1", config=cfg, caller_name="Pat Lee", caller_phone="+15555550100",
                                 service="Emergency repair", date="2026-01-12", time="10:00")
    assert res["success"]
    assert "10:00" not in tools.check_availability(config=cfg, date="2026-01-12", limit=30)["slots"]
    with storage._conn() as conn:
        conn.execute("UPDATE bookings SET created_at = ?", ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),))
    ops.reset_demo_data()
    assert "10:00" in tools.check_availability(config=cfg, date="2026-01-12", limit=30)["slots"]


def test_housekeeping_runs_the_reset_and_reports_it(env, monkeypatch):
    ops, storage = env
    monkeypatch.setattr(ops, "check_balance", lambda: None)
    assert "demo_reset" in ops.housekeeping()


def test_the_real_demo_line_is_flagged_and_capped():
    from app.config import load_client_config

    cfg = load_client_config("callkettle_demo")
    assert cfg.demo_mode is True and cfg.monthly_cost_ceiling_usd is not None and cfg.monthly_cost_ceiling_usd <= 50
    for cid in ("sample_homecare", "callkettle_sales", "demo_dental", "demo_hvac"):
        assert load_client_config(cid).demo_mode is False
