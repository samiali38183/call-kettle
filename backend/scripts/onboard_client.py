"""Create a new client's config file by answering a few questions (about 10 minutes).

    python scripts/onboard_client.py            # answer the questions here, or
    python scripts/onboard_client.py --intake   # list intake forms clients submitted at /start
    python scripts/onboard_client.py --intake 3 # build the config from intake #3

Writes clients/<client_id>.yaml, validated against the same rules the live
server enforces (including the AI-disclosure line). Then:
    fly deploy --app deskline-ai
    python scripts/provision_number.py <client_id> --area-code 703 --buy
    python scripts/report_link.py <client_id>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import CLIENTS_DIR, ClientConfig  # noqa: E402

from app.onboarding import (  # noqa: E402,F401  (re-exported: tests and other scripts import these names from here)
    DAYS, SENSITIVE, build_config, config_from_intake, normalize_phone, parse_hours, slugify,
)


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default


def from_intake(intake_id: int) -> None:
    import os

    import httpx
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    base = os.environ.get("APP_BASE_URL", "https://app.callkettle.com")
    key = os.environ.get("REPORT_KEY")
    if not key:
        sys.exit("REPORT_KEY isn't set in backend/.env")
    if intake_id == 0:  # list
        rows = httpx.get(f"{base}/admin/intakes", params={"key": key}, timeout=20).json().get("intakes", [])
        for r in rows:
            print(f"#{r['id']:<4} {r['created_at'][:16]}  {r['business_name']} ({r['trade']}), {r['owner_name']} {r['owner_phone']}  [{r['status']}]")
        if not rows:
            print("No intakes yet.")
        return
    r = httpx.get(f"{base}/admin/intake/{intake_id}", params={"key": key}, timeout=20)
    if r.status_code != 200:
        sys.exit(f"Couldn't fetch intake #{intake_id}: {r.status_code} {r.text[:100]}")
    config = config_from_intake(r.json()["payload"])
    path = CLIENTS_DIR / f"{config['client_id']}.yaml"
    if path.exists():
        sys.exit(f"{path} already exists. Rename or delete it first.")
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"Wrote {path}. Read it over (especially extra_instructions), then:\n"
          f"  python simulate_call.py {config['client_id']}\n  python scripts/push_config.py {config['client_id']}   (goes live at once, no restart)\n"
          f"  python scripts/provision_number.py {config['client_id']} --area-code 703 --buy\n"
          f"  python scripts/report_link.py {config['client_id']}")
    if config.get("record_transcripts") is False:
        print("\nNOTE: healthcare/legal-type business. Transcripts are OFF. Read the compliance notes before taking this client.")
    if config.get("google_calendar_id"):
        print("\nThey asked for Google Calendar: have them share it with the Call Kettle service account (see the playbook).")


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "--intake":
        from_intake(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
        return
    print("New client setup. Press Enter to accept a [default].\n")
    business_name = ask("Business name")
    client_id = re.sub(r"[^a-z0-9]+", "_", business_name.lower()).strip("_")
    client_id = ask("Client id (no spaces)", client_id)
    if (CLIENTS_DIR / f"{client_id}.yaml").exists():
        sys.exit(f"clients/{client_id}.yaml already exists.")
    vertical = ask("What kind of business (plumbing, salon, auto repair...)")
    if any(word in vertical.lower() for word in SENSITIVE):
        print("\n  HEADS UP: healthcare/legal-type business. Call Kettle has no HIPAA BAA, so transcripts will NOT be stored.")
        print("  Read the compliance section of the offer doc before selling to this client.\n")
    while True:
        try:
            hours = parse_hours(ask("Hours", "mon-fri 8:00-17:00"))
            break
        except ValueError as e:
            print(f"  {e}")
    slot = int(ask("Appointment length in minutes (30 for quick services, 60+ for jobs)", "60"))
    services = []
    print("Services (blank name to finish):")
    while True:
        name = input("  service name: ").strip()
        if not name:
            break
        services.append((name, int(ask("  minutes", str(slot)))))
    if not services:
        sys.exit("Add at least one service.")
    faqs = []
    print("Common customer questions (ask the owner: 'what do people usually ask when they call?'). Blank to finish:")
    while len(faqs) < 8:
        q = input("  question: ").strip()
        if not q:
            break
        faqs.append((q, ask("  answer")))
    while True:
        try:
            phone = normalize_phone(ask("Owner's cell (where live transfers and alerts go)"))
            break
        except ValueError as e:
            print(f"  {e}")
    email = input("Owner's email for booking alerts (optional): ").strip() or None

    config = build_config(client_id=client_id, business_name=business_name, vertical=vertical, hours=hours,
                          services=services, faqs=faqs, escalation_phone=phone, slot_minutes=slot, owner_email=email)
    path = CLIENTS_DIR / f"{client_id}.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"\nWrote {path}\nNext:\n  python simulate_call.py {client_id}   # talk to it first\n  python scripts/push_config.py {client_id}   # goes live at once, no restart\n"
          f"  python scripts/provision_number.py {client_id} --area-code 703 --buy\n  python scripts/report_link.py {client_id}")


if __name__ == "__main__":
    main()
