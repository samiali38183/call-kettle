"""The one-screen after-call 'lead card' used as the body of the owner's text/email alert.

An owner on a ladder reads the first two lines and knows who to call, on which number, and whether it is urgent. Everything comes from what the
system recorded: the caller's name and number as given, the reason code the system itself logged (the urgent flag NEVER comes from the caller's words),
and a ZIP only when the caller actually said one. Missing facts are stated as missing, never guessed. Pure functions, no I/O, no price.
"""
from __future__ import annotations

import re

NAME_MAX, PHONE_MAX, ISSUE_MAX = 50, 20, 100
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ZIP_AFTER_LABEL = re.compile(r"\bzip(?:\s*code)?\s*[:#-]?\s*(\d{5})(?!\d)", re.IGNORECASE)
_ZIP_AFTER_STATE = re.compile(r"\b[A-Za-z]{2}\.?,?\s+(\d{5})(?:-\d{4})?(?!\d)")


def _one_line(text: object, limit: int) -> str:
    """Single line, no control characters (a newline would let caller text forge a line of the card), shortened with '...'."""
    cleaned = " ".join(_CONTROL.sub(" ", str(text or "")).split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3].rstrip() + "..."


def _phone(text: object) -> str:
    return " ".join(re.sub(r"[^0-9+()\-. ]", "", str(text or "")).split())[:PHONE_MAX]


def _issue(summary: object) -> str:
    """The first sentence of the recorded summary, as one line."""
    text = _one_line(summary, 400)
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0] if text else ""
    return _one_line(first, ISSUE_MAX)


def zip_from(summary: object) -> str | None:
    """A ZIP only if the summary states one (after the word ZIP, or after a state abbreviation). Never inferred from a bare number."""
    text = _one_line(summary, 600)
    match = _ZIP_AFTER_LABEL.search(text) or _ZIP_AFTER_STATE.search(text)
    return match.group(1) if match else None


def is_urgent(reason: object) -> bool:
    return "possible_emergency" in str(reason or "").lower()


def _who(name: str, phone: str) -> str:
    if name and phone:
        return f"{name} {phone}"
    return f"{name or 'name not given'}, {phone or 'number not captured'}"


def callback_card(*, name: object, phone: object, reason: object, summary: object) -> str:
    lines = []
    if is_urgent(reason):
        lines.append("URGENT - possible emergency, call first")
    lines.append("Call: " + _who(_one_line(name, NAME_MAX), _phone(phone)))
    lines.append("Issue: " + (_issue(summary) or "not recorded"))
    zip_code = zip_from(summary)
    lines.append(f"Where: ZIP {zip_code} (as the caller said it)" if zip_code else "Where: not captured")
    return "\n".join(lines)


def booking_card(*, name: object, phone: object, service: object, when: object) -> str:
    return "\n".join([
        "Booked: " + _who(_one_line(name, NAME_MAX), _phone(phone)),
        "Service: " + (_one_line(service, ISSUE_MAX) or "not recorded"),
        "When: " + (_one_line(when, 60) or "not recorded"),
    ])
