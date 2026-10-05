"""Booking overlap protection, Google Calendar sync, the intake form, and the
hosted terms/intake pages."""
import json
import sqlite3
from datetime import datetime

import pytest


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("CALLKETTLE_DB_PATH", str(tmp_path / "proc.db"))
    from app import storage

    importlib.reload(storage)
    storage.init_db()
    yield storage


def _dental(**update):
    from app.config import load_client_config

    return load_client_config("demo_dental").model_copy(update=update)


# ------------------------------------------------------------ overlap protection

def test_a_long_job_blocks_the_slots_it_overlaps():
    from app import tools

    config = _dental()  # 30-minute slots; "Filling" takes 45
    first = tools.book_appointment(
        call_sid=None, config=config, caller_name="A", caller_phone="+15555550100",
        service="Filling", date="2026-01-12", time="09:00",
    )
    assert first["success"]
    slots = tools.check_availability(config=config, date="2026-01-12", limit=20)["slots"]
    assert "09:00" not in slots and "09:30" not in slots  # 9:00-9:45 overlaps the 9:30 slot
    assert "10:00" in slots

    clash = tools.book_appointment(
        call_sid=None, config=config, caller_name="B", caller_phone="+15555550100",
        service="Routine cleaning", date="2026-01-12", time="09:30",
    )
    assert clash["success"] is False


def test_back_to_back_bookings_are_fine():
    from app import tools

    config = _dental()
    a = tools.book_appointment(call_sid=None, config=config, caller_name="A", caller_phone="+15555550100",
                               service="Routine cleaning", date="2026-01-12", time="09:00")
    b = tools.book_appointment(call_sid=None, config=config, caller_name="B", caller_phone="+15555550100",
                               service="Routine cleaning", date="2026-01-12", time="09:30")
    assert a["success"] and b["success"]


# ------------------------------------------------------------ Google Calendar

class _FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


@pytest.fixture
def google(monkeypatch):
    from app import gcal

    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps({"client_email": "owner@example.com"}))
    monkeypatch.setattr(gcal, "_headers", lambda: {"Authorization": "Bearer test"})
    calls = []
    responses = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, json))
        for suffix, resp in responses.items():
            if url.endswith(suffix):
                return resp() if callable(resp) else resp
        raise AssertionError(f"unexpected call {url}")

    monkeypatch.setattr(gcal.httpx, "post", fake_post)
    return gcal, calls, responses


def test_freebusy_request_shape_and_utc_to_local_conversion(google):
    gcal, calls, responses = google
    responses["/freeBusy"] = _FakeResponse({"calendars": {"owner@example.com": {"busy": [
        {"start": "2026-01-12T15:00:00Z", "end": "2026-01-12T16:00:00Z"}]}}})  # 10:00-11:00 New York
    busy = gcal.busy_periods("owner@example.com", datetime(2026, 1, 12), datetime(2026, 1, 13), "America/New_York")
    url, body = calls[0]
    assert url == "https://www.googleapis.com/calendar/v3/freeBusy"
    assert body["items"] == [{"id": "owner@example.com"}] and body["timeZone"] == "America/New_York"
    assert body["timeMin"].startswith("2026-01-12T00:00:00-05:00")
    assert busy == [(datetime(2026, 1, 12, 10, 0), datetime(2026, 1, 12, 11, 0))]


def test_calendar_that_was_never_shared_is_reported_as_unreadable_not_empty(google):
    gcal, _, responses = google
    responses["/freeBusy"] = _FakeResponse({"calendars": {"owner@example.com": {"errors": [{"reason": "notFound"}], "busy": []}}})
    assert gcal.busy_periods("owner@example.com", datetime(2026, 1, 12), datetime(2026, 1, 13), "America/New_York") is None


def test_google_outage_fails_soft(google):
    gcal, _, responses = google
    responses["/freeBusy"] = _FakeResponse({}, status=503)
    assert gcal.busy_periods("owner@example.com", datetime(2026, 1, 12), datetime(2026, 1, 13), "America/New_York") is None
    assert gcal.is_free("owner@example.com", datetime(2026, 1, 12, 9), datetime(2026, 1, 12, 10), "America/New_York") is None


