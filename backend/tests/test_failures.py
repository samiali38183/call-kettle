"""Failure injection. Whatever breaks (mail, model, database, notifications, calendar), the caller is never left stranded and is
never told something happened that did not. Where a booking really was saved, the owner is not left unaware of it."""
import os
import sqlite3
import tempfile
import time

import pytest

from tests.test_agent import FakeAnthropicClient, FakeResponse, FakeTextBlock, FakeToolUseBlock

CALLER = "+15555550100"
BOOK = {"caller_name": "Pat Lee", "caller_phone": CALLER, "service": "Emergency repair", "date": "2026-01-12", "time": "10:00"}


@pytest.fixture(autouse=True)
def db(monkeypatch):
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


@pytest.fixture(autouse=True)
def alerts(monkeypatch):
    from app import ops

    seen = []
    monkeypatch.setattr(ops, "alert_operator", lambda title, body, **kw: seen.append((title, body)) or True)
    return seen


def _cfg():
    from app.config import load_client_config

    return load_client_config("demo_hvac")


def _session(monkeypatch, responses, sid="CA_F1"):
    from app import agent, storage

    storage.log_call_start(sid, "demo_hvac", CALLER)
    fake = FakeAnthropicClient(responses)
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)
    return agent.start_session(sid, _cfg(), CALLER)


def _book_then_say(text):
    return [
        FakeResponse([FakeToolUseBlock("t1", "book_appointment", dict(BOOK))], "tool_use"),
        FakeResponse([FakeTextBlock(text)], "end_turn"),
    ]


def _rows(db, sql, *a):
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


# ----------------------------------------------------------------------- the model fails

@pytest.mark.parametrize("exc", [RuntimeError("500"), TimeoutError("timed out"), ConnectionError("reset"), OSError("dns")])
def test_model_failure_hands_off_ends_cleanly_and_alerts(monkeypatch, db, alerts, exc):
    from app import agent

    class Boom:
        class messages:
            @staticmethod
            def create(**kw):
                raise exc

    monkeypatch.setattr(agent, "_anthropic_client", lambda: Boom)
    from app import storage

    storage.log_call_start("CA_F2", "demo_hvac", CALLER)
    s = agent.start_session("CA_F2", _cfg(), CALLER)
    reply, ended, transfer = agent.run_turn(s, "I need to book a visit")
    assert ended and "connecting you" in reply.lower()
    assert transfer == _cfg().escalation_phone                       # the promise on the website: if the AI fails, the caller is put through to the owner
    assert "booked" not in reply.lower() and "confirmed" not in reply.lower()
    assert _rows(db, "SELECT reason FROM escalations WHERE call_sid='CA_F2'") == [("agent_error",)]
    assert alerts and "AI model error" in alerts[0][0]


def test_model_dies_after_a_successful_booking_the_booking_stays_and_the_owner_is_still_told(monkeypatch, db):
    from app import agent

    s = _session(monkeypatch, [FakeResponse([FakeToolUseBlock("t1", "book_appointment", dict(BOOK))], "tool_use")], "CA_F3")
    reply, ended, _ = agent.run_turn(s, "book it")       # the queue runs dry on the 2nd model call = a failure
    assert ended
    assert _rows(db, "SELECT COUNT(*) FROM bookings WHERE call_sid='CA_F3'") == [(1,)]
    assert _rows(db, "SELECT COUNT(*) FROM escalations WHERE call_sid='CA_F3'") == [(1,)]     # owner has a record to act on


def test_empty_model_reply_gets_a_spoken_fallback_not_silence(monkeypatch, db):
    from app import agent

    s = _session(monkeypatch, [FakeResponse([], "end_turn")], "CA_F4")
    reply, ended, _ = agent.run_turn(s, "hello")
    assert reply.strip() and not ended


# ----------------------------------------------------------------------- the database fails

def test_database_locked_when_booking_is_never_reported_as_booked(monkeypatch, db):
    from app import agent, storage

    def locked(**kw):
        raise sqlite3.OperationalError("database is locked")

    s = _session(monkeypatch, _book_then_say("You're all set for Monday at ten a.m.!"), "CA_F5")
    monkeypatch.setattr(storage, "create_booking", locked)
    reply, ended, _ = agent.run_turn(s, "book it")
    assert "all set" not in reply.lower() and "wasn't able to complete" in reply
    assert _rows(db, "SELECT COUNT(*) FROM bookings") == [(0,)]
    assert _rows(db, "SELECT reason FROM escalations WHERE call_sid='CA_F5'") == [("blocked_false_confirmation",)]


