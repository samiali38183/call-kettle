from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import brand, gcal, icalbusy, leadcard, notify, spamtag, storage, webhooks
from app.redact import scrub_sensitive
from app.config import ClientConfig

logger = logging.getLogger("callkettle.tools")

_WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def clean_text(value, limit: int) -> str:
    """Text that came from a caller (through the AI): no control characters, bounded length."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()[:limit]

# Don't offer a slot starting in the next half hour — nobody can show up to it.
MIN_LEAD_MINUTES = 30


def _local_now(config: ClientConfig) -> datetime:
    """Naive 'wall clock' now in the client's timezone (slots are stored naive)."""
    return datetime.now(ZoneInfo(config.timezone)).replace(tzinfo=None)


def is_open_now(config: ClientConfig) -> bool:
    """Is the business open right now (client-local clock, business_hours)? Used to decide who answers the phone."""
    now = _local_now(config)
    hours = config.business_hours.get(_WEEKDAY_KEYS[now.weekday()], "closed")
    if not isinstance(hours, list) or len(hours) != 2:
        return False
    return hours[0] <= now.strftime("%H:%M") < hours[1]


def _day_hours(config: ClientConfig, date: datetime) -> tuple[str, str] | None:
    key = _WEEKDAY_KEYS[date.weekday()]
    hours = config.effective_booking_hours.get(key, "closed")
    if hours == "closed" or not isinstance(hours, list):
        return None
    return hours[0], hours[1]


def _slot_grid(config: ClientConfig, day: datetime) -> list[datetime]:
    window = _day_hours(config, day)
    if window is None:
        return []
    open_t = datetime.strptime(window[0], "%H:%M")
    close_t = datetime.strptime(window[1], "%H:%M")
    step = timedelta(minutes=config.slot_minutes)
    slots = []
    t = open_t
    while t + step <= close_t:
        slots.append(day.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0))
        t += step
    return slots


def _service_minutes(config: ClientConfig, service: str | None) -> int:
    if service:
        for s in config.services:
            if s.name.lower() == service.lower():
                return s.duration_minutes
    return config.slot_minutes