def test_disabled_without_credentials(monkeypatch):
    from app import gcal

    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    assert gcal.enabled() is False
    assert gcal.busy_periods("owner@example.com", datetime(2026, 1, 12), datetime(2026, 1, 13), "UTC") is None
    assert gcal.create_event("owner@example.com", summary="s", description="d", start=datetime(2026, 1, 12, 9),
                             end=datetime(2026, 1, 12, 10), tz_name="UTC") is None


def test_create_event_request_shape(google):
    gcal, calls, responses = google
    responses["/events"] = _FakeResponse({"id": "evt123"})
    event_id = gcal.create_event("owner@example.com", summary="Cleaning: Jane", description="Booked by AI",
                                 start=datetime(2026, 1, 12, 9, 0), end=datetime(2026, 1, 12, 9, 30), tz_name="America/New_York")
    url, body = calls[0]
    assert event_id == "evt123"
    assert url.endswith("/calendars/owner@example.com/events")
    assert body["start"] == {"dateTime": "2026-01-12T09:00:00", "timeZone": "America/New_York"}
    assert body["summary"] == "Cleaning: Jane" and "attendees" not in body  # service accounts can't invite attendees


def test_owner_calendar_conflicts_hide_slots_and_block_booking(monkeypatch):
    from app import gcal, tools

    config = _dental(google_calendar_id="owner@example.com")
    monkeypatch.setattr(gcal, "busy_periods", lambda cal, s, e, tz: [(datetime(2026, 1, 12, 9, 0), datetime(2026, 1, 12, 10, 0))])
    slots = tools.check_availability(config=config, date="2026-01-12", limit=20)["slots"]
    assert "09:00" not in slots and "09:30" not in slots and "10:00" in slots

    monkeypatch.setattr(gcal, "is_free", lambda cal, s, e, tz: False)
    refused = tools.book_appointment(call_sid=None, config=config, caller_name="A", caller_phone="+15555550100",
                                     service="Routine cleaning", date="2026-01-12", time="10:00")
    assert refused["success"] is False and "calendar" in refused["error"]


def test_booking_is_written_to_the_owners_calendar(monkeypatch):
    from app import gcal, tools

    config = _dental(google_calendar_id="owner@example.com")
    monkeypatch.setattr(gcal, "busy_periods", lambda *a: [])
    monkeypatch.setattr(gcal, "is_free", lambda *a: True)
    written = {}
    monkeypatch.setattr(gcal, "create_event_in_background", lambda cal, **kw: written.update(cal=cal, **kw))
    result = tools.book_appointment(call_sid=None, config=config, caller_name="Jane", caller_phone="+15555550100",
                                    service="Routine cleaning", date="2026-01-12", time="11:00")
    assert result["success"]
    assert written["cal"] == "owner@example.com" and written["summary"] == "Routine cleaning: Jane"
    assert written["start"] == datetime(2026, 1, 12, 11, 0) and written["tz_name"] == "America/New_York"


def test_unreadable_calendar_does_not_block_bookings(monkeypatch):
    from app import gcal, tools

    config = _dental(google_calendar_id="owner@example.com")
    monkeypatch.setattr(gcal, "busy_periods", lambda *a: None)
    monkeypatch.setattr(gcal, "is_free", lambda *a: None)
    monkeypatch.setattr(gcal, "create_event_in_background", lambda *a, **k: None)
    assert tools.check_availability(config=config, date="2026-01-12")["slots"]
    assert tools.book_appointment(call_sid=None, config=config, caller_name="A", caller_phone="+15555550100",
                                  service="Routine cleaning", date="2026-01-12", time="09:00")["success"]


# ------------------------------------------------------------ intake form

def _intake(**kw):
    base = {
        "business_name": "Ruiz Plumbing", "owner_name": "Ana Ruiz", "owner_phone": "+15555550100",
        "owner_email": "owner@example.com", "trade": "plumbing", "phone_provider": "Verizon",
        "hours": {"mon": "08:00-17:00", "tue": "08:00-17:00", "sat": "closed"},
        "services": [{"name": "Drain cleaning", "minutes": 60}],
        "faqs": [{"q": "Do you charge for estimates?", "a": "No, estimates are free."}],
        "never_say": "Never quote prices.", "google_calendar_email": "", "notes": "",
    }
    return {**base, **kw}


