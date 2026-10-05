"""Cancel and reschedule: only the caller's own appointments, owner always told, calendar invite really updates."""
import os
import re
import sqlite3
import tempfile

import pytest

CALLER = "+15555550100"
OTHER = "+15555550100"


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
    """Records owner notifications and webhooks instead of sending them."""
    from app import notify, webhooks

    seen = {"notify": [], "hooks": []}
    monkeypatch.setattr(notify, "notify_owner", lambda cfg, **kw: seen["notify"].append(kw))
    monkeypatch.setattr(webhooks, "emit", lambda cfg, event, data, event_id=None: seen["hooks"].append((event, data)))
    return seen


def _cfg(cid="demo_hvac"):
    from app.config import load_client_config

    return load_client_config(cid)


def _book(cfg, *, phone=CALLER, time="10:00", date="2026-01-12", sid="CA_B1", name="Pat Lee"):
    from app import tools

    r = tools.book_appointment(call_sid=sid, config=cfg, caller_name=name, caller_phone=phone,
                               service=cfg.services[0].name, date=date, time=time)
    assert r["success"], r
    return r["booking_id"]


def test_a_caller_sees_only_their_own_upcoming_appointments(events):
    from app import tools

    cfg = _cfg()
    mine = _book(cfg, phone=CALLER, time="10:00", sid="CA1")
    _book(cfg, phone=OTHER, time="13:00", sid="CA2", name="Someone Else")
    found = tools.find_my_appointments(config=cfg, caller_id=CALLER)["appointments"]
    assert [a["booking_id"] for a in found] == [mine]
    assert "Someone Else" not in str(found)
    assert "error" in tools.find_my_appointments(config=cfg, caller_id=None)
    assert "error" in tools.find_my_appointments(config=cfg, caller_id="anonymous")
    assert tools.find_my_appointments(config=cfg, caller_id="+15555550100")["appointments"] == []


def test_phone_formats_match_on_the_last_ten_digits(events):
    from app import tools

    cfg = _cfg()
    bid = _book(cfg, phone="+15555550100")
    assert [a["booking_id"] for a in tools.find_my_appointments(config=cfg, caller_id="+15555550100")["appointments"]] == [bid]


