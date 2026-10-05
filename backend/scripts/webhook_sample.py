"""Print a sample signed webhook delivery for a client, so a receiver (Zapier, Make, any HTTPS endpoint) can be built and checked. No network, no database.

    python scripts/webhook_sample.py demo_hvac --secret dummy-secret+15555550100 [--event call.completed|all] [--timestamp+15555550100]

Pass a DUMMY secret, never a real client's webhook_secret (it lands in your shell history). The data values are fake (555 numbers); the field names,
envelope, headers and signature are produced by the same code as a real delivery (app/webhooks.py). Nothing is sent.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import webhooks  # noqa: E402
from app.config import ClientNotFoundError, load_client_config  # noqa: E402

SAMPLE_DATA = {
    "booking.created": {"booking_id": 101, "caller_name": "Pat Sample", "caller_phone": "+15555550100", "service": "AC repair",
                        "start": "2026-01-12T10:00", "end": "2026-01-12T11:00", "timezone": "{tz}", "call_sid": "CA_SAMPLE_1"},
    "booking.updated": {"booking_id": 101, "caller_name": "Pat Sample", "caller_phone": "+15555550100", "service": "AC repair",
                        "start": "2026-01-12T11:00", "end": "2026-01-12T12:00", "previous_start": "2026-01-12T10:00", "timezone": "{tz}",
                        "call_sid": "CA_SAMPLE_2"},
    "booking.cancelled": {"booking_id": 101, "caller_name": "Pat Sample", "caller_phone": "+15555550100", "service": "AC repair",
                          "start": "2026-01-12T11:00", "end": "2026-01-12T12:00", "timezone": "{tz}", "call_sid": "CA_SAMPLE_3"},
    "callback.requested": {"reason": "wants_callback", "caller_name": "Pat Sample", "caller_phone": "+15555550100",
                           "summary": "Caller reports a noise from the furnace and wants a call back.", "call_sid": "CA_SAMPLE_4"},
    "call.completed": {"call_sid": "CA_SAMPLE_5", "from": "+15555550100", "started_at": "2026-01-12T14:00:00+00:00",
                       "ended_at": "2026-01-12T14:03:10+00:00", "outcome": "completed", "turns": 6, "outcome_class": "BOOKED"},
}


def sample(client_id: str, event: str, secret: str, timestamp: int) -> tuple[dict, bytes, dict]:
    """(payload, exact body bytes, headers) for one event, built with the production payload and signing functions."""
    config = load_client_config(client_id)
    data = {k: (config.timezone if v == "{tz}" else v) for k, v in SAMPLE_DATA[event].items()}
    payload = webhooks.build_payload(config, event, data, event_id=f"sample:{event}:{client_id}")
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")     # same encoding as webhooks.deliver_once
    headers = {"Content-Type": "application/json", "User-Agent": "receptionist-webhooks/1", "X-Event": event,
               "X-Delivery": payload["id"], "X-Signature": webhooks.sign(secret, body, timestamp)}
    return payload, body, headers


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Print a sample signed webhook delivery (no network).")
    ap.add_argument("client_id")
    ap.add_argument("--secret", required=True, help="a DUMMY secret, at least 16 characters")
    ap.add_argument("--event", default="call.completed", choices=[*SAMPLE_DATA, "all"])
    ap.add_argument("--timestamp", type=int, default=None, help="unix seconds for the signature (default: now); fix it for reproducible output")
    args = ap.parse_args(argv)
    if len(args.secret) < 16:
        print("The secret must be at least 16 characters (the same rule real configs follow).")
        return 2
    ts = args.timestamp if args.timestamp is not None else int(time.time())
    events = list(SAMPLE_DATA) if args.event == "all" else [args.event]
    try:
        for event in events:
            payload, body, headers = sample(args.client_id, event, args.secret, ts)
            print(f"=== {event} ===\nPOST <your https endpoint>")
            for k, v in headers.items():
                print(f"{k}: {v}")
            print(f"\n{body.decode('utf-8')}\n")
            print("pretty body:\n" + json.dumps(payload, indent=2, ensure_ascii=False))
            print(f"verifies with that secret: {webhooks.verify(args.secret, body, headers['X-Signature'], now=ts)}\n")
    except ClientNotFoundError:
        print(f"No client config named {args.client_id!r}.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