def _closes_at(config: ClientConfig, day: datetime) -> datetime | None:
    window = _day_hours(config, day)
    if window is None:
        return None
    h, m = map(int, window[1].split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def _external_busy(config: ClientConfig, start: datetime, end: datetime) -> list[tuple[datetime, datetime]] | None:
    """Busy time from the owner's connected calendars (Google service account and/or private iCal link). None when
    nothing is connected or nothing could be read (fail open: the assistant carries on with its own schedule)."""
    periods: list[tuple[datetime, datetime]] = []
    read_any = False
    if config.google_calendar_id:
        got = _best_effort("google calendar", lambda: gcal.busy_periods(config.google_calendar_id, start, end, config.timezone))
        if got is not None:
            periods += got
            read_any = True
    if config.calendar_ical_url:
        got = _best_effort("ical calendar", lambda: icalbusy.busy_periods(
            config.calendar_ical_url, start, end, config.timezone, all_day_blocks=config.calendar_all_day_blocks))
        if got is not None:
            periods += got
            read_any = True
    return periods if read_any else None


def _external_free(config: ClientConfig, start: datetime, end: datetime) -> bool | None:
    """Right before confirming: is the slot free on every connected calendar? Always a fresh read of an iCal feed."""
    result: bool | None = None
    if config.google_calendar_id:
        free = _best_effort("google calendar", lambda: gcal.is_free(config.google_calendar_id, start, end, config.timezone))
        if free is False:
            return False
        result = free if free is not None else result
    if config.calendar_ical_url:
        free = _best_effort("ical calendar", lambda: icalbusy.is_free(
            config.calendar_ical_url, start, end, config.timezone, all_day_blocks=config.calendar_all_day_blocks))
        if free is False:
            return False
        result = free if free is not None else result
    return result


def check_availability(
    *, config: ClientConfig, date: str, preferred_time: str | None = None, limit: int = 3, service: str | None = None
) -> dict:
    try:
        day = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return {"error": f"'{date}' is not a valid date, expected YYYY-MM-DD"}

    if _day_hours(config, day) is None:
        return {"slots": [], "note": f"{config.business_name} is not scheduling on {day.strftime('%A')}."}

    earliest = _local_now(config) + timedelta(minutes=MIN_LEAD_MINUTES)
    day_prefix = day.strftime("%Y-%m-%d")
    taken = [
        (datetime.strptime(a, "%Y-%m-%dT%H:%M"), datetime.strptime(b, "%Y-%m-%dT%H:%M"))
        for a, b in storage.get_booked_intervals(config.client_id, day_prefix)
    ]
    # If the owner connected a calendar, anything already on it is busy too.
    busy = _external_busy(config, day, day + timedelta(days=1))
    if busy:
        taken.extend(busy)
    # A two-hour job needs two free hours, and must finish before closing.
    step = timedelta(minutes=max(config.slot_minutes, _service_minutes(config, service)))
    closes = _closes_at(config, day)

    candidates = [
        s.strftime("%H:%M")
        for s in _slot_grid(config, day)
        if s >= earliest and (closes is None or s + step <= closes) and not any(a < s + step and s < b for a, b in taken)
    ]

    if preferred_time:
        try:
            pref = datetime.strptime(preferred_time, "%H:%M")
            candidates.sort(key=lambda s: abs((datetime.strptime(s, "%H:%M") - pref).total_seconds()))
        except ValueError:
            pass

    return {"slots": candidates[:limit]}


def book_appointment(
    *,
    call_sid: str | None,
    config: ClientConfig,
    caller_name: str,
    caller_phone: str,
    service: str,
    date: str,
    time: str,
) -> dict:
    caller_name, service, caller_phone = clean_text(caller_name, 80), clean_text(service, 100), clean_text(caller_phone, 30)
    if not caller_name:
        return {"success": False, "error": "the caller's name is required. Ask for it."}
    try:
        start = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        return {"success": False, "error": "invalid date/time format, expected YYYY-MM-DD and HH:MM"}

    # The model proposes a slot; the server decides whether it's real. A
    # hallucinated 3am time, a past time, or an off-grid time that would
    # overlap a neighbouring booking is rejected here, not trusted.
    if start not in _slot_grid(config, start.replace(hour=0, minute=0)):
        return {
            "success": False,
            "error": "that time isn't one of the bookable slots — call check_availability and offer one of those",
        }
    if start < _local_now(config) + timedelta(minutes=MIN_LEAD_MINUTES):
        return {"success": False, "error": "that time has already passed or is too soon — offer a later slot"}

    if len(re.sub(r"\D", "", caller_phone or "")) < 10:
        return {"success": False, "error": "that isn't a usable phone number. Ask the caller for a 10-digit number (or confirm the one they're calling from)."}

    # The server, not the model, decides what this business offers. A service nobody configured is never booked.
    offered = next((s for s in config.services if s.name.lower() == service.lower()), None)
    if offered is None:
        names = ", ".join(s.name for s in config.services)
        return {"success": False, "error": f"'{service}' is not a service this business offers. Offer one of: {names}. If the caller wants something else, say it isn't offered and escalate_to_human."}
    service, duration = offered.name, offered.duration_minutes
    end = start + timedelta(minutes=duration)
    closes = _closes_at(config, start)
    if closes is not None and end > closes:
        return {"success": False, "error": "that job would run past closing time. Offer an earlier slot (pass the service to check_availability)."}
    slot_start_iso = start.strftime("%Y-%m-%dT%H:%M")
    slot_end_iso = end.strftime("%Y-%m-%dT%H:%M")

    if _external_free(config, start, end) is False:
        return {"success": False, "error": "that time just got taken on the owner's calendar, offer the caller a different time"}

    if call_sid and _call_booking_cap_reached(config, call_sid, slot_start_iso):
        return {"success": False, "error": f"one phone call can book at most {MAX_BOOKINGS_PER_CALL} appointments. Do not book more; take the rest as a callback with escalate_to_human."}

    uid = uuid.uuid4().hex
    try:
        booking_id = storage.create_booking(
            call_sid=call_sid,
            client_id=config.client_id,
            caller_name=caller_name,
            caller_phone=caller_phone,
            service=service,
            slot_start=slot_start_iso,
            slot_end=slot_end_iso,
            uid=uid,
        )
    except storage.BookingConflict:
        same = storage.get_booking_at(config.client_id, slot_start_iso)
        if same and call_sid and same["call_sid"] == call_sid and storage._last10(same["caller_phone"]) == storage._last10(caller_phone):
            # the AI repeated the same request within one call: it is one booking, not a conflict and not a second booking
            return {"success": True, "booking_id": same["id"], "confirmed_start": same["slot_start"], "confirmed_end": same["slot_end"],
                    "confirmation_text_sent": False, "already_booked": True}
        return {"success": False, "error": "that slot was just taken, offer the caller a different time"}
    except Exception:
        # The database could not take the write (locked, read-only, disk full). The caller must not hear "booked", and the owner
        # must not lose the lead: they get the request itself, to call back, through the channels that do not need the database.
        logger.exception("Booking could not be saved for call_sid=%s", call_sid)
        _best_effort("booking failure metric", lambda: storage.record_metric("booking_write_failed", config.client_id))
        _best_effort("booking failure alert", lambda: _alert_booking_failed(config))
        _best_effort("booking failure notification", lambda: notify.notify_owner(
            config,
            title="A booking could not be saved: call them back",
            body=f"{caller_name} ({caller_phone}) wanted {service} on {date} at {time}. The system could not save it and the caller was told someone will call back.",
        ))
        return {"success": False, "error": "that action failed on our end: the booking system had a problem and nothing was saved. Do not say it is booked. Apologize, confirm the caller's name and best number, and call escalate_to_human with reason callback_requested."}

    when = start.strftime("%A %b %d at %I:%M %p").replace(" 0", " ")

    # The booking is saved. Nothing below may turn that into a reported failure: the caller would be told it failed
    # while the owner's calendar holds it. Each side effect is best effort and a failure is logged and counted.
    if config.google_calendar_id:
        _best_effort("calendar write", lambda: gcal.create_event_in_background(
            config.google_calendar_id,
            summary=f"{service}: {caller_name}",
            description=f"Booked by the AI receptionist. Caller: {caller_name}, {caller_phone}.",
            start=start,
            end=end,
            tz_name=config.timezone,
        ))

    confirmation_text_sent = False
    if notify.sms_enabled():
        def _text() -> bool:
            from app.twilio_utils import send_sms

            return send_sms(
                to=caller_phone,
                body=(
                    f"{config.business_name}: you're booked for {service} on "
                    f"{when}. Need to change it? Just call this number back. Reply STOP to opt out."
                ),
            )

        confirmation_text_sent = bool(_best_effort("confirmation text", _text))

    # A booking that only exists in the database is invisible in practice.
    def _tell_owner() -> None:
        ics = notify.build_ics(
            config=config,
            caller_name=caller_name,
            caller_phone=caller_phone,
            service=service,
            start=start,
            end=end,
            organizer=os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or brand.get().fallback_sender,
            uid=uid,
        )
        notify.notify_owner(
            config,
            title="New booking",
            body=leadcard.booking_card(name=caller_name, phone=caller_phone, service=service, when=when),
            ics=ics,
        )

    _best_effort("owner notification", _tell_owner)

    webhooks.emit(config, "booking.created", {
        "booking_id": booking_id, "caller_name": caller_name, "caller_phone": caller_phone, "service": service,
        "start": slot_start_iso, "end": slot_end_iso, "timezone": config.timezone, "call_sid": call_sid,
    }, event_id=f"booking.created:{config.client_id}:{booking_id}")
    return {
        "success": True,
        "booking_id": booking_id,
        "confirmed_start": slot_start_iso,
        "confirmed_end": slot_end_iso,
        "confirmation_text_sent": confirmation_text_sent,
    }


MAX_BOOKINGS_PER_CALL = 3


def _call_booking_cap_reached(config: ClientConfig, call_sid: str, slot_start_iso: str) -> bool:
    """One call cannot book an unbounded number of slots (an abusive caller or a looping model). Repeating the SAME slot is the
    idempotent case handled by the conflict path, so it is never counted against the cap."""
    try:
        same = storage.get_booking_at(config.client_id, slot_start_iso)
        if same and same["call_sid"] == call_sid:
            return False
        return storage.count_call_bookings(call_sid) >= MAX_BOOKINGS_PER_CALL
    except Exception:
        logger.exception("Could not count bookings for call_sid=%s", call_sid)
        return False


def _alert_booking_failed(config: ClientConfig) -> None:
    from app import ops

    ops.alert_operator(
        "Booking could not be saved on a live call",
        f"{config.business_name}: the database refused a booking write; the caller was told it was not booked and the owner was sent the details to call back. "
        "Check the Fly volume (disk full, read-only) and the logs.",
        key="booking-write",
    )


def _best_effort(what: str, fn):
    """Run a side effect that must never break the caller's call. A failure is logged and counted, and returns None."""
    try:
        return fn()
    except Exception:
        logger.exception("Best-effort step failed: %s", what)
        try:
            storage.record_metric("side_effect_failed", what)
        except Exception:
            pass
        return None


def _callback_event_id(config: ClientConfig, call_sid: str | None, reason: str, caller_name: str | None,
                       caller_phone: str | None, summary: str) -> str:
    """Idempotency key for callback.requested. Per call and reason when the call is known; otherwise a hash of the stable
    content (never random, never "None"), so a retry of the same request cannot queue a second event."""
    if call_sid:
        return f"callback.requested:{config.client_id}:{call_sid}:{reason}"
    digest = hashlib.sha256("".join((reason, caller_name or "", caller_phone or "", summary)).encode()).hexdigest()[:16]
    return f"callback.requested:{config.client_id}:{digest}"


def escalate_to_human(
    *,
    call_sid: str | None,
    config: ClientConfig,
    reason: str,
    caller_name: str | None,
    caller_phone: str | None,
    summary: str,
) -> dict:
    reason, summary = clean_text(reason, 60) or "unspecified", scrub_sensitive(clean_text(summary, 600))
    caller_name, caller_phone = clean_text(caller_name, 80) or None, clean_text(caller_phone, 30) or None
    # Every channel is tried independently: a database that cannot be written must not stop the owner being told,
    # and a mail outage must not stop the record being kept.
    recorded = _best_effort("escalation record", lambda: storage.log_escalation(
        call_sid=call_sid,
        client_id=config.client_id,
        reason=reason,
        caller_phone=caller_phone,
        summary=summary,
    ) or True)
    # The owner's text/email is a one-screen lead card (app/leadcard.py). A caller ID the owner marked "not a customer" does not page them,
    # but a possible emergency always does, and the callback is still recorded above.
    urgent = leadcard.is_urgent(reason)
    if urgent or not spamtag.call_is_spam(config.client_id, call_sid):
        _best_effort("escalation notification", lambda: notify.notify_owner(
            config,
            title=f"URGENT callback ({reason})" if urgent else f"Needs a callback ({reason})",
            body=leadcard.callback_card(name=caller_name, phone=caller_phone, reason=reason, summary=summary),
        ))
    webhooks.emit(config, "callback.requested", {
        "reason": reason, "caller_name": caller_name, "caller_phone": caller_phone, "summary": summary, "call_sid": call_sid,
    }, event_id=_callback_event_id(config, call_sid, reason, caller_name, caller_phone, summary))
    return {"escalated": True, "recorded": bool(recorded)}


# ---------------------------------------------------------------------------
# Changing an appointment. A caller may only touch appointments made with the phone number they are
# calling from (verified caller ID), never one they merely state: otherwise anyone could cancel a
# stranger's job by knowing a phone number. The owner is told about every change.

def _spoken_when(start: datetime) -> str:
    return start.strftime("%A %b %d at %I:%M %p").replace(" 0", " ")


def _parse_slot(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M")


def _owned(config: ClientConfig, booking_id: int, call_sid: str | None, caller_id: str | None) -> dict | None:
    b = storage.get_booking(int(booking_id)) if str(booking_id).lstrip("-").isdigit() else None
    if b is None or b["client_id"] != config.client_id:
        return None
    same_call = bool(call_sid) and b["call_sid"] == call_sid
    same_phone = len(storage._last10(caller_id)) == 10 and storage._last10(b["caller_phone"]) == storage._last10(caller_id)
    return b if (same_call or same_phone) else None


def find_my_appointments(*, config: ClientConfig, caller_id: str | None) -> dict:
    """Upcoming appointments made with the number the caller is calling from."""
    if len(storage._last10(caller_id)) < 10:
        return {"error": "caller ID is not available, so appointments cannot be looked up. Take their details and escalate_to_human."}
    now = _local_now(config).strftime("%Y-%m-%dT%H:%M")
    found = storage.find_upcoming_bookings(config.client_id, caller_id or "", now)
    return {"appointments": [
        {"booking_id": b["id"], "service": b["service"], "start": b["slot_start"], "when": _spoken_when(_parse_slot(b["slot_start"]))}
        for b in found
    ]}


def _change_email(config: ClientConfig, b: dict, *, method: str, start: datetime, end: datetime, sequence: int) -> str:
    return notify.build_ics(
        config=config, caller_name=b["caller_name"], caller_phone=b["caller_phone"], service=b["service"], start=start, end=end,
        organizer=os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or brand.get().fallback_sender,
        uid=b["uid"], method=method, sequence=sequence,
    )


def cancel_appointment(*, config: ClientConfig, call_sid: str | None, booking_id: int, caller_id: str | None) -> dict:
    b = _owned(config, booking_id, call_sid, caller_id)
    if b is None:
        return {"success": False, "error": "no appointment with that id was found for this caller"}
    done = storage.cancel_booking(b["id"], by_call_sid=call_sid)
    if done is None:
        return {"success": False, "error": "that appointment was already cancelled"}
    start, end = _parse_slot(b["slot_start"]), _parse_slot(b["slot_end"])
    notify.notify_owner(
        config, title="Booking cancelled",
        body=f"{b['caller_name']} ({b['caller_phone']}) cancelled {b['service']} on {_spoken_when(start)}.",
        ics=_change_email(config, b, method="CANCEL", start=start, end=end, sequence=int(datetime.now().timestamp())),
    )
    webhooks.emit(config, "booking.cancelled", {
        "booking_id": b["id"], "caller_name": b["caller_name"], "caller_phone": b["caller_phone"], "service": b["service"],
        "start": b["slot_start"], "end": b["slot_end"], "timezone": config.timezone, "call_sid": call_sid,
    }, event_id=f"booking.cancelled:{config.client_id}:{b['id']}")
    return {"success": True, "cancelled": b["service"], "was": _spoken_when(start)}


def reschedule_appointment(
    *, config: ClientConfig, call_sid: str | None, booking_id: int, new_date: str, new_time: str, caller_id: str | None
) -> dict:
    b = _owned(config, booking_id, call_sid, caller_id)
    if b is None:
        return {"success": False, "error": "no appointment with that id was found for this caller"}
    try:
        start = datetime.strptime(f"{new_date} {new_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        return {"success": False, "error": "invalid date/time format, expected YYYY-MM-DD and HH:MM"}
    if start not in _slot_grid(config, start.replace(hour=0, minute=0)):
        return {"success": False, "error": "that time isn't one of the bookable slots; call check_availability and offer one of those"}
    if start < _local_now(config) + timedelta(minutes=MIN_LEAD_MINUTES):
        return {"success": False, "error": "that time has already passed or is too soon; offer a later slot"}
    old_start, old_end = _parse_slot(b["slot_start"]), _parse_slot(b["slot_end"])
    if start == old_start:
        return {"success": True, "confirmed_start": b["slot_start"], "when": _spoken_when(start), "unchanged": True}
    end = start + (old_end - old_start)
    closes = _closes_at(config, start)
    if closes is not None and end > closes:
        return {"success": False, "error": "that job would run past closing time. Offer an earlier slot (pass the service to check_availability)."}
    if _external_free(config, start, end) is False:
        return {"success": False, "error": "that time just got taken on the owner's calendar, offer a different time"}
    try:
        moved = storage.move_booking(b["id"], start.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M"), by_call_sid=call_sid)
    except storage.BookingConflict:
        return {"success": False, "error": "that slot was just taken, offer the caller a different time"}
    notify.notify_owner(
        config, title="Booking moved",
        body=f"{b['caller_name']} ({b['caller_phone']}) moved {b['service']} from {_spoken_when(old_start)} to {_spoken_when(start)}.",
        ics=_change_email(config, b, method="REQUEST", start=start, end=end, sequence=int(datetime.now().timestamp())),
    )
    webhooks.emit(config, "booking.updated", {
        "booking_id": b["id"], "caller_name": b["caller_name"], "caller_phone": b["caller_phone"], "service": b["service"],
        "start": moved["slot_start"], "end": moved["slot_end"], "previous_start": moved["old_start"], "timezone": config.timezone,
        "call_sid": call_sid,
    }, event_id=f"booking.updated:{config.client_id}:{b['id']}:{moved['slot_start']}:{call_sid or 'from-' + str(moved['old_start'])}")
    return {"success": True, "confirmed_start": moved["slot_start"], "when": _spoken_when(start)}
