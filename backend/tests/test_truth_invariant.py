"""HARD INVARIANT: a caller is never told something was booked, rescheduled or cancelled unless the server did it."""
import os
import sqlite3
import tempfile

import pytest

from tests.test_agent import FakeAnthropicClient, FakeResponse, FakeTextBlock, FakeToolUseBlock

CALLER = "+15555550100"


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


def _cfg():
    from app.config import load_client_config

    return load_client_config("demo_hvac")


def _run(monkeypatch, responses, text="please book me", lang="en"):
    from app import agent

    fake = FakeAnthropicClient(responses)
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)
    session = agent.start_session("CA_T" + str(id(responses)), _cfg(), CALLER)
    session.lang = lang
    reply, ended, _ = agent.run_turn(session, text)
    return session, reply, ended


@pytest.mark.parametrize("claim", [
    "You're all set for tomorrow at ten a.m.",
    "Your appointment is confirmed for Tuesday.",
    "I've booked you for two p.m.",
    "Great, you are booked!",
    "All set for Thursday at nine.",
])
def test_a_booking_claim_with_no_booking_is_replaced_and_the_owner_alerted(monkeypatch, db, claim):
    session, reply, ended = _run(monkeypatch, [FakeResponse([FakeTextBlock(claim)], "end_turn")])
    assert "wasn't able to complete" in reply and "confirmed" not in reply.lower() and "booked" not in reply.lower()
    conn = sqlite3.connect(db.DB_PATH)
    assert conn.execute("SELECT reason FROM escalations WHERE call_sid=?", (session.call_sid,)).fetchone()[0] == "blocked_false_confirmation"
    assert conn.execute("SELECT COUNT(*) FROM metrics WHERE name='blocked_false_confirmation'").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize("claim", ["Your appointment is cancelled.", "I've cancelled that for you.", "All done, it's cancelled."])
def test_a_cancel_claim_without_a_real_cancellation_is_blocked(monkeypatch, claim):
    _, reply, _ = _run(monkeypatch, [FakeResponse([FakeTextBlock(claim)], "end_turn")], text="cancel it")
    assert "wasn't able to complete" in reply


@pytest.mark.parametrize("claim", ["Your appointment has been rescheduled to three p.m.", "You're moved to Friday.", "I've moved it to nine."])
def test_a_reschedule_claim_without_a_real_move_is_blocked(monkeypatch, claim):
    _, reply, _ = _run(monkeypatch, [FakeResponse([FakeTextBlock(claim)], "end_turn")], text="move it")
    assert "wasn't able to complete" in reply


def test_spanish_claims_are_blocked_too_and_the_retreat_is_spanish(monkeypatch):
    _, reply, _ = _run(monkeypatch, [FakeResponse([FakeTextBlock("Su cita está confirmada para mañana.")], "end_turn")], lang="es", text="quiero una cita")
    assert reply.startswith("Lo siento") and "confirmada" not in reply


def test_a_claim_after_a_real_booking_is_allowed(monkeypatch, db):
    booking = {"caller_name": "Pat Lee", "caller_phone": CALLER, "service": "Emergency repair", "date": "2026-01-12", "time": "10:00"}
    session, reply, _ = _run(monkeypatch, [
        FakeResponse([FakeToolUseBlock("t1", "book_appointment", booking)], "tool_use"),
        FakeResponse([FakeTextBlock("You're all set for Monday at ten a.m.")], "end_turn"),
    ])
    assert reply == "You're all set for Monday at ten a.m." and "book" in session.done


def test_a_failed_booking_tool_can_never_become_a_confirmation(monkeypatch, db):
    """The tool fails (slot just taken), the model carelessly says you're booked anyway: the caller must not hear it."""
    taken = {"caller_name": "Pat Lee", "caller_phone": CALLER, "service": "Emergency repair", "date": "2026-01-12", "time": "03:00"}  # off the grid
    session, reply, _ = _run(monkeypatch, [
        FakeResponse([FakeToolUseBlock("t1", "book_appointment", taken)], "tool_use"),
        FakeResponse([FakeTextBlock("Perfect, you're booked for three a.m.!")], "end_turn"),
    ])
    assert "book" not in session.done
    assert "wasn't able to complete" in reply
    conn = sqlite3.connect(db.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 0
    conn.close()


def test_a_tool_that_raises_cannot_become_a_confirmation(monkeypatch, db):
    from app import storage

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(storage, "create_booking", locked)
    booking = {"caller_name": "Pat Lee", "caller_phone": CALLER, "service": "Emergency repair", "date": "2026-01-12", "time": "10:00"}
    session, reply, _ = _run(monkeypatch, [
        FakeResponse([FakeToolUseBlock("t1", "book_appointment", booking)], "tool_use"),
        FakeResponse([FakeTextBlock("Your appointment is confirmed for Monday.")], "end_turn"),
    ])
    assert "wasn't able to complete" in reply


def test_end_call_closing_messages_are_checked_too(monkeypatch):
    session, reply, ended = _run(monkeypatch, [
        FakeResponse([FakeToolUseBlock("t1", "end_call", {"closing_message": "You're all set, see you then!"})], "tool_use"),
    ])
    assert ended is True and "wasn't able to complete" in reply


def test_a_lookup_counts_as_evidence_for_stating_an_existing_booking(monkeypatch, db):
    from app import tools

    tools.book_appointment(call_sid="CA_OLD", config=_cfg(), caller_name="Pat Lee", caller_phone=CALLER,
                           service="Emergency repair", date="2026-01-12", time="10:00")
    session, reply, _ = _run(monkeypatch, [
        FakeResponse([FakeToolUseBlock("t1", "find_my_appointments", {})], "tool_use"),
        FakeResponse([FakeTextBlock("Yes, your appointment is confirmed for Monday at ten.")], "end_turn"),
    ], text="do I have an appointment?")
    assert "lookup" in session.done and reply.startswith("Yes, your appointment is confirmed")


@pytest.mark.parametrize("honest", [
    "I can book that for you. What day works?", "The team will follow up to confirm the details.", "Would you like me to cancel it?",
    "I can move it to another time if you like.", "Is the number you're calling from the best one?", "What's your name?",
])
def test_ordinary_sentences_are_never_mistaken_for_claims(honest):
    from app import agent

    assert agent.claimed_actions(honest) == set()
