"""Billing state from Stripe webhooks (sales-led: the owner makes the Payment Link or invoice in Stripe; nothing here charges anyone).

What this does: receives Stripe events at /stripe/webhook, verifies their signature, ignores replays, and keeps a small table of each subscription's state
(active, past_due, canceled...) so the operator can see who is paid without opening Stripe. What it deliberately does NOT do: switch a customer's phone line off.
A lapsed payment alerts the operator, who decides (docs/BILLING.md): cutting off a business's phone line automatically is a worse failure than a late invoice.

Verified how: the signature scheme (`t=<timestamp>,v1=<HMAC-SHA256 of "<t>.<raw body>">`, 5-minute tolerance) is implemented from Stripe's documentation and
tested with locally generated signatures. It has NOT been run against Stripe itself (no test key exists yet): use Stripe's test mode and `stripe trigger` before
relying on it. Set STRIPE_WEBHOOK_SECRET (the endpoint's whsec_... value) as a Fly secret.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone

from app import storage

TOLERANCE_SECONDS = 300
TRACKED = {"checkout.session.completed", "customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted",
           "invoice.payment_failed", "invoice.paid",
           # Delayed methods (ACH Direct Debit) settle days after Checkout completes.
           "checkout.session.async_payment_succeeded", "checkout.session.async_payment_failed"}
_CHECKOUT_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded", "checkout.session.async_payment_failed")


def _init(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS stripe_events (event_id TEXT PRIMARY KEY, type TEXT, received_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS billing (
            subscription_id TEXT PRIMARY KEY, customer_id TEXT, client_ref TEXT, status TEXT NOT NULL,
            current_period_end TEXT, cancel_at_period_end INTEGER DEFAULT 0, last_event TEXT, updated_at TEXT NOT NULL
        );
        """
    )


def verify_signature(raw_body: bytes, header: str, secret: str, *, now: float | None = None) -> bool:
    """Stripe's scheme. Any malformed header, wrong signature or stale timestamp is simply False."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
        timestamp = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - timestamp) > TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    candidates = [v for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p) if k == "v1"]
    return any(hmac.compare_digest(expected, c) for c in candidates)


def _iso(ts) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def handle_event(event: dict) -> dict:
    """Apply one verified event. Returns {"applied": bool, "reason": ...}. Safe to call twice with the same event."""
    eid, etype = event.get("id"), event.get("type")
    if not eid or etype not in TRACKED:
        return {"applied": False, "reason": "not tracked"}
    obj = (event.get("data") or {}).get("object") or {}
    now = datetime.now(timezone.utc).isoformat()
    with storage._conn() as conn:
        _init(conn)
        if conn.execute("SELECT 1 FROM stripe_events WHERE event_id = ?", (eid,)).fetchone():
            return {"applied": False, "reason": "duplicate"}
        conn.execute("INSERT INTO stripe_events (event_id, type, received_at) VALUES (?,?,?)", (eid, etype, now))
        sub_id = obj.get("subscription") if etype in _CHECKOUT_EVENTS + ("invoice.payment_failed", "invoice.paid") else obj.get("id")
        if not sub_id:
            return {"applied": True, "reason": "no subscription on this event"}
        status = ({"customer.subscription.deleted": "canceled", "invoice.payment_failed": "past_due", "invoice.paid": "active",
                   "checkout.session.async_payment_failed": "past_due", "checkout.session.async_payment_succeeded": "active"}.get(etype)
                  or ("pending_payment" if etype == "checkout.session.completed" and obj.get("payment_status") == "unpaid" else None)
                  or obj.get("status") or "active")
        client_ref = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("client_ref")
        prior = conn.execute("SELECT client_ref FROM billing WHERE subscription_id = ?", (sub_id,)).fetchone()
        conn.execute(
            "INSERT INTO billing (subscription_id, customer_id, client_ref, status, current_period_end, cancel_at_period_end, last_event, updated_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(subscription_id) DO UPDATE SET status = excluded.status, last_event = excluded.last_event, updated_at = excluded.updated_at, "
            "customer_id = COALESCE(excluded.customer_id, billing.customer_id), client_ref = COALESCE(excluded.client_ref, billing.client_ref), "
            "current_period_end = COALESCE(excluded.current_period_end, billing.current_period_end), cancel_at_period_end = excluded.cancel_at_period_end",
            (sub_id, obj.get("customer"), client_ref or (prior[0] if prior else None), status, _iso(obj.get("current_period_end")),
             1 if obj.get("cancel_at_period_end") else 0, etype, now),
        )
    return {"applied": True, "status": status, "subscription": sub_id, "attention": status in ("past_due", "unpaid", "canceled")}


def summary() -> dict:
    with storage._conn() as conn:
        _init(conn)
        by_status = dict(conn.execute("SELECT status, COUNT(*) FROM billing GROUP BY status").fetchall())
        recent = conn.execute("SELECT subscription_id, client_ref, status, current_period_end, updated_at FROM billing ORDER BY updated_at DESC LIMIT 20").fetchall()
    return {"configured": bool(os.environ.get("STRIPE_WEBHOOK_SECRET")), "by_status": by_status,
            "subscriptions": [dict(zip(("subscription", "client_ref", "status", "period_end", "updated_at"), r)) for r in recent]}


def parse(raw: bytes) -> dict | None:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except ValueError:
        return None
