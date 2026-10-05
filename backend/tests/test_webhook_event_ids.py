"""Event ids for callback.requested and booking.updated stay deterministic and idempotent when call_sid is missing."""
import json
import os
import re
import sqlite3
import tempfile

import pytest

SECRET = "a-long-enough-secret-123"
PHONE = "+15555550100"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("CALLKETTLE_DB_PATH", path)
    monkeypatch.setenv("CALLKETTLE_DISABLE_PUSH", "1")
    import importlib

    from app import storage

    importlib.reload(storage)
    storage.init_db()
    yield storage
    try:
        os.remove(path)
    except PermissionError:
        pass


def _cfg():
    from app.config import load_client_config

    return load_client_config("demo_hvac").model_copy(update={"webhook_url": "https://example.com/hook", "webhook_secret": SECRET})


def _queue(storage, event):
    from app import webhooks

    webhooks.ensure_table()
    conn = sqlite3.connect(storage.DB_PATH)
    try:
        return [(i, json.loads(p)) for i, p in conn.execute("SELECT event_id, payload_json FROM webhook_outbox WHERE event = ? ORDER BY id", (event,))]
    finally:
        conn.close()


def _callback(cfg, **kw):
    from app import tools

    args = dict(call_sid=None, config=cfg, reason="wants_callback", caller_name="Pat Lee", caller_phone=PHONE, summary="Furnace is noisy.")
    args.update(kw)
    tools.escalate_to_human(**args)


def test_callback_without_call_sid_has_a_stable_id_and_a_retry_does_not_duplicate(env):
    cfg = _cfg()
    _callback(cfg)
    _callback(cfg)                                            # the same event again (a retried request)
    rows = _queue(env, "callback.requested")
    assert len(rows) == 1
    event_id, payload = rows[0]
    assert payload["id"] == event_id
    assert re.fullmatch(r"callback\.requested:demo_hvac:[0-9a-f]{16}", event_id)
    assert "None" not in event_id and payload["data"]["call_sid"] is None


def test_callback_without_call_sid_id_is_the_same_across_processes_and_differs_for_different_content(env):
    cfg = _cfg()
    _callback(cfg)
    _callback(cfg, summary="Water is leaking.")
    _callback(cfg, caller_phone="+15555550100")
    _callback(cfg, reason="after_hours_message")
    ids = [i for i, _ in _queue(env, "callback.requested")]
    assert len(set(ids)) == 4
    assert ids[0] == "callback.requested:demo_hvac:" + __import__("hashlib").sha256(
        "wants_callback\x1fPat Lee\x1f+15555550100\x1fFurnace is noisy.".encode()).hexdigest()[:16]


def test_callback_with_call_sid_keeps_the_documented_id(env):
    _callback(_cfg(), call_sid="CA_X1")
    assert _queue(env, "callback.requested")[0][0] == "callback.requested:demo_hvac:CA_X1:wants_callback"


def _book_and_move(cfg, times, call_sid):
    from app import tools

    booked = tools.book_appointment(call_sid="CA_B1", config=cfg, caller_name="Pat Lee", caller_phone=PHONE,
                                    service="Emergency repair", date="2026-01-12", time="10:00")
    assert booked["success"]
    for t in times:
        r = tools.reschedule_appointment(config=cfg, call_sid=call_sid, booking_id=booked["booking_id"], new_date="2026-01-12",
                                         new_time=t, caller_id=PHONE)
        assert r["success"] and not r.get("unchanged")
    return booked["booking_id"]


def test_booking_updated_without_call_sid_is_deterministic_never_none_and_distinct_per_move(env):
    bid = _book_and_move(_cfg(), ["11:00", "12:00"], call_sid=None)
    ids = [i for i, _ in _queue(env, "booking.updated")]
    assert ids == [f"booking.updated:demo_hvac:{bid}:2026-01-12T11:00:from-2026-01-12T10:00",
                   f"booking.updated:demo_hvac:{bid}:2026-01-12T12:00:from-2026-01-12T11:00"]
    assert all("None" not in i for i in ids)


def test_booking_updated_with_call_sid_keeps_the_documented_id(env):
    bid = _book_and_move(_cfg(), ["11:00"], call_sid="CA_C2")
    assert _queue(env, "booking.updated")[0][0] == f"booking.updated:demo_hvac:{bid}:2026-01-12T11:00:CA_C2"
