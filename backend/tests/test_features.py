"""Behavior added when the product was hardened for real customers: booking
rules enforced server-side, honest SMS handling, multi-channel notifications,
call summaries, per-client dashboard keys, and a transfer that can't dead-end."""
import json
import sqlite3

import pytest

from tests.test_agent import FakeAnthropicClient, FakeResponse, FakeTextBlock


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """Point storage at a fresh file for tests that use the storage module
    directly (the app_client fixture makes its own)."""
    import importlib

    monkeypatch.setenv("CALLKETTLE_DB_PATH", str(tmp_path / "feat.db"))
    from app import storage

    importlib.reload(storage)
    storage.init_db()
    yield storage


def _dental():
    from app.config import load_client_config

    return load_client_config("demo_dental")


# ---------------------------------------------------------------- booking rules

def test_booking_outside_bookable_hours_is_rejected():
    from app import tools

    result = tools.book_appointment(
        call_sid=None, config=_dental(), caller_name="A", caller_phone="+15555550100",
        service="Routine cleaning", date="2026-01-12", time="03:00",
    )
    assert result["success"] is False
    assert "check_availability" in result["error"]


def test_booking_off_the_slot_grid_is_rejected_so_slots_cannot_overlap():
    from app import tools

    result = tools.book_appointment(
        call_sid=None, config=_dental(), caller_name="A", caller_phone="+15555550100",
        service="Routine cleaning", date="2026-01-12", time="09:15",
    )
    assert result["success"] is False


def test_booking_in_the_past_is_rejected():
    from app import tools

    result = tools.book_appointment(
        call_sid=None, config=_dental(), caller_name="A", caller_phone="+15555550100",
        service="Routine cleaning", date="2026-01-05", time="09:00",  # frozen 'now' is 09:00
    )
    assert result["success"] is False
    assert "passed" in result["error"] or "soon" in result["error"]


def test_availability_hides_slots_that_already_passed_today():
    from app import tools

    slots = tools.check_availability(config=_dental(), date="2026-01-05")["slots"]  # frozen now = 09:00
    assert slots and slots[0] >= "09:30"  # 30-minute lead time


def test_booking_hours_can_differ_from_business_hours():
    from app import tools
    from app.config import load_client_config

    config = load_client_config("sample_homecare")  # open 24/7, schedules Mon-Fri 9-5
    assert tools.check_availability(config=config, date="2026-01-10")["slots"] == []  # Saturday
    slots = tools.check_availability(config=config, date="2026-01-12", preferred_time="03:00")["slots"]
    assert slots and all("09:00" <= s < "17:00" for s in slots)


def test_result_reports_no_confirmation_text_when_sms_is_off():
    from app import tools

    result = tools.book_appointment(
        call_sid=None, config=_dental(), caller_name="A", caller_phone="+15555550100",
        service="Routine cleaning", date="2026-01-12", time="10:00",
    )
    assert result["success"] is True
    assert result["confirmation_text_sent"] is False  # the AI must not promise a text


def test_sms_is_not_attempted_at_all_while_disabled(monkeypatch):
    import app.twilio_utils as tw
    from app import tools

    calls = []
    monkeypatch.setattr(tw, "send_sms", lambda *, to, body: calls.append(to) or True)
    tools.book_appointment(
        call_sid=None, config=_dental(), caller_name="A", caller_phone="+15555550100",
        service="Routine cleaning", date="2026-01-12", time="10:30",
    )
    assert calls == []


# ---------------------------------------------------------------- notifications

class _InlineThread:
    def __init__(self, target, args=(), daemon=None):
        self._t, self._a = target, args

    def start(self):
        self._t(*self._a)


