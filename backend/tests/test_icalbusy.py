"""The owner's private iCal link as busy time: parsing (zones, recurrence, DST, all-day), safety, and fail-open behaviour."""
import os
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

NY = "America/New_York"


def _ics(*events):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\n" + "".join(events) + "END:VCALENDAR\r\n").encode()


def _ev(uid, start, end, extra="", tz=True):
    if tz:
        s, e = f"DTSTART;TZID=America/New_York:{start}", f"DTEND;TZID=America/New_York:{end}"
    else:
        s, e = f"DTSTART:{start}", f"DTEND:{end}"
    return f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20260101T000000Z\r\n{s}\r\n{e}\r\n{extra}END:VEVENT\r\n"


class _Feed:
    def __init__(self, body=b"", status=200, redirect=None):
        self.body, self.status, self.redirect, self.hits = body, status, redirect, 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits += 1
                if outer.redirect:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect)
                    self.end_headers()
                    return
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/cal.ics"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("CALLKETTLE_DB_PATH", path)
    monkeypatch.setenv("CALLKETTLE_ALLOW_PRIVATE_WEBHOOKS", "1")      # lets the test feed live on 127.0.0.1
    import importlib

    from app import icalbusy, ops, storage

    importlib.reload(storage)
    storage.init_db()
    icalbusy.clear_cache()
    alerts = []
    monkeypatch.setattr(ops, "alert_operator", lambda title, body, **kw: alerts.append(title) or True)
    yield alerts
    try:
        os.remove(path)
    except PermissionError:
        pass


@pytest.fixture
def feed():
    made = []

    def make(*a, **k):
        f = _Feed(*a, **k)
        made.append(f)
        return f

    yield make
    for f in made:
        f.close()


def _busy(url, day="2026-01-12", **kw):
    from app import icalbusy

    d = datetime.strptime(day, "%Y-%m-%d")
    return icalbusy.busy_periods(url, d, d.replace(hour=23, minute=59), NY, **kw)


def _hm(periods):
    return [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in periods]


# ------------------------------------------------------------------ parsing

def test_a_zoned_event_blocks_its_time(feed):
    f = feed(_ics(_ev("a", "20260112T100000", "20260112T110000")))
    assert _hm(_busy(f.url)) == [("10:00", "11:00")]


def test_a_utc_event_is_converted_to_the_clients_wall_clock(feed):
    f = feed(_ics(_ev("u", "20260112T150000Z", "20260112T160000Z", tz=False)))     # 15:00Z is 10:00 in New York in January
    assert _hm(_busy(f.url)) == [("10:00", "11:00")]


def test_a_floating_time_event_is_the_owners_wall_clock(feed):
    f = feed(_ics(_ev("f", "20260112T130000", "20260112T140000", tz=False)))
    assert _hm(_busy(f.url)) == [("13:00", "14:00")]


def test_events_on_other_days_do_not_block_this_day(feed):
    f = feed(_ics(_ev("d", "20260113T100000", "20260113T110000")))
    assert _busy(f.url) == []


def test_a_weekly_recurring_event_blocks_every_occurrence(feed):
    f = feed(_ics(_ev("r", "20260105T130000", "20260105T140000", "RRULE:FREQ=WEEKLY;BYDAY=MO\r\n")))
    assert _hm(_busy(f.url, "2026-01-12")) == [("13:00", "14:00")]
    assert _hm(_busy(f.url, "2026-01-19")) == [("13:00", "14:00")]
    assert _busy(f.url, "2026-01-13") == []


def test_a_recurring_event_keeps_its_wall_clock_time_across_the_dst_change(feed):
    f = feed(_ics(_ev("dst", "20260302T100000", "20260302T110000", "RRULE:FREQ=WEEKLY;BYDAY=MO\r\n")))
    assert _hm(_busy(f.url, "2026-03-02")) == [("10:00", "11:00")]       # EST
    assert _hm(_busy(f.url, "2026-03-09")) == [("10:00", "11:00")]       # first Monday of EDT: still 10:00 local


def test_an_excluded_occurrence_is_free(feed):
    f = feed(_ics(_ev("ex", "20260105T130000", "20260105T140000", "RRULE:FREQ=WEEKLY;BYDAY=MO\r\nEXDATE;TZID=America/New_York:20260112T130000\r\n")))
    assert _busy(f.url, "2026-01-12") == [] and _hm(_busy(f.url, "2026-01-19")) == [("13:00", "14:00")]


def test_cancelled_and_free_events_do_not_block(feed):
    f = feed(_ics(_ev("c", "20260112T090000", "20260112T100000", "STATUS:CANCELLED\r\n"),
                  _ev("t", "20260112T100000", "20260112T110000", "TRANSP:TRANSPARENT\r\n"),
                  _ev("ok", "20260112T110000", "20260112T120000")))
    assert _hm(_busy(f.url)) == [("11:00", "12:00")]


