"""When calls came in: business hours vs after hours, using the client's OWN configured hours, and what came of each group.

Recorded facts only. Each call is counted once from the outcome class the system recorded; spam the owner marked is left out (it is not a customer).
There is no dollar figure here on purpose: a booking or a message is not revenue. Holidays are not modelled (only the weekly hours the client gave us).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_LEFT_DETAILS = frozenset({"CALLBACK_REQUESTED", "LEAD_CAPTURED", "AFTER_HOURS_MESSAGE", "TRANSFER_FAILED"})


def _empty() -> dict:
    return {"calls": 0, "booked": 0, "left_details": 0, "hung_up": 0, "waiting": 0}


def split(config, rows) -> dict:
    """rows: (started_at_iso, outcome_class, needs_attention, attention_resolved_at). Returns {'business': {...}, 'after': {...}}."""
    from app.digest import _is_after_hours

    tz = ZoneInfo(config.timezone)
    out = {"business": _empty(), "after": _empty()}
    for started_at, cls, needs, resolved in rows:
        if cls == "SPAM":
            continue
        try:
            t = datetime.fromisoformat(started_at)
        except (TypeError, ValueError):
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        bucket = out["after" if _is_after_hours(config, t.astimezone(tz)) else "business"]
        bucket["calls"] += 1
        if cls == "BOOKED":
            bucket["booked"] += 1
        elif cls in _LEFT_DETAILS:
            bucket["left_details"] += 1
        elif cls == "ABANDONED":
            bucket["hung_up"] += 1
        if needs and not resolved:
            bucket["waiting"] += 1
    return out
