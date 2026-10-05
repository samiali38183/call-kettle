"""The product's brand, in one place, changeable without editing code.

Renaming the business (see docs/BRAND_CLEARANCE.md) is a configuration change: set these as Fly secrets / environment
variables, redeploy, and every email, calendar invite, admin page, public page and the terms use the new name.

    BRAND_NAME            display name                          default "Call Kettle"
    BRAND_LEGAL_ENTITY    who the Provider is in the terms      default "Sami Ali, doing business as <BRAND_NAME>"
    BRAND_SUPPORT_EMAIL   customer-facing contact address       default SMTP_FROM, else the owner's address
    SUPPORT_PHONE         customer-facing contact number        default "+15555550100"
    BRAND_CONTACT_NAME    the human customers are told to text  default "Sami"
    BRAND_SENDER_NAME     display name on outgoing email        default BRAND_NAME
    BRAND_AI_GREETING     (informational) the AI's own greeting for the operator's demo/sales lines is set per client
                          in clients/<id>.yaml (`opening_line`), because each client's line says the CLIENT's name.

Deliberately NOT branded: the calendar-invite UID suffix (`@deskline-ai`). A calendar identifies an event by its UID;
changing the suffix would make a client's calendar treat later updates and cancellations as different events.
"""
from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass

ICS_UID_SUFFIX = "@deskline-ai"          # stable identity, see above
_DEFAULT_NAME = "Call Kettle"
_DEFAULT_OWNER_EMAIL = "samiali38183@gmail.com"


@dataclass(frozen=True)
class Brand:
    name: str
    legal_entity: str
    support_email: str
    support_phone: str
    contact_name: str
    sender_name: str

    @property
    def upper(self) -> str:
        return self.name.upper()

    @property
    def fallback_sender(self) -> str:
        """The organizer address used on calendar invites when no SMTP sender is configured."""
        slug = re.sub(r"[^a-z0-9]+", "", self.name.lower()) or "bookings"
        return f"bookings@{slug}.local"


def _env(name: str, default: str) -> str:
    value = (os.environ.get(name) or "").strip()
    return value or default


def get() -> Brand:
    """Read at call time so a changed environment takes effect without code changes (and tests can set it)."""
    name = _env("BRAND_NAME", _DEFAULT_NAME)[:60]
    return Brand(
        name=name,
        legal_entity=_env("BRAND_LEGAL_ENTITY", f"Sami Ali, doing business as {name}"),
        support_email=_env("BRAND_SUPPORT_EMAIL", os.environ.get("SMTP_FROM") or _DEFAULT_OWNER_EMAIL),
        support_phone=_env("SUPPORT_PHONE", "+15555550100"),
        contact_name=_env("BRAND_CONTACT_NAME", "Sami"),
        sender_name=_env("BRAND_SENDER_NAME", name),
    )


def _tel(phone: str) -> str:
    """A dialable tel: value (+1 plus ten digits when it is a US number) so the link works from a phone."""
    digits = re.sub(r"\D", "", phone)
    return "+1" + digits if len(digits) == 10 else ("+" + digits if len(digits) == 11 and digits.startswith("1") else digits)


_TOKENS = {
    "{{brand}}": lambda b: b.name,
    "{{BRAND}}": lambda b: b.upper,
    "{{legal_entity}}": lambda b: b.legal_entity,
    "{{support_email}}": lambda b: b.support_email,
    "{{support_phone}}": lambda b: b.support_phone,
    "{{support_tel}}": lambda b: _tel(b.support_phone),
    "{{contact_name}}": lambda b: b.contact_name,
}


def render(text: str) -> str:
    """Fill the {{tokens}} in a static page. Values are HTML-escaped: a brand name with `<` or `&` cannot break a page."""
    b = get()
    for token, getter in _TOKENS.items():
        text = text.replace(token, html.escape(getter(b), quote=True))
    return text