def test_cancelling_frees_the_slot_keeps_history_and_tells_the_owner(events, db):
    from app import tools

    cfg = _cfg()
    bid = _book(cfg)
    assert "10:00" not in tools.check_availability(config=cfg, date="2026-01-12", limit=50)["slots"]
    r = tools.cancel_appointment(config=cfg, call_sid="CA_X", booking_id=bid, caller_id=CALLER)
    assert r["success"] is True
    assert "10:00" in tools.check_availability(config=cfg, date="2026-01-12", limit=50)["slots"]
    conn = sqlite3.connect(db.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 0
    row = conn.execute("SELECT caller_name, cancelled_by_call_sid FROM cancelled_bookings WHERE id=?", (bid,)).fetchone()
    conn.close()
    assert row == ("Pat Lee", "CA_X")
    assert events["notify"][-1]["title"] == "Booking cancelled" and "Pat Lee" in events["notify"][-1]["body"]
    assert events["hooks"][-1][0] == "booking.cancelled" and events["hooks"][-1][1]["booking_id"] == bid
    # the same slot can be booked again by someone else
    _book(cfg, phone=OTHER, sid="CA_NEW", name="New Person")


def test_the_cancel_email_carries_the_same_calendar_id_as_the_original(events):
    from app import tools

    cfg = _cfg()
    bid = _book(cfg)
    original_ics = next(e["ics"] for e in events["notify"] if e["title"] == "New booking")
    tools.cancel_appointment(config=cfg, call_sid="CA_X", booking_id=bid, caller_id=CALLER)
    cancel_ics = events["notify"][-1]["ics"]
    uid = re.search(r"UID:(\S+)", original_ics).group(1)
    assert f"UID:{uid}" in cancel_ics and "METHOD:CANCEL" in cancel_ics and "STATUS:CANCELLED" in cancel_ics
    assert "METHOD:REQUEST" in original_ics and "STATUS:CONFIRMED" in original_ics


def test_a_caller_cannot_touch_someone_elses_appointment(events, db):
    from app import tools

    cfg = _cfg()
    bid = _book(cfg, phone=OTHER, sid="CA_OTHER", name="Victim")
    for call in (lambda: tools.cancel_appointment(config=cfg, call_sid="CA_ATTACK", booking_id=bid, caller_id=CALLER),
                 lambda: tools.reschedule_appointment(config=cfg, call_sid="CA_ATTACK", booking_id=bid, new_date="2026-01-12", new_time="14:00", caller_id=CALLER),
                 lambda: tools.cancel_appointment(config=cfg, call_sid="CA_ATTACK", booking_id=bid, caller_id=None)):
        assert call()["success"] is False
    assert db.get_booking(bid) is not None and db.get_booking(bid)["slot_start"] == "2026-01-12T10:00"
    assert not [e for e in events["notify"] if e["title"] in ("Booking cancelled", "Booking moved")]


def test_a_caller_cannot_touch_another_clients_booking(events, db):
    from app import tools

    other_client = _cfg("demo_dental")
    bid = _book(other_client, phone=CALLER, sid="CA_D1", name="Dental Patient")
    hvac = _cfg("demo_hvac")
    assert tools.cancel_appointment(config=hvac, call_sid="CA_H", booking_id=bid, caller_id=CALLER)["success"] is False
    assert db.get_booking(bid) is not None


def test_a_booking_made_in_this_call_can_be_changed_even_if_the_caller_gave_another_number(events, db):
    from app import tools

    cfg = _cfg()
    bid = _book(cfg, phone="+15555550100", sid="CA_SAME")           # gave a different callback number
    r = tools.reschedule_appointment(config=cfg, call_sid="CA_SAME", booking_id=bid, new_date="2026-01-12", new_time="14:00", caller_id=CALLER)
    assert r["success"] is True and db.get_booking(bid)["slot_start"] == "2026-01-12T14:00"


def test_nonsense_booking_ids_are_refused_not_crashed(events):
    from app import tools

    cfg = _cfg()
    for bad in ("abc", "1; DROP TABLE bookings", -1, 10**12, None, "", "1.5"):
        assert tools.cancel_appointment(config=cfg, call_sid="CA", booking_id=bad, caller_id=CALLER)["success"] is False


def test_rescheduling_moves_the_booking_and_updates_the_owners_calendar(events, db):
    from app import tools

    cfg = _cfg()
    bid = _book(cfg)
    r = tools.reschedule_appointment(config=cfg, call_sid="CA_X", booking_id=bid, new_date="2026-01-13", new_time="11:00", caller_id=CALLER)
    assert r["success"] is True and r["confirmed_start"] == "2026-01-13T11:00"
    b = db.get_booking(bid)
    assert (b["slot_start"], b["slot_end"]) == ("2026-01-13T11:00", "2026-01-13T12:00")     # the 60-minute length is kept
    note = events["notify"][-1]
    assert note["title"] == "Booking moved" and "Monday" in note["body"] and "Tuesday" in note["body"]
    original_ics = next(e["ics"] for e in events["notify"] if e["title"] == "New booking")
    uid = re.search(r"UID:(\S+)", original_ics).group(1)
    assert f"UID:{uid}" in note["ics"] and "DTSTART:20260113T160000Z" in note["ics"]       # 11:00 EST -> 16:00 UTC
    seq = lambda ics: int(re.search(r"SEQUENCE:(\d+)", ics).group(1))
    assert seq(note["ics"]) > seq(original_ics)
    assert events["hooks"][-1][0] == "booking.updated" and events["hooks"][-1][1]["previous_start"] == "2026-01-12T10:00"
    assert "10:00" in tools.check_availability(config=cfg, date="2026-01-12", limit=50)["slots"]


@pytest.mark.parametrize("date,time,why", [
    ("2026-01-12", "10:15", "off the slot grid"),
    ("2026-01-12", "03:00", "outside hours"),
    ("2026-01-05", "07:00", "in the past / too soon"),
    ("2026-01-11", "10:00", "closed Sunday"),
    ("not-a-date", "10:00", "garbage date"),
])
def test_an_invalid_new_time_leaves_the_original_untouched(events, db, date, time, why):
    from app import tools

    cfg = _cfg()
    bid = _book(cfg)
    r = tools.reschedule_appointment(config=cfg, call_sid="CA_X", booking_id=bid, new_date=date, new_time=time, caller_id=CALLER)
    assert r["success"] is False, why
    assert db.get_booking(bid)["slot_start"] == "2026-01-12T10:00"
    assert not [e for e in events["notify"] if e["title"] == "Booking moved"]


def test_moving_onto_another_bookings_time_is_refused(events, db):
    from app import tools

    cfg = _cfg()
    mine = _book(cfg, time="10:00")
    _book(cfg, phone=OTHER, time="14:00", sid="CA_O", name="Other")
    r = tools.reschedule_appointment(config=cfg, call_sid="CA_X", booking_id=mine, new_date="2026-01-12", new_time="14:00", caller_id=CALLER)
    assert r["success"] is False and db.get_booking(mine)["slot_start"] == "2026-01-12T10:00"


def test_moving_to_a_time_that_overlaps_itself_is_allowed(events, db):
    """A 60 minute job at 10:00 may move to 10:30 (overlapping its own old slot)."""
    from app import tools

    cfg = _cfg().model_copy(update={"slot_minutes": 30})
    bid = _book(cfg, time="10:00")
    r = tools.reschedule_appointment(config=cfg, call_sid="CA_X", booking_id=bid, new_date="2026-01-12", new_time="10:30", caller_id=CALLER)
    assert r["success"] is True


def test_old_databases_gain_the_uid_column_and_keep_their_bookings(tmp_path, monkeypatch):
    import importlib

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE bookings (id INTEGER PRIMARY KEY AUTOINCREMENT, call_sid TEXT, client_id TEXT NOT NULL, caller_name TEXT NOT NULL, "
                 "caller_phone TEXT NOT NULL, service TEXT NOT NULL, slot_start TEXT NOT NULL, slot_end TEXT NOT NULL, created_at TEXT NOT NULL, "
                 "status TEXT NOT NULL DEFAULT 'confirmed', UNIQUE(client_id, slot_start))")
    conn.execute("INSERT INTO bookings (client_id, caller_name, caller_phone, service, slot_start, slot_end, created_at) "
                 "VALUES ('demo_hvac','Old','+15555550100','Repair','2026-01-12T10:00','2026-01-12T11:00','2025-12-01')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("CALLKETTLE_DB_PATH", str(path))
    from app import storage, tools

    importlib.reload(storage)
    storage.init_db()
    storage.init_db()
    cfg = _cfg()
    found = tools.find_my_appointments(config=cfg, caller_id=CALLER)["appointments"]
    assert len(found) == 1
    from app import notify

    notify_calls = []
    monkeypatch.setattr(notify, "notify_owner", lambda c, **kw: notify_calls.append(kw))
    assert tools.cancel_appointment(config=cfg, call_sid="CA", booking_id=found[0]["booking_id"], caller_id=CALLER)["success"] is True


# ---------------------------------------------------------------- through the AI

def test_the_ai_reschedules_with_the_callers_verified_number_not_one_it_makes_up(events, db, monkeypatch):
    from app import agent, tools
    from tests.test_agent import FakeAnthropicClient, FakeResponse, FakeTextBlock, FakeToolUseBlock

    cfg = _cfg()
    bid = _book(cfg, phone=OTHER, sid="CA_VICTIM", name="Victim")          # someone else's booking
    mine = _book(cfg, phone=CALLER, time="13:00", sid="CA_MINE")
    fake = FakeAnthropicClient([
        FakeResponse([FakeToolUseBlock("t1", "find_my_appointments", {})], "tool_use"),
        FakeResponse([FakeToolUseBlock("t2", "cancel_appointment", {"booking_id": bid, "caller_phone": OTHER})], "tool_use"),  # tries the victim's id
        FakeResponse([FakeToolUseBlock("t3", "reschedule_appointment", {"booking_id": mine, "date": "2026-01-12", "time": "15:00"})], "tool_use"),
        FakeResponse([FakeTextBlock("Done, you're moved to three p.m.")], "end_turn"),
    ])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)
    session = agent.start_session("CA_AI", cfg, CALLER)
    reply, ended, _ = agent.run_turn(session, "I need to move my appointment")
    assert "three" in reply
    assert db.get_booking(bid) is not None                                  # the victim's booking survived the attempt
    assert db.get_booking(mine)["slot_start"] == "2026-01-12T15:00"
    results = [m["content"] for m in session.messages if m["role"] == "user" and isinstance(m["content"], list)]
    assert any("no appointment with that id" in str(r) for r in results)


def test_the_system_prompt_forbids_booking_twice_to_move_an_appointment():
    from app import agent

    p = agent.build_system_prompt(_cfg(), CALLER)
    assert "NEVER book a second appointment" in p and "find_my_appointments" in p
    names = {t["name"] for t in agent.TOOLS}
    assert {"find_my_appointments", "cancel_appointment", "reschedule_appointment"} <= names


def test_export_and_client_deletion_include_cancelled_bookings(app_client, events):
    client, main = app_client
    from app import storage, tools

    cfg = _cfg()
    bid = _book(cfg)
    tools.cancel_appointment(config=cfg, call_sid="CA_X", booking_id=bid, caller_id=CALLER)
    exported = client.get("/admin/export?key=master_key_for_tests").json()
    assert len(exported["cancelled_bookings"]) == 1
    r = client.post("/admin/client/demo_hvac/delete?key=master_key_for_tests&confirm=demo_hvac").json()
    assert r["deleted"]["cancelled_bookings"] == 1