def test_owner_notifications_fan_out_to_email_and_ntfy_without_sms(monkeypatch):
    from app import notify
    from app.config import load_client_config

    sent = {"email": [], "ntfy": []}
    monkeypatch.setattr(notify, "_send_email", lambda to, subject, body, ics=None: sent["email"].append((to, subject, body)))
    monkeypatch.setattr(notify, "_send_ntfy", lambda topic, title, body: sent["ntfy"].append((topic, title, body)))
    monkeypatch.setattr(notify.threading, "Thread", _InlineThread)

    config = load_client_config("callkettle_sales")  # has owner_email + ntfy_topic
    notify.notify_owner(config, title="New booking", body="Jane - Tue 10am")

    assert sent["email"][0][0] == config.owner_email
    assert sent["ntfy"][0][0] == config.ntfy_topic
    assert "Jane" in sent["ntfy"][0][2]


def test_booking_email_carries_a_calendar_invite_in_utc(monkeypatch):
    from datetime import datetime

    from app import notify, tools
    from app.config import load_client_config

    captured = {}
    monkeypatch.setattr(notify, "_send_email", lambda to, subject, body, ics=None: captured.update(to=to, ics=ics))
    monkeypatch.setattr(notify.threading, "Thread", _InlineThread)
    monkeypatch.setenv("SMTP_FROM", "bookings@example.com")

    config = load_client_config("callkettle_sales").model_copy(update={"ntfy_topic": None})
    result = tools.book_appointment(
        call_sid=None, config=config, caller_name="Jane Roe", caller_phone="+15555550100",
        service="Call Kettle Consultation", date="2026-01-12", time="10:00",
    )
    assert result["success"]
    ics = captured["ics"]
    assert captured["to"] == "samiali38183@gmail.com"
    assert "BEGIN:VCALENDAR" in ics and "METHOD:REQUEST" in ics
    assert "DTSTART:20260112T150000Z" in ics  # 10:00 America/New_York in January = 15:00 UTC
    assert "DTEND:20260112T153000Z" in ics
    assert "Jane Roe" in ics and "\r\n" in ics


def test_ics_escapes_commas_and_semicolons_so_names_cannot_break_the_invite():
    from datetime import datetime

    from app import notify
    from app.config import load_client_config

    ics = notify.build_ics(
        config=load_client_config("demo_dental"), caller_name="Roe; Jane, Jr.", caller_phone="+1555",
        service="Cleaning", start=datetime(2026, 7, 6, 9, 0), end=datetime(2026, 7, 6, 9, 30), organizer="owner@example.com",
    )
    assert "Roe\\; Jane\\, Jr." in ics
    assert "DTSTART:20260706T130000Z" in ics  # July is EDT: 09:00 -> 13:00 UTC