def test_database_down_for_the_whole_turn_an_emergency_still_gets_911_and_a_transfer(monkeypatch, db):
    from app import agent, storage

    s = _session(monkeypatch, [], "CA_F6")

    def down(*a, **k):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(storage, "log_turn", down)
    monkeypatch.setattr(storage, "log_escalation", down)
    reply, ended, transfer = agent.run_turn(s, "there is gas leaking and I smell gas")
    assert "911" in reply and ended and transfer == _cfg().escalation_phone


def test_database_down_a_human_request_still_transfers(monkeypatch, db):
    from app import agent, storage

    s = _session(monkeypatch, [], "CA_F7")
    monkeypatch.setattr(storage, "log_turn", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    reply, ended, transfer = agent.run_turn(s, "let me talk to a real person")
    assert ended and transfer == _cfg().escalation_phone


# ----------------------------------------------------------------------- notifications fail AFTER the booking is saved

@pytest.mark.parametrize("target", ["notify_owner", "build_ics", "send_sms"])
def test_a_notification_failure_does_not_turn_a_saved_booking_into_a_reported_failure(monkeypatch, db, target):
    from app import notify, tools

    def boom(*a, **k):
        raise RuntimeError(f"{target} exploded")

    if target == "send_sms":
        from app import twilio_utils

        monkeypatch.setattr(notify, "sms_enabled", lambda: True)
        monkeypatch.setattr(twilio_utils, "send_sms", boom)
        monkeypatch.setattr(notify, "notify_owner", lambda *a, **k: None)      # its own SMS path is not what is under test
    else:
        monkeypatch.setattr(notify, target, boom)
    res = tools.book_appointment(call_sid="CA_F8", config=_cfg(), **BOOK)
    assert res["success"] is True and res["booking_id"]
    assert _rows(db, "SELECT COUNT(*) FROM bookings WHERE call_sid='CA_F8'") == [(1,)]


def test_a_failure_to_queue_the_webhook_does_not_break_the_booking(monkeypatch, db):
    from app import tools, webhooks

    monkeypatch.setattr(webhooks, "ensure_table", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    cfg = _cfg().model_copy(update={"webhook_url": "https://example.com/h", "webhook_secret": "x" * 20})
    assert tools.book_appointment(call_sid="CA_F9", config=cfg, **BOOK)["success"] is True


def test_the_calendar_integration_failing_does_not_break_the_booking(monkeypatch, db):
    from app import gcal, tools

    monkeypatch.setattr(gcal, "is_free", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("google down")))
    monkeypatch.setattr(gcal, "create_event_in_background", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("google down")))
    cfg = _cfg().model_copy(update={"google_calendar_id": "owner@example.com"})
    res = tools.book_appointment(call_sid="CA_F10", config=cfg, **BOOK)
    assert res["success"] is True


def test_escalation_survives_a_failing_owner_notification_and_still_leaves_a_record(monkeypatch, db):
    from app import notify, tools

    monkeypatch.setattr(notify, "notify_owner", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("smtp down")))
    out = tools.escalate_to_human(call_sid="CA_F11", config=_cfg(), reason="message", caller_name="Pat", caller_phone=CALLER, summary="call me")
    assert out["escalated"] is True
    assert _rows(db, "SELECT COUNT(*) FROM escalations WHERE call_sid='CA_F11'") == [(1,)]


def test_escalation_still_notifies_when_the_record_cannot_be_written(monkeypatch, db):
    from app import notify, storage, tools

    sent = []
    monkeypatch.setattr(storage, "log_escalation", lambda **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    monkeypatch.setattr(notify, "notify_owner", lambda cfg, **k: sent.append(k))
    tools.escalate_to_human(call_sid="CA_F12", config=_cfg(), reason="message", caller_name="Pat", caller_phone=CALLER, summary="call me")
    assert sent, "the owner must still be told even if the database write failed"


def test_smtp_down_is_swallowed_on_its_thread_and_never_blocks(monkeypatch):
    from app import notify

    import smtplib

    monkeypatch.setenv("SMTP_HOST", "smtp.invalid")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: (_ for _ in ()).throw(OSError("connection refused")))
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **k: (_ for _ in ()).throw(OSError("connection refused")))
    started = time.time()
    notify._send_email("owner@example.com", "subject", "body", None)       # must return, not raise
    assert time.time() - started < 5


