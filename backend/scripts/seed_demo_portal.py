"""Seed the SHOW-ME owner portal for sales demos: a FICTIONAL business (Sample Heating & Air, client `sample_portal_hvac`) with made-up
calls, bookings and needs-attention items, plus one demo owner login. The founder signs in on his phone and shows a prospect exactly what
an owner sees. Every portal page for this client says "Sample business - demo data" and the portal is read-only for it.

Run it ON THE FLY MACHINE (the production database lives on its volume), never against a laptop database:

    fly ssh console --app deskline-ai -C 'python scripts/seed_demo_portal.py'                 # reseed + NEW random password (printed once)
    fly ssh console --app deskline-ai -C 'python scripts/seed_demo_portal.py --keep-login'    # reseed data only, password unchanged

Idempotent: it deletes and recreates ONLY rows whose client_id is `sample_portal_hvac` (calls, bookings, booking history, escalations)
and only the one owner login it manages. Times are relative to now, so rerun it on the morning of a demo to make the sample look current.
The password is generated here, printed once to this terminal and never stored (only its scrypt hash). It is not a temporary password: the
demo account cannot change its password from the portal, so whoever holds the phone cannot lock the founder out.
Database: CALLKETTLE_DB_PATH (same as the server). This script does not read backend/.env and makes no network calls.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import owner_auth, storage  # noqa: E402
from app.config import load_client_config  # noqa: E402

SAMPLE_CLIENT_ID = "sample_portal_hvac"
DEFAULT_EMAIL = "owner@example.com"
SID_PREFIX = "SAMPLE-PORTAL-"
_TABLES = ("booking_events", "cancelled_bookings", "bookings", "escalations", "calls")


def _phone(n: int) -> str:
    return f"+1571555{n:04d}"          # 555-01xx: reserved fictional numbers


def _next_weekday(d: date, skip: int = 1) -> date:
    """The `skip`-th Monday-Friday day after d."""
    while skip:
        d += timedelta(days=1)
        if d.weekday() < 5:
            skip -= 1
    return d


def build_sample(now_utc: datetime, tz: ZoneInfo) -> tuple[list[dict], list[dict]]:
    """(calls, bookings) for the sample business, placed relative to now so the portal looks current."""
    now = now_utc.astimezone(tz)
    night = datetime.combine(now.date(), time(22, 40), tz)
    if night > now - timedelta(hours=1):
        night -= timedelta(days=1)                       # the most recent 10:40 pm at least an hour ago
    day = night.date()

    def at(d: date, hh: int, mm: int) -> datetime:
        return datetime.combine(d, time(hh, mm), tz).astimezone(timezone.utc)

    first, second = _next_weekday(now.date(), 1), _next_weekday(now.date(), 2)
    calls = [
        dict(n=1, started=at(day, 10, 5), cls="BOOKED", outcome="completed", attention=0, frm=_phone(142),
             summary="SAMPLE: Upstairs AC not cooling (about 82 degrees upstairs). Booked a repair visit for "
                     f"{first:%a %b} {first.day} at 9:00 AM. Caller: Dana Sample.",
             turns=[("ai", "Thanks for calling Sample Heating and Air. This is the AI receptionist. How can I help?"),
                    ("caller", "Hi, our upstairs AC stopped cooling. It's about 82 up there."),
                    ("ai", f"Sorry to hear that. I can book a repair visit. The first opening is {first:%A} at 9 AM. Does that work?"),
                    ("caller", "Yes, that works. It's Dana Sample."),
                    ("ai", f"Done: Dana Sample, AC or heating repair visit, {first:%A} at 9 AM. The technician explains any cost before work starts. Anything else?"),
                    ("caller", "No, thanks.")]),
        dict(n=2, started=at(day, 13, 20), cls="CALLBACK_REQUESTED", outcome="transfer_unanswered", attention=1, frm=_phone(157),
             summary="SAMPLE: Caller asked for a person about an estimate he already received. Put-through rang about 25 seconds with no "
                     "answer; message taken. Wants a call back this afternoon. Caller: Chris Example.",
             turns=[("ai", "Thanks for calling Sample Heating and Air. This is the AI receptionist. How can I help?"),
                    ("caller", "Can I talk to a real person? It's about the estimate you sent me."),
                    ("ai", "Of course. I'm putting you through now."),
                    ("ai", "Nobody could pick up just now. Can I take a message so they call you back?"),
                    ("caller", "Yes, Chris Example, please call me back this afternoon."),
                    ("ai", "Got it, Chris. I've passed that on as a callback request.")]),
        dict(n=3, started=at(day, 15, 45), cls="OUTSIDE_SERVICE_AREA", outcome="completed", attention=0, frm=_phone(163),
             summary="SAMPLE: Caller in Richmond asked for a furnace check. Outside the service area; told politely, nothing booked.",
             turns=[("ai", "Thanks for calling Sample Heating and Air. This is the AI receptionist. How can I help?"),
                    ("caller", "Do you come out to Richmond for a furnace check?"),
                    ("ai", "I'm sorry, Richmond is outside our service area. We cover Fairfax, Arlington, Alexandria and Loudoun County."),
                    ("caller", "Okay, thanks anyway.")]),
        dict(n=4, started=at(day, 16, 30), cls="BOOKED", outcome="completed", attention=0, frm=_phone(171),
             summary=f"SAMPLE: Booked a seasonal maintenance tune-up for {second:%a %b} {second.day} at 1:00 PM. Caller: Sam Placeholder.",
             turns=[("ai", "Thanks for calling Sample Heating and Air. This is the AI receptionist. How can I help?"),
                    ("caller", "I'd like a heating tune-up before it gets cold."),
                    ("ai", f"Happy to. I have {second:%A} at 1 PM. Would that work?"),
                    ("caller", "Perfect. Sam Placeholder."),
                    ("ai", f"You're booked, Sam: seasonal maintenance tune-up, {second:%A} at 1 PM.")]),
        dict(n=5, started=at(day, 21, 10), cls="EMERGENCY_ESCALATED", outcome="completed", attention=1, frm=_phone(188),
             summary="SAMPLE: Caller reported a gas smell near the furnace and was told to hang up and call 911 right now (a built-in rule, "
                     "not the AI's choice); the team was alerted. Follow up right away. Caller: Robin Demo.",
             turns=[("ai", "Thanks for calling Sample Heating and Air. This is the AI receptionist. How can I help?"),
                    ("caller", "This is Robin Demo. I think I smell gas near my furnace."),
                    ("ai", "This sounds like an emergency. Please hang up and call 911 right now. I'm also alerting the team.")]),
        dict(n=6, started=at(day, 22, 40), cls="AFTER_HOURS_MESSAGE", outcome="completed", attention=1, frm=_phone(195),
             summary="SAMPLE: After hours. Furnace running but blowing cold air; house at 61 degrees, nobody unwell. Wants a call first thing "
                     "in the morning. Caller: Lee Testcase.",
             turns=[("ai", "Thanks for calling Sample Heating and Air. We're closed right now. This is the AI receptionist. How can I help?"),
                    ("caller", "My furnace is running but it's blowing cold air. It's 61 in here."),
                    ("ai", "I'm sorry. I can't give repair advice, but I'll make sure the office calls you first thing. Is everyone okay, and no gas smell?"),
                    ("caller", "Everyone's fine, no gas smell. Lee Testcase."),
                    ("ai", "Thanks, Lee. I've left an urgent message for the morning.")]),
    ]
    bookings = [
        dict(call=1, name="Dana Sample (sample)", phone=_phone(142), service="AC or heating repair visit", start=datetime.combine(first, time(9, 0)), minutes=60),
        dict(call=4, name="Sam Placeholder (sample)", phone=_phone(171), service="Seasonal maintenance tune-up", start=datetime.combine(second, time(13, 0)), minutes=60),
    ]
    return calls, bookings


def seed(now_utc: datetime | None = None, *, email: str = DEFAULT_EMAIL, reset_login: bool = True) -> dict:
    """Reset the sample business's data (and, unless reset_login is False, its owner login). Returns counts and the new password (or None)."""
    config = load_client_config(SAMPLE_CLIENT_ID)
    if config.client_id != SAMPLE_CLIENT_ID or not (config.portal_sample and config.demo_mode):
        raise RuntimeError(f"{SAMPLE_CLIENT_ID} is not a portal_sample demo client; refusing to write anything")
    email = owner_auth.normalize_email(email)
    if not owner_auth.valid_email(email):
        raise ValueError("That does not look like an email address.")
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = ZoneInfo(config.timezone)
    calls, bookings = build_sample(now_utc, tz)
    created = now_utc.isoformat()
    password = None
    storage.init_db()
    with storage._conn() as conn:
        row = conn.execute("SELECT id, client_id FROM owner_users WHERE email = ?", (email,)).fetchone()
        if row and row[1] != SAMPLE_CLIENT_ID:
            raise RuntimeError(f"{email} already belongs to another business; refusing to touch it. Use --email with a different address.")
        for table in _TABLES:
            conn.execute(f"DELETE FROM {table} WHERE client_id = ?", (SAMPLE_CLIENT_ID,))
        for c in calls:
            sid = f"{SID_PREFIX}{c['n']:02d}"
            started = c["started"]
            turns = [{"role": role, "text": text, "at": (started + timedelta(seconds=15 * i)).isoformat()} for i, (role, text) in enumerate(c["turns"])]
            conn.execute(
                "INSERT INTO calls (call_sid, client_id, from_number, started_at, ended_at, turn_count, outcome, transcript_json, summary, "
                "outcome_class, needs_attention) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sid, SAMPLE_CLIENT_ID, c["frm"], started.isoformat(), (started + timedelta(seconds=15 * len(turns) + 10)).isoformat(),
                 len(turns), c["outcome"], json.dumps(turns), c["summary"], c["cls"], c["attention"]))
        for b in bookings:
            conn.execute(
                "INSERT INTO bookings (call_sid, client_id, caller_name, caller_phone, service, slot_start, slot_end, created_at, status) "
                "VALUES (?,?,?,?,?,?,?,?, 'confirmed')",
                (f"{SID_PREFIX}{b['call']:02d}", SAMPLE_CLIENT_ID, b["name"], b["phone"], b["service"], b["start"].strftime("%Y-%m-%dT%H:%M"),
                 (b["start"] + timedelta(minutes=b["minutes"])).strftime("%Y-%m-%dT%H:%M"), created))
        if reset_login or row is None:
            password = owner_auth.new_temp_password()
            pw_hash = owner_auth.hash_password(password)
            if row is None:
                conn.execute("INSERT INTO owner_users (client_id, email, pw_hash, must_change, created_at) VALUES (?, ?, ?, 0, ?)",
                             (SAMPLE_CLIENT_ID, email, pw_hash, owner_auth._now()))
            else:
                conn.execute("UPDATE owner_users SET pw_hash = ?, must_change = 0, failed_count = 0, locked_until = 0, disabled_at = NULL WHERE id = ?",
                             (pw_hash, row[0]))
                conn.execute("DELETE FROM owner_sessions WHERE user_id = ?", (row[0],))
        elif row is not None:
            conn.execute("UPDATE owner_users SET must_change = 0, failed_count = 0, locked_until = 0 WHERE id = ?", (row[0],))
    return {"calls": len(calls), "bookings": len(bookings), "attention": sum(c["attention"] for c in calls), "email": email, "password": password}


