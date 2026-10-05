"""Build a PRIVATE demo for one prospect from the public information in marketing/prospects.csv, reachable with a one-time code on the public demo number.

    python backend/scripts/prep_demo.py "PD Heating & Cooling, LLC"             # builds it, makes it live, runs the synthetic QA, prints the code
    python backend/scripts/prep_demo.py "PD Heating & Cooling, LLC" --no-push   # files only: nothing live, no code
    python backend/scripts/prep_demo.py "PD Heating & Cooling, LLC" --revoke    # turn it off now

The prospect calls the ordinary demo number, presses 9 and types the 6-digit code. No dedicated phone number is needed.
Output (gitignored): marketing/proposals/out/demos/<slug>.yaml, <slug>_PREP.md (talk track, scenarios, follow-up draft) and index.json (the code and its expiry).

Rules this tool and the server follow, because the data is public and unverified:
  * Everything it knows is labeled UNVERIFIED PUBLIC DATA. The prospect must confirm hours, services and area before anything is promised.
  * The assistant introduces itself as an AI demonstration built from the business's public information. It never claims to BE the business,
    never quotes a price, and never pretends the business has agreed to anything.
  * The SERVER forces the isolation: demo_mode, client id starting prep_, no owner email/ntfy/webhook/calendar, spend capped at $10, hand-off simulated.
  * The code is random, one prospect, limited uses, expires (72 hours by default), and then the demo is unserved and its transcripts, bookings and
    callbacks are wiped. Guessing codes is rate limited and every attempt is audited.
Nothing is sent to the prospect. You decide whether and how to share it.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

PROSPECTS = REPO / "marketing" / "prospects.csv"
OUT = REPO / "marketing" / "proposals" / "out" / "demos"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_ALIASES = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3,
               "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6}
SERVICES = {
    "HVAC": [("AC or heating repair visit", 60), ("Seasonal maintenance tune-up", 60), ("New system estimate", 45)],
    "Plumbing": [("Leak or drain repair visit", 60), ("Water heater estimate", 45), ("General plumbing repair", 60)],
    "Garage door": [("Garage door repair visit", 60), ("New door or opener estimate", 45), ("Opener installation", 90)],
    "Electrical": [("Electrical repair visit", 60), ("Estimate", 45)],
    "Roofing": [("Roof inspection or repair estimate", 60)],
    "Restoration": [("Damage assessment visit", 60)],
    "Pest control": [("Pest inspection visit", 45), ("Treatment visit", 60)],
}
OWNER_PHONE = "+15555550100"


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "prospect"


def _time(token: str) -> str | None:
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", token.strip().lower())
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    h = (h % 12) + (12 if ap == "pm" else 0)
    return f"{h:02d}:{mi:02d}"


def parse_public_hours(text: str) -> tuple[dict, bool]:
    """(hours, verified_from_text). Understands 'Mon-Fri 8am-5pm; Sat-Sun closed' and similar. Anything else gets a plain weekday default
    and verified=False, which the prep sheet shows in capitals."""
    hours: dict = {d: "closed" for d in DAYS}
    found = False
    for part in re.split(r"[;,]", text or ""):
        m = re.match(r"\s*([A-Za-z]+)(?:\s*[-–]\s*([A-Za-z]+))?\s+(.*)$", part)
        if not m or m.group(1).lower() not in DAY_ALIASES:
            continue
        a = DAY_ALIASES[m.group(1).lower()]
        b = DAY_ALIASES.get((m.group(2) or m.group(1)).lower(), a)
        rest = m.group(3).strip().lower()
        days = [DAYS[(a + i) % 7] for i in range(((b - a) % 7) + 1)]
        if rest.startswith("closed"):
            found = True
            continue
        t = re.match(r"(\d{1,2}(?::\d{2})?\s*[ap]m)\s*[-–to]+\s*(\d{1,2}(?::\d{2})?\s*[ap]m)", rest)
        if t and _time(t.group(1)) and _time(t.group(2)):
            for d in days:
                hours[d] = [_time(t.group(1)), _time(t.group(2))]
            found = True
    if not found or all(v == "closed" for v in hours.values()):
        return {**{d: ["08:00", "17:00"] for d in DAYS[:5]}, "sat": "closed", "sun": "closed"}, False
    return hours, True


def find_prospect(name: str) -> dict:
    with PROSPECTS.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    exact = [r for r in rows if r["business_name"].lower() == name.lower()]
    near = exact or [r for r in rows if name.lower() in r["business_name"].lower()]
    if len(near) != 1:
        raise SystemExit(f"{'No' if not near else 'Several'} prospects match {name!r}: {[r['business_name'] for r in near][:5]}")
    return near[0]


def build_config(row: dict) -> tuple[dict, bool]:
    biz, city = row["business_name"], row["city"]
    cid = f"prep_{slug(biz)}"[:60]
    hours, hours_ok = parse_public_hours(row.get("business_hours", ""))
    services = SERVICES.get(row["vertical"], [("Service visit", 60)])
    cfg = {
        "client_id": cid,
        "business_name": biz,
        "vertical": row["vertical"].lower(),
        "timezone": "America/New_York",
        "model": "claude-haiku-4-5-20251001",
        "opening_line": (f"Hi, this is a demonstration of an AI receptionist for {biz}, set up from the information on your website. "
                         "It is a demo, so nothing you say reaches anyone. How can I help?"),
        "business_hours": hours,
        "slot_minutes": 60,
        "services": [{"name": n, "duration_minutes": m} for n, m in services],
        "faqs": [
            {"q": "What area do you serve?", "a": f"{biz} is based in {city}. I'd take your zip code and the team would confirm whether they cover it."},
            {"q": "How much does a visit cost?", "a": "I can't quote prices on the phone. The technician explains the cost and gets your approval before any work starts."},
        ],
        "escalation_phone": OWNER_PHONE,
        "demo_mode": True,
        "monthly_cost_ceiling_usd": 10,
        "ceiling_mode": "message",
        "extra_instructions": (
            f"THIS IS A PRIVATE DEMO built from UNVERIFIED PUBLIC DATA about {biz}. You are demonstrating how an AI receptionist could answer for them; you are NOT {biz} "
            "and the business has agreed to nothing. Never claim to be a person at the business, never claim they use this service, and never quote a price. "
            "Do the job of a great receptionist: understand what the caller needs, answer from the facts above, book a real open time, move or cancel when asked, and "
            "hand off to a person when asked (the demo simulates the hand-off). If the caller says a detail about the business is wrong, accept the correction and say it would be "
            "set up that way. Never give repair or safety instructions."
        ),
    }
    return cfg, hours_ok


def scenarios(row: dict) -> list[str]:
    sys.path.insert(0, str(REPO / "marketing"))
    import triggers

    first = triggers.demo_scenario(row["vertical"], date.today()).split(" (")[0]
    out = [first, "Change your mind: book a time, then say 'actually, can we move that to a different day?', then cancel it.",
           "Ask something it must not know: 'what will the repair cost?' It should decline to quote and offer a technician."]
    if row.get("spanish_bilingual_signal") == "yes":
        out.append("Spanish: switch languages mid-call ('Hola, necesito una cita para manana').")
    return out


def make_id(name: str) -> str:
    sys.path.insert(0, str(REPO / "marketing"))
    import triggers

    return triggers.make_id(name)


def prep_sheet(row: dict, cfg: dict, hours_ok: bool, *, code: str | None = None, expires: str | None = None, max_calls: int = 10, dashboard: str | None = None, qa: str | None = None) -> str:
    code_block = (f"**DEMO CODE: {code}**   (call+15555550100, press 9, type the code)   EXPIRES: {expires}   MAX CALLS: {max_calls}\n"
                  "Shown once. Revoke early: `python backend/scripts/prep_demo.py \"<business>\" --revoke`. It expires and its data is wiped automatically."
                  if code else "**NOT LIVE.** No code yet (files only). Run without --no-push to create the code.")
    sc = "\n".join(f"{i}. {t}" for i, t in enumerate(scenarios(row), 1))
    return f"""# PRIVATE DEMO PREP: {row['business_name']}

