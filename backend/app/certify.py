"""Readiness certification for one client: the pure configuration checks (no network, no model).

The live part (synthetic calls against a disposable copy of the client, database-state assertions, phone-number wiring)
is scripts/certify_client.py, which calls lint() first. Verdict rules:

    any FAIL                     -> BLOCKED
    no FAIL, at least one WARN   -> READY WITH WARNINGS
    otherwise                    -> READY

A FAIL means a caller could be harmed or a customer's money/business put at risk if this client went live as configured.
A WARN means it works but something the owner would want fixed (or knows nothing about) is missing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import ClientConfig

_E164 = re.compile(r"^\+1\d{10}$")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Finding:
    level: str          # PASS | WARN | FAIL
    name: str
    detail: str


def verdict(findings: list[Finding]) -> str:
    if any(f.level == "FAIL" for f in findings):
        return "BLOCKED"
    if any(f.level == "WARN" for f in findings):
        return "READY WITH WARNINGS"
    return "READY"


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _windows(hours: dict) -> list[tuple[str, int, int]]:
    out = []
    for day in _DAYS:
        w = hours.get(day)
        if isinstance(w, list) and len(w) == 2:
            try:
                out.append((day, _minutes(w[0]), _minutes(w[1])))
            except (ValueError, TypeError):
                pass
    return out


def lint(config: ClientConfig, *, own_numbers: list[str] | None = None, hours_confirmed: bool = False) -> list[Finding]:
    """`own_numbers`: the Twilio numbers that ring this client's assistant (a transfer to one of them would loop).
    `hours_confirmed`: the owner has said these opening hours are right (a person must assert this; we cannot know)."""
    f: list[Finding] = []

    def add(level: str, name: str, detail: str) -> None:
        f.append(Finding(level, name, detail))

    # --- the number a human will be put through to
    if not _E164.match(config.escalation_phone or ""):
        add("FAIL", "Transfer number", f"{config.escalation_phone!r} is not a +1XXXXXXXXXX number; urgent calls could not be put through")
    elif own_numbers and config.escalation_phone in own_numbers:
        add("FAIL", "Transfer number", "the transfer number is the assistant's own line: an urgent call would ring back into the assistant (a loop)")
    elif re.fullmatch(r"\+1555555\d{4}", config.escalation_phone):
        add("FAIL", "Transfer number", "that is a fictional 555 number; real callers could not be put through")
    else:
        add("PASS", "Transfer number", config.escalation_phone)

    # --- hours
    try:
        ZoneInfo(config.timezone)
        add("PASS", "Time zone", config.timezone)
    except ZoneInfoNotFoundError:
        add("FAIL", "Time zone", f"{config.timezone!r} is not a valid IANA time zone: every booking time would be wrong")
    book_windows = _windows(config.effective_booking_hours)
    if not book_windows:
        add("FAIL", "Booking hours", "no day has opening hours, so nothing can ever be booked")
    else:
        bad = [d for d, a, b in book_windows if b <= a]
        longest = max(b - a for _, a, b in book_windows)
        if bad:
            add("FAIL", "Booking hours", f"closing time is not after opening time on: {', '.join(bad)}")
        else:
            add("PASS", "Booking hours", f"{len(book_windows)} open days, longest window {longest // 60}h{longest % 60:02d}")
        if not hours_confirmed:
            add("WARN", "Hours confirmed with the owner", "nobody has confirmed these hours with the owner (pass --hours-confirmed once they have)")
        tiny = [s.name for s in config.services if s.duration_minutes > longest]
        if tiny:
            add("FAIL", "Services fit the hours", f"these services are longer than any opening window and can never be booked: {', '.join(tiny)}")
    # --- services
    names = [s.name.strip().lower() for s in config.services]
    if not config.services:
        add("FAIL", "Services", "no services are configured, so the assistant cannot book anything")
    elif len(set(names)) != len(names):
        add("FAIL", "Services", "two services share a name; the booking would be ambiguous")
    elif any(s.duration_minutes <= 0 or s.duration_minutes > 12 * 60 for s in config.services):
        add("FAIL", "Services", "a service has a duration of zero, negative or over 12 hours")
    else:
        add("PASS", "Services", ", ".join(f"{s.name} ({s.duration_minutes} min)" for s in config.services))
    if config.slot_minutes not in (15, 20, 30, 45, 60, 90, 120):
        add("WARN", "Slot length", f"{config.slot_minutes}-minute slots are unusual; confirm that is intended")

    # --- how the owner hears about things
    if config.owner_email:
        add("PASS", "Owner email", "booking invites and the weekly recap go to the owner")
    else:
        add("WARN", "Owner email", "no owner_email: the owner learns about bookings only from the dashboard and the ntfy topic (if any)")
    if not config.owner_email and not config.ntfy_topic:
        add("FAIL", "Owner notification", "no way to tell the owner about a booking or an urgent call (set owner_email or ntfy_topic)")

    # --- calendar
    if config.calendar_ical_url or config.google_calendar_id:
        add("PASS", "Calendar", "the owner's calendar is connected, so existing jobs block those times")
    else:
        add("WARN", "Calendar", "no calendar connected: the assistant cannot see the owner's own jobs, so it can offer a time that is already taken on their calendar")

    # --- the opening line and what is announced
    if config.business_name.lower().split()[0] not in config.opening_line.lower():
        add("WARN", "Greeting", "the opening line does not say the business name")
    else:
        add("PASS", "Greeting", "names the business and discloses the AI (enforced by validation)")
    if not config.faqs:
        add("WARN", "Answers", "no FAQs: every question about prices, area and hours will be handed to the owner")

    # --- who answers first
    if config.routing_mode != "ai_first" or config.always_ring_owner:
        extra = "" if config.transfer_screening else " Their voicemail may pick up before the assistant gets the call (turn on transfer_screening once verified)."
        add("WARN", "Routing", f"mode '{config.routing_mode}': the owner's phone rings first for {config.owner_ring_seconds}s. Their phone must NOT forward ALL calls to us (the ring would loop); use forward-when-no-answer or a dedicated number.{extra}")
    else:
        add("PASS", "Routing", "the assistant answers every call")

    # --- limits and degraded behavior
    if config.ceiling_mode == "transfer":
        add("WARN", "Over-limit behavior", "after the monthly limit callers are put through to the owner's phone; 'message' mode takes a message instead and is the safer default")
    else:
        add("PASS", "Over-limit behavior", "takes a message at no AI cost")
    if config.record_transcripts is False:
        add("PASS", "Privacy", "transcripts are not stored for this client")
    if config.max_call_seconds > 900 or config.max_turns > 30:
        add("WARN", "Call length limits", "long calls cost real money; confirm the limits are intended")

    # --- webhook secret quality (validated elsewhere; this is about hygiene)
    if config.webhook_url and config.webhook_secret and len(set(config.webhook_secret)) < 6:
        add("WARN", "Webhook secret", "the secret is guessable (too few distinct characters)")

    # --- leftovers that should never ship
    text = " ".join([config.opening_line, config.extra_instructions] + [f.a for f in config.faqs])
    if re.search(r"lorem ipsum|TODO|TBD|\bXXX\b|example\.com|555-01\d\d", text, re.I):
        add("FAIL", "Placeholder text", "the greeting, instructions or FAQs still contain placeholder text (TODO / lorem ipsum / example.com / 555 numbers)")
    else:
        add("PASS", "Placeholder text", "none found")
    return f


def render(client_id: str, findings: list[Finding], extra: list[Finding] | None = None, when: datetime | None = None) -> str:
    allf = list(findings) + list(extra or [])
    when = when or datetime.now()
    lines = [f"# Readiness certification: {client_id}", "", f"**Verdict: {verdict(allf)}**", f"Run {when:%Y-%m-%d %H:%M}.", "",
             "| Result | Check | Detail |", "|---|---|---|"]
    for x in allf:
        lines.append(f"| {x.level} | {x.name} | {x.detail.replace('|', '/')} |")
    lines += ["", "FAIL = could harm a caller or the customer's business if live as configured. WARN = works, but the owner would want it fixed.",
              "A certification covers the configuration and the checks run on that date. It is not a guarantee about future calls."]
    return "\n".join(lines) + "\n"


# ---- the customer-facing readiness summary: the same checks, in the words an owner uses. No score, no percentage: each line is a result or "not tested".
_SUMMARY_LINES = (
    ("BUSINESS HOURS", ("Booking hours", "Hours confirmed with the owner", "Time zone")),
    ("SERVICE RULES", ("Services", "Services fit the hours", "Slot length")),
    ("BOOKING", ("Books exactly one in-hours appointment",)),
    ("RESCHEDULE", ("Moving an appointment leaves exactly one booking",)),
    ("CANCEL", ("Cancelling frees the slot",)),
    ("HUMAN HANDOFF", ("Asks for a human", "Owner does not answer")),
    ("EMERGENCIES (911 FIRST)", ("Emergency: says 911 first",)),
    ("AFTER HOURS", ("Every booking made falls inside booking hours", "Routing")),
    ("OWNER ALERTS", ("Owner notification", "Owner email")),
    ("HONESTY (SAYS IT IS AN AI, NO MADE-UP BOOKINGS)", ("Answers and discloses it is an AI", "Honest about being an AI")),
    ("FAILURE FALLBACK", ("Over-limit behavior", "Number ")),
)


def readiness_summary(config: ClientConfig, findings: list[Finding]) -> str:
    """The owner's view of a certification. A line is PASS when every check behind it passed, NEEDS ATTENTION when one warned, BLOCKED when one failed,
    and NOT TESTED when none ran (for example a run without the conversation checks). Spanish is only ever 'enabled' or 'not enabled': it has its own test."""
    def status(names: tuple[str, ...]) -> tuple[str, str]:
        hit = [x for x in findings if any(x.name.startswith(n) for n in names)]
        if not hit:
            return "NOT TESTED", "no check ran for this"
        if any(x.level == "FAIL" for x in hit):
            return "BLOCKED", "; ".join(x.detail for x in hit if x.level == "FAIL")[:140]
        if any(x.level == "WARN" for x in hit):
            return "NEEDS ATTENTION", "; ".join(x.detail for x in hit if x.level == "WARN")[:140]
        return "PASS", ""

    lines = [f"# Readiness summary: {config.business_name}", "", f"Overall: **{verdict(findings)}**", "",
             "| Area | Result | Note |", "|---|---|---|"]
    for label, names in _SUMMARY_LINES:
        r, note = status(names)
        lines.append(f"| {label} | {r} | {note.replace('|', '/')} |")
    lines.append(f"| SPANISH | {'ENABLED (certify Spanish separately before relying on it)' if config.spanish else 'NOT ENABLED'} | |")
    lines += ["", "This summary reports what was checked on the date it was run. It is not a guarantee about future calls. "
              "A line that says NOT TESTED was not covered by the run: do not read it as passing."]
    return "\n".join(lines) + "\n"
