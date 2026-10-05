"""Turning a client's intake form into a live receptionist config. Used by the admin pages (one click)
and by scripts/onboard_client.py (command line). No network, no side effects: pure functions."""
from __future__ import annotations

import re

import yaml

from app.config import ClientConfig

# Verticals where callers routinely say health or legal details out loud.
SENSITIVE = ("health", "medical", "dental", "med spa", "medspa", "therapy", "clinic", "doctor", "law", "attorney", "legal")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_hours(text: str) -> dict:
    """'mon-fri 8:00-17:00, sat 9:00-13:00' -> {'mon': ['08:00','17:00'], ..., 'sun': 'closed'}"""
    hours = {d: "closed" for d in DAYS}
    for part in filter(None, (p.strip() for p in text.split(","))):
        m = re.fullmatch(r"([a-z]{3})(?:\s*-\s*([a-z]{3}))?\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", part.lower())
        if not m:
            raise ValueError(f"Can't read '{part}'. Use e.g. 'mon-fri 8:00-17:00, sat 9:00-13:00'")
        start, end, open_t, close_t = m.groups()
        first = DAYS.index(start)
        last = DAYS.index(end) if end else first
        for d in DAYS[first : last + 1]:
            hours[d] = [f"{int(open_t.split(':')[0]):02d}:{open_t.split(':')[1]}", f"{int(close_t.split(':')[0]):02d}:{close_t.split(':')[1]}"]
    return hours


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        raise ValueError("Enter a 10-digit US phone number")
    return "+" + digits


def build_config(*, client_id, business_name, vertical, hours, services, faqs, escalation_phone, timezone="America/New_York",
                 slot_minutes=60, owner_email=None, extra_instructions="", google_calendar_id=None) -> dict:
    sensitive = any(word in vertical.lower() for word in SENSITIVE)
    config = {
        "client_id": client_id,
        "business_name": business_name,
        "vertical": vertical,
        "timezone": timezone,
        "model": "claude-haiku-4-5-20251001",
        "opening_line": f"Thanks for calling {business_name}, this is the AI receptionist. How can I help?",
        "business_hours": hours,
        "slot_minutes": slot_minutes,
        "services": [{"name": n, "duration_minutes": d} for n, d in services],
        "faqs": [{"q": q, "a": a} for q, a in faqs],
        "escalation_phone": escalation_phone,
        "max_turns": 12,
        "max_call_seconds": 360,
    }
    if owner_email:
        config["owner_email"] = owner_email
    if google_calendar_id:
        config["google_calendar_id"] = google_calendar_id
    if extra_instructions:
        config["extra_instructions"] = extra_instructions
    if sensitive:
        config["record_transcripts"] = False
    ClientConfig.model_validate(config)  # same validation as production
    return config


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:50] or "client"


def unique_client_id(name: str, existing: list[str]) -> str:
    base, cid, n = slugify(name), slugify(name), 2
    while cid in existing:
        cid, n = f"{base}_{n}", n + 1
    return cid


def config_from_intake(p: dict, *, client_id: str | None = None) -> dict:
    """Turn a submitted intake form (from /start) into a client config dict."""
    hours = {d: "closed" for d in DAYS}
    for day, value in (p.get("hours") or {}).items():
        if value != "closed":
            opens, closes = value.split("-")
            hours[day] = [opens, closes]
    services = [(x["name"], int(x["minutes"])) for x in p["services"]]
    # The booking grid is the shortest job; longer jobs block the slots they overlap.
    slot = max(15, min(m for _, m in services))
    faqs = [(f["q"], f["a"]) for f in p.get("faqs", [])]
    never = (p.get("never_say") or "").strip()
    extra = (
        "The business owner asked for this additional rule (it never overrides the safety, honesty and "
        f"AI-disclosure rules above): {never}"
        if never else ""
    )
    return build_config(
        client_id=client_id or slugify(p["business_name"]), business_name=p["business_name"].strip(), vertical=p["trade"].strip(),
        hours=hours, services=services, faqs=faqs, escalation_phone=normalize_phone(p["owner_phone"]),
        slot_minutes=slot, owner_email=(p.get("owner_email") or None), extra_instructions=extra,
        google_calendar_id=(p.get("google_calendar_email") or None),
    )


def to_yaml(config: dict) -> str:
    return yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