> **UNVERIFIED PUBLIC DATA. PRIVATE. NEVER PUBLISH, POST OR SEND THIS SHEET.** Everything below came from the company's own public website
> on {row.get('last_verified_date', '?')} (sources: {row.get('source_urls', '')}). The prospect has agreed to nothing.

## PROSPECT
{row['business_name']} ({row['vertical']}, {row['city']}); client id `{cfg['client_id']}`. Why now: {row.get('reason_to_call_now') or row.get('specific_outreach_trigger') or 'n/a'}

## DEMO CODE / EXPIRES
{code_block}
{('Outcome view for you (what the owner would see; do not share the key): ' + dashboard) if dashboard else ''}
{('QA before use: ' + qa) if qa else ''}

## PUBLIC DATA USED
- Hours: {'parsed from their site' if hours_ok else '**NOT FOUND ON THEIR SITE: a default weekday 8-5 was used.**'}: `{row.get('business_hours') or 'none published'}`
- Services: a generic set for the trade, **not theirs**: {', '.join(s['name'] for s in cfg['services'])}
- Their claims: emergency/same-day `{row.get('emergency_same_day_claim') or 'none found'}`; after-hours `{row.get('after_hours_claim') or 'not stated'}`
- Their tools: FSM/CRM `{row.get('known_fsm_crm') or 'unknown'}`; answering service `{row.get('existing_answering_service_signal') or 'none seen'}`; AI `{row.get('existing_ai_signal') or 'none seen'}`

## UNVERIFIED FIELDS (confirm on the call, write the answers in the note)
1. Are these your real hours? Who answers after them? {'(HOURS WERE DEFAULTED: CONFIRM FIRST. CONFIRM ON THE CALL.)' if not hours_ok else ''}
2. Which services should it book, and how long is a typical visit?
3. Service area; what counts as an emergency for you and who should ring first?
4. Where should a transferred call go? (In the demo it is simulated.)

