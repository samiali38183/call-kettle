"""Prove that a customer's business number really forwards to their assistant line.

    python scripts/verify_forwarding.py <client_id> <business_number> --from-number <one of OUR other numbers> --owner-permission

How it works: Twilio places a short test call FROM one of our other numbers TO the customer's business number. If the
carrier forwarding is set up, the call arrives at the client's assistant line and appears in our call log for that client
with that caller number within a minute. If it does not, nothing arrives and we say so.

Read before running:
  * It rings the customer's real business number (briefly, if forwarding works the call is handed on). Run it only with the
    owner's permission and while they expect it: `--owner-permission` is required so that this is a conscious choice.
  * With "forward when no answer" the owner's phone rings for the ring time first: have them let it ring.
  * It tests the forwarding path only. It does not test the owner's voicemail, call waiting, or a number they have not set up.
  * The test call is logged like any other call for that client (one short call, no booking).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

BASE = os.environ.get("APP_BASE_URL", "https://app.callkettle.com")
KEY = os.environ["REPORT_KEY"]
E164 = re.compile(r"^\+1\d{10}$")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("client_id")
    ap.add_argument("business_number", help="the number the customer's callers dial, e.g. +15555550100")
    ap.add_argument("--from-number", required=True, help="one of OUR Twilio numbers (not the client's own line)")
    ap.add_argument("--wait", type=int, default=75, help="seconds to wait for the call to arrive")
    ap.add_argument("--owner-permission", action="store_true", help="confirm the owner knows and agrees this call will ring their business number")
    args = ap.parse_args()

    if not args.owner_permission:
        print("Refusing to run: this rings the customer's real business number. Get the owner's OK, then add --owner-permission.")
        return 2
    for n in (args.business_number, args.from_number):
        if not E164.match(n):
            print(f"{n!r} must look like +15555550100")
            return 2
    if args.business_number == args.from_number:
        print("The from-number and the business number must differ.")
        return 2

    from twilio.rest import Client

    client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    ours = {n.phone_number for n in client.incoming_phone_numbers.list(limit=100)}
    if args.from_number not in ours:
        print(f"{args.from_number} is not a number on our Twilio account (we can only place the test call from our own numbers).")
        return 2

    before = _calls_for(args.client_id)
    started = time.time()
    call = client.calls.create(
        to=args.business_number, from_=args.from_number, timeout=45,
        twiml='<Response><Pause length="40"/><Hangup/></Response>',
    )
    print(f"Test call {call.sid} placed from {args.from_number} to {args.business_number}. Waiting up to {args.wait}s for it to reach {args.client_id}...")
    seen = None
    while time.time() - started < args.wait:
        time.sleep(4)
        new = [c for c in _calls_for(args.client_id) if c["call_sid"] not in {b["call_sid"] for b in before} and c["from_number"] == args.from_number]
        if new:
            seen = new[0]
            break
    try:
        client.calls(call.sid).update(status="completed")
    except Exception:
        pass
    if seen:
        print(f"FORWARDING WORKS: the call reached {args.client_id} ({seen['call_sid']}) {int(time.time() - started)}s after it was placed.")
        return 0
    status = client.calls(call.sid).fetch().status
    print(f"FORWARDING NOT CONFIRMED: no call reached {args.client_id} from {args.from_number}. The test call ended as '{status}'.")
    print("Likely causes: forwarding not set (or typed wrong), 'forward when no answer' but the phone was answered or has voicemail "
          "that picks up first, or the carrier refuses forwarding to this number. See docs/TELEPHONY.md section 5.")
    return 1


def _calls_for(client_id: str) -> list[dict]:
    data = httpx.get(f"{BASE}/admin/export", params={"key": KEY}, timeout=60).json()
    return [c for c in data["calls"] if c["client_id"] == client_id]


if __name__ == "__main__":
    sys.exit(main())
