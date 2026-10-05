"""Cleanly end a client: keep a copy for you, erase them from the server, free their number.

    python scripts/offboard_client.py <client_id>            # preview only
    python scripts/offboard_client.py <client_id> --do-it    # actually do it

What --do-it does, in order:
  1. saves that client's data to Documents\\CallKettle-Backups (your record),
  2. erases their calls, bookings and callbacks from the server (the terms promise this),
  3. points their Twilio number(s) at nothing-special and, with --release-number, releases them,
  4. moves their config to clients/_archived/ so it's no longer served.
You then run `flyctl deploy --app deskline-ai` to finish removing the config.

Remind the client to turn off call forwarding first (Verizon *73, AT&T #21#, T-Mobile ##21#).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from twilio.rest import Client  # noqa: E402

from app.config import CLIENTS_DIR, ClientNotFoundError, load_client_config  # noqa: E402

load_dotenv(ROOT / ".env")
base = os.environ.get("APP_BASE_URL", "https://app.callkettle.com")
key = os.environ.get("REPORT_KEY")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if len(args) != 1 or not key:
        sys.exit(__doc__)
    client_id = args[0]
    try:
        config = load_client_config(client_id)
    except ClientNotFoundError:
        sys.exit(f"No clients/{client_id}.yaml")
    if client_id in {"callkettle_sales", "callkettle_demo"}:
        sys.exit("Refusing: that's one of your own lines.")

    twilio = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    numbers = [n for n in twilio.incoming_phone_numbers.list()
               if (parse_qs(urlparse(n.voice_url or "").query).get("client_id") or [None])[0] == client_id]

    export = httpx.get(f"{base}/admin/export", params={"key": key}, timeout=120).json()
    mine = {t: [r for r in export[t] if r.get("client_id") == client_id] for t in ("calls", "bookings", "cancelled_bookings", "escalations")}
    print(f"{config.business_name} ({client_id}): {len(mine['calls'])} calls, {len(mine['bookings'])} bookings, "
          f"{len(mine['escalations'])} callbacks; numbers: {[n.phone_number for n in numbers] or 'none'}")

    if "--do-it" not in flags:
        print("\nPreview only. Re-run with --do-it to save a copy, erase their data, and archive their config.")
        return

    folder = Path.home() / "Documents" / "CallKettle-Backups"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"offboarded-{client_id}-{datetime.now().strftime('%Y-%m-%d')}.json"
    path.write_text(json.dumps(mine, indent=2), encoding="utf-8")
    print(f"1. Saved a copy to {path}")

    r = httpx.post(f"{base}/admin/client/{client_id}/delete", params={"key": key, "confirm": client_id}, timeout=60)
    r.raise_for_status()
    print(f"2. Erased from the server: {r.json()['deleted']}")

    for n in numbers:
        if "--release-number" in flags:
            n.delete()
            print(f"3. Released {n.phone_number}")
        else:
            print(f"3. Kept {n.phone_number} (add --release-number to release it and stop paying $1.15/month)")

    r = httpx.post(f"{base}/admin/client/{client_id}/config-remove", params={"key": key, "confirm": client_id}, timeout=60)
    r.raise_for_status()
    served_from_image = r.json().get("still_served_from_image")
    archive = CLIENTS_DIR / "_archived"
    archive.mkdir(exist_ok=True)
    shutil.move(str(CLIENTS_DIR / f"{client_id}.yaml"), str(archive / f"{client_id}.yaml"))
    if served_from_image:
        print("4. Archived their config. It is also baked into the server image, so run: flyctl deploy --app deskline-ai")
    else:
        print("4. Archived their config and stopped serving it. Nothing else to do.")


if __name__ == "__main__":
    main()