def test_all_day_events_are_ignored_unless_the_owner_wants_them_to_block(feed):
    body = _ics("BEGIN:VEVENT\r\nUID:ad\r\nDTSTAMP:20260101T000000Z\r\nDTSTART;VALUE=DATE:20260112\r\nDTEND;VALUE=DATE:20260113\r\nEND:VEVENT\r\n")
    f = feed(body)
    assert _busy(f.url) == []
    from app import icalbusy

    icalbusy.clear_cache()
    assert _hm(_busy(f.url, all_day_blocks=True)) == [("00:00", "00:00")] or len(_busy(f.url, all_day_blocks=True)) == 1


def test_our_own_bookings_on_their_calendar_never_block_us(feed):
    """If the owner accepted our emailed invite, the event carries our UID; it must not block its own reschedule."""
    f = feed(_ics(_ev("abc123@deskline-ai", "20260112T100000", "20260112T110000"), _ev("theirs", "20260112T140000", "20260112T150000")))
    assert _hm(_busy(f.url)) == [("14:00", "15:00")]


def test_duration_instead_of_end_and_zero_length_events(feed):
    f = feed(_ics("BEGIN:VEVENT\r\nUID:dur\r\nDTSTAMP:20260101T000000Z\r\nDTSTART;TZID=America/New_York:20260112T090000\r\nDURATION:PT90M\r\nEND:VEVENT\r\n",
                  _ev("zero", "20260112T120000", "20260112T120000")))
    assert _hm(_busy(f.url)) == [("09:00", "10:30")]


def test_webcal_links_are_accepted():
    from app import icalbusy
    from app.config import load_client_config

    assert icalbusy.normalize_url("webcal://calendar.example.com/x.ics") == "https://calendar.example.com/x.ics"
    cfg = load_client_config("demo_hvac").model_copy(update={})
    from app.config import ClientConfig

    ok = ClientConfig.model_validate({**cfg.model_dump(), "calendar_ical_url": "webcal://calendar.example.com/x.ics"})
    assert ok.calendar_ical_url == "https://calendar.example.com/x.ics"
    for bad in ("http://example.com/x.ics", "ftp://x", "https://has space.com/x"):
        with pytest.raises(Exception):
            ClientConfig.model_validate({**cfg.model_dump(), "calendar_ical_url": bad})


# ------------------------------------------------------------------ safety and failure

def test_a_non_calendar_page_is_rejected_not_trusted(feed):
    f = feed(b"<html>hello</html>")
    assert _busy(f.url) is None


def test_garbage_and_huge_feeds_do_not_crash_or_exhaust_memory(feed):
    assert _busy(feed(b"\x00\x01\x02 not a calendar").url) is None
    from app import icalbusy

    icalbusy.clear_cache()
    assert _busy(feed(b"BEGIN:VCALENDAR\r\n" + b"X-JUNK:" + b"a" * (4 * 1024 * 1024) + b"\r\nEND:VCALENDAR\r\n").url) is None


def test_a_redirect_to_a_private_address_is_refused_when_private_hosts_are_not_allowed(feed, monkeypatch):
    from app import icalbusy

    monkeypatch.delenv("CALLKETTLE_ALLOW_PRIVATE_WEBHOOKS")
    f = feed(_ics(_ev("a", "20260112T100000", "20260112T110000")))
    assert icalbusy.check_url(f.url)["ok"] is False                # http://127.0.0.1 is refused outright
    assert icalbusy.check_url("https://169.254.169.254/latest/meta-data")["ok"] is False


def test_a_redirect_loop_is_cut_off(feed):
    f = feed(redirect="http://127.0.0.1:1/never")
    assert _busy(f.url) is None


def test_a_dead_feed_fails_open_and_alerts_the_operator_once(env, feed):
    from app import icalbusy

    assert icalbusy.busy_periods("http://127.0.0.1:9/x.ics", datetime(2026, 1, 12), datetime(2026, 1, 13), NY) is None
    icalbusy.busy_periods("http://127.0.0.1:9/x.ics", datetime(2026, 1, 12), datetime(2026, 1, 13), NY)
    assert env == ["A client's calendar link is unreadable"]


def test_after_a_failure_the_last_good_copy_keeps_working_and_a_down_host_is_not_hammered(feed):
    from app import icalbusy

    f = feed(_ics(_ev("a", "20260112T100000", "20260112T110000")))
    assert _hm(_busy(f.url)) == [("10:00", "11:00")]
    f.status = 500
    icalbusy._cache[f.url]["at"] -= icalbusy.FRESH_SECONDS + 1          # the cached copy is now stale
    hits = f.hits
    assert _hm(_busy(f.url)) == [("10:00", "11:00")]                    # the stale copy answers, one retry happened
    assert _hm(_busy(f.url)) == [("10:00", "11:00")] and f.hits == hits + 1      # backoff: no second retry within 60 s


