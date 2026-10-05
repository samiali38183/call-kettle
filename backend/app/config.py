from __future__ import annotations

import functools
import os
import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

CLIENTS_DIR = Path(__file__).resolve().parent.parent / "clients"
# Clients added after a deploy live on the persistent volume, so signing a client
# never restarts the server. A config here overrides a same-named baked-in one.
LIVE_CLIENTS_DIR = Path(os.environ["CALLKETTLE_CLIENTS_DIR"]) if os.environ.get("CALLKETTLE_CLIENTS_DIR") else None
MAX_CONFIG_BYTES = 100_000
_ID_RE = re.compile(r"[A-Za-z0-9_]{1,60}")

# Maine's Chatbot Disclosure Act (effective Sept 2025) requires telling a
# consumer they're talking to AI when they could otherwise be misled, and Utah
# requires disclosure on request (proactively for licensed professions).
# California SB 243 and Washington HB 2225 target companion chatbots and
# exclude customer-service bots, so they don't apply here — but disclosing up
# front on every call is cheap, honest, and future-proof. Enforced here at
# config-validation time, not left to whoever writes a new client's
# opening_line remembering to include it.
_AI_DISCLOSURE_PHRASES = (
    "artificial intelligence",
    "automated",
    "virtual assistant",
    "virtual receptionist",
)
_AI_WORD_RE = re.compile(r"\bai\b", re.IGNORECASE)


def _discloses_ai(text: str) -> bool:
    lowered = text.lower()
    return bool(_AI_WORD_RE.search(lowered)) or any(p in lowered for p in _AI_DISCLOSURE_PHRASES)

DayHours = list[str] | Literal["closed"]
_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_NAMES = {"monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"}
_CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})")


def _normalized_hours(value: dict[str, DayHours] | None, field: str) -> dict[str, DayHours] | None:
    """Hours exactly as every open/closed and booking check reads them: day keys mon..sun and zero-padded HH:MM, opening
    before closing. Open/closed is a text comparison, so "8:00" (which sorts after "09:15") would read as closed all day,
    and a misspelled day would silently be closed: both are fixed or refused here instead of on a live call."""
    if value is None:
        return None
    out: dict[str, DayHours] = {}
    for raw_day, hours in value.items():
        day = str(raw_day).strip().lower()
        day = _DAY_NAMES.get(day, day)
        if day not in _DAY_KEYS:
            raise ValueError(f"{field}: unknown day {raw_day!r}; use mon, tue, wed, thu, fri, sat, sun")
        if day in out:
            raise ValueError(f"{field}: {raw_day!r} repeats a day that is already set")
        if hours == "closed":
            out[day] = "closed"
            continue
        if len(hours) != 2:
            raise ValueError(f"{field}.{day}: give exactly one opening and one closing time, e.g. [\"08:00\", \"17:00\"], or closed")
        times = []
        for t in hours:
            m = _CLOCK_RE.fullmatch(str(t).strip())
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                raise ValueError(f"{field}.{day}: {t!r} is not a 24-hour time like 08:00 or 17:30")
            times.append(f"{int(m.group(1)):02d}:{m.group(2)}")
        if times[0] >= times[1]:
            raise ValueError(f"{field}.{day}: opening time {times[0]} must be before closing time {times[1]} (overnight hours are not supported)")
        out[day] = times
    return out


class Service(BaseModel):
    name: str
    duration_minutes: int


class Faq(BaseModel):
    q: str
    a: str


class Policy(BaseModel):
    """What this client's assistant is allowed to do, as typed switches (a typo in a YAML key is an error, not a silent default).
    The booking and transfer switches are enforced in code as well as in the prompt: a disabled tool is not offered to the model and is
    refused if it is called anyway. The wording switches (quotes, fees, address) are prompt rules, so they are only as strong as the model."""

    model_config = {"extra": "forbid"}

    can_book: bool = True
    can_reschedule: bool = True
    can_cancel: bool = True
    can_transfer: bool = True
    # Prices are never invented. True lets it repeat only a price written word for word in this client's FAQs.
    can_quote: bool = False
    # True lets it state ONE fixed dispatch/trip fee, exactly as written in dispatch_fee_text, and nothing else about money.
    can_state_dispatch_fee: bool = False
    dispatch_fee_text: str | None = Field(default=None, max_length=300)
    # True: it asks for the service address (street and zip) and puts it in the callback summary.
    can_collect_address: bool = False
    # What happens when a call arrives while the business is closed: "book" offers the next open time, "message" takes a message only.
    after_hours_action: Literal["book", "message"] = "book"
    # After the built-in 911 advice: "transfer" also rings the owner (default), "message" only alerts the owner.
    emergency_action: Literal["transfer", "message"] = "transfer"

    @model_validator(mode="after")
    def _fee_needs_text(self) -> "Policy":
        if self.can_state_dispatch_fee and not (self.dispatch_fee_text or "").strip():
            raise ValueError("policy.dispatch_fee_text is required when can_state_dispatch_fee is true")
        return self


