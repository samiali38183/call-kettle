import os
import tempfile

import pytest
from starlette.requests import Request
from twilio.request_validator import RequestValidator


def _make_request(path="/voice/incoming", query="client_id=demo_dental", headers=None, scheme="http"):
    headers = headers or []
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": query.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
        "scheme": scheme,
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def test_public_base_url_uses_forwarded_proto_and_real_host():
    from app.main import _public_base_url

    req = _make_request(
        headers=[("host", "posts-dollars-marilyn-yoga.trycloudflare.com"), ("x-forwarded-proto", "https")]
    )
    assert _public_base_url(req) == "https://posts-dollars-marilyn-yoga.trycloudflare.com"


def test_public_base_url_falls_back_to_local_without_proxy_headers():
    from app.main import _public_base_url

    req = _make_request(headers=[("host", "127.0.0.1:8000")], scheme="http")
    assert _public_base_url(req) == "http://127.0.0.1:8000"


def test_public_url_includes_path_and_query_string():
    from app.main import _public_url

    req = _make_request(
        path="/voice/incoming",
        query="client_id=demo_dental&retry=0",
        headers=[("host", "x.trycloudflare.com"), ("x-forwarded-proto", "https")],
    )
    assert _public_url(req) == "https://x.trycloudflare.com/voice/incoming?client_id=demo_dental&retry=0"


def test_reproduces_the_reported_403_signature_mismatch(monkeypatch):
    """Twilio signs the public https URL; validating against the local
    http://127.0.0.1 URL must fail — this is the exact bug from the field
    report (call to +15555550100, webhook returned 403)."""
    from app import twilio_utils

    monkeypatch.setattr(twilio_utils, "_AUTH_TOKEN", "test_token_abc")
    monkeypatch.setattr(twilio_utils, "_SKIP_SIGNATURE_CHECK", False)

    public_url = "https://posts-dollars-marilyn-yoga.trycloudflare.com/voice/incoming?client_id=demo_dental"
    form = {"CallSid": "CA1", "From": "+15551234567"}
    signature = RequestValidator("test_token_abc").compute_signature(public_url, form)

    wrong_local_url = "http://127.0.0.1:8000/voice/incoming?client_id=demo_dental"
    assert twilio_utils.validate_signature(url=wrong_local_url, form=form, signature=signature) is False


def test_fix_validates_correctly_against_reconstructed_public_url(monkeypatch):
    """Same signature as above, but validated against the URL _public_base_url
    reconstructs from the forwarded headers — this must now pass."""
    from app import twilio_utils
    from app.main import _public_base_url

    monkeypatch.setattr(twilio_utils, "_AUTH_TOKEN", "test_token_abc")
    monkeypatch.setattr(twilio_utils, "_SKIP_SIGNATURE_CHECK", False)

    req = _make_request(
        headers=[("host", "posts-dollars-marilyn-yoga.trycloudflare.com"), ("x-forwarded-proto", "https")]
    )
    reconstructed_url = _public_base_url(req) + "/voice/incoming?client_id=demo_dental"
    form = {"CallSid": "CA1", "From": "+15551234567"}
    signature = RequestValidator("test_token_abc").compute_signature(reconstructed_url, form)

    assert twilio_utils.validate_signature(url=reconstructed_url, form=form, signature=signature) is True


