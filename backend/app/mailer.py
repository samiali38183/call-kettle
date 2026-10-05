"""Transactional email: one place that sends, records and learns what happened to every message.

Why this exists: "accepted by the mail server" is not "delivered", and a business whose booking alert bounced must be known, not assumed. Every send is recorded
(`email_log`), an address that hard-bounces or complains is suppressed, a repeated idempotency key never sends twice, and provider events (delivered, bounced,
complained) update the record. Sales email is NOT sent through here: transactional mail (booking alerts, recaps) and sales mail stay separate so a sales complaint
can never hurt critical delivery.

Providers (choose with EMAIL_PROVIDER = smtp | postmark | resend; the default is smtp when SMTP_* is set, which is how email works today):
  * smtp      the existing SMTP account (SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_PORT, SMTP_FROM). Accepted only: there are no delivery events.
  * postmark  POSTMARK_SERVER_TOKEN, EMAIL_FROM. Events: point Postmark's webhook at /email/events/postmark?token=<see /admin/status>.
  * resend    RESEND_API_KEY, EMAIL_FROM. Events: point Resend's webhook at /email/events/resend?token=<...>.
The Postmark and Resend adapters follow each vendor's documented JSON API but have NOT been run against a live account (none exists yet): verify with a real
test message and a real bounce before relying on them. Authenticated sending also needs SPF, DKIM and DMARC records on callkettle.com (docs/PLATFORM_NOTES.md).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import smtplib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

import httpx

from app import storage

logger = logging.getLogger("callkettle.mailer")

HARD_STATES = {"bounced", "complained"}


@dataclass
class SendResult:
    accepted: bool
    provider: str | None
    message_id: str | None = None
    status: str = "failed"           # accepted | suppressed | failed | duplicate
    error: str | None = None


# ---------------------------------------------------------------------------------------------------------- storage
def _init(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS email_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL, to_addr TEXT NOT NULL, subject TEXT, kind TEXT, provider TEXT, message_id TEXT,
            status TEXT NOT NULL, detail TEXT, idempotency_key TEXT UNIQUE, updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS email_log_message ON email_log (provider, message_id);
        CREATE TABLE IF NOT EXISTS email_suppressions (
            addr TEXT PRIMARY KEY, reason TEXT NOT NULL, at TEXT NOT NULL
        );
        """
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(addr: str) -> str:
    return parseaddr(addr)[1].strip().lower()


def is_suppressed(addr: str) -> bool:
    with storage._conn() as conn:
        _init(conn)
        return conn.execute("SELECT 1 FROM email_suppressions WHERE addr = ?", (_norm(addr),)).fetchone() is not None


def unsuppress(addr: str) -> None:
    with storage._conn() as conn:
        _init(conn)
        conn.execute("DELETE FROM email_suppressions WHERE addr = ?", (_norm(addr),))


def _record(to: str, subject: str, kind: str, provider: str | None, message_id: str | None, status: str, detail: str | None, key: str | None) -> None:
    with storage._conn() as conn:
        _init(conn)
        conn.execute(
            "INSERT INTO email_log (created_at, to_addr, subject, kind, provider, message_id, status, detail, idempotency_key, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_now(), _norm(to), subject[:200], kind, provider, message_id, status, (detail or "")[:300], key, _now()),
        )


# ---------------------------------------------------------------------------------------------------------- providers
def provider_name() -> str | None:
    chosen = (os.environ.get("EMAIL_PROVIDER") or "").lower()
    if chosen in {"smtp", "postmark", "resend"}:
        return chosen
    if os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"):
        return "smtp"
    return None


def configured() -> bool:
    p = provider_name()
    if p == "smtp":
        return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"))
    if p == "postmark":
        return bool(os.environ.get("POSTMARK_SERVER_TOKEN") and os.environ.get("EMAIL_FROM"))
    if p == "resend":
        return bool(os.environ.get("RESEND_API_KEY") and os.environ.get("EMAIL_FROM"))
    return False


def _from_header(default_name: str) -> str:
    raw = os.environ.get("EMAIL_FROM") or os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or ""
    shown, addr = parseaddr(raw)
    return formataddr((shown or default_name, addr or raw))


def _send_smtp(msg: EmailMessage) -> str | None:
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=15) as smtp:
        smtp.starttls()
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)
    return None                                                      # SMTP gives no message id


def _attachments(ics: str | None) -> list[tuple[str, bytes, str]]:
    return [("booking.ics", ics.encode("utf-8"), "text/calendar")] if ics else []


def _send_postmark(client: httpx.Client, frm: str, to: str, subject: str, body: str, ics: str | None) -> str:
    payload = {"From": frm, "To": to, "Subject": subject, "TextBody": body, "MessageStream": os.environ.get("POSTMARK_STREAM", "outbound"),
               "Attachments": [{"Name": n, "Content": base64.b64encode(c).decode(), "ContentType": t} for n, c, t in _attachments(ics)]}
    r = client.post("https://api.postmarkapp.com/email", json=payload, headers={"X-Postmark-Server-Token": os.environ["POSTMARK_SERVER_TOKEN"], "Accept": "application/json"})
    data = r.json()
    if r.status_code != 200 or data.get("ErrorCode", 0) != 0:
        raise RuntimeError(f"Postmark refused the message: {r.status_code} {data.get('Message', '')[:120]}")
    return data["MessageID"]


