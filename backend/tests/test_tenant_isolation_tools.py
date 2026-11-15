"""Tenant isolation at the tool layer, with two synthetic tenants that share the same caller phone number and the same slot.

tests/test_isolation.py covers storage and the portal. This file drives the functions the model can reach (tools.*) and checks
that nothing from tenant B shows up in, or is changed by, a call made as tenant A. Isolation here is application-level: one
SQLite file, every query filtered by client_id (see docs/SECURITY_INVARIANTS.md, I-4).
"""
import json
import sqlite3

import pytest

from app import storage, tools
from app.config import load_client_config

PHONE = "+15555550111"
DAY = "2026-01-12"


def _status(booking_id: int) -> str:
    conn = sqlite3.connect(storage.DB_PATH)
    try:
        return conn.execute("SELECT status FROM bookings WHERE id = ?", (booking_id,)).fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def two_tenants(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "iso.db"))
    storage.init_db()
    monkeypatch.setattr(tools.notify, "notify_owner", lambda *a, **k: None)
    base = load_client_config("demo_hvac")
    a = base.model_copy(update={"client_id": "tenant_a", "business_name": "Alpha HVAC"})
    b = base.model_copy(update={"client_id": "tenant_b", "business_name": "Bravo HVAC"})
    ra = tools.book_appointment(call_sid="CA_A", config=a, caller_name="Pat Alpha", caller_phone=PHONE, service="Emergency repair", date=DAY, time="11:00", caller_id=PHONE)
    rb = tools.book_appointment(call_sid="CA_B", config=b, caller_name="Sam Bravo", caller_phone=PHONE, service="Emergency repair", date=DAY, time="11:00", caller_id=PHONE)
    assert ra["success"] and rb["success"], "the same phone number and slot must be bookable at two tenants"
    return a, b, ra["booking_id"], rb["booking_id"]


def test_lookup_returns_only_the_calling_tenants_bookings(two_tenants):
    a, b, ida, idb = two_tenants
    out = tools.find_my_appointments(config=a, caller_id=PHONE)
    assert [x["booking_id"] for x in out["appointments"]] == [ida]
    assert "Bravo" not in json.dumps(out) and "Sam" not in json.dumps(out)


def test_cancel_by_id_cannot_reach_the_other_tenant_even_with_the_same_phone(two_tenants):
    a, b, ida, idb = two_tenants
    r = tools.cancel_appointment(config=a, call_sid="CA_NEW", booking_id=idb, caller_id=PHONE)
    assert r["success"] is False
    assert _status(idb) == "confirmed"


def test_reschedule_by_id_cannot_reach_the_other_tenant(two_tenants):
    a, b, ida, idb = two_tenants
    r = tools.reschedule_appointment(config=a, call_sid="CA_NEW", booking_id=idb, new_date=DAY, new_time="15:00", caller_id=PHONE)
    assert r["success"] is False
    assert storage.get_booking(idb)["slot_start"] == f"{DAY}T11:00"


def test_availability_is_per_tenant(two_tenants):
    a, b, ida, idb = two_tenants
    assert tools.cancel_appointment(config=a, call_sid="CA_A", booking_id=ida, caller_id=PHONE)["success"]
    # tenant A's 11:00 is free again; tenant B's is still taken
    assert "11:00" in tools.check_availability(config=a, date=DAY, preferred_time="11:00", limit=20)["slots"]
    assert "11:00" not in tools.check_availability(config=b, date=DAY, preferred_time="11:00", limit=20)["slots"]


@pytest.mark.parametrize("bad_id", [-1, 0, 10**9, "1 OR 1=1", None, "1; DROP TABLE bookings"])
def test_malformed_ids_change_nothing_at_either_tenant(two_tenants, bad_id):
    a, b, ida, idb = two_tenants
    for fn in (lambda: tools.cancel_appointment(config=a, call_sid="CA_NEW", booking_id=bad_id, caller_id=PHONE),
               lambda: tools.reschedule_appointment(config=a, call_sid="CA_NEW", booking_id=bad_id, new_date=DAY, new_time="15:00", caller_id=PHONE)):
        try:
            assert fn().get("success") is False
        except (TypeError, ValueError):
            pass    # the gate's schema check refuses these before the tool runs; a raise here is also a refusal
    assert _status(ida) == "confirmed" and _status(idb) == "confirmed"
