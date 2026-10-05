"""Contract for the customer-facing webhook payloads. Offline and deterministic.

Pins (1) the envelope and per-event `data` fields the code really emits, (2) the signing and retry constants, and (3) that
docs/WEBHOOK_INTEGRATIONS.md mentions every pinned event and field, so the documentation cannot drift from the code.
Changing a payload on purpose means updating CONTRACT below AND the doc (and bumping webhooks.SCHEMA_VERSION if it breaks receivers).
"""
import hashlib
import hmac
import json
import os
import re
import tempfile
from pathlib import Path

import pytest

SECRET = "a-long-enough-secret-123"
DOC = Path(__file__).resolve().parents[2] / "docs" / "WEBHOOK_INTEGRATIONS.md"

ENVELOPE = {"id", "version", "event", "created_at", "client_id", "business_name", "data"}
CONTRACT = {
    "booking.created": {"booking_id", "caller_name", "caller_phone", "service", "start", "end", "timezone", "call_sid"},
    "booking.updated": {"booking_id", "caller_name", "caller_phone", "service", "start", "end", "previous_start", "timezone", "call_sid"},
    "booking.cancelled": {"booking_id", "caller_name", "caller_phone", "service", "start", "end", "timezone", "call_sid"},
    "callback.requested": {"reason", "caller_name", "caller_phone", "summary", "call_sid"},
    "call.completed": {"call_sid", "from", "started_at", "ended_at", "outcome", "turns", "outcome_class"},
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
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


def _cfg(cid="demo_hvac"):
    from app.config import load_client_config

    return load_client_config(cid).model_copy(update={"webhook_url": "https://example.com/hook", "webhook_secret": SECRET})


def _queued(storage) -> dict[str, dict]:
    import sqlite3

    from app import webhooks

    webhooks.ensure_table()
    conn = sqlite3.connect(storage.DB_PATH)
    try:
        return {event: json.loads(p) for event, p in conn.execute("SELECT event, payload_json FROM webhook_outbox ORDER BY id")}
    finally:
        conn.close()


@pytest.fixture
def emitted(env, monkeypatch):
    """Drive every event through the real code paths and return {event: queued payload}."""
    from app import summary, tools

    cfg = _cfg()
    booked = tools.book_appointment(call_sid="CA_C1", config=cfg, caller_name="Pat Lee", caller_phone="+15555550100",
                                    service="Emergency repair", date="2026-01-12", time="10:00")
    assert booked["success"]
    moved = tools.reschedule_appointment(config=cfg, call_sid="CA_C2", booking_id=booked["booking_id"], new_date="2026-01-12",
                                         new_time="11:00", caller_id="+15555550100")
    assert moved["success"] and not moved.get("unchanged")
    tools.cancel_appointment(config=cfg, call_sid="CA_C3", booking_id=booked["booking_id"], caller_id="+15555550100")
    tools.escalate_to_human(call_sid="CA_C4", config=cfg, reason="wants_callback", caller_name="Pat Lee", caller_phone="+15555550100",
                            summary="Furnace is making a noise.")
    monkeypatch.setattr(summary, "load_client_config", lambda cid: cfg)
    monkeypatch.setattr(summary.agent, "_anthropic_client", lambda: (_ for _ in ()).throw(RuntimeError("no model in this test")))
    env.log_call_start("CA_C5", cfg.client_id, "+15555550100")
    env.log_call_end("CA_C5", "completed")
    summary.summarize_call("CA_C5")
    return _queued(env), booked["booking_id"]


def test_every_documented_event_is_emitted_with_exactly_the_documented_fields(emitted):
    payloads, _ = emitted
    assert set(payloads) == set(CONTRACT)
    for event, fields in CONTRACT.items():
        p = payloads[event]
        assert set(p) == ENVELOPE, event
        assert p["event"] == event and p["version"] == 1 and p["client_id"] == "demo_hvac"
        assert set(p["data"]) == fields, event


def test_field_values_and_types(emitted):
    payloads, booking_id = emitted
    created, updated, cancelled = (payloads[e]["data"] for e in ("booking.created", "booking.updated", "booking.cancelled"))
    assert created["booking_id"] == booking_id and isinstance(booking_id, int)
    assert created["start"] == "2026-01-12T10:00" and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d", created["end"])   # local time, no offset
    assert created["timezone"] == _cfg().timezone
    assert updated["previous_start"] == "2026-01-12T10:00" and updated["start"] == "2026-01-12T11:00"
    assert cancelled["start"] == "2026-01-12T11:00"
    cb = payloads["callback.requested"]["data"]
    assert cb["reason"] == "wants_callback" and cb["call_sid"] == "CA_C4"
    done = payloads["call.completed"]["data"]
    assert done["call_sid"] == "CA_C5" and done["outcome"] == "completed" and done["from"] == "+15555550100"
    assert isinstance(done["turns"], int)
    from app import outcomes

    assert done["outcome_class"] in outcomes.OUTCOMES                    # a zero-turn completed call classifies as ABANDONED
    assert datetime_parses(payloads["call.completed"]["created_at"])


def datetime_parses(s: str) -> bool:
    from datetime import datetime

    return datetime.fromisoformat(s).tzinfo is not None


def test_event_ids_are_the_documented_idempotency_keys(emitted):
    payloads, bid = emitted
    ids = {e: p["id"] for e, p in payloads.items()}
    assert ids == {
        "booking.created": f"booking.created:demo_hvac:{bid}",
        "booking.updated": f"booking.updated:demo_hvac:{bid}:2026-01-12T11:00:CA_C2",
        "booking.cancelled": f"booking.cancelled:demo_hvac:{bid}",
        "callback.requested": "callback.requested:demo_hvac:CA_C4:wants_callback",
        "call.completed": "call.completed:demo_hvac:CA_C5",
    }


def test_outcome_classes_are_the_documented_set():
    from app import outcomes

    assert set(outcomes.OUTCOMES) == {
        "FAQ_RESOLVED", "LEAD_CAPTURED", "BOOKED", "RESCHEDULED", "CANCELLED", "CALLBACK_REQUESTED", "TRANSFERRED", "TRANSFER_FAILED",
        "OUTSIDE_SERVICE_AREA", "SERVICE_NOT_OFFERED", "AFTER_HOURS_MESSAGE", "EMERGENCY_ESCALATED", "ABANDONED", "SPAM", "AI_FAILURE", "UNKNOWN",
    }


def test_signature_and_delivery_constants_match_the_docs():
    from app import webhooks

    body = b'{"a":1}'
    header = webhooks.sign(SECRET, body, 1_700_000_000)
    expected = hmac.new(SECRET.encode(), b"+15555550100." + body, hashlib.sha256).hexdigest()
    assert header == f"t=+15555550100,v1={expected}"
    assert webhooks.verify(SECRET, body, header, now=1_700_000_100)
    assert not webhooks.verify(SECRET, body, header, now=1_700_000_301)          # 5 minute replay window
    assert not webhooks.verify(SECRET, body + b" ", header, now=1_700_000_100)
    assert not webhooks.verify("another-long-secret-1234", body, header, now=1_700_000_100)
    assert webhooks.BACKOFF_SECONDS == (10, 30, 120, 600, 3600, 21600, 86400) and webhooks.MAX_ATTEMPTS == 8
    assert webhooks.TIMEOUT_SECONDS == 8.0 and webhooks.REPLAY_TOLERANCE_SECONDS == 300 and webhooks.SCHEMA_VERSION == 1


def test_request_headers_and_body_encoding(monkeypatch):
    """What a receiver sees on the wire: header names, compact JSON, signature over the exact raw body."""
    from app import webhooks

    seen = {}

    class _R:
        status_code = 200

        def iter_bytes(self):
            return iter(())

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_stream(method, url, content, headers, timeout, follow_redirects):
        seen.update(method=method, url=url, content=content, headers=headers, follow=follow_redirects)
        return _R()

    monkeypatch.setattr(webhooks, "url_is_safe", lambda url: (True, "ok"))
    monkeypatch.setattr(webhooks.httpx, "stream", fake_stream)
    payload = {"id": "evt-1", "version": 1, "event": "call.completed", "data": {"note": "caf\u00e9"}}
    assert webhooks.deliver_once("https://example.com/hook", SECRET, "call.completed", payload) == (True, None)
    h = seen["headers"]
    assert seen["method"] == "POST" and seen["follow"] is False
    assert set(h) == {"Content-Type", "User-Agent", "X-Event", "X-Delivery", "X-Signature"}
    assert h["X-Event"] == "call.completed" and h["X-Delivery"] == "evt-1" and h["Content-Type"] == "application/json"
    assert seen["content"] == json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    t = int(h["X-Signature"].split(",")[0].split("=")[1])
    assert h["X-Signature"] == webhooks.sign(SECRET, seen["content"], t)


def test_the_integration_doc_covers_every_event_and_field():
    assert DOC.exists(), "docs/WEBHOOK_INTEGRATIONS.md is part of the contract"
    text = DOC.read_text(encoding="utf-8")
    for token in ENVELOPE | set(CONTRACT) | {f for fields in CONTRACT.values() for f in fields}:
        assert f"`{token}`" in text, f"doc does not mention `{token}`"
    from app import outcomes

    for cls in outcomes.OUTCOMES:
        assert f"`{cls}`" in text, f"doc does not list outcome class {cls}"
    for header in ("X-Signature", "X-Delivery", "X-Event"):
        assert header in text
    for phrase in ("NOT tested by Call Kettle", "no native integration"):
        assert phrase.lower() in text.lower(), phrase


def test_sample_cli_matches_the_contract_and_verifies():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import webhook_sample

    for event, fields in CONTRACT.items():
        payload, body, headers = webhook_sample.sample("demo_hvac", event, "dummy-secret+15555550100", 1_767_603_600)
        assert set(payload) == ENVELOPE and set(payload["data"]) == fields, event
        from app import webhooks

        assert webhooks.verify("dummy-secret+15555550100", body, headers["X-Signature"], now=1_767_603_600)
    assert webhook_sample.main(["demo_hvac", "--secret", "short"]) == 2