def test_smtp_send_attaches_the_invite_and_uses_tls(monkeypatch):
    from app import notify

    log = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            log["host"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            log["tls"] = True

        def login(self, user, pw):
            log["login"] = user

        def send_message(self, msg):
            log["msg"] = msg

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USER", "owner@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    notify._send_email("owner@example.com", "Subj", "Body", ics="BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    assert log["tls"] and log["login"] == "owner@example.com" and log["host"] == ("smtp.gmail.com", 587)
    types = [part.get_content_type() for part in log["msg"].iter_attachments()]
    assert types == ["text/calendar"]


def test_a_failing_channel_never_raises_into_the_call(monkeypatch):
    from app import notify

    def _boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(notify.httpx, "post", _boom)
    monkeypatch.delenv("CALLKETTLE_DISABLE_PUSH")
    notify._send_ntfy("topic", "t", "b")  # must swallow


def test_email_is_skipped_quietly_when_smtp_is_not_configured(monkeypatch):
    from app import notify

    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    notify._send_email("owner@example.com", "s", "b")  # no exception, nothing to send with


# ---------------------------------------------------------------- agent prompt & redaction

def test_prompt_offers_caller_id_and_includes_booking_hours_and_privacy_rules():
    from app import agent

    prompt = agent.build_system_prompt(_dental(), caller_number="+15555550100")
    assert "+15555550100" in prompt
    assert "calling from" in prompt
    assert "Appointments can only be booked" in prompt
    assert "diagnoses" in prompt  # never solicit medical details
    assert "confirmation_text_sent" in prompt


def test_prompt_puts_911_ahead_of_everything_for_emergencies():
    from app import agent

    prompt = agent.build_system_prompt(_dental())
    assert "call 911" in prompt and "gas smell" in prompt
    assert prompt.index("SAFETY FIRST") < prompt.index("After book_appointment succeeds")


def test_prompt_without_caller_id_asks_for_a_number():
    from app import agent

    assert "Caller ID is not available" in agent.build_system_prompt(_dental(), caller_number="anonymous")


def test_demo_line_prompt_carries_its_lead_capture_instructions():
    from app import agent
    from app.config import load_client_config

    prompt = agent.build_system_prompt(load_client_config("demo_riverside"))
    assert "demo_lead" in prompt
    assert "$•••" not in prompt and "$•••" not in prompt and "do not state or guess any amount" in prompt      # the public demo never advertises the price


def test_transcript_text_is_not_stored_for_no_record_clients(monkeypatch, _fresh_db):
    from app import agent
    from app.config import load_client_config

    storage = _fresh_db
    config = load_client_config("sample_homecare")
    fake = FakeAnthropicClient([FakeResponse(content=[FakeTextBlock(text="Sure, can I get your name?")], stop_reason="end_turn")])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)

    storage.log_call_start("CA_PRIV", config.client_id, "+15555550100")
    session = agent.start_session("CA_PRIV", config)
    agent.run_turn(session, "my mother has dementia and needs help")

    stored = json.loads(storage.get_call("CA_PRIV")["transcript_json"])
    assert stored and all(t["text"] == storage.NOT_RECORDED for t in stored)
    assert "dementia" not in json.dumps(stored)


# ---------------------------------------------------------------- storage

def test_status_callback_does_not_overwrite_a_specific_outcome(_fresh_db):
    storage = _fresh_db
    storage.log_call_start("CA1", "demo_dental", "+1")
    storage.log_call_end("CA1", "transferred")
    storage.log_call_end_if_open("CA1", "caller_hung_up")
    assert storage.get_call("CA1")["outcome"] == "transferred"


def test_status_callback_fills_in_outcome_for_a_call_the_app_never_closed(_fresh_db):
    storage = _fresh_db
    storage.log_call_start("CA2", "demo_dental", "+1")
    storage.log_call_end_if_open("CA2", "caller_hung_up")
    assert storage.get_call("CA2")["outcome"] == "caller_hung_up"


def test_init_db_adds_summary_column_to_an_existing_database(tmp_path, monkeypatch):
    import importlib

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE calls (call_sid TEXT PRIMARY KEY, client_id TEXT NOT NULL, from_number TEXT, "
        "started_at TEXT NOT NULL, ended_at TEXT, turn_count INTEGER NOT NULL DEFAULT 0, outcome TEXT, "
        "transcript_json TEXT NOT NULL DEFAULT '[]')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("CALLKETTLE_DB_PATH", str(path))
    from app import storage

    importlib.reload(storage)
    storage.init_db()
    storage.init_db()  # idempotent
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(calls)")}
    assert "summary" in cols


# ---------------------------------------------------------------- call summaries

def _seed_call(storage, sid, client_id, turns):
    storage.log_call_start(sid, client_id, "+15555550100")
    for role, text in turns:
        storage.log_turn(sid, role, text)


def test_summary_is_written_from_the_transcript(monkeypatch, _fresh_db):
    from app import agent, summary

    storage = _fresh_db
    _seed_call(storage, "CA_S1", "demo_dental", [("ai", "Hello"), ("caller", "I need a cleaning"), ("ai", "Booked Tuesday")])
    fake = FakeAnthropicClient([FakeResponse(content=[FakeTextBlock(text="Caller booked a cleaning for Tuesday.")], stop_reason="end_turn")])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)

    summary.summarize_call("CA_S1")
    assert storage.get_call("CA_S1")["summary"] == "Caller booked a cleaning for Tuesday."
    summary.summarize_call("CA_S1")  # already summarized: no second API call
    assert len(fake.messages.calls) == 1


def test_summary_is_skipped_for_clients_that_do_not_record(monkeypatch, _fresh_db):
    from app import agent, summary

    storage = _fresh_db
    _seed_call(storage, "CA_S2", "sample_homecare", [("caller", "hello")])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: FakeAnthropicClient([]))  # any call would assert
    summary.summarize_call("CA_S2")
    assert storage.get_call("CA_S2")["summary"] is None