@pytest.fixture
def client(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("CALLKETTLE_DB_PATH", path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("CALLKETTLE_SKIP_SIGNATURE_CHECK", "1")
    monkeypatch.setenv("REPORT_KEY", "test_report_key")

    import importlib

    from app import storage, twilio_utils, main, notify

    # These route tests do not exercise notification delivery. Keep their
    # failure alerts offline and prevent daemon email writes outliving the
    # temporary database fixture (Windows cannot unlink an open SQLite file).
    monkeypatch.setattr(notify, "notify_owner", lambda *args, **kwargs: None)

    importlib.reload(twilio_utils)
    importlib.reload(storage)
    importlib.reload(main)
    # Patch the exact safety-net dependency too: other tests reload notify,
    # so ops may retain a different module reference than the local import.
    monkeypatch.setattr(main.ops, "alert_operator", lambda *args, **kwargs: False)
    storage.init_db()

    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c, main

    os.remove(path)


def test_incoming_call_always_includes_recording_disclosure(client):
    """The disclosure must be present regardless of what's in the client's
    YAML — it's applied in code, not left to per-client config discipline."""
    test_client, main = client

    response = test_client.post(
        "/voice/incoming?client_id=demo_dental",
        data={"CallSid": "CA_DISC", "From": "+15551234567", "To": "+15550000000"},
    )

    assert response.status_code == 200
    assert "This call may be recorded and monitored for quality." in response.text
    assert "Bright Smile Dental" in response.text  # disclosure prepended, not replacing the greeting


def test_unhandled_error_on_voice_route_degrades_to_graceful_twiml(client, monkeypatch):
    """Reproduces a real incident: an unhandled exception (e.g. a missing DB
    table) must never surface as a bare 500 on a /voice/* route — Twilio has
    no TwiML to work with then and plays "application error" to a real
    caller. This must return valid TwiML with a 200, not crash."""
    test_client, main = client

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated: database file lost its schema underneath a running process")

    monkeypatch.setattr(main.storage, "log_call_start", _boom)

    # raise_server_exceptions=False: a real Twilio request has no idea an
    # exception was raised server-side, it only sees the HTTP response our
    # exception handler produced — that's what this test needs to check.
    from fastapi.testclient import TestClient

    with TestClient(main.app, raise_server_exceptions=False) as no_raise_client:
        response = no_raise_client.post(
            "/voice/incoming?client_id=demo_dental",
            data={"CallSid": "CA_CRASH", "From": "+15551234567", "To": "+15550000000"},
        )

    assert response.status_code == 200
    # A crashed AI rings the business's real phone instead of stranding the caller.
    assert "connecting you with the team" in response.text.lower()
    assert "+1" in response.text and "</Dial>" in response.text


def test_voice_gather_transfer_produces_dial_twiml(client, monkeypatch):
    """Full HTTP round trip: when run_turn signals a transfer, the actual
    response TwiML must contain <Dial>, not just say-and-hangup."""
    test_client, main = client

    incoming = test_client.post(
        "/voice/incoming?client_id=demo_dental",
        data={"CallSid": "CA_XFER", "From": "+15551234567", "To": "+15550000000"},
    )
    assert incoming.status_code == 200

    monkeypatch.setattr(
        main.agent, "run_turn", lambda session, text: ("Connecting you now.", True, "+15555550111")
    )

    gather = test_client.post(
        "/voice/gather?client_id=demo_dental&retry=0",
        data={"CallSid": "CA_XFER", "SpeechResult": "I need to talk to a real person"},
    )

    assert gather.status_code == 200
    assert "+15555550111</Dial>" in gather.text
    assert "voice/transfer-result" in gather.text
    assert "Connecting you now." in gather.text
    assert "<Hangup" not in gather.text


def test_book_availability_returns_real_slots(client):
    test_client, main = client
    response = test_client.get("/book/availability", params={"date": "2026-01-12"})  # a Monday
    assert response.status_code == 200
    assert "09:00" in response.json()["slots"]


def test_book_confirm_creates_a_real_conflict_checked_booking(client):
    test_client, main = client
    response = test_client.post(
        "/book/confirm",
        json={"name": "Prospect Business Owner", "phone": "+15555550100", "date": "2026-01-12", "time": "09:00"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True

    # The exact same slot must now be gone from availability — proves this
    # is a real conflict-checked booking, not a contact form pretending.
    availability = test_client.get("/book/availability", params={"date": "2026-01-12"})
    assert "09:00" not in availability.json()["slots"]


def test_book_confirm_rejects_a_double_booking(client):
    test_client, main = client
    first = test_client.post(
        "/book/confirm", json={"name": "A", "phone": "+15555550100", "date": "2026-01-12", "time": "10:00"}
    )
    second = test_client.post(
        "/book/confirm", json={"name": "B", "phone": "+15555550100", "date": "2026-01-12", "time": "10:00"}
    )
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["success"] is False


def test_book_confirm_has_no_client_id_parameter_to_exploit(client):
    """Security-relevant: confirms the endpoint shape gives no way to target
    a real client's calendar — client_id isn't accepted as input at all."""
    test_client, main = client
    response = test_client.post(
        "/book/confirm",
        json={
            "name": "A", "phone": "+15555550100", "date": "2026-01-12", "time": "11:00",
            "client_id": "sample_homecare",  # extra field — pydantic ignores unknown fields by default
        },
    )
    assert response.status_code == 200
    # Confirm it landed on the sales calendar, not sample_homecare's.
    import sqlite3

    conn = sqlite3.connect(os.environ["CALLKETTLE_DB_PATH"])
    row = conn.execute(
        "SELECT client_id FROM bookings WHERE slot_start = '2026-01-12T11:00'"
    ).fetchone()
    conn.close()
    assert row[0] == "callkettle_sales"


def test_report_page_rejects_wrong_or_missing_key(client):
    """The report shows real customer names/phone numbers — it must not be
    viewable by guessing the client_id alone."""
    test_client, main = client

    no_key = test_client.get("/report/sample_homecare")
    assert no_key.status_code == 403

    wrong_key = test_client.get("/report/sample_homecare", params={"key": "nope"})
    assert wrong_key.status_code == 403


def test_report_page_rejects_unknown_client(client):
    test_client, main = client
    response = test_client.get("/report/not_a_real_client", params={"key": "test_report_key"})
    assert response.status_code == 404


def test_report_page_shows_real_booking_this_is_the_actual_bug_fix(client):
    """Reproduces the reported bug: 'when asked to make an appointment,
    where does it make an appointment, nobody knows.' A booking made
    through the phone flow must be visible on this page, by name, phone,
    and time — proving there is now a real, working way to see where a
    booking landed without depending on SMS (which is currently silently
    failing due to incomplete A2P 10DLC registration)."""
    test_client, main = client

    booking = test_client.post(
        "/book/confirm",
        json={"name": "Real Caller", "phone": "+15551234999", "date": "2026-01-12", "time": "09:00"},
    )
    assert booking.status_code == 200

    # /book/confirm always lands on callkettle_sales, so read that report.
    response = test_client.get("/report/callkettle_sales", params={"key": "test_report_key"})
    assert response.status_code == 200
    assert "Real Caller" in response.text
    assert "+15551234999" in response.text
    assert "Mon Jan 12, 9:00 AM" in response.text


def test_report_page_shows_escalation(client):
    test_client, main = client

    main.storage.log_escalation(
        call_sid="CA_ESC",
        client_id="sample_homecare",
        reason="asked_for_human",
        caller_phone="+15557778888",
        summary="Caller asked to speak to the owner directly.",
    )

    response = test_client.get("/report/sample_homecare", params={"key": "test_report_key"})
    assert response.status_code == 200
    assert "+15557778888" in response.text
    assert "asked to speak to the owner" in response.text