## OWNER TALK TRACK (15 minutes)
- 0-2: "This is a private simulation I configured from your public website. It isn't connected to your real phone or your customers." Confirm normal coverage, overflow, after hours, scheduler, biggest concern.
- 2-3: explain only the mode that fits (usually overflow + after hours; their team stays primary).
- 3-8: THEY call the demo. Say nothing. Let them speak normally.
- 8-10: show what it classified, captured and did, and what the owner would see (the outcome view).
- 10-12: they try the thing that worries them: change of mind, a hard question, a human request.
- 12-13: implementation: existing number, forwarding/overflow, config, testing before live calls, human fallback, easy switch-off.
- 13-15: recommendation, price (if fit is established), pilot, next action. Direct question: "Would you be comfortable starting in overflow mode and testing it on your real after-hours calls?"

## DEMO SCENARIOS (suggest three)
{sc}

## SAFETY RAILS
Isolated demo client (bookings reset, hard $10 spend cap, simulated hand-off, no real calendar, no webhook, no email or SMS to anyone). The code is random, for one prospect, {max_calls} uses, expires automatically, and then its transcripts, bookings and callbacks are wiped. Every attempt is audited and guessing is locked out. It never claims to be {row['business_name']}. A dedicated phone number is NOT required.

## FOLLOW-UP DRAFT (review, fill the brackets, send it yourself; no price in it)
Subject: What you tried today

Hi [NAME], thanks for trying it. What I'd set up for {row['business_name']}: overflow and after-hours coverage so your team stays primary; your rules, tested before real calls, monitored at the start, easy to switch off. If you'd like to start I'll send a one-page proposal today.
Sami Ali, Call Kettle, [YOUR MAILING ADDRESS]. Reply "stop" and I won't contact you again.

After the call: `python marketing/sales.py log {make_id(row['business_name'])} DEMO_DONE --note "..."`.
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("business")
    ap.add_argument("--no-push", action="store_true", help="write the files only: no code, nothing live")
    ap.add_argument("--hours", type=int, default=72, help="how long the code works (default 72)")
    ap.add_argument("--max-calls", type=int, default=10)
    ap.add_argument("--skip-qa", action="store_true", help="skip the synthetic QA run (about a minute and a few cents)")
    ap.add_argument("--revoke", action="store_true", help="revoke this prospect's private demo now")
    a = ap.parse_args()

    import httpx
    import yaml
    from dotenv import load_dotenv

    row = find_prospect(a.business)
    cfg, hours_ok = build_config(row)
    from app.config import ClientConfig

    ClientConfig.model_validate(cfg)                      # the same validation the server applies
    load_dotenv(ROOT / ".env")
    base_url, key = os.environ.get("APP_BASE_URL", "https://app.callkettle.com"), os.environ.get("REPORT_KEY", "")
    if a.revoke:
        r = httpx.post(f"{base_url}/admin/private-demo/revoke", params={"key": key, "client_id": cfg["client_id"]}, timeout=30)
        print("revoke:", r.status_code, r.text[:200])
        return
    OUT.mkdir(parents=True, exist_ok=True)
    base = slug(row["business_name"])
    text = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
    (OUT / f"{base}.yaml").write_text(text, encoding="utf-8")
    code = expires = dashboard = qa = None
    if not a.no_push:
        r = httpx.post(f"{base_url}/admin/private-demo", params={"key": key, "label": row["business_name"], "hours": a.hours, "max_calls": a.max_calls},
                       content=text.encode(), timeout=60)
        if r.status_code != 200:
            raise SystemExit(f"the server refused the demo: {r.status_code} {r.text[:300]}")
        made = r.json()
        code, expires = made["code"], made["expires_at"]
        dashboard = f"{base_url}/report/{cfg['client_id']}?key=<report key: see report_link.py>"
        if not a.skip_qa:
            import subprocess

            res = subprocess.run([sys.executable, str(ROOT / "scripts" / "certify_client.py"), cfg["client_id"]], capture_output=True, text=True, cwd=ROOT, timeout=900)
            fails = [ln for ln in res.stdout.splitlines() if ln.startswith("| FAIL") and "Owner notification" not in ln]   # a private demo deliberately has no owner channel
            qa = "no FAIL in the synthetic certification" if not fails else f"CHECK BEFORE USE: {len(fails)} FAIL line(s): {' / '.join(fails)[:300]}"
        index = OUT / "index.json"
        data = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {}
        data[row["business_name"]] = {"client_id": cfg["client_id"], "code": code, "expires_at": expires}
        index.write_text(json.dumps(data, indent=2), encoding="utf-8")
    (OUT / f"{base}_PREP.md").write_text(prep_sheet(row, cfg, hours_ok, code=code, expires=expires, max_calls=a.max_calls, dashboard=dashboard, qa=qa), encoding="utf-8")
    print(f"wrote {OUT / (base + '_PREP.md')}")
    if code:
        print(f"PRIVATE DEMO LIVE for {row['business_name']}: code {code}, expires {expires}. Call+15555550100, press 9, type the code.")
        print(f"QA: {qa or 'skipped'}")
    print(f"hours {'parsed from their site' if hours_ok else 'DEFAULTED: confirm with the prospect'}")


if __name__ == "__main__":
    main()