# ----------------------------------------------------------------------- a tool raises mid-turn

def test_a_crashing_tool_cannot_become_a_confirmation(monkeypatch, db):
    from app import agent, tools

    monkeypatch.setattr(tools, "book_appointment", lambda **k: (_ for _ in ()).throw(RuntimeError("bug")))
    s = _session(monkeypatch, _book_then_say("Perfect, you're booked for Monday at ten."), "CA_F13")
    reply, ended, _ = agent.run_turn(s, "book it")
    assert "booked" not in reply.lower() and "wasn't able to complete" in reply


def test_a_crashing_cancel_tool_cannot_become_a_cancellation_claim(monkeypatch, db):
    from app import agent, tools

    monkeypatch.setattr(tools, "cancel_appointment", lambda **k: (_ for _ in ()).throw(RuntimeError("bug")))
    s = _session(monkeypatch, [
        FakeResponse([FakeToolUseBlock("t1", "cancel_appointment", {"booking_id": 1})], "tool_use"),
        FakeResponse([FakeTextBlock("Done, your appointment is cancelled.")], "end_turn"),
    ], "CA_F14")
    reply, _, _ = agent.run_turn(s, "cancel it")
    assert "cancelled" not in reply.lower()


# ----------------------------------------------------------------------- the HTTP layer

def test_voice_gather_crash_rings_the_owner_instead_of_an_application_error(monkeypatch, db):
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr("app.twilio_utils._SKIP_SIGNATURE_CHECK", True)
    monkeypatch.setattr(main.agent, "run_turn", lambda s, t: (_ for _ in ()).throw(RuntimeError("boom")))
    with TestClient(main.app, raise_server_exceptions=False) as c:
        c.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_F15", "From": CALLER, "To": "+15550000000"})
        r = c.post("/voice/gather?client_id=demo_hvac&retry=0", data={"CallSid": "CA_F15", "SpeechResult": "hello"})
    assert r.status_code == 200 and "<Dial" in r.text


def test_transfer_that_nobody_answers_takes_a_message_instead_of_hanging_up(monkeypatch, db):
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr("app.twilio_utils._SKIP_SIGNATURE_CHECK", True)
    with TestClient(main.app, raise_server_exceptions=False) as c:
        c.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_F16", "From": CALLER, "To": "+15550000000"})
        r = c.post("/voice/transfer-result?client_id=demo_hvac", data={"CallSid": "CA_F16", "DialCallStatus": "no-answer"})
    assert r.status_code == 200 and "<Gather" in r.text            # asks for a message, not a dead line


def test_ceiling_reached_never_strands_the_caller(monkeypatch, db):
    from fastapi.testclient import TestClient

    from app import main, storage

    monkeypatch.setattr("app.twilio_utils._SKIP_SIGNATURE_CHECK", True)
    from app import costing

    monkeypatch.setattr(costing, "month_usage", lambda cid, now=None: (10**6, 10**6))
    with TestClient(main.app, raise_server_exceptions=False) as c:
        r = c.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_F17", "From": CALLER, "To": "+15550000000"})
    assert r.status_code == 200 and ("<Gather" in r.text or "<Dial" in r.text)       # a message is taken or the owner is rung; never dead air


def test_model_failure_without_live_transfer_takes_a_callback_instead(monkeypatch, db, alerts):
    from app import agent, storage

    class Boom:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("credit balance is too low")

    monkeypatch.setattr(agent, "_anthropic_client", lambda: Boom)
    cfg = _cfg()
    cfg = cfg.model_copy(update={"policy": cfg.policy.model_copy(update={"can_transfer": False})})
    storage.log_call_start("CA_F9", "demo_hvac", CALLER)
    s = agent.start_session("CA_F9", cfg, CALLER)
    reply, ended, transfer = agent.run_turn(s, "I need to book a visit")
    assert ended and transfer is None and "call you back" in reply.lower()
