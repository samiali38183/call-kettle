"""Outbound webhooks with a durable outbox.

A client with `webhook_url` and `webhook_secret` in their config receives signed JSON for:
  booking.created | booking.updated | booking.cancelled | callback.requested | call.completed

Design (so a restart or an outage can never silently lose a customer's event):
  1. emit() writes the event to the `webhook_outbox` table in the database. That is the only thing on the call path.
  2. A background worker (process_outbox) delivers due events, one attempt at a time, and records the result.
  3. Failures retry with growing delays (10s, 30s, 2m, 10m, 1h, 6h, 24h: about 31 hours in total), surviving restarts.
     After the last attempt the event is marked failed and the operator is alerted.
  4. Every event has a stable id (`id`, also the `X-Delivery` header). Retries reuse it, so receivers de-duplicate.

Security:
  * https only; the host must resolve to PUBLIC addresses only (no loopback, private, link-local/cloud-metadata, CGNAT,
    reserved or multicast; IPv6 and IPv4-mapped IPv6 included). Checked again right before every attempt.
  * Redirects are never followed. Response bodies are read only up to 64 KB. 8 second timeout.
  * Every request is signed: `X-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<raw body>" keyed with webhook_secret>`.
    Receivers must verify the signature AND reject a timestamp more than 5 minutes old (replay protection).
  * Only the operator can set a webhook URL (it lives in the client's config), so there is no tenant-controlled SSRF path.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from app import storage
from app.config import ClientConfig, load_client_config

logger = logging.getLogger("callkettle.webhooks")

SCHEMA_VERSION = 1
BACKOFF_SECONDS = (10, 30, 120, 600, 3600, 21600, 86400)      # delay before attempt 2, 3, ... 8
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1
TIMEOUT_SECONDS = 8.0
MAX_RESPONSE_BYTES = 64 * 1024
REPLAY_TOLERANCE_SECONDS = 300
_ALLOW_PRIVATE_ENV = "CALLKETTLE_ALLOW_PRIVATE_WEBHOOKS"        # tests only

_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    client_id TEXT NOT NULL,
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',      -- pending | delivered | failed | cancelled
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS webhook_outbox_due ON webhook_outbox (status, next_attempt_at);
"""


def ensure_table() -> None:
    with storage._conn() as conn:
        conn.executescript(_SCHEMA)


# ---------------------------------------------------------------------------------------------- signing
def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    t = int(timestamp if timestamp is not None else time.time())
    mac = hmac.new(secret.encode("utf-8"), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify(secret: str, body: bytes, header: str, *, tolerance: int = REPLAY_TOLERANCE_SECONDS, now: float | None = None) -> bool:
    """For receivers (and our tests): the signature must match AND the timestamp must be recent."""
    try:
        parts = dict(p.split("=", 1) for p in (header or "").split(","))
        t = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs((now if now is not None else time.time()) - t) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, body, t), header)


# ---------------------------------------------------------------------------------------------- safety checks
def _is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def url_is_safe(url: str) -> tuple[bool, str]:
    """https only, and every address the host resolves to must be public."""
    parsed = urlparse(url)
    if os.environ.get(_ALLOW_PRIVATE_ENV) == "1":
        return (parsed.scheme in {"http", "https"} and bool(parsed.hostname)), "test mode"
    if parsed.scheme != "https" or not parsed.hostname:
        return False, "webhook URL must start with https://"
    if parsed.username or parsed.password:
        return False, "webhook URL must not contain credentials"
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False, "webhook host did not resolve"
    for info in infos:
        if not _is_public(ipaddress.ip_address(info[4][0])):
            return False, "webhook host resolves to a non-public address"
    return True, "ok"


# ---------------------------------------------------------------------------------------------- the outbox
def build_payload(config: ClientConfig, event: str, data: dict, *, event_id: str | None = None) -> dict:
    return {
        "id": event_id or str(uuid.uuid4()),
        "version": SCHEMA_VERSION,
        "event": event,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "client_id": config.client_id,
        "business_name": config.business_name,
        "data": data,
    }


