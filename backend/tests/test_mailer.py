"""Transactional email: provider adapters, idempotency, suppression after a bounce, provider events, and honest 'accepted is not delivered' health."""
import base64
import json

import httpx
import pytest

from app import mailer


@pytest.fixture(autouse=True)
def _db(monkeypatch, tmp_path):
    from app import storage

    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "m.db"))
    storage.init_db()
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_PROVIDER", "POSTMARK_SERVER_TOKEN", "RESEND_API_KEY", "EMAIL_FROM", "SMTP_FROM"):
        monkeypatch.delenv(k, raising=False)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_nothing_is_sent_and_nothing_pretends_when_email_is_not_configured():
    r = mailer.send("owner@example.com", "hi", "body")
    assert not r.accepted and r.status == "failed" and "not configured" in r.error
    assert mailer.health()["configured"] is False


def test_postmark_payload_and_accepted_record(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "postmark")
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "tok")
    monkeypatch.setenv("EMAIL_FROM", "Call Kettle <owner@example.com>")
    seen = {}

    def handler(request):
        seen["headers"], seen["json"] = request.headers, json.loads(request.content)
        return httpx.Response(200, json={"ErrorCode": 0, "MessageID": "pm-123"})

    r = mailer.send("owner@example.com", "Booking\nwith newline", "body", "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nEND:VCALENDAR", kind="booking", http=_client(handler))
    assert r.accepted and r.message_id == "pm-123" and r.provider == "postmark"
    assert seen["headers"]["x-postmark-server-token"] == "tok" and seen["json"]["To"] == "owner@example.com" and "\n" not in seen["json"]["Subject"]
    assert base64.b64decode(seen["json"]["Attachments"][0]["Content"]).startswith(b"BEGIN:VCALENDAR")
    assert mailer.health()["last_24h"] == {"accepted": 1}


def test_resend_payload_and_idempotency_header(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    monkeypatch.setenv("EMAIL_FROM", "owner@example.com")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "rs-1"})

    first = mailer.send("owner@example.com", "Recap", "b", kind="recap", idempotency_key="recap:x:2026-W41", http=_client(handler))
    again = mailer.send("owner@example.com", "Recap", "b", kind="recap", idempotency_key="recap:x:2026-W41", http=_client(handler))
    assert first.accepted and again.status == "duplicate" and len(calls) == 1               # the second call never reaches the provider
    assert calls[0].headers["idempotency-key"] == "recap:x:2026-W41" and calls[0].headers["authorization"] == "Bearer re_x"


def test_a_provider_error_is_a_failure_not_a_silent_success(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "postmark")
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "tok")
    monkeypatch.setenv("EMAIL_FROM", "owner@example.com")
    r = mailer.send("owner@example.com", "s", "b", http=_client(lambda req: httpx.Response(422, json={"ErrorCode": 300, "Message": "Invalid email request"})))
    assert not r.accepted and r.status == "failed" and "Postmark refused" in r.error
    assert mailer.health()["last_24h"] == {"failed": 1}


def test_a_hard_bounce_suppresses_the_address_and_a_late_delivered_does_not_undo_it(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "k")
    monkeypatch.setenv("EMAIL_FROM", "owner@example.com")
    mailer.send("Owner <owner@example.com>", "s", "b", http=_client(lambda req: httpx.Response(200, json={"id": "rs-9"})))
    ev = mailer.parse_event("resend", {"type": "email.bounced", "data": {"email_id": "rs-9", "to": ["owner@example.com"], "bounce": {"type": "Permanent"}}})
    assert ev[0] == "bounced"
    mailer.record_event("resend", *ev[:1], ev[1], ev[2], ev[3])
    mailer.record_event("resend", "delivered", "rs-9", "owner@example.com")                # out-of-order delivered
    assert mailer.is_suppressed("owner@example.com")
    again = mailer.send("owner@example.com", "s2", "b", http=_client(lambda req: pytest.fail("a suppressed address must not be sent to")))
    assert again.status == "suppressed" and not again.accepted
    h = mailer.health()
    assert h["suppressed_addresses"] == 1 and h["last_24h"].get("bounced") == 1 and h["last_24h"].get("suppressed") == 1
    mailer.unsuppress("owner@example.com")
    assert not mailer.is_suppressed("owner@example.com")


def test_parse_event_covers_postmark_and_ignores_unknown_events():
    assert mailer.parse_event("postmark", {"RecordType": "Delivery", "MessageID": "m", "Recipient": "owner@example.com"})[0] == "delivered"
    assert mailer.parse_event("postmark", {"RecordType": "Bounce", "Type": "HardBounce", "MessageID": "m", "Email": "owner@example.com"})[0] == "bounced"
    assert mailer.parse_event("postmark", {"RecordType": "Bounce", "Type": "Transient", "MessageID": "m", "Email": "owner@example.com"})[0] == "soft_bounce"
    assert mailer.parse_event("postmark", {"RecordType": "SpamComplaint", "MessageID": "m", "Email": "owner@example.com"})[0] == "complained"
    assert mailer.parse_event("postmark", {"RecordType": "Open"}) is None and mailer.parse_event("resend", {"type": "email.opened"}) is None


def test_event_webhook_needs_the_secret_and_updates_status(app_client, monkeypatch):
    client, main = app_client
    monkeypatch.setenv("EMAIL_PROVIDER", "postmark")
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "t")
    monkeypatch.setenv("EMAIL_FROM", "owner@example.com")
    mailer.send("owner@example.com", "s", "b", http=_client(lambda req: httpx.Response(200, json={"ErrorCode": 0, "MessageID": "pm-5"})))
    body = {"RecordType": "Bounce", "Type": "HardBounce", "MessageID": "pm-5", "Email": "owner@example.com"}
    assert client.post("/email/events/postmark?token=wrong", json=body).status_code == 403
    assert client.post("/email/events/other?token=" + mailer.events_token(), json=body).status_code == 403
    r = client.post("/email/events/postmark?token=" + mailer.events_token(), json=body)
    assert r.status_code == 200 and r.json()["event"] == "bounced" and mailer.is_suppressed("owner@example.com")
    status = client.get("/admin/status", params={"key": "master_key_for_tests"}).json()
    assert status["email"]["provider"] == "postmark" and status["email"]["last_24h"].get("bounced") == 1
