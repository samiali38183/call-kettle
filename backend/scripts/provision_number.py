"""Point a Twilio number at a client's AI receptionist (and optionally buy one).

Dry-run by default. Buying a number costs money (about $1.15/month), so it only
happens with --buy. Repointing a number you already own is free.

    python scripts/provision_number.py acme_plumbing --number +15555550100
    python scripts/provision_number.py acme_plumbing --area-code 703 --buy

Every number this touches gets the same three settings:
  * voice webhook   -> the AI, for that client
  * status callback -> closes out calls that end when the caller hangs up
  * voice fallback  -> if the AI server is ever unreachable, ring the owner's
                       real phone (fallback function in deskline-ai/fallback)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from twilio.rest import Client  # noqa: E402

from app.config import ClientNotFoundError, load_client_config  # noqa: E402

APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://app.callkettle.com")
FALLBACK_BASE_URL = os.environ.get("FALLBACK_BASE_URL", "https://fallback-ruddy.vercel.app")


def webhook_settings(client_id: str, owner_phone: str) -> dict:
    from app import provisioning

    return provisioning.webhook_settings(client_id, owner_phone, app_base=APP_BASE_URL, fallback_base=FALLBACK_BASE_URL)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("client_id")
    parser.add_argument("--number", help="existing Twilio number to repoint, e.g. +15555550100")
    parser.add_argument("--area-code", help="area code to search when buying a new number")
    parser.add_argument("--buy", action="store_true", help="actually purchase a new number (costs ~$1.15/mo)")
    args = parser.parse_args()

    try:
        config = load_client_config(args.client_id)
    except ClientNotFoundError:
        sys.exit(f"No clients/{args.client_id}.yaml — run scripts/onboard_client.py first.")

    settings = webhook_settings(args.client_id, config.escalation_phone)
    client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

    if args.number:
        matches = client.incoming_phone_numbers.list(phone_number=args.number)
        if not matches:
            sys.exit(f"{args.number} isn't a number on this Twilio account.")
        number = matches[0].update(**settings)
        print(f"Repointed {number.phone_number} to {config.business_name}.")
    elif args.area_code:
        available = client.available_phone_numbers("US").local.list(area_code=args.area_code, voice_enabled=True, limit=3)
        if not available:
            sys.exit(f"No numbers available in area code {args.area_code}.")
        choice = available[0].phone_number
        if not args.buy:
            print(f"DRY RUN. Would buy {choice} (~$1.15/month) and point it at {config.business_name}.")
            print("Re-run with --buy to purchase it.")
            return
        number = client.incoming_phone_numbers.create(phone_number=choice, **settings)
        print(f"Bought {number.phone_number} and pointed it at {config.business_name}.")
    else:
        sys.exit("Give --number (repoint an existing number) or --area-code (search for a new one).")

    print("Next: forward the client's business line to this number, then place a test call.")


if __name__ == "__main__":
    main()