def emit(config: ClientConfig, event: str, data: dict, event_id: str | None = None) -> bool:
    """Queue an event. Writes one row; never raises, never makes a network call. `event_id` makes it idempotent."""
    try:
        if not config.webhook_url or not config.webhook_secret:
            return False
        ensure_table()
        payload = build_payload(config, event, data, event_id=event_id)
        with storage._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO webhook_outbox (event_id, client_id, event, payload_json, next_attempt_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (payload["id"], config.client_id, event, json.dumps(payload, ensure_ascii=False), storage._now(), storage._now()),
            )
            return cur.rowcount == 1
    except Exception:
        logger.exception("Could not queue webhook %s", event)
        return False


def deliver_once(url: str, secret: str, event: str, payload: dict) -> tuple[bool, str | None]:
    """One attempt. Returns (delivered, error text). Never raises."""
    ok, why = url_is_safe(url)
    if not ok:
        return False, why
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json", "User-Agent": "receptionist-webhooks/1", "X-Event": event,
        "X-Delivery": payload["id"], "X-Signature": sign(secret, body),
    }
    try:
        with httpx.stream("POST", url, content=body, headers=headers, timeout=TIMEOUT_SECONDS, follow_redirects=False) as r:
            read = 0
            for chunk in r.iter_bytes():
                read += len(chunk)
                if read > MAX_RESPONSE_BYTES:
                    break                       # we never need the response body; do not let a hostile one fill memory
            if 200 <= r.status_code < 300:
                return True, None
            return False, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, type(exc).__name__


def process_outbox(now: datetime | None = None, limit: int = 20) -> dict:
    """Attempt every due event once. Safe to call from several workers or after a restart."""
    ensure_table()
    now = now or datetime.now(timezone.utc)
    stats = {"delivered": 0, "retrying": 0, "failed": 0, "cancelled": 0}
    with storage._conn() as conn:
        due = conn.execute(
            "SELECT id, client_id, event, payload_json, attempts FROM webhook_outbox "
            "WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY id LIMIT ?", (now.isoformat(), limit)).fetchall()
    for row_id, client_id, event, payload_json, attempts in due:
        try:
            cfg = load_client_config(client_id)
        except Exception:
            cfg = None
        if cfg is None or not cfg.webhook_url or not cfg.webhook_secret:
            _finish(row_id, "cancelled", attempts, "client has no webhook configured")
            stats["cancelled"] += 1
            continue
        ok, err = deliver_once(cfg.webhook_url, cfg.webhook_secret, event, json.loads(payload_json))
        attempts += 1
        if ok:
            _finish(row_id, "delivered", attempts, None, delivered=True)
            stats["delivered"] += 1
        elif attempts >= MAX_ATTEMPTS:
            _finish(row_id, "failed", attempts, err)
            stats["failed"] += 1
            storage.record_metric("webhook_failed", client_id)
            try:
                from app import ops

                ops.alert_operator("A client's webhook is failing",
                                   f"{client_id}: {event} could not be delivered after {attempts} attempts over about 31 hours ({err}). "
                                   "Check the receiving system.", key=f"webhook-{client_id}", min_interval=6 * 3600)
            except Exception:
                logger.exception("Could not alert about a failed webhook")
        else:
            delay = BACKOFF_SECONDS[attempts - 1]
            with storage._conn() as conn:
                conn.execute("UPDATE webhook_outbox SET attempts = ?, last_error = ?, next_attempt_at = ? WHERE id = ?",
                             (attempts, err, (now + timedelta(seconds=delay)).isoformat(), row_id))
            stats["retrying"] += 1
    return stats


def _finish(row_id: int, status: str, attempts: int, error: str | None, delivered: bool = False) -> None:
    with storage._conn() as conn:
        conn.execute("UPDATE webhook_outbox SET status = ?, attempts = ?, last_error = ?, delivered_at = ? WHERE id = ?",
                     (status, attempts, error, storage._now() if delivered else None, row_id))


def outbox_health() -> dict:
    """For the status page: how many events are waiting, failed, and how old the oldest waiting one is."""
    ensure_table()
    with storage._conn() as conn:
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM webhook_outbox GROUP BY status").fetchall())
        oldest = conn.execute("SELECT MIN(created_at) FROM webhook_outbox WHERE status = 'pending'").fetchone()[0]
    age = None
    if oldest:
        age = int((datetime.now(timezone.utc) - datetime.fromisoformat(oldest)).total_seconds())
    return {"pending": counts.get("pending", 0), "failed": counts.get("failed", 0), "delivered": counts.get("delivered", 0),
            "oldest_pending_seconds": age}
