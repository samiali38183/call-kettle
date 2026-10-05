"""Stripe billing state: signature verification, replays, status tracking, operator alerts, and no automatic cut-off of a phone line."""
import hashlib
import hmac
import json
import time

import pytest

from app import billing

SECRET = "whsec_test_secret"


def _sign(body: bytes, secret=SECRET, ts=None):
    ts = int(ts or time.time())
    return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def _evt(eid, etype, **obj):
    return {"id": eid, "type": etype, "data": {"object": obj}}


@pytest.fixture(autouse=True)
def _db(monkeypatch, tmp_path):
    from app import storage

    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "b.db"))
    storage.init_db()


def test_signature_scheme_accepts_good_rejects_bad_stale_and_malformed():
    body = b'{"id":"evt_1"}'
    now = time.time()
    assert billing.verify_signature(body, _sign(body, ts=now), SECRET, now=now)
    assert not billing.verify_signature(body + b" ", _sign(body, ts=now), SECRET, now=now)           # body altered
    assert not billing.verify_signature(body, _sign(body, secret="other", ts=now), SECRET, now=now)  # wrong secret
    assert not billing.verify_signature(body, _sign(body, ts=now - 3600), SECRET, now=now)           # replayed an hour later
    for bad in ("", "garbage", "t=abc,v1=00", "v1=00"):
        assert not billing.verify_signature(body, bad, SECRET, now=now)
    # Stripe may send several v1 values during secret rotation: any one matching is enough
    rotating = _sign(body, ts=now) + ",v1=" + "0" * 64
    assert billing.verify_signature(body, rotating, SECRET, now=now)


def test_subscription_lifecycle_is_tracked_and_replays_are_ignored():
    created = _evt("evt_1", "customer.subscription.created", id="sub_1", customer="cus_1", status="active", current_period_end=+15555550100, metadata={"client_ref": "sample_homecare"})
    assert billing.handle_event(created)["status"] == "active"
    assert billing.handle_event(created) == {"applied": False, "reason": "duplicate"}
    failed = billing.handle_event(_evt("evt_2", "invoice.payment_failed", subscription="sub_1"))
    assert failed["status"] == "past_due" and failed["attention"] is True
    assert billing.handle_event(_evt("evt_3", "invoice.paid", subscription="sub_1"))["status"] == "active"
    gone = billing.handle_event(_evt("evt_4", "customer.subscription.deleted", id="sub_1", status="canceled"))
    assert gone["status"] == "canceled" and gone["attention"] is True
    row = billing.summary()["subscriptions"][0]
    assert row["subscription"] == "sub_1" and row["client_ref"] == "sample_homecare" and row["status"] == "canceled" and row["period_end"]    # the link survives later events


def test_checkout_completed_links_the_client_reference_and_unknown_events_are_ignored():
    r = billing.handle_event(_evt("evt_c", "checkout.session.completed", subscription="sub_9", customer="cus_9", client_reference_id="intake-12"))
    assert r["applied"] and billing.summary()["subscriptions"][0]["client_ref"] == "intake-12"
    assert billing.handle_event(_evt("evt_x", "customer.created", id="cus_1")) == {"applied": False, "reason": "not tracked"}
    assert billing.handle_event({"type": "invoice.paid"}) == {"applied": False, "reason": "not tracked"}


def test_the_webhook_endpoint_verifies_alerts_the_operator_and_never_touches_a_phone_line(app_client, monkeypatch):
    client, main = app_client
    from app import ops

    alerts = []
    monkeypatch.setattr(ops, "alert_operator", lambda title, body, **kw: alerts.append((title, body)) or True)
    body = json.dumps(_evt("evt_w1", "invoice.payment_failed", subscription="sub_w")).encode()
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    assert client.post("/stripe/webhook", content=body).status_code == 403                    # not configured: refuses rather than trusting
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    assert client.post("/stripe/webhook", content=body, headers={"Stripe-Signature": "t=1,v1=bad"}).status_code == 400
    assert client.post("/stripe/webhook", content=body, headers={"Stripe-Signature": _sign(body)}).status_code == 200
    again = client.post("/stripe/webhook", content=body, headers={"Stripe-Signature": _sign(body)})
    assert again.json()["reason"] == "duplicate" and len(alerts) == 1 and "NOT switched off" in alerts[0][1]
    status = client.get("/admin/status", params={"key": "master_key_for_tests"}).json()
    assert status["billing"]["by_status"] == {"past_due": 1}


def test_delayed_bank_debit_checkout_is_pending_then_active_or_alerts_on_failure():
    """ACH Direct Debit settles days after Checkout: unpaid-at-checkout is pending, never silently 'complete'."""
    pending = billing.handle_event(_evt("evt_a1", "checkout.session.completed", subscription="sub_ach", customer="cus_a",
                                        client_reference_id="intake-7", payment_status="unpaid", status="complete"))
    assert pending["status"] == "pending_payment" and pending["attention"] is False
    ok = billing.handle_event(_evt("evt_a2", "checkout.session.async_payment_succeeded", subscription="sub_ach"))
    assert ok["status"] == "active" and ok["attention"] is False
    assert billing.summary()["subscriptions"][0]["client_ref"] == "intake-7"
    bad = billing.handle_event(_evt("evt_a3", "checkout.session.async_payment_failed", subscription="sub_ach"))
    assert bad["status"] == "past_due" and bad["attention"] is True


def test_paid_card_checkout_keeps_existing_status_behavior():
    r = billing.handle_event(_evt("evt_c2", "checkout.session.completed", subscription="sub_card", customer="cus_c", payment_status="paid", status="complete"))
    assert r["status"] == "complete" and r["attention"] is False
