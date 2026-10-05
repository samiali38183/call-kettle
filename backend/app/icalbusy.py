"""Read the owner's own calendar as BUSY TIME through a private iCal link.

Why: the receptionist must not offer a slot the owner has already filled with a job. Every major calendar can publish a
private read-only iCal address (Google: Settings -> your calendar -> "Secret address in iCal format"; Outlook: publish
calendar -> ICS; iCloud: public calendar link), so the owner can connect it themselves in a minute, with no Google service
account and no login from us. It is READ ONLY: we never write to their calendar. Bookings reach it through the .ics
invitation that is emailed with every booking.

What this is and is not (keep the claims honest):
  * It is availability awareness: events on the owner's calendar block those times.
  * It is NOT two-way sync. A booking made by the assistant appears on the owner's calendar only when they accept the
    emailed invitation, and a feed is refreshed by the calendar provider on its own schedule (Google can lag many hours),
    so a change the owner makes on their phone may not be visible here for a while. We re-read the feed at most every
    5 minutes and always right before confirming a booking.

Behaviour on failure: fail open. If the link cannot be read the assistant keeps using its own schedule, the operator is
alerted (rate limited), and the last good copy (up to 1 hour old) is used meanwhile.

Safety: https only (webcal:// is rewritten), every hop's host must resolve to public addresses only (same check as
webhooks), at most 3 redirects each re-checked, 6 s timeout, 3 MB cap, and the feed is parsed as data only.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx

from app import brand

logger = logging.getLogger("callkettle.icalbusy")

FRESH_SECONDS = 300          # a feed younger than this is reused
STALE_OK_SECONDS = 3600      # an older copy is still used when the feed cannot be fetched
FAIL_BACKOFF_SECONDS = 60    # after a failure do not retry for this long (a down host must not slow every call)
MAX_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 6.0

_lock = threading.Lock()
_cache: dict[str, dict] = {}     # url -> {"cal": parsed, "at": monotonic, "error": str|None, "failed_at": monotonic|None, "events": int}


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    return url


def _fetch(url: str) -> bytes:
    """GET with manual redirects so every hop is checked. Raises on any problem."""
    from app import webhooks

    current = normalize_url(url)
    with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False, headers={"User-Agent": f"{brand.get().name} calendar-reader/1"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            ok, why = webhooks.url_is_safe(current)
            if not ok:
                raise ValueError(why)
            with client.stream("GET", current) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    current = urljoin(current, r.headers["location"])
                    if current.lower().startswith("webcal://"):
                        current = normalize_url(current)
                    continue
                if r.status_code != 200:
                    raise ValueError(f"HTTP {r.status_code}")
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise ValueError("calendar feed is larger than 3 MB")
                return bytes(body)
        raise ValueError("too many redirects")


def _parse(raw: bytes):
    from icalendar import Calendar

    cal = Calendar.from_ical(raw)
    if str(cal.get("PRODID", "")) == "" and not list(cal.walk("VEVENT")):
        raise ValueError("that link is not an iCal calendar")
    return cal


def _load(url: str, force: bool = False):
    """The parsed calendar (fresh, or the last good copy), or None. Never raises."""
    now = time.monotonic()
    with _lock:
        entry = _cache.get(url)
        if entry and entry.get("cal") is not None and not force and now - entry["at"] < FRESH_SECONDS:
            return entry["cal"]
        if entry and entry.get("failed_at") is not None and not force and now - entry["failed_at"] < FAIL_BACKOFF_SECONDS:
            return entry["cal"] if entry.get("cal") is not None and now - entry["at"] < STALE_OK_SECONDS else None
    try:
        cal = _parse(_fetch(url))
        events = sum(1 for _ in cal.walk("VEVENT"))
        with _lock:
            _cache[url] = {"cal": cal, "at": time.monotonic(), "error": None, "failed_at": None, "events": events}
        return cal
    except Exception as exc:
        reason = f"{type(exc).__name__}: {str(exc)[:120]}"
        logger.warning("Calendar feed unreadable (%s)", reason)
        with _lock:
            entry = _cache.get(url) or {"cal": None, "at": 0.0, "events": 0}
            entry.update(error=reason, failed_at=time.monotonic())
            _cache[url] = entry
            usable = entry["cal"] is not None and time.monotonic() - entry["at"] < STALE_OK_SECONDS
            stale = entry["cal"] if usable else None
        _alert(url, reason)
        return stale


def _alert(url: str, reason: str) -> None:
    try:
        from app import ops

        host = urlparse(normalize_url(url)).hostname or "?"
        ops.alert_operator("A client's calendar link is unreadable",
                           f"The calendar feed at {host} could not be read ({reason}). Bookings continue without checking it. "
                           "Ask the owner to re-copy the private iCal link.", key=f"ical-{host}", min_interval=6 * 3600)
    except Exception:
        logger.exception("Could not alert about an unreadable calendar")


def _to_local(value, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value                                   # floating time: the owner's wall clock
    return value.astimezone(tz).replace(tzinfo=None)


def busy_periods(url: str, start: datetime, end: datetime, tz_name: str, *, all_day_blocks: bool = False,
                 force: bool = False) -> list[tuple[datetime, datetime]] | None:
    """Busy intervals (naive client-local times) overlapping [start, end), or None if the calendar could not be read.
    Skipped: cancelled events, events marked Free/transparent, our own bookings (matched by UID suffix, so a booking the owner
    accepted into their calendar does not block its own reschedule), zero-length events, and all-day events unless asked."""
    cal = _load(url, force=force)
    if cal is None:
        return None
    try:
        import recurring_ical_events

        tz = ZoneInfo(tz_name)
        lo, hi = start.replace(tzinfo=tz), end.replace(tzinfo=tz)
        busy: list[tuple[datetime, datetime]] = []
        for ev in recurring_ical_events.of(cal).between(lo, hi):
            if str(ev.get("STATUS", "")).upper() == "CANCELLED" or str(ev.get("TRANSP", "")).upper() == "TRANSPARENT":
                continue
            if str(ev.get("UID", "")).endswith(brand.ICS_UID_SUFFIX):
                continue
            s = ev["DTSTART"].dt
            e = ev["DTEND"].dt if ev.get("DTEND") else None
            if e is None and ev.get("DURATION"):
                e = s + ev["DURATION"].dt
            if isinstance(s, date) and not isinstance(s, datetime):
                if not all_day_blocks:
                    continue
                busy.append((datetime(s.year, s.month, s.day), (datetime(e.year, e.month, e.day) if e else datetime(s.year, s.month, s.day) + timedelta(days=1))))
                continue
            if e is None:
                continue
            ls, le = _to_local(s, tz), _to_local(e, tz)
            if le > ls:
                busy.append((ls, le))
        return [b for b in busy if b[0] < end and start < b[1]]
    except Exception:
        logger.exception("Could not read events from the calendar feed")
        return None


def is_free(url: str, start: datetime, end: datetime, tz_name: str, *, all_day_blocks: bool = False) -> bool | None:
    """True/False if we could check (always with a fresh read), None if we could not (the caller carries on)."""
    busy = busy_periods(url, start, end, tz_name, all_day_blocks=all_day_blocks, force=True)
    if busy is None:
        return None
    return not any(b0 < end and start < b1 for b0, b1 in busy)


def check_url(url: str, tz_name: str = "America/New_York") -> dict:
    """For onboarding and the readiness check: can we read this link, and what do we see in the next 30 days?"""
    url = normalize_url(url)
    try:
        cal = _parse(_fetch(url))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    with _lock:
        _cache[url] = {"cal": cal, "at": time.monotonic(), "error": None, "failed_at": None, "events": sum(1 for _ in cal.walk("VEVENT"))}
    now = datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)
    upcoming = busy_periods(url, now, now + timedelta(days=30), tz_name) or []
    return {"ok": True, "events_total": _cache[url]["events"], "busy_blocks_next_30_days": len(upcoming)}


def clear_cache() -> None:
    with _lock:
        _cache.clear()
