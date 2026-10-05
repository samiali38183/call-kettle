#!/usr/bin/env python
"""Account-wide Twilio usage guard (PRIVATE; docs/TWILIO_USAGE_GUARD.md).

  status                      read-only: this month's usage per category + total price
  plan                        dry run: the usage triggers that WOULD be created (default)
  apply --i-understand-this-modifies-my-twilio-account
                              create the triggers (idempotent; skips ones that already exist)
  delete --i-understand-this-modifies-my-twilio-account
                              delete only the triggers this script created (name prefix)

Twilio UsageTriggers can only call a URL (no built-in email), so triggers call
https://app.callkettle.com/ops/twilio-usage-trigger, which must be DEPLOYED first;
`apply` refuses until an unsigned POST to it answers 403. Credentials come from
backend/.env or the environment and are never printed.
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

NAME_PREFIX = "callkettle-guard: "
CALLBACK_URL = "https://app.callkettle.com/ops/twilio-usage-trigger"
FLAG = "--i-understand-this-modifies-my-twilio-account"

# Defaults derive from docs/WORST_CASE_CAP.md (zero customers today, so the whole account is the first customer):
#   60  ~ expected usage for one customer (64.2)       -> early warning
#   120 ~ the 110 per-client ceiling + number fees     -> real flood suspected
#   200 > the 164.20 worst-case usage of one customer  -> something outside the model
DEFAULT_MONTHLY_PRICE = ("60", "120", "200")
DEFAULT_DAILY_PRICE = "25"       # ~ 4x the busiest modeled day (a 450-call month is about $2.9/day)
DEFAULT_DAILY_CALLS = "150"      # inbound calls per day; the per-client monthly call ceiling is 900
DEFAULT_DAILY_SMS = "60"         # outbound SMS per day (owner escalations)
DEFAULT_DAILY_SPEECH = "400"     # Gather speech-recognition uses per day

RELEVANT = ["totalprice", "calls", "calls-inbound", "calls-inbound-local", "calls-inbound-tollfree",
            "calls-outbound", "calls-transfers", "speech-recognition", "sms", "sms-inbound", "sms-outbound",
            "sms-messages-carrierfees", "phonenumbers", "phonenumbers-local", "phonenumbers-tollfree",
            "tts-polly", "recordings", "lookups"]


def _num(value, label: str) -> str:
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{label}: {value!r} is not a number") from None
    if not d.is_finite() or d <= 0:
        raise ValueError(f"{label}: {value!r} must be a positive number")
    return format(d.normalize(), "f")


def build_plan(*, monthly_price=DEFAULT_MONTHLY_PRICE, daily_price=DEFAULT_DAILY_PRICE, daily_calls=DEFAULT_DAILY_CALLS,
               daily_sms=DEFAULT_DAILY_SMS, daily_speech=DEFAULT_DAILY_SPEECH, callback_url=CALLBACK_URL) -> list[dict]:
    if not str(callback_url).startswith("https://"):
        raise ValueError("callback_url must be https")
    rows = [("totalprice", "monthly", "price", _num(v, "monthly price")) for v in monthly_price]
    rows += [("totalprice", "daily", "price", _num(daily_price, "daily price")),
             ("calls-inbound", "daily", "count", _num(daily_calls, "daily calls")),
             ("sms-outbound", "daily", "count", _num(daily_sms, "daily sms")),
             ("speech-recognition", "daily", "count", _num(daily_speech, "daily speech"))]
    plan = []
    for category, recurring, by, value in rows:
        plan.append({"friendly_name": f"{NAME_PREFIX}{recurring} {category} {by} {value}"[:64],
                     "usage_category": category, "recurring": recurring, "trigger_by": by, "trigger_value": value,
                     "callback_url": callback_url, "callback_method": "POST"})
    return plan


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v)) if v not in (None, "") else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def collect_status(client, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    records = client.usage.records.this_month.list()
    cats = {}
    for r in records:
        cats[r.category] = {"count": _dec(r.count), "usage": _dec(r.usage), "price": _dec(r.price),
                            "unit": getattr(r, "usage_unit", "") or ""}
    total = cats.get("totalprice", {}).get("price", Decimal(0))
    return {"month": now.strftime("%Y-%m"), "total_price": total, "categories": cats}


def print_status(s: dict) -> None:
    print(f"Twilio usage this month ({s['month']}, GMT), whole account. Totals are Twilio's figures; some costs sit in no category.")
    print(f"{'category':32}{'count':>10}{'usage':>12}{'price USD':>12}  unit")
    shown = set()
    for c in RELEVANT:
        if c in s["categories"]:
            shown.add(c)
            v = s["categories"][c]
            print(f"{c:32}{v['count']:>10}{v['usage']:>12}{v['price']:>12}  {v['unit']}")
    others = {c: v for c, v in s["categories"].items() if c not in shown and (v["price"] or v["count"])}
    if others:
        print("other categories with non-zero use:")
        for c, v in sorted(others.items(), key=lambda kv: -kv[1]["price"]):
            print(f"  {c:30}{v['count']:>10}{v['usage']:>12}{v['price']:>12}  {v['unit']}")
    print(f"TOTAL PRICE this month: ${s['total_price']}")


def print_plan(plan: list[dict], existing_names=()) -> None:
    print("DRY RUN: nothing is created. These triggers WOULD be created by `apply`:")
    for t in plan:
        mark = "  (already exists)" if t["friendly_name"] in existing_names else ""
        print(f"- {t['recurring']:8} {t['usage_category']:20} by {t['trigger_by']:6} > {t['trigger_value']:>6}  -> {t['callback_url']}{mark}")
    print(f"Apply: python backend/scripts/twilio_usage_guard.py apply {FLAG}")


def endpoint_is_live(url: str) -> bool:
    """True only when an UNSIGNED POST is refused with 403 (our endpoint is deployed and checks signatures)."""
    req = urllib.request.Request(url, data=b"x=1", method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        return e.code == 403
    except Exception:
        return False
    return False


def default_client():
    from dotenv import load_dotenv
    from twilio.rest import Client

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    sid, token = os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        raise SystemExit("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set (backend/.env)")
    return Client(sid, token)


def _existing(client) -> dict:
    return {t.friendly_name: t for t in client.usage.triggers.list() if (t.friendly_name or "").startswith(NAME_PREFIX)}


def main(argv=None, *, client_factory=default_client, endpoint_check=endpoint_is_live) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="read-only usage for this month")
    for name in ("plan", "apply", "delete"):
        sp = sub.add_parser(name)
        if name != "delete":
            sp.add_argument("--monthly-price", nargs="+", default=list(DEFAULT_MONTHLY_PRICE), metavar="USD")
            sp.add_argument("--daily-price", default=DEFAULT_DAILY_PRICE, metavar="USD")
            sp.add_argument("--daily-calls", default=DEFAULT_DAILY_CALLS)
            sp.add_argument("--daily-sms", default=DEFAULT_DAILY_SMS)
            sp.add_argument("--daily-speech", default=DEFAULT_DAILY_SPEECH)
            sp.add_argument("--callback-url", default=CALLBACK_URL)
        if name == "plan":
            sp.add_argument("--check-existing", action="store_true", help="read existing triggers (read-only API call)")
        if name in ("apply", "delete"):
            sp.add_argument(FLAG, dest="confirmed", action="store_true", required=True)
    a = p.parse_args(argv)

    if a.cmd == "status":
        print_status(collect_status(client_factory()))
        return 0
    if a.cmd == "delete":
        client = client_factory()
        found = _existing(client)
        for name, t in found.items():
            client.usage.triggers(t.sid).delete()
            print(f"deleted {t.sid} {name}")
        print(f"{len(found)} trigger(s) deleted (only those named '{NAME_PREFIX}...').")
        return 0
    try:
        plan = build_plan(monthly_price=a.monthly_price, daily_price=a.daily_price, daily_calls=a.daily_calls,
                          daily_sms=a.daily_sms, daily_speech=a.daily_speech, callback_url=a.callback_url)
    except ValueError as e:
        print(f"error: {e}")
        return 2
    if a.cmd == "plan":
        names = set(_existing(client_factory())) if a.check_existing else set()
        print_plan(plan, names)
        return 0
    # apply
    if not endpoint_check(a.callback_url):
        print(f"REFUSED: {a.callback_url} is not live (an unsigned POST must answer 403). Deploy the backend first; "
              "a trigger that fires into a dead URL is lost (Twilio retries only 5xx, 3 times).")
        return 3
    client = client_factory()
    have = _existing(client)
    made = 0
    for t in plan:
        if t["friendly_name"] in have:
            print(f"already exists: {t['friendly_name']}")
            continue
        r = client.usage.triggers.create(**t)
        made += 1
        print(f"created {r.sid}: {t['friendly_name']}")
    print(f"{made} created. Delete with: python backend/scripts/twilio_usage_guard.py delete {FLAG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
