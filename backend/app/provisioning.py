"""Phone numbers for clients: find one, buy it, point it at the AI. Used by the admin pages and
scripts/provision_number.py. Every function takes the Twilio client as an argument so tests never touch the network."""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import quote

from app import storage

logger = logging.getLogger("callkettle.provisioning")

APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://app.callkettle.com")
FALLBACK_BASE_URL = os.environ.get("FALLBACK_BASE_URL", "https://fallback-ruddy.vercel.app")
NUMBER_PRICE_PER_MONTH = 1.15   # Twilio US local number, twilio.com/en-us/voice/pricing/us


def webhook_settings(client_id: str, owner_phone: str, *, app_base: str | None = None, fallback_base: str | None = None) -> dict:
    app_base = app_base if app_base is not None else APP_BASE_URL
    fallback_base = fallback_base if fallback_base is not None else FALLBACK_BASE_URL
    settings = {
        "voice_url": f"{app_base}/voice/incoming?client_id={client_id}",
        "voice_method": "POST",
        "status_callback": f"{app_base}/voice/status",
        "status_callback_method": "POST",
    }
    if fallback_base:
        settings["voice_fallback_url"] = f"{fallback_base}/api/fallback?to={quote(owner_phone, safe='')}"
        settings["voice_fallback_method"] = "POST"
    return settings


def ensure_table() -> None:
    with storage._conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS client_numbers (client_id TEXT NOT NULL, phone_number TEXT NOT NULL, "
                     "sid TEXT, created_at TEXT NOT NULL, PRIMARY KEY (client_id, phone_number))")


def record_number(client_id: str, phone_number: str, sid: str | None) -> None:
    ensure_table()
    with storage._conn() as conn:
        conn.execute("INSERT OR REPLACE INTO client_numbers (client_id, phone_number, sid, created_at) VALUES (?, ?, ?, ?)",
                     (client_id, phone_number, sid, storage._now()))


def numbers_for(client_id: str, twilio_client=None) -> list[str]:
    """Numbers that answer for this client: our record first, then whatever Twilio says points at them."""
    ensure_table()
    with storage._conn() as conn:
        found = [r[0] for r in conn.execute("SELECT phone_number FROM client_numbers WHERE client_id = ? ORDER BY created_at", (client_id,))]
    if found or twilio_client is None:
        return found
    try:
        for n in twilio_client.incoming_phone_numbers.list():
            if re.search(rf"client_id={re.escape(client_id)}(&|$)", n.voice_url or ""):
                found.append(n.phone_number)
                record_number(client_id, n.phone_number, getattr(n, "sid", None))
    except Exception:
        logger.exception("Could not look up %s's numbers at Twilio", client_id)
    return found


def search_number(twilio_client, area_code: str) -> str | None:
    """The first available local number in an area code, or None."""
    if not re.fullmatch(r"\d{3}", area_code or ""):
        raise ValueError("Area code must be 3 digits")
    options = twilio_client.available_phone_numbers("US").local.list(area_code=int(area_code), limit=1)
    return options[0].phone_number if options else None


def buy_number(twilio_client, client_id: str, number: str, owner_phone: str) -> dict:
    """Buy `number` and point it at the AI with the safety fallback. Costs about $1.15 a month."""
    if not re.fullmatch(r"\+1\d{10}", number or ""):
        raise ValueError("Not a US phone number")
    bought = twilio_client.incoming_phone_numbers.create(phone_number=number, **webhook_settings(client_id, owner_phone))
    record_number(client_id, bought.phone_number, getattr(bought, "sid", None))
    return {"phone_number": bought.phone_number, "sid": getattr(bought, "sid", None)}


def format_phone(e164: str) -> str:
    d = re.sub(r"\D", "", e164)[-10:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else e164
