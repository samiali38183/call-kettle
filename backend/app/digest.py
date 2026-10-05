"""Weekly "what your AI receptionist did" email.

A client paying every month who never hears from the service eventually asks
what they're paying for. This sends a short, factual recap each Monday so the
value is visible without anyone logging in: calls answered, how many came in
outside business hours, appointments booked, callbacks, and what's coming up.

Only counts what's in the database. No revenue guesses, no promises.
Sent at most once per client per week (tracked in `digests_sent`), and only
marked sent when the email actually went out, so if email isn't configured yet
the recap goes out as soon as it is. Nothing here is on the call path.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sqlite3
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import brand, notify, outcomes, storage
from app.config import ClientConfig, list_client_ids, load_client_config

logger = logging.getLogger("callkettle.digest")

SEND_AFTER_LOCAL = dtime(8, 0)  # Monday 8am in the client's timezone
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Clients that aren't real customers never get a recap.
SKIP_CLIENTS = {"callkettle_demo", "demo_dental", "demo_hvac"}


def ensure_table() -> None:
    with storage._conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS digests_sent ("
            "client_id TEXT NOT NULL, period TEXT NOT NULL, sent_at TEXT NOT NULL, "
            "PRIMARY KEY (client_id, period))"
        )


def _already_sent(client_id: str, period: str) -> bool:
    with storage._conn() as conn:
        return conn.execute(
            "SELECT 1 FROM digests_sent WHERE client_id = ? AND period = ?", (client_id, period)
        ).fetchone() is not None


def _mark_sent(client_id: str, period: str) -> None:
    with storage._conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO digests_sent (client_id, period, sent_at) VALUES (?, ?, ?)",
            (client_id, period, datetime.now(timezone.utc).isoformat()),
        )


def _is_after_hours(config: ClientConfig, local: datetime) -> bool:
    hours = config.business_hours.get(_DAYS[local.weekday()], "closed")
    if hours == "closed":
        return True
    try:
        opens = datetime.strptime(hours[0], "%H:%M").time()
        closes = datetime.strptime(hours[1], "%H:%M").time()
    except (ValueError, IndexError):
        return False
    return not (opens <= local.time() < closes)


def week_bounds(now_utc: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The last full Monday-to-Sunday week (client-local), as aware datetimes."""
    local_now = now_utc.astimezone(tz)
    this_monday = (local_now - timedelta(days=local_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return this_monday - timedelta(days=7), this_monday


def week_stats(config: ClientConfig, start: datetime, end: datetime) -> dict:
    tz = ZoneInfo(config.timezone)
    s, e = start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()
    conn = sqlite3.connect(storage.DB_PATH)
    try:
        calls = conn.execute(
            "SELECT started_at, outcome, outcome_class, needs_attention, attention_resolved_at FROM calls WHERE client_id = ? AND started_at >= ? AND started_at < ?",
            (config.client_id, s, e),
        ).fetchall()
        booked = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE client_id = ? AND status = 'confirmed' "
            "AND created_at >= ? AND created_at < ?",
            (config.client_id, s, e),
        ).fetchone()[0]
        callbacks = conn.execute(
            "SELECT COUNT(*) FROM escalations WHERE client_id = ? AND created_at >= ? AND created_at < ?",
            (config.client_id, s, e),
        ).fetchone()[0]
    finally:
        conn.close()
    from app import callhours

    after = callhours.split(config, [(r[0], r[2], r[3], r[4]) for r in calls])["after"]      # spam the owner marked is not a customer call
    return {
        "calls": len(calls),
        "after_hours": after["calls"],
        "after_hours_booked": after["booked"],
        "after_hours_left_details": after["left_details"],
        "after_hours_hung_up": after["hung_up"],
        "booked": int(booked),
        "callbacks": int(callbacks),
        "transferred": sum(1 for c in calls if c[1] == "transferred"),
    }


def upcoming_bookings(config: ClientConfig, now_utc: datetime, days: int = 7, limit: int = 8) -> list[tuple]:
    tz = ZoneInfo(config.timezone)
    local_now = now_utc.astimezone(tz).replace(tzinfo=None)
    lo = local_now.strftime("%Y-%m-%dT%H:%M")
    hi = (local_now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
    conn = sqlite3.connect(storage.DB_PATH)
    try:
        return conn.execute(
            "SELECT slot_start, caller_name, service FROM bookings WHERE client_id = ? AND status = 'confirmed' "
            "AND slot_start >= ? AND slot_start < ? ORDER BY slot_start LIMIT ?",
            (config.client_id, lo, hi, limit),
        ).fetchall()
    finally:
        conn.close()


STALE_AFTER_HOURS = 24
STALE_LISTED = 5


def week_outcomes(config: ClientConfig, start: datetime, end: datetime) -> dict[str, int]:
    """Calls in the window by recorded outcome_class. Calls with no class (still in progress, or recorded before classes existed) are counted under None."""
    s, e = start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()
    conn = sqlite3.connect(storage.DB_PATH)
    try:
        rows = conn.execute(
            "SELECT outcome_class, COUNT(*) FROM calls WHERE client_id = ? AND started_at >= ? AND started_at < ? GROUP BY outcome_class",
            (config.client_id, s, e),
        ).fetchall()
    finally:
        conn.close()
    return {cls: int(n) for cls, n in rows}


def stale_attention(config: ClientConfig, now_utc: datetime, hours: int = STALE_AFTER_HOURS) -> list[dict]:
    """Follow-ups flagged needs-attention that nobody has marked handled and that are older than `hours`, oldest first. Not limited to the recap week:
    a callback from three weeks ago that is still open is exactly what the owner should hear about."""
    cutoff = (now_utc - timedelta(hours=hours)).astimezone(timezone.utc).isoformat()
    conn = sqlite3.connect(storage.DB_PATH)
    try:
        rows = conn.execute(
            "SELECT started_at, from_number, outcome_class, summary FROM calls WHERE client_id = ? AND needs_attention = 1 "
            "AND attention_resolved_at IS NULL AND started_at < ? ORDER BY started_at",
            (config.client_id, cutoff),
        ).fetchall()
    finally:
        conn.close()
    items = []
    for started_at, number, cls, summary in rows:
        try:
            t = datetime.fromisoformat(started_at)
        except (TypeError, ValueError):
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        items.append({"started_at": t, "age_days": max(0, (now_utc - t).days), "number": number or "", "class": cls, "summary": summary or ""})
    return items


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def dashboard_link(client_id: str) -> str | None:
    master = os.environ.get("REPORT_KEY", "")
    if not master:
        return None
    key = hmac.new(master.encode(), client_id.encode(), hashlib.sha256).hexdigest()[:24]
    base = os.environ.get("APP_BASE_URL", "https://app.callkettle.com")
    return f"{base}/report/{client_id}?key={key}"


def _day(d: datetime) -> str:
    return d.strftime("%b %d").replace(" 0", " ")


def _outcome_lines(counts: dict[str, int]) -> list[str]:
    if not counts:
        return ["", "Call outcomes: no data recorded for this week."]
    lines = ["", "What happened on those calls (as recorded):"]
    for cls, n in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        lines.append(f"  {outcomes.LABELS.get(cls, cls) if cls else 'Not classified':.<27} {n}")
    return lines


def _stale_lines(stale: list[dict]) -> list[str]:
    if not stale:
        return ["", f"Follow-ups waiting more than {STALE_AFTER_HOURS} hours: none recorded."]
    lines = ["", f"Follow-ups still open after {STALE_AFTER_HOURS}+ hours: {len(stale)}. Open the link below and press Mark handled once you have dealt with each."]
    for item in stale[:STALE_LISTED]:
        label = outcomes.LABELS.get(item["class"] or "", "Needs a look")
        what = brand_one_line(item["summary"])
        lines.append(f"  {_day(item['started_at'])} ({_plural(item['age_days'], 'day')} ago)  {item['number']}  {label}" + (f": {what}" if what else ""))
    if len(stale) > STALE_LISTED:
        lines.append(f"  ...and {len(stale) - STALE_LISTED} more on the dashboard.")
    return lines


def brand_one_line(text: str, limit: int = 120) -> str:
    return notify.one_line(text, limit)


def compose(config: ClientConfig, stats: dict, start: datetime, end: datetime, upcoming: list[tuple],
            outcome_counts: dict[str, int] | None = None, stale: list[dict] | None = None) -> tuple[str, str]:
    """`outcome_counts` / `stale` of None leave those sections out; an empty value prints an explicit 'no data' / 'none recorded' line."""
    last_day = end - timedelta(days=1)
    span = f"{_day(start)} to {_day(last_day)}"
    bits = [f"{_plural(stats['calls'], 'call')} answered"]
    if stats["booked"]:
        bits.append(f"{stats['booked']} booked")
    if stale:
        bits.append(f"{_plural(len(stale), 'follow-up')} waiting")
    subject = f"[{config.business_name}] Your week with {brand.get().name}: " + ", ".join(bits)

    lines = [f"Here's what your AI receptionist did for {config.business_name}, {span}.", ""]
    glance = f"This week at a glance: {_plural(stats['calls'], 'call')} ({stats['after_hours']} after hours), {stats['booked']} booked"
    if stale is not None:
        glance += (f", {_plural(len(stale), 'caller')} still waiting on you more than {STALE_AFTER_HOURS} hours." if stale
                   else f", no callers waiting on you more than {STALE_AFTER_HOURS} hours.")
    else:
        glance += "."
    lines += [glance, ""]
    lines.append(f"  Calls answered ............ {stats['calls']}")
    lines.append(f"  Outside business hours .... {stats['after_hours']}")
    lines.append(f"  Appointments booked ....... {stats['booked']}")
    lines.append(f"  Messages / callbacks ...... {stats['callbacks']}")
    lines.append(f"  Put through to you live ... {stats['transferred']}")
    if stats["after_hours"]:
        lines += ["", f"{_plural(stats['after_hours'], 'call')} came in while you were closed. "
                      "Those are the calls that often end up in voicemail."]
        if "after_hours_booked" in stats:
            known = stats["after_hours_booked"] + stats["after_hours_left_details"] + stats["after_hours_hung_up"]
            rest = " (the rest have other or unclassified outcomes)" if known < stats["after_hours"] else ""
            lines.append(f"Of the {stats['after_hours']} after-hours calls: {stats['after_hours_booked']} booked, "
                         f"{stats['after_hours_left_details']} left details, {stats['after_hours_hung_up']} hung up{rest}.")
    if outcome_counts is not None:
        lines += _outcome_lines(outcome_counts)
    if stale is not None:
        lines += _stale_lines(stale)
    if upcoming:
        lines += ["", "Coming up in the next 7 days:"]
        for slot, name, service in upcoming:
            try:
                when = datetime.strptime(slot, "%Y-%m-%dT%H:%M").strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
            except ValueError:
                when = slot
            lines.append(f"  {when}  {name}  ({service})")
    base = os.environ.get("APP_BASE_URL", "https://app.callkettle.com")
    lines += ["", f"Sign in to your owner portal for every call, booking and follow-up: {base}/portal/login"]
    link = dashboard_link(config.client_id)
    if link:
        lines += [f"(Quick link without signing in: {link})"]
    b = brand.get()
    lines += ["", f"Something you'd like changed (hours, prices, what it says)? Reply to this email or text {b.contact_name} at {b.support_phone}. "
                  "Changes are made within one business day.", "", b.name]
    return subject, "\n".join(lines) + "\n"


def preview(client_id: str, now_utc: datetime | None = None) -> tuple[str, str]:
    """The recap exactly as it would be composed right now from the database. Reads only: never sends, never marks anything sent."""
    now_utc = now_utc or datetime.now(timezone.utc)
    config = load_client_config(client_id)
    start, end = week_bounds(now_utc, ZoneInfo(config.timezone))
    return compose(config, week_stats(config, start, end), start, end, upcoming_bookings(config, now_utc),
                   week_outcomes(config, start, end), stale_attention(config, now_utc))


def send_due_digests(now_utc: datetime | None = None, client_ids: list[str] | None = None) -> list[str]:
    """Send this week's recap to every client that is due one. Returns the client
    ids emailed. Safe to call as often as you like."""
    now_utc = now_utc or datetime.now(timezone.utc)
    ensure_table()
    if client_ids is None:
        client_ids = list_client_ids()
    sent: list[str] = []
    for cid in client_ids:
        if cid in SKIP_CLIENTS:
            continue
        try:
            config = load_client_config(cid)
            if not config.owner_email or not config.weekly_recap:
                continue
            tz = ZoneInfo(config.timezone)
            local_now = now_utc.astimezone(tz)
            if local_now.weekday() == 0 and local_now.time() < SEND_AFTER_LOCAL:
                continue  # not Monday 8am yet
            start, end = week_bounds(now_utc, tz)
            period = start.strftime("%G-W%V")
            if _already_sent(cid, period):
                continue
            stats = week_stats(config, start, end)
            stale = stale_attention(config, now_utc)
            if stats["calls"] == 0 and not stale:
                _mark_sent(cid, period)  # nothing to report; don't re-check all week
                continue
            subject, body = compose(config, stats, start, end, upcoming_bookings(config, now_utc),
                                    week_outcomes(config, start, end), stale)
            if notify._send_email(config.owner_email, subject, body, kind="recap", key=f"recap:{cid}:{period}"):
                _mark_sent(cid, period)
                sent.append(cid)
        except Exception:
            logger.exception("Weekly recap failed for %s", cid)
    return sent