class ClientConfig(BaseModel):
    client_id: str
    business_name: str
    vertical: str
    timezone: str
    model: str = "claude-haiku-4-5-20251001"
    opening_line: str
    business_hours: dict[str, DayHours]
    slot_minutes: int = 30
    services: list[Service]
    faqs: list[Faq] = Field(default_factory=list)
    escalation_phone: str
    max_turns: int = 12
    max_call_seconds: int = 360
    # Hours the AI may offer appointment slots in. Defaults to business_hours;
    # set separately when a business answers calls 24/7 but only schedules
    # consultations during office hours.
    booking_hours: dict[str, DayHours] | None = None
    # False for clients whose callers may volunteer health/legal details: the
    # call is still handled, but the words spoken are not stored or summarized.
    record_transcripts: bool = True
    # Extra owner-notification channels that work without SMS registration.
    owner_email: str | None = None
    ntfy_topic: str | None = None
    # Optional two-way Google Calendar sync: the client shares their calendar
    # with the service account ("Make changes to events") and this is
    # that calendar's ID (usually their Gmail address).
    google_calendar_id: str | None = None
    # The owner's PRIVATE iCal link (Google: "Secret address in iCal format"). Read only: events on it block those times.
    # See app/icalbusy.py. All-day events are ignored unless calendar_all_day_blocks is true.
    calendar_ical_url: str | None = Field(default=None, max_length=600)
    calendar_all_day_blocks: bool = False
    # Lets the client's website submit "call me back" requests to /lead.
    web_leads: bool = False
    # Abuse brake: after this many calls in a calendar month, new calls ring the owner
    # directly instead of starting the AI. None = the server default (MONTHLY_CALL_CEILING, 900).
    monthly_call_ceiling: int | None = None
    # Spending ceiling in estimated dollars per calendar month (see app/costing.py). None = the server default
    # (MONTHLY_COST_CEILING_USD, 150). Warnings go out at 75/90/100%; at 100% new calls use `ceiling_mode`.
    monthly_cost_ceiling_usd: float | None = Field(default=None, gt=0, le=100000)
    # What a caller gets once a ceiling is reached: "message" = we take their name/number/reason at no AI cost and
    # tell the owner; "transfer" = ring the owner's phone (if unanswered, a message is taken).
    ceiling_mode: Literal["message", "transfer"] = "message"
    # Worst-case loss cap (docs/WORST_CASE_CAP.md). After a ceiling is reached, once this many degraded calls have been handled
    # in the month, further calls are rejected before they are answered (unbilled). None = the server default (60).
    post_ceiling_call_cap: int | None = Field(default=None, ge=0, le=100000)
    # Longest an over-ceiling transfer (or emergency bridge) may stay connected; Twilio's own default would be 4 hours.
    ceiling_transfer_seconds: int = Field(default=300, ge=30, le=1800)
    # A public demo line: callers are prospects trying the product. Bookings, cancellations and booking history older than
    # 24 hours are deleted automatically so the demo calendar never fills up. Call records are kept (they feed the spending
    # estimate). Never set this on a paying client.
    demo_mode: bool = False
    # Explicit operator-controlled trial marker; never inferred from demos or billing.
    trial_enabled: bool = False

    @model_validator(mode="after")
    def _trial_is_not_a_demo(self) -> "ClientConfig":
        if self.trial_enabled and (self.demo_mode or self.client_id.startswith(("demo_", "prep_", "sample_"))):
            raise ValueError("Trials cannot be enabled on demos or sample tenants.")
        return self
    policy: Policy = Field(default_factory=Policy)
    # A demo line that offers a choice of fictional companies: {"1": "demo_nova_hvac", "2": "demo_nova_garage"}. The caller presses a
    # digit and is moved to that demo client. Only valid with demo_mode, and every target must itself be a demo (see app/main.py).
    demo_menu: dict[str, str] | None = None
    demo_menu_prompt: str | None = Field(default=None, max_length=700)
    # Lets a caller press 9 on the demo menu and type a private demo code (see app/storage.py private demos): a temporary, isolated
    # demo built for one prospect. Only valid on a demo menu.
    demo_private_codes: bool = False
    # A FICTIONAL business whose owner portal is shown to prospects in sales demos (scripts/seed_demo_portal.py fills it with sample
    # calls). Every portal page shows a "Sample business - demo data" banner and the portal is read-only for it. Needs demo_mode.
    portal_sample: bool = False
    # Who answers first. "ai_first" (default): the assistant answers every call. "owner_first": the owner's phone rings
    # first and the assistant answers only if they do not pick up (busy, unreachable or no answer within owner_ring_seconds).
    # "after_hours": owner first while the business is open, assistant directly when closed.
    # always_ring_owner: caller numbers (family, VIP customers) whose calls ring the owner first in every mode.
    # Do NOT use owner_first/after_hours when the owner's own phone forwards ALL calls to us: the ring would loop back.
    routing_mode: Literal["ai_first", "owner_first", "after_hours"] = "ai_first"
    owner_ring_seconds: int = Field(default=20, ge=10, le=40)
    always_ring_owner: list[str] = Field(default_factory=list, max_length=50)
    # Warm transfer: before a transferred call is connected, the owner hears who is calling and why, and presses 1 to accept.
    # Anything else (including their voicemail picking up) counts as unanswered, so the caller is offered a message instead of
    # landing in a voicemail box. OFF until verified with a real phone (see docs/TELEPHONY.md).
    transfer_screening: bool = False
    # Spanish (beta): offer "Para espa\u00f1ol, oprima dos." on every call. A caller who presses 2 gets
    # Spanish speech recognition, a native Spanish voice and Spanish replies. Off by default.
    spanish: bool = False
    # Speech recognition mode. "gather" (default) is the production path: Twilio <Gather>. "stream" = Twilio Media Streams + a streaming
    # STT provider (app/stream_stt.py). OFF unless set explicitly AND the server env flags/provider are ready AND no kill switch is set;
    # otherwise the call silently stays on Gather. Offline/unit-tested only. See docs/STREAM_STT_DESIGN.md.
    stt_mode: Literal["gather", "stream"] = "gather"
    # Optional: send signed webhooks (booking created/updated/cancelled, callback requested, call completed) to a URL the operator
    # sets. Not a native Zapier/CRM/field-service integration. See app/webhooks.py and docs/WEBHOOK_INTEGRATIONS.md.
    webhook_url: str | None = None
    webhook_secret: str | None = None
    # Monday email recap of calls answered / bookings (needs owner_email). Set false to opt out.
    weekly_recap: bool = True
    # Appended to the system prompt (used by the demo line to turn curious
    # callers into leads).
    extra_instructions: str = ""

    @property
    def effective_booking_hours(self) -> dict[str, DayHours]:
        return self.booking_hours if self.booking_hours is not None else self.business_hours

    @model_validator(mode="after")
    def _webhook_needs_https_and_a_secret(self) -> "ClientConfig":
        if self.webhook_url:
            if not self.webhook_url.lower().startswith("https://"):
                raise ValueError("webhook_url must start with https://")
            if not self.webhook_secret or len(self.webhook_secret) < 16:
                raise ValueError("webhook_secret (at least 16 characters) is required when webhook_url is set")
        return self

    @field_validator("business_hours", "booking_hours")
    @classmethod
    def _hours_shape(cls, v: dict[str, DayHours] | None, info) -> dict[str, DayHours] | None:
        return _normalized_hours(v, info.field_name)

    @field_validator("timezone")
    @classmethod
    def _timezone_exists(cls, v: str) -> str:
        # An unknown zone used to load fine and then crash every call and every owner-portal page.
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, OSError):        # OSError: a folder name such as "America"
            raise ValueError(f"timezone {v!r} is not a known IANA zone (for example America/New_York)") from None
        return v

    @field_validator("demo_menu")
    @classmethod
    def _demo_menu_shape(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if not v:
            return None
        for digit, target in v.items():
            if not re.fullmatch(r"[0-9]", digit) or not re.fullmatch(r"[A-Za-z0-9_]{1,60}", target):
                raise ValueError("demo_menu maps one digit (0-9) to a client id")
        return v

    @model_validator(mode="after")
    def _menu_needs_demo_mode(self) -> "ClientConfig":
        if self.demo_menu and not self.demo_mode:
            raise ValueError("demo_menu is only allowed on a demo_mode client")
        if self.demo_private_codes and not self.demo_menu:
            raise ValueError("demo_private_codes needs a demo_menu")
        if self.portal_sample and (not self.demo_mode or self.demo_menu or self.owner_email or self.webhook_url):
            raise ValueError("portal_sample is only allowed on a demo_mode client with no menu, owner_email or webhook")
        return self

    @field_validator("always_ring_owner")
    @classmethod
    def _vip_numbers_are_e164(cls, v: list[str]) -> list[str]:
        for n in v:
            if not re.fullmatch(r"\+1[0-9]{10}", n):
                raise ValueError(f"always_ring_owner entries must look like +15555550100, got {n!r}")
        return v

    @field_validator("calendar_ical_url")
    @classmethod
    def _ical_url_is_https(cls, v: str | None) -> str | None:
        if not v:
            return None
        v = v.strip()
        if v.lower().startswith("webcal://"):
            v = "https://" + v[9:]
        if not v.lower().startswith("https://") or " " in v:
            raise ValueError("calendar_ical_url must be an https:// (or webcal://) address with no spaces")
        return v

    @field_validator("opening_line")
    @classmethod
    def _opening_line_must_disclose_ai(cls, v: str) -> str:
        if not _discloses_ai(v):
            raise ValueError(
                "opening_line must clearly disclose this is AI, not a human — e.g. contain "
                "'AI receptionist', 'automated', or 'virtual assistant'. Maine requires this by "
                "law and Utah on request; the service discloses on every call regardless."
            )
        return v


class ClientNotFoundError(Exception):
    pass


def _config_path(client_id: str) -> Path | None:
    # client_id becomes a file name, so it must never be able to walk out of
    # the clients folders (e.g. "../something").
    if not _ID_RE.fullmatch(client_id or ""):
        return None
    if LIVE_CLIENTS_DIR is not None:
        live = LIVE_CLIENTS_DIR / f"{client_id}.yaml"
        if live.exists():
            return live
    baked = CLIENTS_DIR / f"{client_id}.yaml"
    return baked if baked.exists() else None


@functools.lru_cache(maxsize=512)
def load_client_config(client_id: str) -> ClientConfig:
    path = _config_path(client_id)
    if path is None:
        raise ClientNotFoundError(client_id)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ClientConfig.model_validate(raw)


def list_client_ids() -> list[str]:
    """Every client served right now: baked into the image plus uploaded to the volume."""
    ids = {p.stem for p in CLIENTS_DIR.glob("*.yaml")}
    if LIVE_CLIENTS_DIR is not None and LIVE_CLIENTS_DIR.exists():
        ids |= {p.stem for p in LIVE_CLIENTS_DIR.glob("*.yaml")}
    return sorted(i for i in ids if not i.startswith("_") and _ID_RE.fullmatch(i))


class ConfigRejected(ValueError):
    """An uploaded config that can't be served. The message says why."""


def save_live_config(yaml_text: str) -> tuple[ClientConfig, bool]:
    """Validate a client config and start serving it immediately. Returns
    (config, created); created is False when it replaced an existing client."""
    config = validate_live_config(yaml_text)
    client_id = config.client_id
    LIVE_CLIENTS_DIR.mkdir(parents=True, exist_ok=True)
    target = LIVE_CLIENTS_DIR / f"{client_id}.yaml"
    created = _config_path(client_id) is None
    tmp = target.with_suffix(".yaml.tmp")
    tmp.write_text(yaml_text, encoding="utf-8")
    os.replace(tmp, target)  # atomic: a call never sees half a file
    load_client_config.cache_clear()
    return config, created


def validate_live_config(yaml_text: str) -> ClientConfig:
    """Validate without making a client live (onboarding checks ownership first)."""
    if LIVE_CLIENTS_DIR is None:
        raise ConfigRejected("This server has no live config folder (CALLKETTLE_CLIENTS_DIR).")
    if len(yaml_text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ConfigRejected("Config is too large.")
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ConfigRejected(f"Not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigRejected("Config must be a YAML mapping.")
    client_id = str(raw.get("client_id", ""))
    if client_id.startswith("_") or not _ID_RE.fullmatch(client_id):
        raise ConfigRejected("client_id must be 1-60 letters, digits or underscores, and not start with _.")
    try:
        config = ClientConfig.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError: surface every problem to the operator
        raise ConfigRejected(str(exc)) from exc
    return config


def remove_live_config(client_id: str) -> bool:
    if LIVE_CLIENTS_DIR is None or not _ID_RE.fullmatch(client_id or ""):
        return False
    target = LIVE_CLIENTS_DIR / f"{client_id}.yaml"
    if not target.exists():
        return False
    target.unlink()
    load_client_config.cache_clear()
    return True
