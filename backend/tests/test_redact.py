import os
import sqlite3
import tempfile

import pytest

from app.redact import REDACTED, scrub_sensitive


@pytest.mark.parametrize("text", [
    "my card is 4111 1111 1111 1111 expiring next year",
    "4111-1111-1111-1111",
    "+15555550100111111",
    "it's 5555 5555 5555 4444 and the code is 123",
    "amex 3782+155555501005",
    "4 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1",
])
def test_card_numbers_are_scrubbed(text):
    out = scrub_sensitive(text)
    assert REDACTED in out and not any(g in out for g in ("4111", "5555 5555", "3782"))


@pytest.mark.parametrize("text", ["my social is 123-45-6789", "ssn 123 45 6789", "123456789", "social security number is 1 2 3 4 5 6 7 8 9"])
def test_social_security_numbers_are_scrubbed(text):
    out = scrub_sensitive(text)
    assert REDACTED in out and "6789" not in out and "56789" not in out


@pytest.mark.parametrize("text", [
    "call me back at+15555550100", "my number is+15555550100", "+15555550100", "+15555550100", "+15555550100",
    "7 0 3 5 5 5 0 1 9 9", "Tuesday January 12th at 10:30 AM", "booking id 4821", "zip code 20147", "2026-01-12T10:00",
    "I have 3 units, 12 rooms and 100 feet of pipe", "4111 1111 1111 1112",           # fails Luhn: not a card, leave alone
    "my account is+15555550100+155555501002",                                           # too long for a card
])
def test_ordinary_numbers_are_left_alone(text):
    assert scrub_sensitive(text) == text


def test_empty_and_none_like_inputs():
    assert scrub_sensitive("") == "" and scrub_sensitive("no digits at all") == "no digits at all"


def test_stored_transcripts_and_escalation_summaries_never_hold_card_or_ssn_numbers(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("CALLKETTLE_DB_PATH", path)
    import importlib

    from app import storage

    importlib.reload(storage)
    storage.init_db()
    storage.log_call_start("CA_R1", "demo_hvac", "+15555550100")
    storage.log_turn("CA_R1", "caller", "sure, my card is 4111 1111 1111 1111 and my phone is+15555550100")
    storage.log_escalation(call_sid="CA_R1", client_id="demo_hvac", reason="message", caller_phone="+15555550100",
                           summary="Caller read out SSN 123-45-6789 and wants a callback at+15555550100")
    conn = sqlite3.connect(path)
    transcript = conn.execute("SELECT transcript_json FROM calls WHERE call_sid='CA_R1'").fetchone()[0]
    summary = conn.execute("SELECT summary FROM escalations WHERE call_sid='CA_R1'").fetchone()[0]
    conn.close()
    assert "4111" not in transcript and "+15555550100" in transcript          # the phone number is kept: the owner needs it
    assert "123-45-6789" not in summary and "+15555550100" in summary
    os.remove(path)
