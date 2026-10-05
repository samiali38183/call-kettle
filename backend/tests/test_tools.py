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

    return load_client_config("demo_dental")


def test_check_availability_respects_closed_day():
    from app import tools

    result = tools.check_availability(config=_config(), date="2026-01-11")  # a Sunday
    assert result["slots"] == []


def test_check_availability_returns_open_slots():
    from app import tools

    result = tools.check_availability(config=_config(), date="2026-01-12")  # a Monday
    assert "09:00" in result["slots"]


def test_check_availability_invalid_date():
    from app import tools

    result = tools.check_availability(config=_config(), date="not-a-date")
    assert "error" in result


def test_book_appointment_notifies_both_caller_and_owner(monkeypatch):
    """Real bug fix: a booking that only exists in the database is invisible
    to the owner in practice. Confirms both SMS sends happen — one to the
    caller, one to the business's escalation number."""
    from app import tools

    monkeypatch.setenv("SMS_ENABLED", "1")
    sent = []
    # book_appointment imports send_sms lazily from app.twilio_utils inside
    # the function body, so that's where it must be patched.
    import app.twilio_utils as twilio_utils_module

    monkeypatch.setattr(twilio_utils_module, "send_sms", lambda *, to, body: sent.append((to, body)) or True)

    config = _config()
    booking = tools.book_appointment(
        call_sid="CA1", config=config, caller_name="Sami", caller_phone="+15555550100",
        service="Routine cleaning", date="2026-01-12", time="09:00",
    )

    assert booking["success"] is True
    assert len(sent) == 2
    recipients = {to for to, _ in sent}
    assert "+15555550100" in recipients  # caller confirmation
    assert config.escalation_phone in recipients  # owner notification
    owner_msg = next(body for to, body in sent if to == config.escalation_phone)
    assert "Sami" in owner_msg
    assert "Routine cleaning" in owner_msg


def test_book_appointment_then_slot_disappears():
    from app import tools

    booking = tools.book_appointment(
        call_sid="CA1",
        config=_config(),
        caller_name="Sami",
        caller_phone="+15555550100",
        service="Routine cleaning",
        date="2026-01-12",
        time="09:00",
    )
    assert booking["success"] is True

    availability = tools.check_availability(config=_config(), date="2026-01-12")
    assert "09:00" not in availability["slots"]


def test_double_booking_returns_graceful_error_not_exception():
    from app import tools

    first = tools.book_appointment(
        call_sid="CA1", config=_config(), caller_name="A", caller_phone="+15555550100",
        service="Routine cleaning", date="2026-01-12", time="09:00",
    )
    second = tools.book_appointment(
        call_sid="CA2", config=_config(), caller_name="B", caller_phone="+15555550100",
        service="Routine cleaning", date="2026-01-12", time="09:00",
    )
    assert first["success"] is True
    assert second["success"] is False
    assert "error" in second


# ---- services longer than a slot must fit before closing; phones must be real

def _long_job_config():
    from app.config import Service

    cfg = _config().model_copy(update={
        "slot_minutes": 30,
        "services": [Service(name="Yard cleanup", duration_minutes=120), Service(name="Estimate", duration_minutes=30)],
    })
    return cfg


def test_availability_for_a_long_service_never_offers_a_start_that_runs_past_closing():
    from app import tools

    cfg = _long_job_config()
    close = cfg.effective_booking_hours["mon"][1]  # e.g. "17:00"
    close_h, close_m = map(int, close.split(":"))
    slots = tools.check_availability(config=cfg, date="2026-01-12", service="Yard cleanup", limit=50)["slots"]
    assert slots, "a Monday should have openings"
    for s in slots:
        h, m = map(int, s.split(":"))
        assert h * 60 + m + 120 <= close_h * 60 + close_m, f"{s} + 2h runs past {close}"


def test_availability_without_a_service_still_uses_the_slot_length():
    from app import tools

    cfg = _long_job_config()
    plain = tools.check_availability(config=cfg, date="2026-01-12", limit=50)["slots"]
    long = tools.check_availability(config=cfg, date="2026-01-12", service="Yard cleanup", limit=50)["slots"]
    assert len(plain) > len(long)


def test_booking_a_long_service_past_closing_is_refused():
    from app import tools

    cfg = _long_job_config()
    close = cfg.effective_booking_hours["mon"][1]
    close_h, close_m = map(int, close.split(":"))
    late = f"{close_h - 1:02d}:{close_m:02d}"  # one hour before close, but the job takes two
    r = tools.book_appointment(call_sid="CA1", config=cfg, caller_name="Pat", caller_phone="+15555550100",
                               service="Yard cleanup", date="2026-01-12", time=late)
    assert r["success"] is False and "closing" in r["error"]


@pytest.mark.parametrize("phone", ["", "555", "call me", "five seven zero", "123456789"])
def test_booking_refuses_an_unusable_phone_number(phone):
    from app import tools

    r = tools.book_appointment(call_sid="CA2", config=_config(), caller_name="Pat", caller_phone=phone,
                               service="Routine cleaning", date="2026-01-12", time="10:00")
    assert r["success"] is False and "phone" in r["error"]


def test_booking_accepts_common_phone_formats():
    from app import tools

    for i, phone in enumerate(["+15555550100", "+15555550100", "+15555550100", "+15555550100"]):
        r = tools.book_appointment(call_sid=f"CA3{i}", config=_config(), caller_name="Pat", caller_phone=phone,
                                   service="Routine cleaning", date="2026-01-12", time=f"{9 + i}:00".zfill(5))
        assert r["success"] is True, (phone, r)