def test_silent_call_gets_a_summary_without_an_api_call(monkeypatch, _fresh_db):
    from app import agent, summary

    storage = _fresh_db
    _seed_call(storage, "CA_S3", "demo_dental", [("ai", "Hello")])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: FakeAnthropicClient([]))
    summary.summarize_call("CA_S3")
    assert "without saying anything" in storage.get_call("CA_S3")["summary"]


def test_summary_failure_is_swallowed(monkeypatch, _fresh_db):
    from app import agent, summary

    def _boom():
        raise RuntimeError("api down")

    storage = _fresh_db
    _seed_call(storage, "CA_S4", "demo_dental", [("caller", "hi")])
    monkeypatch.setattr(agent, "_anthropic_client", _boom)
    summary.summarize_call("CA_S4")  # must not raise
    assert storage.get_call("CA_S4")["summary"] is None


# ---------------------------------------------------------------- transfer that can't dead-end

def test_answered_transfer_just_ends(app_client):
    c, main = app_client
    r = c.post("/voice/transfer-result?client_id=demo_dental", data={"CallSid": "CA_T1", "DialCallStatus": "completed"})
    assert r.status_code == 200
    assert "<Say" not in r.text and "<Gather" not in r.text


@pytest.mark.parametrize("status", ["no-answer", "busy", "failed"])
def test_unanswered_transfer_takes_a_message_instead_of_dead_air(app_client, status):
    c, main = app_client
    c.post("/voice/incoming?client_id=demo_dental", data={"CallSid": "CA_T2", "From": "+15557770000"})
    r = c.post(
        "/voice/transfer-result?client_id=demo_dental",
        data={"CallSid": "CA_T2", "From": "+15557770000", "DialCallStatus": status},
    )
    assert r.status_code == 200
    assert "<Gather" in r.text and "nobody was able to pick up" in r.text
    assert "Hangup" not in r.text

    conn = sqlite3.connect(main.storage.DB_PATH)
    reason, phone = conn.execute("SELECT reason, caller_phone FROM escalations WHERE call_sid='CA_T2'").fetchone()
    conn.close()
    assert reason == "transfer_unanswered" and phone == "+15557770000"

    session = main.agent.get_session("CA_T2")
    assert session is not None  # the conversation continues with context
    assert session.messages[0]["role"] == "user" and session.messages[1]["role"] == "assistant"


def test_after_a_failed_transfer_the_ai_is_told_to_take_a_message_and_wrap_up(app_client):
    from app import agent

    c, main = app_client
    c.post("/voice/incoming?client_id=demo_dental", data={"CallSid": "CA_T5", "From": "+15555550100"})
    c.post("/voice/transfer-result?client_id=demo_dental", data={"CallSid": "CA_T5", "From": "+15555550100", "DialCallStatus": "busy"})
    session = main.agent.get_session("CA_T5")
    prompt = agent.build_system_prompt(session.config, session.caller_number, session.session_note)
    assert "RIGHT NOW IN THIS CALL" in prompt and "callback_requested" in prompt and "end_call" in prompt


def test_unanswered_transfer_records_the_outcome(app_client):
    c, main = app_client
    c.post("/voice/incoming?client_id=demo_dental", data={"CallSid": "CA_T3", "From": "+1555"})
    c.post("/voice/transfer-result?client_id=demo_dental", data={"CallSid": "CA_T3", "DialCallStatus": "no-answer"})
    assert main.storage.get_call("CA_T3")["outcome"] == "transfer_unanswered"


