"""Booking lifecycle integrity: ledger, races, idempotency, daylight-saving time."""
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

CALLER, OTHER = "+15555550100", "+15555550100"


@pytest.fixture(autouse=True)
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("CALLKETTLE_DB_PATH", path)
    import importlib

    from app import storage

    importlib.reload(storage)
    storage.init_db()
    yield storage
    try:
        os.remove(path)
    except PermissionError:
        pass


@pytest.fixture
def events(monkeypatch):
    from app import notify, webhooks

    seen = {"notify": [], "hooks": []}
    monkeypatch.setattr(notify, "notify_owner", lambda cfg, **kw: seen["notify"].append(kw))
    monkeypatch.setattr(webhooks, "emit", lambda cfg, event, data, event_id=None: seen["hooks"].append((event, data)))
    return seen


def _cfg():
    from app.config import load_client_config

    return load_client_config("demo_hvac")


def _book(*, sid="CA1", phone=CALLER, time="10:00", date="2026-01-12", name="Pat Lee"):
    from app import tools

    return tools.book_appointment(call_sid=sid, config=_cfg(), caller_name=name, caller_phone=phone,
                                  service="Emergency repair", date=date, time=time)


def test_the_ledger_records_every_change_in_order_with_old_and_new_times(events, db):
    from app import tools

    bid = _book()["booking_id"]
    tools.reschedule_appointment(config=_cfg(), call_sid="CA2", booking_id=bid, new_date="2026-01-13", new_time="11:00", caller_id=CALLER)
    tools.reschedule_appointment(config=_cfg(), call_sid="CA3", booking_id=bid, new_date="2026-01-14", new_time="09:00", caller_id=CALLER)
    tools.cancel_appointment(config=_cfg(), call_sid="CA4", booking_id=bid, caller_id=CALLER)
    h = db.booking_history(bid)
    assert [e["event"] for e in h] == ["created", "rescheduled", "rescheduled", "cancelled"]
    assert (h[1]["old_start"], h[1]["new_start"]) == ("2026-01-12T10:00", "2026-01-13T11:00")
    assert (h[2]["old_start"], h[2]["new_start"]) == ("2026-01-13T11:00", "2026-01-14T09:00")
    assert h[3]["old_start"] == "2026-01-14T09:00" and [e["call_sid"] for e in h] == ["CA1", "CA2", "CA3", "CA4"]
    assert all(e["at"] for e in h)


def test_sixteen_callers_racing_for_one_slot_produce_exactly_one_booking(events, db):
    def attempt(i):
        return _book(sid=f"CA_R{i}", phone=f"+1703555{1000 + i}", name=f"Caller {i}", time="10:00")["success"]

    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(attempt, range(16)))
    assert sum(results) == 1
    import sqlite3

    conn = sqlite3.connect(db.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM booking_events WHERE event='created'").fetchone()[0] == 1
    conn.close()


def test_racing_for_overlapping_slots_never_double_books_the_time(events, db):
    """A 60-minute job at 10:00 and one at 10:30 overlap: at most one may exist, whatever the thread order."""
    cfg = _cfg().model_copy(update={"slot_minutes": 30})
    from app import tools

    def attempt(args):
        i, t = args
        return tools.book_appointment(call_sid=f"CA_O{i}", config=cfg, caller_name=f"C{i}", caller_phone=f"+1703555{2000 + i}",
                                      service="Emergency repair", date="2026-01-12", time=t)["success"]

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(attempt, enumerate(["10:00", "10:30"] * 4)))
    assert sum(results) == 1


def test_two_reschedules_racing_for_the_same_new_slot_have_one_winner(events, db):
    from app import tools

    a = _book(sid="CA_A", phone=CALLER, time="09:00", name="A")["booking_id"]
    b = _book(sid="CA_B", phone=OTHER, time="11:00", name="B")["booking_id"]

    def move(args):
        bid, phone = args
        return tools.reschedule_appointment(config=_cfg(), call_sid="CA_M", booking_id=bid, new_date="2026-01-12", new_time="15:00", caller_id=phone)["success"]

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(move, [(a, CALLER), (b, OTHER)]))
    assert sorted(results) == [False, True]
    at_three = [x for x in (db.get_booking(a), db.get_booking(b)) if x["slot_start"] == "2026-01-12T15:00"]
    assert len(at_three) == 1


