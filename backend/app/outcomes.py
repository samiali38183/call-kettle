"""What happened on a call, in the owner's terms, and which calls need a human to follow up.

The class is derived from facts the system itself recorded (a saved booking, an escalation reason, how the call ended), never from the
model's own opinion of how it went. `classify` is a pure function so it can be tested without a database.
"""
from __future__ import annotations

import hashlib
import json

FAQ_RESOLVED = "FAQ_RESOLVED"
LEAD_CAPTURED = "LEAD_CAPTURED"
BOOKED = "BOOKED"
RESCHEDULED = "RESCHEDULED"
CANCELLED = "CANCELLED"
CALLBACK_REQUESTED = "CALLBACK_REQUESTED"
TRANSFERRED = "TRANSFERRED"
TRANSFER_FAILED = "TRANSFER_FAILED"
OUTSIDE_SERVICE_AREA = "OUTSIDE_SERVICE_AREA"
SERVICE_NOT_OFFERED = "SERVICE_NOT_OFFERED"
AFTER_HOURS_MESSAGE = "AFTER_HOURS_MESSAGE"
EMERGENCY_ESCALATED = "EMERGENCY_ESCALATED"
ABANDONED = "ABANDONED"
SPAM = "SPAM"          # reserved: nothing sets it automatically yet (the repeat-caller brake hangs up before a call is recorded)
AI_FAILURE = "AI_FAILURE"
UNKNOWN = "UNKNOWN"

OUTCOMES = (
    FAQ_RESOLVED, LEAD_CAPTURED, BOOKED, RESCHEDULED, CANCELLED, CALLBACK_REQUESTED, TRANSFERRED, TRANSFER_FAILED,
    OUTSIDE_SERVICE_AREA, SERVICE_NOT_OFFERED, AFTER_HOURS_MESSAGE, EMERGENCY_ESCALATED, ABANDONED, SPAM, AI_FAILURE, UNKNOWN,
)

# A person should look at these. Bookings and resolved questions are already in the owner's alerts and recap.
NEEDS_ATTENTION = frozenset({CALLBACK_REQUESTED, LEAD_CAPTURED, TRANSFER_FAILED, AFTER_HOURS_MESSAGE, AI_FAILURE, EMERGENCY_ESCALATED})

LABELS = {
    FAQ_RESOLVED: "Question answered", LEAD_CAPTURED: "Lead captured", BOOKED: "Booked", RESCHEDULED: "Rescheduled",
    CANCELLED: "Cancelled", CALLBACK_REQUESTED: "Callback requested", TRANSFERRED: "Put through", TRANSFER_FAILED: "Put-through failed",
    OUTSIDE_SERVICE_AREA: "Outside service area", SERVICE_NOT_OFFERED: "Service not offered", AFTER_HOURS_MESSAGE: "After-hours message",
    EMERGENCY_ESCALATED: "Possible emergency", ABANDONED: "Hung up", SPAM: "Spam", AI_FAILURE: "Assistant problem", UNKNOWN: "Unclear",
}

_FAILURE_REASONS = ("agent_error", "blocked_false_confirmation", "max_turns_reached", "max_duration_reached", "limit_reached")


def classify(*, outcome: str | None, caller_turns: int, booked: int, rescheduled: int, cancelled: int, reasons: list[str], ended_mid_question: bool = False) -> tuple[str, bool]:
    """(class, needs_attention) from what the system recorded. `ended_mid_question`: the caller hung up while the assistant was still asking them something
    (for example "keep tomorrow's time or move it?"), so even a booked call may have been left unsettled and a person should check."""
    reasons = [r.lower() for r in reasons]

    def has(*words: str) -> bool:
        return any(w in r for r in reasons for w in words)

    if has("possible_emergency"):
        cls = EMERGENCY_ESCALATED
    elif outcome == "transfer_unanswered":
        cls = CALLBACK_REQUESTED if has("callback", "message") else TRANSFER_FAILED
    elif outcome in ("transferred", "owner_answered"):
        cls = TRANSFERRED
    elif booked:
        cls = BOOKED
    elif rescheduled:
        cls = RESCHEDULED
    elif cancelled:
        cls = CANCELLED
    elif has("outside_service_area"):
        cls = OUTSIDE_SERVICE_AREA
    elif has("service_not_offered"):
        cls = SERVICE_NOT_OFFERED
    elif has("after_hours"):
        cls = AFTER_HOURS_MESSAGE
    elif has(*_FAILURE_REASONS):
        cls = AI_FAILURE
    elif has("callback", "message", "non_english", "over_limit"):
        cls = CALLBACK_REQUESTED
    elif reasons:
        cls = LEAD_CAPTURED
    elif outcome is None:
        cls = UNKNOWN
    elif caller_turns == 0 or (caller_turns <= 1 and outcome != "completed"):
        cls = ABANDONED
    elif outcome == "completed":
        cls = FAQ_RESOLVED
    else:
        cls = UNKNOWN
    attention = cls in NEEDS_ATTENTION or (cls == UNKNOWN and outcome is not None and caller_turns >= 2)
    if ended_mid_question and caller_turns >= 2 and cls in (BOOKED, RESCHEDULED, CANCELLED, FAQ_RESOLVED, UNKNOWN):
        attention = True
    return cls, attention


def fingerprint(obj) -> str:
    """A short stable hash, so a call can say exactly which config, prompt and tool schema it ran with."""
    raw = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def weekly_summary(rows: list[tuple[str, str | None, int]], tz, weeks: int = 8, now=None) -> list[dict]:
    """Outcomes by week from (started_at_iso_utc, outcome_class, needs_attention) rows, newest week first, empty weeks included so a quiet week is visible.
    Everything here is MEASURED (recorded by the system). It deliberately has no revenue: a booking, a lead or a callback is not revenue."""
    from datetime import datetime, timedelta, timezone

    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for i in range(weeks):
        start = monday - timedelta(weeks=i)
        out.append({"week_start": start.date().isoformat(), "calls": 0, "after_hours": 0, "booked": 0, "rescheduled_or_cancelled": 0, "callbacks_and_leads": 0,
                    "put_through": 0, "answered_questions": 0, "needs_attention": 0, "unclassified": 0, "_start": start, "_end": start + timedelta(weeks=1)})
    for started_at, cls, attention in rows:
        try:
            t = datetime.fromisoformat(started_at)
        except (TypeError, ValueError):
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        t = t.astimezone(tz)
        for w in out:
            if w["_start"] <= t < w["_end"]:
                w["calls"] += 1
                if cls is None:
                    w["unclassified"] += 1
                elif cls == BOOKED:
                    w["booked"] += 1
                elif cls in (RESCHEDULED, CANCELLED):
                    w["rescheduled_or_cancelled"] += 1
                elif cls in (CALLBACK_REQUESTED, LEAD_CAPTURED, AFTER_HOURS_MESSAGE, TRANSFER_FAILED):
                    w["callbacks_and_leads"] += 1
                elif cls in (TRANSFERRED, EMERGENCY_ESCALATED):
                    w["put_through"] += 1
                elif cls == FAQ_RESOLVED:
                    w["answered_questions"] += 1
                if attention:
                    w["needs_attention"] += 1
                if t.hour < 8 or t.hour >= 18 or t.weekday() >= 5:      # a crude after-hours proxy; the client's own hours are not applied here
                    w["after_hours"] += 1
                break
    for w in out:
        w.pop("_start"), w.pop("_end")
    return out