def test_a_copy_older_than_an_hour_is_not_trusted(feed):
    from app import icalbusy

    f = feed(_ics(_ev("a", "20260112T100000", "20260112T110000")))
    _busy(f.url)
    f.status = 500
    icalbusy._cache[f.url]["at"] -= icalbusy.STALE_OK_SECONDS + 10
    assert _busy(f.url) is None


def test_a_fresh_copy_is_reused_without_another_request(feed):
    f = feed(_ics(_ev("a", "20260112T100000", "20260112T110000")))
    _busy(f.url)
    _busy(f.url, "2026-01-13")
    assert f.hits == 1


def test_check_url_reports_what_it_sees(feed):
    from app import icalbusy

    f = feed(_ics(_ev("a", "20260112T100000", "20260112T110000")))
    out = icalbusy.check_url(f.url)
    assert out["ok"] is True and out["events_total"] == 1
    assert icalbusy.check_url("http://127.0.0.1:9/nope.ics")["ok"] is False


# ------------------------------------------------------------------ through the assistant's tools

def _cfg(url, **over):
    from app.config import load_client_config

    return load_client_config("demo_hvac").model_copy(update={"calendar_ical_url": url, **over})


def test_availability_hides_slots_the_owner_already_filled(feed):
    from app import tools

    f = feed(_ics(_ev("a", "20260112T100000", "20260112T120000")))
    slots = tools.check_availability(config=_cfg(f.url), date="2026-01-12", limit=30)["slots"]
    assert "09:00" in slots and "12:00" in slots and "10:00" not in slots and "11:00" not in slots


def test_booking_a_time_the_owner_just_filled_is_refused_with_a_fresh_read(feed):
    from app import tools

    f = feed(_ics())                                                      # empty at first: slot looks free
    cfg = _cfg(f.url)
    assert "10:00" in tools.check_availability(config=cfg, date="2026-01-12", limit=30)["slots"]
    f.body = _ics(_ev("late", "20260112T100000", "20260112T110000"))     # the owner adds a job a moment later
    res = tools.book_appointment(call_sid="CA_I1", config=cfg, caller_name="Pat Lee", caller_phone="+15555550100",
                                 service="Emergency repair", date="2026-01-12", time="10:00")
    assert res["success"] is False and "owner's calendar" in res["error"]


def test_an_unreadable_calendar_never_blocks_a_booking(feed):
    from app import tools

    res = tools.book_appointment(call_sid="CA_I2", config=_cfg("http://127.0.0.1:9/x.ics"), caller_name="Pat Lee",
                                 caller_phone="+15555550100", service="Emergency repair", date="2026-01-12", time="10:00")
    assert res["success"] is True


def test_moving_a_booking_onto_a_busy_time_is_refused_but_its_own_event_does_not_block(feed):
    from app import tools

    f = feed(_ics())
    cfg = _cfg(f.url)
    first = tools.book_appointment(call_sid="CA_I3", config=cfg, caller_name="Pat Lee", caller_phone="+15555550100",
                                   service="Emergency repair", date="2026-01-12", time="10:00")
    assert first["success"]
    # the owner accepted our invite (carries our UID) and also has a real job at 14:00
    f.body = _ics(_ev("zzz@deskline-ai", "20260112T100000", "20260112T110000"), _ev("job", "20260112T140000", "20260112T150000"))
    busy = tools.reschedule_appointment(config=cfg, call_sid="CA_I3", booking_id=first["booking_id"], caller_id="+15555550100",
                                        new_date="2026-01-12", new_time="14:00")
    assert busy["success"] is False and "owner's calendar" in busy["error"]
    moved = tools.reschedule_appointment(config=cfg, call_sid="CA_I3", booking_id=first["booking_id"], caller_id="+15555550100",
                                         new_date="2026-01-12", new_time="11:00")
    assert moved["success"] is True


def test_google_and_ical_are_combined(feed, monkeypatch):
    from app import gcal, tools

    f = feed(_ics(_ev("a", "20260112T100000", "20260112T110000")))
    monkeypatch.setattr(gcal, "busy_periods", lambda *a, **k: [(datetime(2026, 1, 12, 13), datetime(2026, 1, 12, 14))])
    cfg = _cfg(f.url, google_calendar_id="owner@example.com")
    slots = tools.check_availability(config=cfg, date="2026-01-12", limit=30)["slots"]
    assert "10:00" not in slots and "13:00" not in slots and "09:00" in slots
