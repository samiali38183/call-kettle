"""The first paying customer's whole journey, end to end, offline.

Admin intake -> publish -> config live in-process -> owner account with a temporary password -> forced password change ->
signed Twilio webhooks -> mocked model -> booking -> owner email (through the real mailer with only the SMTP socket faked) ->
emergency / human / after-hours calls -> needs-attention -> portal (calls, transcript, calendar, mark handled, CSV) ->
tenant isolation -> weekly recap -> activation page -> operator password recovery.

Temp database and temp live-config folder only. No real Twilio, Anthropic, SMTP or ntfy traffic. This proves the code path, not
production: carriers, speech recognition, real model behaviour and real mail delivery are NOT exercised here.
"""
from __future__ import annotations

import html
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from tests.test_agent import FakeAnthropicClient, FakeResponse, FakeTextBlock, FakeToolUseBlock

MASTER = "master_key_for_tests"
AUTH_TOKEN = "journey_twilio_token"
CID = "dulles_comfort_heating_cooling"
OWNER_EMAIL = "owner@example.com"
OWNER_CELL = "+15555550100"
CALLER = "+15555550100"
NEW_PW = "furnace season passphrase 7"

INTAKE = {
    "business_name": "Dulles Comfort Heating & Cooling", "owner_name": "Maria Lopez", "owner_phone": "+15555550100",
    "owner_email": OWNER_EMAIL, "trade": "HVAC", "phone_provider": "Verizon",
    "hours": {"mon": "08:00-17:00", "tue": "08:00-17:00", "wed": "08:00-17:00", "thu": "08:00-17:00", "fri": "08:00-17:00",
              "sat": "08:00-12:00", "sun": "closed"},
    "services": [{"name": "Repair visit", "minutes": 60}, {"name": "Seasonal tune-up", "minutes": 60},
                 {"name": "New system estimate", "minutes": 90}],
    "faqs": [{"q": "What areas do you serve?", "a": "Herndon, Reston, Sterling, Chantilly and Ashburn."}],
    "never_say": "Never promise a same-day visit.", "google_calendar_email": "", "notes": "Synthetic test business.",
}


