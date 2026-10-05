"""Owner notifications. A booking or handoff that only lives in a database is
invisible in practice, so every event fans out to whichever channels are
actually working: SMS (only once Twilio A2P 10DLC registration is approved and
SMS_ENABLED=1), email (SMTP env vars + config.owner_email), and ntfy push
(config.ntfy_topic). No channel raising can ever break a live call.

Bookings also go out as a calendar invite (.ics attachment), so they land on
the owner's Google / Outlook / Apple calendar without any calendar API."""
from __future__ import annotations

import logging
import os
import re
import smtplib
import threading
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from zoneinfo import ZoneInfo

import httpx

from app import brand, twilio_utils
from app.config import ClientConfig

logger = logging.getLogger("callkettle.notify")


def sms_enabled() -> bool:
    return os.environ.get("SMS_ENABLED", "0") == "1"


def _ics_escape(text: str) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)   # a control character would start a new calendar line (injection)
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


def build_ics(
    *, config: ClientConfig, caller_name: str, caller_phone: str, service: str, start: datetime, end: datetime, organizer: str,
    uid: str | None = None, method: str = "REQUEST", sequence: int = 0,
) -> str:
    """A calendar invite for one booking. Times are converted to UTC so no
    VTIMEZONE block is needed and every calendar app renders it correctly."""
    tz = ZoneInfo(config.timezone)
    fmt = "%Y%m%dT%H%M%SZ"
    start_utc = start.replace(tzinfo=tz).astimezone(timezone.utc)
    end_utc = end.replace(tzinfo=tz).astimezone(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{brand.get().name}//Bookings//EN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid or uuid.uuid4().hex}{brand.ICS_UID_SUFFIX}",
        f"SEQUENCE:{sequence}",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime(fmt)}",
        f"DTSTART:{start_utc.strftime(fmt)}",
        f"DTEND:{end_utc.strftime(fmt)}",
        f"SUMMARY:{_ics_escape(f'{service}: {caller_name}')}",
        f"DESCRIPTION:{_ics_escape(f'Booked by the AI receptionist. Caller: {caller_name}, {caller_phone}.')}",
        f'ORGANIZER;CN="{brand.get().name.replace(chr(34), "")}":mailto:{organizer}',
        f"ATTENDEE;CN={_ics_escape(config.business_name)};RSVP=FALSE:mailto:{config.owner_email or organizer}",
        "STATUS:CANCELLED" if method == "CANCEL" else "STATUS:CONFIRMED",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def one_line(text: str, limit: int = 200) -> str:
    """A header value must be a single line: a newline in an email subject is an error at best and a
    header-injection attack at worst. Anything that came from a caller or the AI goes through this."""
    return " ".join(str(text).split())[:limit]


def _send_email(to: str, subject: str, body: str, ics: str | None = None, *, kind: str = "notification", key: str | None = None) -> bool:
    """True only if the provider accepted the message (accepted is not delivered: see app/mailer.py for events). Never raises."""
    from app import mailer

    try:
        return mailer.send(to, subject, body, ics, kind=kind, idempotency_key=key, sender_name=brand.get().sender_name).accepted
    except Exception:
        logger.exception("Email notification to %s failed", to)
        return False


def _send_ntfy(topic: str, title: str, body: str) -> None:
    if os.environ.get("CALLKETTLE_DISABLE_PUSH") == "1":
        return  # set for the test suite so it can never buzz a real phone
    try:
        httpx.post(
            f"https://ntfy.sh/{topic}",
            content=body.encode("utf-8"),
            headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": "high"},
            timeout=10,
        )
    except Exception:
        logger.exception("ntfy notification failed")


def notify_owner(config: ClientConfig, *, title: str, body: str, ics: str | None = None) -> None:
    """Fire-and-forget: slow channels run on daemon threads so the caller
    never waits on an SMTP handshake."""
    if sms_enabled():
        twilio_utils.send_sms(to=config.escalation_phone, body=f"[{config.business_name}] {title}: {body}"[:320])
    if config.owner_email:
        threading.Thread(
            target=_send_email, args=(config.owner_email, f"[{config.business_name}] {title}", body, ics), daemon=True
        ).start()
    if config.ntfy_topic:
        threading.Thread(target=_send_ntfy, args=(config.ntfy_topic, title, body), daemon=True).start()