def test_intake_is_saved_and_visible_to_the_operator_only(app_client):
    c, main = app_client
    r = c.post("/intake", json=_intake(), headers={"fly-client-ip": "198.51.100.20"})
    assert r.status_code == 200 and r.json()["success"] and r.json()["id"] == 1

    assert c.get("/admin/intakes").status_code == 403
    assert c.get("/admin/intake/1", params={"key": "wrong"}).status_code == 403
    listing = c.get("/admin/intakes", params={"key": "master_key_for_tests"}).json()["intakes"]
    assert listing[0]["business_name"] == "Ruiz Plumbing"
    full = c.get("/admin/intake/1", params={"key": "master_key_for_tests"}).json()
    assert full["payload"]["services"][0]["name"] == "Drain cleaning" and full["payload"]["hours"]["sat"] == "closed"
    assert c.get("/admin/intake/99", params={"key": "master_key_for_tests"}).status_code == 404


@pytest.mark.parametrize("bad", [
    {"owner_phone": "12345"},
    {"owner_email": "not-an-email"},
    {"business_name": ""},
    {"services": []},
    {"hours": {"mon": "9am-5pm"}},
    {"hours": {"funday": "closed"}},
    {"business_name": "x" * 300},
])
def test_intake_rejects_bad_input(app_client, bad):
    c, _ = app_client
    assert c.post("/intake", json=_intake(**bad), headers={"fly-client-ip": "198.51.100.21"}).status_code == 422


def test_intake_honeypot_and_rate_limit(app_client):
    c, main = app_client
    assert c.post("/intake", json=_intake(website="http://spam"), headers={"fly-client-ip": "198.51.100.22"}).status_code == 200
    assert c.get("/admin/intakes", params={"key": "master_key_for_tests"}).json()["intakes"] == []  # bot saved nothing
    codes = [c.post("/intake", json=_intake(), headers={"fly-client-ip": "203.0.113.77"}).status_code
             for _ in range(main._INTAKES_PER_IP_PER_HOUR + 1)]
    assert codes[-1] == 429


def test_intake_tells_the_operator(app_client, monkeypatch):
    c, main = app_client
    from app import notify

    sent = []
    monkeypatch.setattr(notify, "notify_owner", lambda config, **kw: sent.append((config.client_id, kw)))
    c.post("/intake", json=_intake(), headers={"fly-client-ip": "198.51.100.23"})
    assert sent and sent[0][0] == "callkettle_sales" and "Ruiz Plumbing" in sent[0][1]["body"]


def test_terms_and_intake_pages_are_served(app_client):
    c, _ = app_client
    terms = c.get("/terms")
    assert terms.status_code == 200 and "Terms of Service" in terms.text and "Monthly fee" in terms.text and "$•••" not in terms.text and "$•••" not in terms.text
    assert "no setup fee" in terms.text.lower() and "Client signature" not in terms.text
    start = c.get("/start")
    assert start.status_code == 200 and "Set up your front desk" in start.text
    assert "does not turn on phone service" in start.text


# ------------------------------------------------------------ intake -> client config

def test_intake_converts_to_a_valid_client_config():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "onboard_client", Path(__file__).resolve().parent.parent / "scripts" / "onboard_client.py")
    onboard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(onboard)

    config = onboard.config_from_intake(_intake(google_calendar_email="owner@example.com"))
    from app.config import ClientConfig

    ClientConfig.model_validate(config)
    assert config["client_id"] == "ruiz_plumbing"
    assert config["escalation_phone"] == "+15555550100" and config["owner_email"] == "owner@example.com"
    assert config["business_hours"]["mon"] == ["08:00", "17:00"] and config["business_hours"]["sun"] == "closed"
    assert config["google_calendar_id"] == "owner@example.com"
    assert "Never quote prices." in config["extra_instructions"]
    assert "record_transcripts" not in config  # plumbing keeps summaries on

    healthcare = onboard.config_from_intake(_intake(trade="dental practice"))
    assert healthcare["record_transcripts"] is False
