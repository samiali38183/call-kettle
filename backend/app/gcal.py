"""Optional two-way Google Calendar sync.

How it works: The service has ONE Google service account (a robot user). A client
opens Google Calendar, shares their calendar with the robot's email address
("Make changes to events"), and gives us the calendar's ID. From then on:

  * check_availability hides times the owner is already busy (freeBusy API), so
    the AI never offers a slot that clashes with a job already on their calendar;
  * book_appointment re-checks right before booking, then writes the event to
    their calendar.

Why a service account instead of per-client Google login (OAuth): no consent
screen, no Google app-verification process, no refresh tokens that expire, and
the client's whole "setup" is a 30-second calendar share. The trade-off is that
it only works for Google Calendar (Outlook/Apple need a different route).

Everything here fails soft. A Google outage, a calendar that was never shared,
or a missing credential returns None/[] and the booking flow carries on with
the built-in scheduler. Never let this break a live call.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("callkettle.gcal")

_SCOPES = ["https://www.googleapis.com/auth/calendar"]
_API = "https://www.googleapis.com/calendar/v3"
_TIMEOUT = 5.0

_lock = threading.Lock()
_credentials = None


def enabled() -> bool:
    return bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))


def service_account_email() -> str | None:
    try:
        return json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]).get("client_email")
    except Exception:
        return None


def _access_token() -> str:
    global _credentials
    with _lock:
        if _credentials is None:
            from google.oauth2 import service_account

            info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
            _credentials = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
        if not _credentials.valid:
            from google.auth.transport.requests import Request

            _credentials.refresh(Request())
        return _credentials.token


def _headers() -> dict:
    return {"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"}


def _rfc3339(naive_local: datetime, tz_name: str) -> str:
    return naive_local.replace(tzinfo=ZoneInfo(tz_name)).isoformat()


def _parse(value: str, tz_name: str) -> datetime:
    """Google returns RFC3339 with an offset; convert to naive local wall-clock time."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)


def busy_periods(calendar_id: str, start: datetime, end: datetime, tz_name: str) -> list[tuple[datetime, datetime]] | None:
    """Busy intervals (naive local times) between start and end, or None if the
    calendar can't be read (not shared, credentials missing, Google down)."""
    if not enabled() or not calendar_id:
        return None
    try:
        response = httpx.post(
            f"{_API}/freeBusy",
            headers=_headers(),
            json={
                "timeMin": _rfc3339(start, tz_name),
                "timeMax": _rfc3339(end, tz_name),
                "timeZone": tz_name,
                "items": [{"id": calendar_id}],
            },
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        entry = response.json().get("calendars", {}).get(calendar_id, {})
        if entry.get("errors"):
            logger.warning(
                "Google Calendar %s can't be read (%s). Has it been shared with %s?",
                calendar_id, entry["errors"], service_account_email(),
            )
            return None
        return [(_parse(b["start"], tz_name), _parse(b["end"], tz_name)) for b in entry.get("busy", [])]
    except Exception:
        logger.exception("Google Calendar freeBusy failed for %s; using the built-in schedule only", calendar_id)
        return None


def overlaps(busy: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    return any(b_start < end and start < b_end for b_start, b_end in busy)


def is_free(calendar_id: str, start: datetime, end: datetime, tz_name: str) -> bool | None:
    """True/False if we could check, None if we couldn't (caller should carry on)."""
    busy = busy_periods(calendar_id, start - timedelta(minutes=1), end + timedelta(minutes=1), tz_name)
    if busy is None:
        return None
    return not overlaps(busy, start, end)


def create_event(
    calendar_id: str, *, summary: str, description: str, start: datetime, end: datetime, tz_name: str
) -> str | None:
    """Create the booking on the owner's calendar. Returns the event id, or None on failure."""
    if not enabled() or not calendar_id:
        return None
    try:
        response = httpx.post(
            f"{_API}/calendars/{calendar_id}/events",
            headers=_headers(),
            json={
                "summary": summary,
                "description": description,
                "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz_name},
                "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz_name},
            },
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        return response.json().get("id")
    except Exception:
        logger.exception("Could not write the booking to Google Calendar %s", calendar_id)
        return None


def create_event_in_background(*args, **kwargs) -> None:
    threading.Thread(target=create_event, args=args, kwargs=kwargs, daemon=True).start()