def main(argv: list[str]) -> int:
    args = list(argv)
    keep = "--keep-login" in args
    allow_local = "--allow-local" in args
    args = [a for a in args if a not in ("--keep-login", "--allow-local")]
    email = DEFAULT_EMAIL
    if args[:1] == ["--email"] and len(args) == 2:
        email, args = args[1], []
    if args:
        print(__doc__)
        return 2
    if not os.environ.get("FLY_APP_NAME") and not allow_local:
        print("Refusing: this is not the Fly machine. Run it there so the sample lands in the production database:\n"
              "  fly ssh console --app deskline-ai -C 'python scripts/seed_demo_portal.py'\n"
              "(For a throwaway local database only: set CALLKETTLE_DB_PATH and pass --allow-local.)")
        return 1
    try:
        result = seed(email=email, reset_login=not keep)
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    print(f"Sample owner portal seeded for {SAMPLE_CLIENT_ID} (Sample Heating & Air, FICTIONAL): "
          f"{result['calls']} calls, {result['bookings']} bookings, {result['attention']} need attention.")
    print("Sign in at https://app.callkettle.com/portal/login")
    print(f"Email: {result['email']}")
    if result["password"]:
        print(f"Password (shown once, not stored; save it in your phone's password manager): {result['password']}")
    else:
        print("Password unchanged (--keep-login).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