def test_cancelling_the_same_booking_twice_concurrently_cancels_it_once(events, db):
    from app import tools

    bid = _book()["booking_id"]

    def cancel(i):
        return tools.cancel_appointment(config=_cfg(), call_sid=f"CA_C{i}", booking_id=bid, caller_id=CALLER)["success"]

    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(cancel, range(6)))
    assert sum(results) == 1
    assert [e["event"] for e in db.booking_history(bid)].count("cancelled") == 1
    assert len([n for n in events["notify"] if n["title"] == "Booking cancelled"]) == 1


def test_asking_for_the_same_slot_twice_in_one_call_is_one_booking_and_one_notification(events, db):
    first = _book(sid="CA_SAME")
    second = _book(sid="CA_SAME")
    assert first["success"] and second["success"] and second["booking_id"] == first["booking_id"] and second.get("already_booked")
    assert len([n for n in events["notify"] if n["title"] == "New booking"]) == 1
    assert len([h for h in events["hooks"] if h[0] == "booking.created"]) == 1


def test_a_different_caller_asking_for_a_taken_slot_still_conflicts(events, db):
    assert _book(sid="CA_X", phone=CALLER)["success"]
    assert _book(sid="CA_Y", phone=OTHER, name="Other")["success"] is False


def test_rescheduling_to_the_same_time_changes_nothing_and_alerts_nobody(events, db):
    from app import tools

    bid = _book()["booking_id"]
    before = len(events["notify"])
    r = tools.reschedule_appointment(config=_cfg(), call_sid="CA_Z", booking_id=bid, new_date="2026-01-12", new_time="10:00", caller_id=CALLER)
    assert r["success"] is True and r.get("unchanged") is True
    assert len(events["notify"]) == before and [e["event"] for e in db.booking_history(bid)] == ["created"]


def test_the_cancelled_slot_is_immediately_bookable_and_history_is_kept(events, db):
    from app import tools

    bid = _book()["booking_id"]
    tools.cancel_appointment(config=_cfg(), call_sid="CA_C", booking_id=bid, caller_id=CALLER)
    again = _book(sid="CA_NEW", phone=OTHER, name="Next")
    assert again["success"] and again["booking_id"] != bid
    assert db.booking_history(bid)[-1]["event"] == "cancelled"


# ------------------------------------------------------------------ time zones and daylight-saving time

@pytest.mark.parametrize("local,utc", [
    (datetime(2026, 10, 30, 10, 0), "20261030T140000Z"),    # EDT, UTC-4
    (datetime(2026, 11, 2, 10, 0), "20261102T150000Z"),     # after the fall-back: EST, UTC-5
    (datetime(2026, 3, 6, 10, 0), "20260306T150000Z"),      # before spring-forward: EST
    (datetime(2026, 3, 9, 10, 0), "20260309T140000Z"),      # after spring-forward: EDT
])
def test_calendar_invites_use_the_right_utc_offset_across_dst(local, utc):
    from datetime import timedelta

    from app import notify

    ics = notify.build_ics(config=_cfg(), caller_name="Pat", caller_phone=CALLER, service="Repair", start=local,
                           end=local + timedelta(hours=1), organizer="owner@example.com")
    assert f"DTSTART:{utc}" in ics


def test_a_time_that_does_not_exist_on_spring_forward_day_does_not_crash():
    from datetime import timedelta

    from app import notify

    gap = datetime(2026, 3, 8, 2, 30)     # 2:30am never happens in New York that day
    ics = notify.build_ics(config=_cfg(), caller_name="Pat", caller_phone=CALLER, service="Repair", start=gap,
                           end=gap + timedelta(hours=1), organizer="owner@example.com")
    assert "DTSTART:" in ics


def test_the_slot_grid_is_the_same_on_dst_change_days():
    from app import tools

    cfg = _cfg()
    normal = tools._slot_grid(cfg, datetime(2026, 10, 28))     # a Wednesday
    fall_back = tools._slot_grid(cfg, datetime(2026, 11, 2))   # the Monday after the change
    assert [s.strftime("%H:%M") for s in normal] == [s.strftime("%H:%M") for s in fall_back]
    spring = tools._slot_grid(cfg, datetime(2026, 3, 9))       # Monday after spring-forward
    assert len(spring) == len(normal)


def test_each_clients_own_timezone_decides_what_now_means(monkeypatch):
    from app import tools

    ny = _cfg()
    la = ny.model_copy(update={"timezone": "America/Los_Angeles"})
    monkeypatch.undo()          # use the real clock for this one
    a, b = tools._local_now(ny), tools._local_now(la)
    assert 2.9 <= (a - b).total_seconds() / 3600 <= 3.1