def test_status_callback_closes_out_a_call_the_caller_hung_up_on(app_client):
    c, main = app_client
    c.post("/voice/incoming?client_id=demo_dental", data={"CallSid": "CA_T4", "From": "+1555"})
    c.post("/voice/status", data={"CallSid": "CA_T4", "CallStatus": "completed"})
    assert main.storage.get_call("CA_T4")["outcome"] == "caller_hung_up"
    assert main.agent.get_session("CA_T4") is None  # session memory is released


def test_dictation_gets_a_longer_speech_timeout(app_client):
    _, main = app_client
    assert main._speech_timeout_for("What's the best phone number to reach you?") == "3"
    assert main._speech_timeout_for("Is the number you're calling from the best one?") == "auto"
    assert main._speech_timeout_for("Tuesday at ten works. Anything else?") == "auto"


# ---------------------------------------------------------------- dashboard keys

def test_each_client_gets_its_own_key_that_opens_only_that_client(app_client):
    c, main = app_client
    dental_key = main.report_key_for("demo_dental")
    assert c.get("/report/demo_dental", params={"key": dental_key}).status_code == 200
    assert c.get("/report/sample_homecare", params={"key": dental_key}).status_code == 403  # no cross-client peeking


def test_master_key_opens_any_client_and_garbage_opens_none(app_client):
    c, main = app_client
    assert c.get("/report/sample_homecare", params={"key": "master_key_for_tests"}).status_code == 200
    assert c.get("/report/sample_homecare", params={"key": "x"}).status_code == 403
    assert c.get("/report/sample_homecare").status_code == 403


def test_dashboard_shows_summary_transcript_and_escapes_html(app_client):
    c, main = app_client
    storage = main.storage
    storage.log_call_start("CA_R1", "demo_dental", "+15555550100")
    storage.log_turn("CA_R1", "caller", "<script>alert(1)</script> I need a cleaning")
    storage.log_call_end("CA_R1", "completed")
    storage.set_call_summary("CA_R1", "Wants a cleaning next week.")
    page = c.get("/report/demo_dental", params={"key": "master_key_for_tests"}).text
    assert "Wants a cleaning next week." in page
    assert "Full transcript" in page
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "Handled by AI" in page


def test_dashboard_never_shows_transcripts_for_no_record_clients(app_client):
    c, main = app_client
    storage = main.storage
    storage.log_call_start("CA_R2", "sample_homecare", "+15555550100")
    storage.log_turn("CA_R2", "caller", "she has diabetes", store_text=False)
    page = c.get("/report/sample_homecare", params={"key": "master_key_for_tests"}).text
    assert "diabetes" not in page and "Full transcript" not in page
    assert "not recorded for privacy" in page


def test_unanswered_transfer_in_a_spanish_call_stays_in_spanish(app_client):
    c, main = app_client
    c.post("/voice/incoming?client_id=demo_dental", data={"CallSid": "CA_ES1", "From": "+155****0000"})
    main.agent.get_session("CA_ES1").lang = "es"
    r = c.post("/voice/transfer-result?client_id=demo_dental",
               data={"CallSid": "CA_ES1", "From": "+155****0000", "DialCallStatus": "no-answer"})
    assert r.status_code == 200 and "<Gather" in r.text
    assert "nobody was able to pick up" not in r.text and "nadie pudo contestar" in r.text
    assert 'language="es-US"' in r.text
    session = main.agent.get_session("CA_ES1")
    assert session.lang == "es"
    assert "nadie pudo contestar" in session.messages[1]["content"]
    # the English path is unchanged
    c.post("/voice/incoming?client_id=demo_dental", data={"CallSid": "CA_EN1", "From": "+155****0000"})
    r = c.post("/voice/transfer-result?client_id=demo_dental",
               data={"CallSid": "CA_EN1", "From": "+155****0000", "DialCallStatus": "no-answer"})
    assert "nobody was able to pick up" in r.text and main.agent.get_session("CA_EN1").lang == "en"
