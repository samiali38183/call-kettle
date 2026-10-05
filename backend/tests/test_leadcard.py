"""One-screen after-call 'lead card' for the owner's text/email alert: who, number, the issue in one line, urgency, where, when.
Built only from what the system recorded; nothing is invented, no price, hostile caller text cannot break the message."""
import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("CALLKETTLE_DB_PATH", path)
    monkeypatch.setenv("CALLKETTLE_SKIP_SIGNATURE_CHECK", "1")
    import importlib

    from app import storage

    importlib.reload(storage)
    storage.init_db()
    yield storage
    os.remove(path)


def _config():
    from app.config import load_client_config

    return load_client_config("demo_hvac")


def test_callback_card_leads_with_name_number_then_issue():
    from app import leadcard

    text = leadcard.callback_card(name="Pat Caller", phone="+15555550100", reason="callback",
                                  summary="Furnace is blowing cold air. Zip 22030, please call after 5.")
    lines = text.splitlines()
    assert lines[0] == "Call: Pat Caller +15555550100"
    assert lines[1].startswith("Issue: Furnace is blowing cold air.")
    assert "Where: ZIP 22030" in text
    assert "URGENT" not in text
    assert "$" not in text


def test_urgent_flag_comes_only_from_the_recorded_emergency_reason():
    from app import leadcard

    urgent = leadcard.callback_card(name="Pat", phone="+15555550100", reason="possible_emergency", summary="Smell of gas at the furnace.")
    assert urgent.splitlines()[0].startswith("URGENT")
    assert "+15555550100" in urgent.splitlines()[1]
    calm = leadcard.callback_card(name="Pat", phone="+15555550100", reason="callback_requested", summary="URGENT!!! emergency!!!")
    assert "URGENT" not in calm.splitlines()[0]          # caller words never set the flag


def test_missing_facts_are_stated_not_invented():
    from app import leadcard

    text = leadcard.callback_card(name=None, phone=None, reason="callback", summary="")
    assert "Call: name not given, number not captured" in text
    assert "Issue: not recorded" in text
    assert "Where: not captured" in text


def test_card_is_one_screen_and_the_number_survives_sms_truncation():
    from app import leadcard

    text = leadcard.callback_card(name="N" * 300, phone="+15555550100", reason="callback", summary="word " * 400)
    assert len(text) <= 260
    assert "+15555550100" in text.splitlines()[0]
    sms = f"[Acme HVAC] Needs a callback (callback): {text}"[:320]
    assert "+15555550100" in sms


def test_hostile_text_cannot_inject_lines_or_control_characters():
    from app import leadcard

    text = leadcard.callback_card(name="Pat\r\nBcc: evil@example.com", phone="+1703\x00555\n0100", reason="callback",
                                  summary="line1\nCall: Fake 911\x07 <script>alert(1)</script>")
    assert [ln for ln in text.splitlines() if ln.startswith("Call:")] == [text.splitlines()[0]]
    assert not any(ord(ch) < 32 and ch != "\n" for ch in text)
    assert "\x00" not in text


def test_booking_card_has_name_number_service_and_slot():
    from app import leadcard

    text = leadcard.booking_card(name="Pat Caller", phone="+15555550100", service="Routine maintenance", when="Monday Jan 12 at 10:00 AM")
    assert text.splitlines()[0] == "Booked: Pat Caller +15555550100"
    assert "Service: Routine maintenance" in text and "When: Monday Jan 12 at 10:00 AM" in text


def test_escalation_alert_uses_the_card_and_flags_urgent_in_the_title(monkeypatch):
    from app import notify, tools

    sent = []
    monkeypatch.setattr(notify, "notify_owner", lambda config, *, title, body, ics=None: sent.append((title, body)))
    tools.escalate_to_human(call_sid="c1", config=_config(), reason="possible_emergency", caller_name="Pat Caller",
                            caller_phone="+15555550100", summary="No heat and a gas smell. Zip 22101.")
    title, body = sent[0]
    assert title.startswith("URGENT") and "possible_emergency" in title
    assert body.splitlines()[0].startswith("URGENT") and body.splitlines()[1].startswith("Call: Pat Caller +15555550100")
    assert "Where: ZIP 22101" in body


def test_booking_alert_uses_the_card(monkeypatch):
    from app import notify, tools

    sent = []
    monkeypatch.setattr(notify, "notify_owner", lambda config, *, title, body, ics=None: sent.append((title, body)))
    result = tools.book_appointment(call_sid="c2", config=_config(), caller_name="Pat Caller", caller_phone="+15555550100",
                                    service="Routine maintenance", date="2026-01-12", time="10:00")
    assert result["success"], result
    title, body = next(x for x in sent if x[0] == "New booking")
    assert body.splitlines()[0] == "Booked: Pat Caller +15555550100"
    assert "Service:" in body and "When:" in body
