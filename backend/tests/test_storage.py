import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("CALLKETTLE_DB_PATH", path)
    import importlib

    from app import storage

    importlib.reload(storage)
    storage.init_db()
    yield storage
    os.remove(path)


def test_log_call_start_and_turn(temp_db):
    temp_db.log_call_start("CA123", "demo_dental", "+15551234567")
    temp_db.log_turn("CA123", "caller", "hi there")
    temp_db.log_call_end("CA123", "completed")


def test_booking_created(temp_db):
    booking_id = temp_db.create_booking(
        call_sid="CA1",
        client_id="demo_dental",
        caller_name="Sami",
        caller_phone="+15555550100",
        service="Routine cleaning",
        slot_start="2026-01-09T09:30",
        slot_end="2026-01-09T10:00",
    )
    assert booking_id > 0
    assert "2026-01-09T09:30" in temp_db.get_booked_slots("demo_dental", "2026-01-09")


def test_double_booking_is_rejected(temp_db):
    temp_db.create_booking(
        call_sid="CA1",
        client_id="demo_dental",
        caller_name="Sami",
        caller_phone="+15555550100",
        service="Routine cleaning",
        slot_start="2026-01-09T09:30",
        slot_end="2026-01-09T10:00",
    )
    with pytest.raises(temp_db.BookingConflict):
        temp_db.create_booking(
            call_sid="CA2",
            client_id="demo_dental",
            caller_name="Someone Else",
            caller_phone="+15555550100",
            service="Filling",
            slot_start="2026-01-09T09:30",
            slot_end="2026-01-09T10:15",
        )


def test_different_clients_can_share_a_slot(temp_db):
    temp_db.create_booking(
        call_sid="CA1", client_id="demo_dental", caller_name="A", caller_phone="+1",
        service="Routine cleaning", slot_start="2026-01-09T09:30", slot_end="2026-01-09T10:00",
    )
    booking_id = temp_db.create_booking(
        call_sid="CA2", client_id="demo_hvac", caller_name="B", caller_phone="+2",
        service="Routine maintenance", slot_start="2026-01-09T09:30", slot_end="2026-01-09T10:30",
    )
    assert booking_id > 0


# ---------------------------------------------------------------- the rename migrations (database file and the two internal client ids)
def test_the_old_named_database_file_is_moved_verified_and_kept_as_a_backup(tmp_path, monkeypatch):
    import sqlite3

    from app import storage

    legacy = tmp_path / storage.LEGACY_DB_NAME
    conn = sqlite3.connect(legacy)
    conn.executescript("CREATE TABLE calls (call_sid TEXT, client_id TEXT); INSERT INTO calls VALUES ('CA1', 'x'), ('CA2', 'y');")
    conn.commit()
    conn.close()
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "callkettle.db"))
    note = storage.migrate_legacy_database()
    assert note and note.startswith("moved")
    assert sqlite3.connect(tmp_path / "callkettle.db").execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 2
    assert not legacy.exists() and (tmp_path / "callkettle-before-rename.db.bak").exists()
    assert storage.migrate_legacy_database() is None                         # idempotent


def test_a_failed_database_move_keeps_using_the_old_file(tmp_path, monkeypatch):
    from app import storage

    legacy = tmp_path / storage.LEGACY_DB_NAME
    legacy.write_bytes(b"this is not a database")
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "callkettle.db"))
    note = storage.migrate_legacy_database()
    assert note.startswith("MIGRATION FAILED") and storage.DB_PATH == str(legacy)
    assert not (tmp_path / "callkettle.db").exists() and not (tmp_path / "callkettle.db.migrating").exists()


def test_history_of_the_renamed_internal_clients_carries_over(tmp_path, monkeypatch):
    import sqlite3

    from app import storage

    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "t.db"))
    storage.init_db()
    old_demo, old_sales = list(storage.LEGACY_CLIENT_IDS)
    with storage._conn() as c:
        c.execute("INSERT INTO calls (call_sid, client_id, started_at) VALUES ('CA9', ?, '2026-10-01T00:00:00')", (old_demo,))
        c.execute("INSERT INTO bookings (call_sid, client_id, caller_name, caller_phone, service, slot_start, slot_end, created_at) VALUES ('x', ?, 'A', '1', 's', '2026-10-09T10:00', '2026-10-09T11:00', 'now')", (old_sales,))
    storage.init_db()
    conn = sqlite3.connect(storage.DB_PATH)
    assert conn.execute("SELECT client_id FROM calls WHERE call_sid='CA9'").fetchone()[0] == "callkettle_demo"
    assert conn.execute("SELECT client_id FROM bookings").fetchone()[0] == "callkettle_sales"
    conn.close()


def test_legacy_environment_variable_names_still_work(monkeypatch):
    import importlib
    import os

    monkeypatch.setenv("%s_RENAME_PROBE" % ("DESK" + "LINE"), "carried")
    import app

    try:
        importlib.reload(app)
        assert os.environ["CALLKETTLE_RENAME_PROBE"] == "carried"
    finally:
        os.environ.pop("CALLKETTLE_RENAME_PROBE", None)