def _send_resend(client: httpx.Client, frm: str, to: str, subject: str, body: str, ics: str | None, key: str | None) -> str:
    payload = {"from": frm, "to": [to], "subject": subject, "text": body,
               "attachments": [{"filename": n, "content": base64.b64encode(c).decode()} for n, c, _t in _attachments(ics)]}
    headers = {"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"}
    if key:
        headers["Idempotency-Key"] = key[:256]
    r = client.post("https://api.resend.com/emails", json=payload, headers=headers)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Resend refused the message: {r.status_code} {r.text[:120]}")
    return r.json()["id"]


def one_line(text: str, limit: int = 200) -> str:
    return " ".join(str(text).split())[:limit]


def send(to: str, subject: str, body: str, ics: str | None = None, *, kind: str = "notification", idempotency_key: str | None = None,
         sender_name: str = "", http: httpx.Client | None = None) -> SendResult:
    """Send one transactional message. Never raises. `accepted` means the provider took it; delivery is learned later from events (providers that report them)."""
    provider = provider_name()
    subject, to = one_line(subject), one_line(to)
    if not configured():
        return SendResult(False, provider, status="failed", error="email is not configured")
    if idempotency_key:
        with storage._conn() as conn:
            _init(conn)
            prior = conn.execute("SELECT status, message_id FROM email_log WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        if prior:
            return SendResult(prior[0] != "failed", provider, prior[1], status="duplicate")
    if is_suppressed(to):
        _record(to, subject, kind, provider, None, "suppressed", "address previously bounced or complained", idempotency_key)
        return SendResult(False, provider, status="suppressed", error="address is suppressed after a bounce or complaint")
    try:
        frm = _from_header(sender_name)
        message_id = None
        if provider == "smtp":
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = frm, to, subject
            msg.set_content(body)
            if ics:
                msg.add_attachment(ics.encode("utf-8"), maintype="text", subtype="calendar", filename="booking.ics",
                                   params={"method": "CANCEL" if "METHOD:CANCEL" in ics else "REQUEST"})
            message_id = _send_smtp(msg)
        else:
            client = http or httpx.Client(timeout=15)
            message_id = _send_postmark(client, frm, to, subject, body, ics) if provider == "postmark" else _send_resend(client, frm, to, subject, body, ics, idempotency_key)
        _record(to, subject, kind, provider, message_id, "accepted", None, idempotency_key)
        return SendResult(True, provider, message_id, status="accepted")
    except Exception as exc:
        logger.exception("Email to %s failed", to)
        try:
            _record(to, subject, kind, provider, None, "failed", f"{type(exc).__name__}: {exc}", None)
        except sqlite3.Error:
            pass
        return SendResult(False, provider, status="failed", error=f"{type(exc).__name__}: {str(exc)[:120]}")


# ---------------------------------------------------------------------------------------------------------- provider events
def events_token() -> str:
    """The secret in the webhook URL (providers that cannot sign requests call a URL we give them). Derived from REPORT_KEY, so it needs no storage."""
    return hmac.new((os.environ.get("REPORT_KEY") or "callkettle").encode(), b"email-events", hashlib.sha256).hexdigest()[:32]


def parse_event(provider: str, payload: dict) -> tuple[str, str | None, str | None, str] | None:
    """(event, message_id, recipient, detail) with event in delivered | bounced | complained, or None for an event we do not track."""
    if provider == "postmark":
        kind = payload.get("RecordType")
        if kind == "Delivery":
            return "delivered", payload.get("MessageID"), payload.get("Recipient"), ""
        if kind == "Bounce":
            hard = payload.get("Type") in {"HardBounce", "BadEmailAddress", "SpamNotification", "ManuallyDeactivated", "Unsubscribe"}
            return ("bounced" if hard else "soft_bounce"), payload.get("MessageID"), payload.get("Email"), str(payload.get("Type", ""))
        if kind == "SpamComplaint":
            return "complained", payload.get("MessageID"), payload.get("Email"), "spam complaint"
    if provider == "resend":
        t, data = payload.get("type", ""), payload.get("data") or {}
        to = (data.get("to") or [None])[0]
        if t == "email.delivered":
            return "delivered", data.get("email_id"), to, ""
        if t == "email.bounced":
            return "bounced", data.get("email_id"), to, str((data.get("bounce") or {}).get("type", ""))
        if t == "email.complained":
            return "complained", data.get("email_id"), to, "spam complaint"
    return None


def record_event(provider: str, event: str, message_id: str | None, recipient: str | None, detail: str = "") -> None:
    with storage._conn() as conn:
        _init(conn)
        if message_id:
            row = conn.execute("SELECT status FROM email_log WHERE provider = ? AND message_id = ?", (provider, message_id)).fetchone()
            if row and not (row[0] in HARD_STATES and event == "delivered"):         # a bounce is never overwritten by a late 'delivered'
                conn.execute("UPDATE email_log SET status = ?, detail = ?, updated_at = ? WHERE provider = ? AND message_id = ?", (event, detail[:300], _now(), provider, message_id))
        if event in HARD_STATES and recipient:
            conn.execute("INSERT OR REPLACE INTO email_suppressions (addr, reason, at) VALUES (?,?,?)", (_norm(recipient), event + (f": {detail}" if detail else ""), _now()))


def health() -> dict:
    """For /admin/status: provider, whether it is configured, and what happened to the last 24 hours of mail (accepted is NOT delivered)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with storage._conn() as conn:
        _init(conn)
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM email_log WHERE created_at >= ? GROUP BY status", (since,)).fetchall())
        suppressed = conn.execute("SELECT COUNT(*) FROM email_suppressions").fetchone()[0]
    return {"provider": provider_name(), "configured": configured(), "last_24h": counts, "suppressed_addresses": suppressed,
            "delivery_events": provider_name() in {"postmark", "resend"}}