def _csrf(text: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', text).group(1)


@pytest.fixture
def journey(app_client, tmp_path, monkeypatch):
    client, main = app_client
    from app import agent, config as config_module, mailer, notify, tools, twilio_utils

    monkeypatch.setattr(config_module, "LIVE_CLIENTS_DIR", tmp_path / "live")
    config_module.load_client_config.cache_clear()

    # Real signature checks, with a test token: every webhook below is signed the way Twilio signs it.
    monkeypatch.setattr(twilio_utils, "_SKIP_SIGNATURE_CHECK", False)
    monkeypatch.setattr(twilio_utils, "_AUTH_TOKEN", AUTH_TOKEN)

    # The production mail path (EMAIL_PROVIDER unset + SMTP_* set => smtp), with only the network send replaced.
    for name in ("EMAIL_PROVIDER", "POSTMARK_SERVER_TOKEN", "RESEND_API_KEY", "EMAIL_FROM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SMTP_HOST", "smtp.invalid")
    monkeypatch.setenv("SMTP_USER", "owner@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "not-a-real-password")
    monkeypatch.setenv("SMTP_FROM", "Call Kettle <owner@example.com>")
    sent: list = []
    monkeypatch.setattr(mailer, "_send_smtp", lambda msg: sent.append(msg))
    pushes: list = []
    monkeypatch.setattr(notify, "_send_ntfy", lambda topic, title, body: pushes.append((topic, title, body)))

    class _Now:                                       # owner alerts run on threads in production; run them inline here
        def __init__(self, target, args=(), kwargs=None, daemon=None):
            self._t, self._a, self._k = target, args, kwargs or {}

        def start(self):
            self._t(*self._a, **self._k)

    monkeypatch.setattr(notify.threading, "Thread", _Now)

    fake = FakeAnthropicClient([])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)

    class J:
        pass

    j = J()
    j.client, j.main, j.cfg, j.sent, j.pushes, j.fake, j.tools, j.agent = client, main, config_module, sent, pushes, fake, tools, agent
    from app import storage
    j.storage = storage

    def queue(*responses):
        fake.messages._responses.extend(responses)

    def signed(path: str, form: dict, *, browser: TestClient | None = None):
        url = "http://testserver" + path
        sig = RequestValidator(AUTH_TOKEN).compute_signature(url, form)
        return (browser or client).post(path, data=form, headers={"X-Twilio-Signature": sig})

    j.queue, j.signed = queue, signed
    yield j
    config_module.load_client_config.cache_clear()


def _admin_login(client):
    assert client.post("/admin/login", data={"key": MASTER}, follow_redirects=False).status_code == 303
    client.headers["Origin"] = "http://testserver"


def _publish(j, *, after_hours_action: str = "book") -> str:
    """Operator: review the intake, add the HVAC policy block from the runbook, Go live. Returns the temporary password."""
    iid = j.storage.create_intake(dict(INTAKE))
    _admin_login(j.client)
    page = j.client.get(f"/admin/intake/{iid}/review")
    assert page.status_code == 200 and f"client_id: {CID}" in page.text
    yaml_text = html.unescape(page.text.split("<textarea")[1].split(">", 1)[1].split("</textarea>")[0])
    yaml_text += ("policy:\n  can_book: true\n  can_transfer: true\n  can_quote: false\n  can_collect_address: true\n"
                  f"  after_hours_action: {after_hours_action}\n  emergency_action: transfer\n")
    r = j.client.post(f"/admin/intake/{iid}/publish", data={"yaml": yaml_text})
    assert r.status_code == 200, r.text[:500]
    assert "is live" in r.text and OWNER_EMAIL in r.text and MASTER not in r.text
    j.client.headers.pop("Origin", None)
    j.client.cookies.clear()
    return html.unescape(r.text.split("Temporary password</th><td><code>", 1)[1].split("</code>", 1)[0])


def _owner_ready(j, temp: str) -> TestClient:
    owner = TestClient(j.main.app)
    r = owner.post("/portal/login", data={"email": OWNER_EMAIL, "password": temp, "csrf": _csrf(owner.get("/portal/login").text)},
                   follow_redirects=False)
    assert r.headers["location"] == "/portal/password"
    r = owner.post("/portal/password", data={"current": temp, "new": NEW_PW, "new2": NEW_PW, "csrf": _csrf(owner.get("/portal/password").text)},
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/portal/overview"
    return owner


def _call_row(j, sid):
    conn = sqlite3.connect(j.storage.DB_PATH)
    try:
        return conn.execute("SELECT outcome, outcome_class, needs_attention, transcript_json, summary FROM calls WHERE call_sid = ?", (sid,)).fetchone()
    finally:
        conn.close()


def _incoming(j, sid):
    r = j.signed(f"/voice/incoming?client_id={CID}", {"CallSid": sid, "From": CALLER, "To": "+15555550100"})
    assert r.status_code == 200 and "Dulles Comfort Heating" in r.text and "AI receptionist" in r.text
    return r


def _say(j, sid, words):
    return j.signed(f"/voice/gather?client_id={CID}&retry=0", {"CallSid": sid, "From": CALLER, "SpeechResult": words})


def _booking_call(j, sid="CA_JOURNEY_BOOK"):
    """Monday 09:00 (frozen clock): the caller books a repair visit for Tuesday 10:00, inside hours."""
    _incoming(j, sid)
    j.queue(
        FakeResponse([FakeToolUseBlock("t1", "check_availability", {"date": "2026-01-06", "preferred_time": "10:00", "service": "Repair visit"})], "tool_use"),
        FakeResponse([FakeToolUseBlock("t2", "book_appointment", {"caller_name": "Pat Rivera", "caller_phone": CALLER, "service": "Repair visit",
                                                                    "date": "2026-01-06", "time": "10:00"})], "tool_use"),
        FakeResponse([FakeTextBlock("You're booked for a repair visit Tuesday at ten a.m. Anything else?")], "end_turn"),
    )
    r = _say(j, sid, "My AC stopped blowing cold. This is Pat Rivera, can someone come Tuesday around ten? This number is fine.")
    assert r.status_code == 200 and "booked" in r.text and "<Gather" in r.text
    j.queue(FakeResponse([FakeToolUseBlock("t3", "end_call", {"closing_message": "Thanks Pat, see you Tuesday. Goodbye."})], "tool_use"),
            FakeResponse([FakeTextBlock("Pat Rivera booked a repair visit for Tuesday 10 AM; AC not cooling.")], "end_turn"))   # the after-call summary
    r = _say(j, sid, "No, that's all. Thanks!")
    assert r.status_code == 200 and "<Hangup" in r.text


# ------------------------------------------------------------------------------------------------ 1 + 2: onboarding and login
def test_1_publish_makes_the_client_live_and_creates_its_owner_account(journey):
    j = journey
    temp = _publish(j)
    assert CID in j.cfg.list_client_ids()
    live = j.cfg.load_client_config(CID)
    assert live.owner_email == OWNER_EMAIL and live.escalation_phone == OWNER_CELL and live.vertical == "HVAC"
    assert [s.name for s in live.services] == ["Repair visit", "Seasonal tune-up", "New system estimate"]
    assert live.policy.can_collect_address and live.policy.emergency_action == "transfer" and not live.policy.can_quote
    with j.storage._conn() as conn:
        assert conn.execute("SELECT client_id, must_change FROM owner_users WHERE email = ?", (OWNER_EMAIL,)).fetchone() == (CID, 1)
    assert len(temp) >= 12


def test_2_first_login_forces_a_change_then_new_password_works_after_logout(journey):
    j = journey
    temp = _publish(j)
    owner = TestClient(j.main.app)
    page = owner.get("/portal/login")
    r = owner.post("/portal/login", data={"email": OWNER_EMAIL, "password": temp, "csrf": _csrf(page.text)}, follow_redirects=False)
    assert r.headers["location"] == "/portal/password"
    assert owner.get("/portal/overview", follow_redirects=False).headers["location"] == "/portal/password"
    r = owner.post("/portal/password", data={"current": temp, "new": NEW_PW, "new2": NEW_PW, "csrf": _csrf(owner.get("/portal/password").text)},
                   follow_redirects=False)
    assert r.headers["location"] == "/portal/overview"
    assert owner.get("/portal/overview").status_code == 200
    r = owner.post("/portal/logout", data={"csrf": _csrf(owner.get("/portal/overview").text)}, follow_redirects=False)
    assert r.headers["location"] == "/portal/login"
    assert owner.get("/portal/overview", follow_redirects=False).headers["location"] == "/portal/login"
    # the temporary password is dead; the new one goes straight to the overview
    bad = owner.post("/portal/login", data={"email": OWNER_EMAIL, "password": temp, "csrf": _csrf(owner.get("/portal/login").text)}, follow_redirects=False)
    assert bad.status_code == 200 and "did not match" in bad.text
    ok = owner.post("/portal/login", data={"email": OWNER_EMAIL, "password": NEW_PW, "csrf": _csrf(owner.get("/portal/login").text)}, follow_redirects=False)
    assert ok.headers["location"] == "/portal/overview"


def test_2b_operator_can_recover_a_lost_temporary_password_from_the_admin_page(journey):
    """The owner lost the temporary password. The operator resets it from the (phone-friendly) admin page: production runs
    from a container image, so a local script would write to the wrong database."""
    j = journey
    temp = _publish(j)
    _admin_login(j.client)
    page = j.client.get(f"/admin/client/{CID}/owner")
    assert page.status_code == 200 and OWNER_EMAIL in page.text and "must change" in page.text.lower()
    assert MASTER not in page.text and temp not in page.text
    r = j.client.post(f"/admin/client/{CID}/owner", data={"action": "reset", "email": OWNER_EMAIL})
    assert r.status_code == 200 and "Temporary password" in r.text
    recovery = html.unescape(r.text.split("Temporary password</th><td><code>", 1)[1].split("</code>", 1)[0])
    assert recovery != temp and len(recovery) >= 12
    raw = open(j.storage.DB_PATH, "rb").read()
    assert recovery.encode() not in raw
    j.client.headers.pop("Origin", None)
    j.client.cookies.clear()
    owner = TestClient(j.main.app)
    old = owner.post("/portal/login", data={"email": OWNER_EMAIL, "password": temp, "csrf": _csrf(owner.get("/portal/login").text)}, follow_redirects=False)
    assert old.status_code == 200 and "did not match" in old.text
    _owner_ready(j, recovery)


def test_2c_admin_owner_page_is_operator_only_and_bound_to_the_client(journey):
    j = journey
    _publish(j)
    from app import owner_auth
    owner_auth.create_user("demo_hvac", "owner@example.com")
    assert j.client.get(f"/admin/client/{CID}/owner").status_code == 403                       # not signed in
    assert j.client.post(f"/admin/client/{CID}/owner", data={"action": "reset", "email": OWNER_EMAIL}).status_code == 403
    _admin_login(j.client)
    # another client's owner cannot be reset through this client's page
    r = j.client.post(f"/admin/client/{CID}/owner", data={"action": "reset", "email": "owner@example.com"})
    assert r.status_code == 404 and "Temporary password" not in r.text
    # cross-site POST with the admin cookie is refused before it reaches the handler
    r = j.client.post(f"/admin/client/{CID}/owner", data={"action": "reset", "email": OWNER_EMAIL}, headers={"Origin": "https://attacker.example"})
    assert r.status_code == 403
    assert j.client.get("/admin/client/no_such_client/owner").status_code == 404


def test_2d_admin_can_create_an_owner_login_for_a_client_that_has_none(journey):
    j = journey
    _admin_login(j.client)
    r = j.client.post("/admin/client/demo_hvac/owner", data={"action": "create", "email": "owner@example.com"})
    assert r.status_code == 200 and "Temporary password" in r.text and "owner@example.com" in r.text
    with j.storage._conn() as conn:
        assert conn.execute("SELECT client_id, must_change FROM owner_users WHERE email = 'owner@example.com'").fetchone() == ("demo_hvac", 1)
    again = j.client.post("/admin/client/demo_hvac/owner", data={"action": "create", "email": "owner@example.com"})
    assert again.status_code == 422 and "Temporary password" not in again.text


# ------------------------------------------------------------------------------------------------ 3: a booking call
def test_3_signed_booking_call_stores_the_booking_and_emails_the_owner(journey):
    j = journey
    _publish(j)
    j.sent.clear()
    # an unsigned or wrongly signed webhook is refused
    assert j.client.post(f"/voice/incoming?client_id={CID}", data={"CallSid": "CA_FORGED", "From": CALLER}).status_code == 403
    _booking_call(j)
    with j.storage._conn() as conn:
        rows = conn.execute("SELECT client_id, caller_name, caller_phone, service, slot_start, slot_end, status FROM bookings").fetchall()
    assert rows == [(CID, "Pat Rivera", CALLER, "Repair visit", "2026-01-06T10:00", "2026-01-06T11:00", "confirmed")]
    outcome, cls, attention, transcript, summary = _call_row(j, "CA_JOURNEY_BOOK")
    assert outcome == "completed" and cls == "BOOKED" and attention == 0
    assert "Pat Rivera" in transcript and "booked" in transcript and summary.startswith("Pat Rivera booked")
    booking_mail = [m for m in j.sent if "New booking" in m["Subject"]]
    assert len(booking_mail) == 1
    msg = booking_mail[0]
    assert msg["To"] == OWNER_EMAIL and msg["Subject"].startswith("[Dulles Comfort Heating & Cooling]")
    assert "Call Kettle" in msg["From"]
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert "Pat Rivera" in body and "Repair visit" in body and "Tuesday Jan 6 at 10:00 AM" in body
    ics = [p for p in msg.iter_attachments() if p.get_content_type() == "text/calendar"]
    assert ics and "DTSTART:20260106T150000Z" in ics[0].get_content()            # 10:00 EST
    with j.storage._conn() as conn:
        assert conn.execute("SELECT status, kind FROM email_log WHERE to_addr = ?", (OWNER_EMAIL,)).fetchall() == [("accepted", "notification")]


def test_3b_mail_provider_selection_matches_production_secret_names(monkeypatch, app_client):
    """Production sets SMTP_HOST/SMTP_USER/SMTP_PASSWORD/SMTP_PORT/SMTP_FROM and no EMAIL_PROVIDER: that must resolve to smtp."""
    from app import mailer

    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.setenv(name, "x")
    assert mailer.provider_name() == "smtp" and mailer.configured()
    monkeypatch.delenv("SMTP_PASSWORD")
    assert mailer.provider_name() is None and not mailer.configured()
    assert mailer.send("owner@example.com", "s", "b").error == "email is not configured"


# ------------------------------------------------------------------------------------------------ 4: emergency, human, after hours
def test_4a_gas_smell_gets_911_advice_rings_the_owner_and_needs_attention(journey):
    j = journey
    _publish(j)
    j.sent.clear()
    _incoming(j, "CA_JOURNEY_GAS")
    r = _say(j, "CA_JOURNEY_GAS", "I smell gas in the basement by the furnace")
    assert "call 911" in r.text and f"<Dial" in r.text and OWNER_CELL in r.text
    assert j.fake.messages.calls == []                                       # the model is never consulted on an emergency
    j.queue(FakeResponse([FakeTextBlock("Caller reported a gas smell near the furnace; told to call 911.")], "end_turn"))
    r = j.signed(f"/voice/transfer-result?client_id={CID}", {"CallSid": "CA_JOURNEY_GAS", "From": CALLER, "DialCallStatus": "completed", "DialCallDuration": "40"})
    assert r.status_code == 200
    outcome, cls, attention, _t, _s = _call_row(j, "CA_JOURNEY_GAS")
    assert (outcome, cls, attention) == ("transferred", "EMERGENCY_ESCALATED", 1)
    assert any("possible_emergency" in m["Subject"] and m["To"] == OWNER_EMAIL for m in j.sent)


def test_4b_caller_wants_a_human_owner_misses_it_message_taken_and_flagged(journey):
    j = journey
    _publish(j)
    _incoming(j, "CA_JOURNEY_HUMAN")
    r = _say(j, "CA_JOURNEY_HUMAN", "Can I talk to a real person please")
    assert "<Dial" in r.text and OWNER_CELL in r.text and "transfer-result" in r.text
    r = j.signed(f"/voice/transfer-result?client_id={CID}", {"CallSid": "CA_JOURNEY_HUMAN", "From": CALLER, "DialCallStatus": "no-answer"})
    assert "nobody was able to pick up" in r.text and "<Gather" in r.text
    j.queue(
        FakeResponse([FakeToolUseBlock("e1", "escalate_to_human", {"reason": "callback_requested", "caller_name": "Sam Ortiz", "caller_phone": CALLER,
                                                                     "summary": "Wants to talk about a heat pump replacement."})], "tool_use"),
        FakeResponse([FakeToolUseBlock("e2", "end_call", {"closing_message": "Thanks Sam, someone will call you back soon. Goodbye."})], "tool_use"),
        FakeResponse([FakeTextBlock("Sam Ortiz wants a callback about a heat pump replacement.")], "end_turn"),
    )
    r = _say(j, "CA_JOURNEY_HUMAN", "Sam Ortiz, this number is fine")
    assert "<Hangup" in r.text
    _o, cls, attention, _t, _s = _call_row(j, "CA_JOURNEY_HUMAN")
    assert (cls, attention) == ("CALLBACK_REQUESTED", 1)


def test_4c_after_hours_message_policy_takes_a_message_and_never_books(journey, monkeypatch):
    j = journey
    _publish(j, after_hours_action="message")
    monkeypatch.setattr(j.tools, "_local_now", lambda config: datetime(2026, 1, 5, 20, 30))      # Monday 8:30 pm, closed
    _incoming(j, "CA_JOURNEY_NIGHT")
    j.queue(
        FakeResponse([FakeToolUseBlock("n1", "book_appointment", {"caller_name": "Lee Chen", "caller_phone": CALLER, "service": "Seasonal tune-up",
                                                                    "date": "2026-01-06", "time": "09:00"})], "tool_use"),
        FakeResponse([FakeToolUseBlock("n2", "escalate_to_human", {"reason": "after_hours_message", "caller_name": "Lee Chen", "caller_phone": CALLER,
                                                                     "summary": "Wants a tune-up this week."}),
                      FakeToolUseBlock("n3", "end_call", {"closing_message": "We're closed now; the team will call you tomorrow. Goodbye."})], "tool_use"),
        FakeResponse([FakeTextBlock("Lee Chen wants a tune-up; message taken after hours.")], "end_turn"),
    )
    r = _say(j, "CA_JOURNEY_NIGHT", "Hi, this is Lee Chen, I'd like to book a tune-up")
    assert "<Hangup" in r.text
    with j.storage._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 0
    _o, cls, attention, _t, _s = _call_row(j, "CA_JOURNEY_NIGHT")
    assert (cls, attention) == ("AFTER_HOURS_MESSAGE", 1)


def test_4d_open_check_agrees_with_the_booking_grid_at_closing_time(journey, monkeypatch):
    """At exactly 17:00 the shop is closed: the after-hours policy and the booking grid must agree (closing time is exclusive)."""
    j = journey
    _publish(j, after_hours_action="message")
    config = j.cfg.load_client_config(CID)
    monkeypatch.setattr(j.tools, "_local_now", lambda c: datetime(2026, 1, 5, 17, 0))
    assert j.tools.is_open_now(config) is False
    assert j.agent.is_open_now(config) is False
    monkeypatch.setattr(j.tools, "_local_now", lambda c: datetime(2026, 1, 5, 16, 59))
    assert j.agent.is_open_now(config) is True
    assert "CLOSED" in j.agent._open_status_line(config, datetime(2026, 1, 5, 17, 0))


# ------------------------------------------------------------------------------------------------ 5: portal and isolation
def test_5_portal_shows_the_call_transcript_booking_attention_and_exports_and_isolates(journey):
    j = journey
    temp = _publish(j)
    _booking_call(j)
    _incoming(j, "CA_JOURNEY_GAS")
    _say(j, "CA_JOURNEY_GAS", "There's a carbon monoxide alarm going off")
    j.queue(FakeResponse([FakeTextBlock("CO alarm reported; told to call 911.")], "end_turn"))
    j.signed(f"/voice/transfer-result?client_id={CID}", {"CallSid": "CA_JOURNEY_GAS", "From": CALLER, "DialCallStatus": "completed", "DialCallDuration": "30"})

    owner = _owner_ready(j, temp)
    overview = owner.get("/portal/overview").text
    assert "Dulles Comfort Heating" in overview and "Needs your attention (1)" in overview and "CO alarm reported" in overview
    calls = owner.get("/portal/calls").text
    assert "2 call(s) match" in calls and "Pat Rivera booked" in calls and "Read the full conversation" in calls and "AC stopped blowing cold" in calls
    cal = owner.get("/portal/calendar?month=2026-01").text
    assert "Repair visit" in cal and "Pat Rivera" in cal and "Tue Jan 06, 10:00 AM".replace(" 0", " ") in cal
    calls_csv = owner.get("/portal/export/calls.csv")
    assert calls_csv.status_code == 200 and calls_csv.headers["content-type"].startswith("text/csv")
    assert "Booked" in calls_csv.text and "Possible emergency" in calls_csv.text
    bookings_csv = owner.get("/portal/export/bookings.csv").text
    assert "2026-01-06T10:00,2026-01-06T11:00,Repair visit,Pat Rivera" in bookings_csv

    # mark handled clears the queue and is recorded
    r = owner.post("/portal/handled", data={"call": "CA_JOURNEY_GAS", "csrf": _csrf(overview)}, follow_redirects=False)
    assert r.status_code == 303
    assert "Needs your attention (0)" in owner.get("/portal/overview").text
    assert "yes,yes" in owner.get("/portal/export/calls.csv").text                 # needed attention, marked handled

    # a second customer's owner sees none of it, on any page or export, and cannot mark it handled
    from app import owner_auth
    other_temp = owner_auth.create_user("demo_hvac", "owner@example.com")
    other = TestClient(j.main.app)
    r = other.post("/portal/login", data={"email": "owner@example.com", "password": other_temp, "csrf": _csrf(other.get("/portal/login").text)}, follow_redirects=False)
    other.post("/portal/password", data={"current": other_temp, "new": NEW_PW, "new2": NEW_PW, "csrf": _csrf(other.get("/portal/password").text)})
    for path in ("/portal/overview", "/portal/calls", "/portal/calendar?month=2026-01", "/portal/settings", "/portal/export/calls.csv", "/portal/export/bookings.csv"):
        text = other.get(path).text
        for secret in ("Dulles Comfort", "Pat Rivera", "CO alarm", CALLER, "+15555550100", "AC stopped"):
            assert secret not in text, (path, secret)
    with j.storage._conn() as conn:
        conn.execute("UPDATE calls SET attention_resolved_at = NULL WHERE call_sid = 'CA_JOURNEY_GAS'")
    other.post("/portal/handled", data={"call": "CA_JOURNEY_GAS", "csrf": _csrf(other.get("/portal/overview").text)})
    with j.storage._conn() as conn:
        assert conn.execute("SELECT attention_resolved_at FROM calls WHERE call_sid = 'CA_JOURNEY_GAS'").fetchone()[0] is None


# ------------------------------------------------------------------------------------------------ 6: weekly recap
def test_6_weekly_recap_renders_with_the_new_calls_and_points_to_the_portal(journey, capsys):
    j = journey
    _publish(j)
    _booking_call(j)
    _incoming(j, "CA_JOURNEY_GAS")
    _say(j, "CA_JOURNEY_GAS", "I smell gas")
    j.queue(FakeResponse([FakeTextBlock("Gas smell reported.")], "end_turn"))
    j.signed(f"/voice/transfer-result?client_id={CID}", {"CallSid": "CA_JOURNEY_GAS", "From": CALLER, "DialCallStatus": "completed", "DialCallDuration": "30"})
    import importlib
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    preview = importlib.import_module("digest_preview")
    next_week = (datetime.now(timezone.utc) - timedelta(hours=5) + timedelta(days=7)).strftime("%Y-%m-%d")
    assert preview.main([CID, "--as-of", next_week]) == 0
    out = capsys.readouterr().out
    assert "Subject: [Dulles Comfort Heating & Cooling]" in out and "2 calls answered" in out and "1 booked" in out
    assert "Booked" in out and "Possible emergency" in out
    assert "/portal/login" in out                                             # the recap sends the owner to their portal
    assert "$" not in out                                                     # no price, no invented revenue


# ------------------------------------------------------------------------------------------------ 7: forwarding activation
def test_7_activation_page_and_forwarding_check_guidance(journey, monkeypatch):
    j = journey
    _publish(j)
    from app import provisioning
    monkeypatch.setattr(provisioning, "numbers_for", lambda client_id, tw: ["+15555550100"] if client_id == CID else [])
    page = j.client.get(f"/activate/{CID}?key={j.main.report_key_for(CID)}")
    assert page.status_code == 200 and "+15555550100" in page.text and "*+1555555010023" in page.text and "*73" in page.text
    assert "different phone" in page.text and "/portal/login" in page.text and MASTER not in page.text
    assert j.client.get(f"/activate/{CID}?key={j.main.report_key_for('demo_hvac')}").status_code == 403
    runbook = (Path(__file__).resolve().parents[2] / "docs" / "HVAC_ONBOARDING_RUNBOOK.md").read_text(encoding="utf-8")
    assert "verify_forwarding.py" in runbook and "--owner-permission" in runbook
    script = (Path(__file__).resolve().parent.parent / "scripts" / "verify_forwarding.py").read_text(encoding="utf-8")
    assert "Refusing to run" in script and "--owner-permission" in script

