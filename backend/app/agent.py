from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import anthropic

from app import cost_guard, cost_observability, datefix, storage, tools, twilio_utils
from app.config import ClientConfig

logger = logging.getLogger("callkettle.agent")

_client: anthropic.Anthropic | None = None


MAX_CALLER_CHARS = 600
# A spoken reply is 1-2 short sentences (~40 tokens); the largest tool call (escalate_to_human with a summary) is ~100.
# 220 keeps headroom for both while capping a runaway reply, which is billed as output tokens AND as TTS characters.
MAX_REPLY_TOKENS = 220
# Twilio gives a webhook ~15 s. Each model call has its own 6 s timeout; this caps the whole turn (several calls plus tools) so the hand-off
# TwiML is always returned in time.
TURN_BUDGET_SECONDS = 8.0


def _anthropic_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        # Twilio gives a webhook ~15s. The SDK default (10 min timeout, 2 retries)
        # would let one slow API call strand a live caller; fail fast instead
        # and let run_turn's fail-soft path take over.
        _client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""), timeout=6.0, max_retries=1
        )
    return _client


TOOLS = [
    {
        "name": "check_availability",
        "description": "Look up open appointment slots for a date, optionally near a preferred time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "preferred_time": {"type": "string", "description": "HH:MM 24h, optional"},
                "service": {"type": "string", "description": "Service the caller wants, exactly as listed, so long jobs only get times they fit."},
            },
            "required": ["date"],
        },
    },
    {
        "name": "book_appointment",
        "description": "Book a confirmed appointment, only after the caller agreed to a specific date and time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "caller_name": {"type": "string"},
                "caller_phone": {"type": "string"},
                "service": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "time": {"type": "string", "description": "HH:MM 24h"},
            },
            "required": ["caller_name", "caller_phone", "service", "date", "time"],
        },
    },
    {
        "name": "find_my_appointments",
        "description": "List the caller's upcoming appointments (matched on the calling number). Call first when they want to cancel, move or ask about one.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_appointment",
        "description": "Cancel one of the caller's appointments, only after find_my_appointments and a clear request to cancel.",
        "input_schema": {"type": "object", "properties": {"booking_id": {"type": "integer"}}, "required": ["booking_id"]},
    },
    {
        "name": "reschedule_appointment",
        "description": "Move one of the caller's appointments to a time check_availability offered. Use instead of booking a second one.",
        "input_schema": {
            "type": "object",
            "properties": {"booking_id": {"type": "integer"}, "date": {"type": "string", "description": "YYYY-MM-DD"}, "time": {"type": "string", "description": "HH:MM 24h"}},
            "required": ["booking_id", "date", "time"],
        },
    },
    {
        "name": "escalate_to_human",
        "description": "Hand off to the team for a callback: anything out of scope, upset callers, or anything you're unsure about.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "caller_name": {"type": "string"},
                "caller_phone": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["reason", "summary"],
        },
    },
    {
        "name": "end_call",
        "description": "End the call once the request is resolved or the caller says goodbye.",
        "input_schema": {
            "type": "object",
            "properties": {"closing_message": {"type": "string"}},
            "required": ["closing_message"],
        },
    },
    {
        "name": "transfer_call",
        "description": (
            "Connect the live caller to a real person right now. Use only when they explicitly ask for a human, "
            "or it is urgent and can't wait for a callback. Unlike escalate_to_human (owner follows up later, call ends), "
            "the caller stays on the line."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "handoff_message": {
                    "type": "string",
                    "description": "Spoken to the CALLER just before connecting (not a note to the owner). One short sentence.",
                },
            },
            "required": ["reason", "handoff_message"],
        },
    },
]


_WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def is_open_now(config: ClientConfig, now: datetime | None = None) -> bool:
    # One clock for every open/closed decision (tools._local_now), and closing time is exclusive, exactly like the booking
    # grid and tools.is_open_now: at 17:00 a 08:00-17:00 shop is closed, so an after-hours "message" policy already applies.
    now = now or tools._local_now(config)
    hours = config.business_hours.get(_WEEKDAY_KEYS[now.weekday()], "closed")
    return isinstance(hours, list) and hours[0] <= now.strftime("%H:%M") < hours[1]


_TOOL_POLICY = {
    "book_appointment": "can_book",
    "reschedule_appointment": "can_reschedule",
    "cancel_appointment": "can_cancel",
    "transfer_call": "can_transfer",
}


def tool_allowed(config: ClientConfig, name: str) -> bool:
    flag = _TOOL_POLICY.get(name)
    return True if flag is None else bool(getattr(config.policy, flag))


def tools_for(config: ClientConfig) -> list[dict]:
    """The tools this client's assistant is offered. A disabled one is not shown to the model at all (and is refused in dispatch if called)."""
    allowed = [t for t in TOOLS if tool_allowed(config, t["name"])]
    if not (config.policy.can_book or config.policy.can_reschedule):
        allowed = [t for t in allowed if t["name"] != "check_availability"]
    return allowed


def prompt_version() -> str:
    """Identifies the prompt template and tool schema in force (a change to either changes this)."""
    import inspect

    from app import outcomes

    return outcomes.fingerprint(inspect.getsource(build_system_prompt) + _SPANISH_RULES)


def tool_version() -> str:
    from app import outcomes

    return outcomes.fingerprint(TOOLS)


def config_version(config: ClientConfig) -> str:
    from app import outcomes

    dumped = config.model_dump(mode="json")
    if dumped.get("stt_mode") == "gather":   # a default client keeps the fingerprint it had before the stream-STT flag existed
        dumped.pop("stt_mode")
    return outcomes.fingerprint(dumped)


def _policy_rules(config: ClientConfig, open_now: bool) -> str:
    p = config.policy
    rules = [
        "- NEVER give repair, troubleshooting, do-it-yourself or safety instructions about the caller's equipment or problem (no \"try resetting the breaker\", \"turn off the water\", \"check the filter\", \"push the release cord\", \"hold the spring\"). "
        "Say you can't advise on that over the phone, and offer to book a technician or have the team call. The only safety step you ever give is the 911 advice below.",
    ]
    if p.can_quote:
        rules.append("- You may give a price ONLY if it is written word for word in the FAQs above. Otherwise say the technician will explain cost before any work starts.")
    else:
        rules.append("- Never quote, estimate or guess any price, fee, range or discount. Say the technician will explain the cost before any work starts.")
    if p.can_state_dispatch_fee:
        rules.append(f"- If asked, you may state this one fee exactly as written and nothing more about money: {p.dispatch_fee_text.strip()}")
    if p.can_collect_address:
        rules.append("- Ask for the service address (street and zip) and include it in the summary when you call escalate_to_human or hand off. Do not read it back unless asked.")
    else:
        rules.append("- Do not ask for a street address. If the caller wants to give one, take it only as part of a callback request (escalate_to_human) and do not repeat it back.")
    if not p.can_book:
        rules.append("- You cannot book appointments for this business. Take the caller's name, number and what they need, and call escalate_to_human with reason callback_requested.")
    if not p.can_reschedule or not p.can_cancel:
        rules.append("- You cannot change or cancel appointments for this business; take a message with escalate_to_human (reason callback_requested) instead.")
    if not p.can_transfer:
        rules.append("- You cannot connect the caller to a person on the line. If they ask for one, take their name and number and call escalate_to_human with reason callback_requested.")
    if p.after_hours_action == "message" and not open_now:
        rules.append("- The business is closed right now and does not book appointments by phone after hours. Take a message (name, number, what they need) and call escalate_to_human with reason after_hours_message. Do not offer appointment times.")
    return chr(10).join(rules)


def _open_status_line(config: ClientConfig, now: datetime) -> str:
    # Computed in code, not left to the model to infer from raw hours + a
    # timestamp — "AI wrongly claims open/closed" is a documented, common
    # real-world failure mode for this exact product category. Removing the
    # class of error beats instructing the model to avoid it.
    today_key = _WEEKDAY_KEYS[now.weekday()]
    today_hours = config.business_hours.get(today_key, "closed")
    if today_hours == "closed" or not isinstance(today_hours, list):
        return f"Right now: CLOSED — {config.business_name} does not open on {now.strftime('%A')}s."
    open_t, close_t = today_hours
    current = now.strftime("%H:%M")
    if open_t <= current < close_t:
        return f"Right now: OPEN — today's hours are {open_t}-{close_t}, closing at {close_t}."
    return f"Right now: CLOSED — today's hours are {open_t}-{close_t}, but the current time ({current}) is outside that window."


def _usable_caller_number(number: str | None) -> str | None:
    if not number:
        return None
    digits = re.sub(r"\D", "", number)
    return number if len(digits) >= 10 else None


_SPANISH_RULES = """
LANGUAGE (this call is in SPANISH; it overrides the English-only rule above): the caller chose Spanish. Speak ONLY Spanish
(neutral Latin American, polite "usted"), in 1-2 short spoken sentences. Say times like "diez de la ma\u00f1ana" and dates like
"el martes nueve"; read phone numbers digit by digit in groups. Tool arguments keep their required formats (dates YYYY-MM-DD,
times HH:MM, service names exactly as listed above). Everything else in the rules above still applies.
"""


def build_system_prompt(
    config: ClientConfig, caller_number: str | None = None, session_note: str = "", lang: str = "en"
) -> str:
    now = datetime.now(ZoneInfo(config.timezone))
    services = "\n".join(f"- {s.name} ({s.duration_minutes} min)" for s in config.services)
    faqs = "\n".join(f"Q: {f.q}\nA: {f.a}" for f in config.faqs)
    hours = "\n".join(
        f"- {day}: {('closed' if h == 'closed' else f'{h[0]}-{h[1]}')}"
        for day, h in config.business_hours.items()
    )
    open_status = _open_status_line(config, now)
    booking_hours = "\n".join(
        f"- {day}: {('no appointments' if h == 'closed' else f'{h[0]}-{h[1]}')}"
        for day, h in config.effective_booking_hours.items()
    )
    caller_id = _usable_caller_number(caller_number)
    caller_line = (
        f"The caller's number from caller ID is {caller_id}. When you need their phone number, ask whether "
        f"the number they're calling from is the best one to reach them, and use it if they say yes."
        if caller_id
        else "Caller ID is not available, so ask the caller for their phone number."
    )
    extra_text = config.extra_instructions.strip()
    extra = f"\n{extra_text}\n" if extra_text else ""
    policy_text = _policy_rules(config, is_open_now(config, now))
    if lang == "es":
        extra += _SPANISH_RULES
    if session_note:
        extra += f"\nRIGHT NOW IN THIS CALL: {session_note}\n"
    return f"""You are the AI phone receptionist for {config.business_name}, a {config.vertical} business.
Today is {now.strftime('%A, %Y-%m-%d')}, current time {now.strftime('%H:%M')} ({config.timezone}).

{open_status}
To answer whether you're open right now, use this line; don't recompute it from the hours table.

Business hours:
{hours}

Appointments can only be booked in these windows (check_availability enforces it):
{booking_hours}

{caller_line}

Services:
{services}

FAQs:
{faqs}

Rules:
- You are heard over the phone via text-to-speech. Keep every reply to 1-2 short sentences, at most 25 words: no filler, no repeating what the caller just said, no lists. Only the readbacks required below may run longer.
- Never invent availability — always call check_availability before promising a time.
- Get the caller's FIRST AND LAST name and phone number before calling book_appointment. Speech recognition often mishears names: if you only caught one name, or it sounds odd, ask again (offer to have them spell it), and say the full name back to confirm before booking. Never book under a name you are unsure of.
- If the caller asks what something costs, answer that FIRST in one short sentence (the technician explains the cost and gets their approval before any work starts; you cannot quote prices), then continue helping.
- If something is outside what you know or can do, or the caller seems upset, call escalate_to_human rather than guessing (the team follows up later).
- If the caller asks whether they are talking to a real person or a robot, answer honestly and briefly that you are an AI receptionist for {config.business_name}, then offer to connect them to a person. Never claim to be human.
- If the caller asks to speak to a person, the owner, a manager, or anyone real — in ANY phrasing, including casual or indirect ones ("put me through to your mom," "is there a manager I can talk to," "can I talk to the owner") — call transfer_call immediately. Don't ask which they'd prefer or offer a callback; connect them right now. Treat "your mom," "the owner" and "the manager" as the business's real point of contact, and never act confused by the phrasing.
- Use escalate_to_human only when the caller has NOT asked to be connected live — e.g. they just want a message left, or it's after a limit was hit.
- Call end_call once the caller's need is resolved or they say goodbye.
- Never discuss pricing you are not given above. Never give medical, legal, or safety advice — escalate instead.
- Speak like a person: say times as "ten a.m." or "two thirty p.m.", dates as "Tuesday the ninth", and read phone numbers back in groups of digits ("five seven one, two nine zero, eight nine zero eight"). Never read out symbols, ISO dates, or 24-hour times.
- Do not ask for, and do not repeat back, sensitive details such as diagnoses, medications, Social Security numbers, or card numbers. If the caller starts sharing medical or personal details, say you don't need those and only take their name, phone number, and what they're calling about; the team will discuss specifics directly.
- SAFETY FIRST: if anyone may be in immediate danger (fire, gas smell, carbon monoxide alarm, someone injured, unconscious, or having a medical emergency), tell them to hang up and call 911 right now. That comes before anything else. Then call transfer_call so the owner is alerted, with a handoff_message that starts with the 911 advice.
- Changing or cancelling (including an appointment booked earlier in this call): call find_my_appointments, confirm which one with the caller, then use reschedule_appointment (after check_availability offers a new time) or cancel_appointment. NEVER book a second appointment to "move" one. Never say it is changed or cancelled until the tool returns success. If find_my_appointments reports caller ID is unavailable or finds nothing, take their name and number and call escalate_to_human.
- After book_appointment succeeds, tell the caller it is confirmed. Say a confirmation text was sent only if the result has confirmation_text_sent true; otherwise say the team will follow up to confirm.
- If a caller speaks a language other than English, say in English that you can only help in English right now, and call escalate_to_human so someone can call them back.
{policy_text}
{extra}"""


@dataclass
class CallSession:
    call_sid: str
    client_id: str
    config: ClientConfig
    messages: list[dict] = field(default_factory=list)
    caller_number: str | None = None
    session_note: str = ""
    done: set = field(default_factory=set)   # outcomes that REALLY happened this call: book, reschedule, cancel, lookup
    lang: str = "en"               # "es" after the caller presses 2 (Spanish beta)
    offer_spanish: bool = False    # the next prompt should invite the caller to press 2
    turn_count: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Private cost observability: cumulative provider-reported totals + a monotonic snapshot revision.
    obs_tokens: dict = field(default_factory=dict)
    obs_rev: int = 0
    gather_count: int = 0          # Gather TwiML the server emitted for this call (server-measured upper bound)
    tts_chars: int = 0             # characters of text the server put into <Say> for this call (never caller text)
    stt_seconds: float = 0.0       # measured seconds of mu-law audio sent to a streaming STT provider (stream mode only)
    transfer_count: int = 0
    # One turn at a time per call: a retried or duplicated webhook must not interleave with the turn already running, or the
    # message history stops alternating and the model API rejects every later turn of the call.
    turn_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)


_SESSIONS: dict[str, CallSession] = {}
# In-memory session store — fine for a single-process prototype deployment.
# Move to Redis (keyed by CallSid, short TTL) before running multiple uvicorn
# workers or more than one instance, or calls will land on the wrong worker.


def start_session(call_sid: str, config: ClientConfig, caller_number: str | None = None) -> CallSession:
    session = CallSession(
        call_sid=call_sid, client_id=config.client_id, config=config, caller_number=caller_number
    )
    _SESSIONS[call_sid] = session
    try:
        storage.set_call_versions(call_sid, config_version=config_version(config), prompt_version=prompt_version(),
                                  tool_version=tool_version(), model_id=config.model)
    except Exception:
        logger.exception("Could not record the config/prompt versions for %s", call_sid)
    return session


_RESTART_NOTE = (
    "The phone system restarted a moment ago, so part of this call may be missing from your memory. Carry on naturally: do not re-ask "
    "for details already in the conversation above; if you are unsure where things stood, apologize briefly for the interruption "
    "and ask once."
)


def recover_session(call_sid: str, config: ClientConfig, caller_number: str | None = None) -> CallSession:
    """A caller is mid-call but this process has no memory of them (it restarted or was redeployed). Rebuild what the database
    knows: when the call began, how many turns it has had (so the length caps still hold), whether a booking was already made, and,
    for businesses that keep transcripts, the conversation so far. Never raises: the worst case is a fresh session."""
    session = start_session(call_sid, config, caller_number)
    session.session_note = _RESTART_NOTE
    try:
        storage.record_metric("session_recovered", config.client_id)
        row = storage.get_call(call_sid)
        if row is None or row.get("client_id") != config.client_id:
            return session
        began = datetime.fromisoformat(row["started_at"])
        session.started_at = began if began.tzinfo else began.replace(tzinfo=timezone.utc)
        transcript = json.loads(row.get("transcript_json") or "[]")
        session.turn_count = sum(1 for t in transcript if t.get("role") == "caller")
        history: list[dict] = []
        for turn in transcript:
            text = turn.get("text")
            if not text or text == storage.NOT_RECORDED:
                continue
            role = "user" if turn.get("role") == "caller" else "assistant"
            if history and history[-1]["role"] == role:
                history[-1]["content"] += " " + text
            else:
                history.append({"role": role, "content": text})
        while history and history[-1]["role"] == "user":
            history.pop()                      # an unanswered caller line is re-sent as the new turn
        if history and history[0]["role"] == "assistant":
            history.insert(0, {"role": "user", "content": "(The call began and the greeting was played.)"})
        session.messages = history
        if storage.count_call_bookings(call_sid):
            session.done.add("book")
    except Exception:
        logger.exception("Could not fully rebuild the session for %s", call_sid)
    return session


def get_session(call_sid: str) -> CallSession | None:
    return _SESSIONS.get(call_sid)


def end_session(call_sid: str) -> None:
    _SESSIONS.pop(call_sid, None)


def session_count() -> int:
    return len(_SESSIONS)


def purge_stale_sessions(max_age_seconds: float) -> int:
    """Live-call state is freed when Twilio reports a call ended. If that report
    is ever lost, this stops the memory from growing forever."""
    now = datetime.now(timezone.utc)
    stale = [sid for sid, s in list(_SESSIONS.items()) if (now - s.started_at).total_seconds() > max_age_seconds]
    for sid in stale:
        _SESSIONS.pop(sid, None)
    return len(stale)


FINAL_REVISION = 1_000_000  # provider-final values (Twilio callbacks) outrank any in-call snapshot


def observe(client_id: str, call_sid: str, evidence: dict, revision: int) -> None:
    """Record cumulative measured evidence in the private ledger. Never raises, never affects the call."""
    try:
        if not storage.live_path_open():
            return                                  # the database is locked right now: skip the measurement, never hold the caller
        cost_observability.record_snapshot(storage.DB_PATH, client_id, call_sid, evidence, revision=revision, timeout=storage.LIVE_BUSY_SECONDS)
    except Exception as exc:
        storage.trip_live_breaker(exc)
        logger.exception("Could not record private cost evidence for %s", call_sid)


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _observe_tokens(call_sid: str, usage, tenant: str | None = None) -> None:
    try:
        session = _SESSIONS.get(call_sid)
        if session is None:
            if tenant:     # post-call work (the summary) after the live session ended: add to the ledger's totals
                _observe_tokens_after_call(call_sid, usage, tenant)
            return
        fields = (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                  ("cache_read_tokens", "cache_read_input_tokens"), ("cache_write_tokens", "cache_creation_input_tokens"))
        for metric, attr in fields:
            n = _count(getattr(usage, attr, None))
            if n is not None:  # a field the provider did not report stays unknown, never measured zero
                session.obs_tokens[metric] = session.obs_tokens.get(metric, 0) + n
        session.obs_rev += 1
        evidence = {m: {"value": v, "status": "measured", "source": "anthropic_usage"} for m, v in session.obs_tokens.items()}
        observe(session.client_id, call_sid, evidence, session.obs_rev)
    except Exception:
        logger.exception("Could not observe model usage for %s", call_sid)


def _observe_tokens_after_call(call_sid: str, usage, tenant: str) -> None:
    try:
        if not storage.live_path_open():
            return
        deltas = {m: _count(getattr(usage, attr, None)) for m, attr in (
            ("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
            ("cache_read_tokens", "cache_read_input_tokens"), ("cache_write_tokens", "cache_creation_input_tokens"))}
        cost_observability.add_token_usage(storage.DB_PATH, tenant, call_sid, {m: v for m, v in deltas.items() if v is not None},
                                           timeout=storage.LIVE_BUSY_SECONDS)
    except Exception as exc:
        storage.trip_live_breaker(exc)
        logger.exception("Could not record post-call token usage for %s", call_sid)


def record_usage(call_sid: str, response, started: float, tenant: str | None = None) -> None:
    """Remember what this model call cost (tokens) and how long it took. Never raises."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        storage.add_model_usage(
            call_sid,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            ms=int((time.perf_counter() - started) * 1000),
        )
        _observe_tokens(call_sid, usage, tenant)
    except Exception:
        logger.exception("Could not record model usage for %s", call_sid)


def _dispatch_tool(session: CallSession, name: str, tool_input: dict) -> dict:
    config = session.config
    if not tool_allowed(config, name):
        storage.record_metric("policy_blocked_tool", config.client_id)
        return {"error": "that action is not available for this business; take the caller's name and number and call escalate_to_human with reason callback_requested"}
    if name in ("book_appointment", "reschedule_appointment") and config.policy.after_hours_action == "message" and not is_open_now(config):
        storage.record_metric("policy_blocked_tool", config.client_id)
        return {"error": "the business does not book by phone after hours; take a message and call escalate_to_human with reason after_hours_message"}
    if name == "check_availability":
        return tools.check_availability(
            config=config, date=tool_input.get("date", ""), preferred_time=tool_input.get("preferred_time"),
            service=tool_input.get("service"),
        )
    if name == "book_appointment":
        return tools.book_appointment(
            call_sid=session.call_sid,
            config=config,
            caller_name=tool_input.get("caller_name", ""),
            caller_phone=tool_input.get("caller_phone", ""),
            service=tool_input.get("service", ""),
            date=tool_input.get("date", ""),
            time=tool_input.get("time", ""),
        )
    if name == "find_my_appointments":
        return tools.find_my_appointments(config=config, caller_id=session.caller_number)
    if name == "cancel_appointment":
        return tools.cancel_appointment(
            config=config, call_sid=session.call_sid, booking_id=tool_input.get("booking_id", -1), caller_id=session.caller_number
        )
    if name == "reschedule_appointment":
        return tools.reschedule_appointment(
            config=config, call_sid=session.call_sid, booking_id=tool_input.get("booking_id", -1),
            new_date=tool_input.get("date", ""), new_time=tool_input.get("time", ""), caller_id=session.caller_number,
        )
    if name == "escalate_to_human":
        return tools.escalate_to_human(
            call_sid=session.call_sid,
            config=config,
            reason=tool_input.get("reason", "unspecified"),
            caller_name=tool_input.get("caller_name"),
            caller_phone=tool_input.get("caller_phone") or _usable_caller_number(session.caller_number),   # the owner always gets a number to call back
            summary=tool_input.get("summary", ""),
        )
    if name == "end_call":
        return {"ended": True}
    if name == "transfer_call":
        return {"transferring": True}
    return {"error": f"unknown tool {name}"}


# A deterministic backstop, not a replacement for the model's own judgment via
# the transfer_call tool. An explicit, unambiguous request for a human must
# transfer reliably every time — it can't depend on an LLM correctly noticing
# the request in the middle of a longer conversation, especially several
# turns deep where earlier instructions can get de-prioritized. Word-boundary
# matched to avoid false positives (e.g. "personal", "impersonate").
_HUMAN_TARGET = r"((a\s+|an\s+|the\s+|your\s+)?(person|human|someone|somebody|owner|manager|business\s+owner)|your\s+mom)"

_HUMAN_REQUEST_RE = re.compile(
    r"\b(real|actual|live)\s+(person|human)\b"
    r"|\btalk\s+to\s+" + _HUMAN_TARGET + r"\b"
    r"|\bspeak\s+(to|with)\s+" + _HUMAN_TARGET + r"\b"
    r"|\bput\s+(me\s+)?through(\s+to\s+" + _HUMAN_TARGET + r")?\b"
    r"|\btransfer\s+me\b"
    r"|\bconnect\s+me\s+(to|with)(\s+" + _HUMAN_TARGET + r")?\b"
    r"|\brepresentative\b",
    re.IGNORECASE,
)


# Phrases that mean someone may be in danger. Deliberately narrow so ordinary
# service requests ("gas water heater," "fireplace inspection") don't trigger it.
_EMERGENCY_RE = re.compile(
    r"\b(smell(s|ed|ing)?\s+(of\s+)?(natural\s+)?gas|gas\s+(leak|smell|odor)|(a\s+)?leak(ing)?\s+gas)\b"
    r"|\bcarbon\s+monoxide\b|\bco\s+alarm\b"
    r"|\b(on|in)\s+fire\b|\bthere'?s\s+a\s+fire\b|\bhouse\s+fire\b|\bfire\s+in\s+(the|my)\b"
    r"|\b(not|stopped|isn'?t)\s+breathing\b|\bcan'?t\s+breathe\b|\bunconscious\b|\bunresponsive\b"
    r"|\bheart\s+attack\b|\bchest\s+pain\b|\boverdos(e|ed|ing)\b"
    r"|\bneed\s+an?\s+ambulance\b|\bcall(ing)?\s+911\b"
    r"|\bsomeone\s+(is\s+)?(hurt|injured|dying|bleeding)\b",
    re.IGNORECASE,
)


_MSGS = {
    "limit": ("Let me get a team member to help you directly from here \u2014 they'll follow up shortly.",
              "Voy a pedir a alguien del equipo que le ayude directamente. Le llamar\u00e1n pronto."),
    "error_transfer": ("Sorry, I'm having trouble on my end. I'm connecting you with the team right now.",
                       "Perd\u00f3n, tengo un problema en este momento. Le conecto con el equipo ahora mismo."),
    "error": ("Sorry, I'm having trouble right now \u2014 I'll have someone from the team call you back shortly.",
              "Perd\u00f3n, tengo un problema en este momento. Alguien del equipo le llamar\u00e1 pronto."),
    "loop": ("Let me connect you with a team member to finish this up.",
             "Voy a conectarle con alguien del equipo para terminar esto."),
    "emergency": ("This sounds like an emergency. Please hang up and call 911 right now. I'm also alerting the team.",
                  "Esto parece una emergencia. Cuelgue y llame al 911 ahora mismo. Tambi\u00e9n estoy avisando al equipo."),
    "human": ("Of course \u2014 connecting you now.", "Claro, le conecto ahora mismo."),
    "again": ("Sorry, could you say that again?", "Perd\u00f3n, \u00bfpuede repetirlo?"),
}


def _m(session: "CallSession", key: str) -> str:
    return _MSGS[key][1 if session.lang == "es" else 0]


SPANISH_OFFER = "[es]Para hablar en espa\u00f1ol, oprima dos.[/es] "
NON_ENGLISH_HANDOFF = (
    "I'm sorry, I can only help in English right now. [es]Lo siento, por ahora solo puedo ayudar en ingl\u00e9s. "
    "Le avisar\u00e9 al equipo para que le llamen.[/es] I've let the team know and someone will call you back. Goodbye."
)


def spanish_greeting(config: ClientConfig) -> str:
    return f"Con gusto le ayudo en espa\u00f1ol. Gracias por llamar a {config.business_name}. \u00bfEn qu\u00e9 le puedo ayudar?"


def switch_to_spanish(session: "CallSession") -> str:
    """The caller pressed 2. Everything from here is Spanish: recognition, voice and replies."""
    session.lang = "es"
    session.offer_spanish = False
    greeting = spanish_greeting(session.config)
    _log_turn(session.call_sid, "ai", greeting, store_text=session.config.record_transcripts)
    return greeting


# Spanish safety nets. Like the English ones they never depend on the model.
_EMERGENCY_ES_RE = re.compile(
    r"\b(huele\s+a\s+gas|olor\s+a\s+gas|fuga\s+de\s+gas|escape\s+de\s+gas|incendio|hay\s+fuego|se\s+est\u00e1\s+quemando|"
    r"mon\u00f3xido\s+de\s+carbono|monoxido\s+de\s+carbono|no\s+respira|ambulancia|ataque\s+al\s+coraz\u00f3n|dolor\s+de\s+pecho|"
    r"sobredosis|inconsciente|est\u00e1\s+sangrando|alguien\s+est\u00e1\s+herido)\b",
    re.IGNORECASE,
)
_HUMAN_ES_RE = re.compile(
    r"\bhablar\s+con\s+(una\s+|un\s+|el\s+|la\s+)?(persona|humano|alguien|due\u00f1o|due\u00f1a|gerente|encargado|encargada)\b"
    r"|\bpersona\s+real\b|\bp\u00e1same\b|\bpasame\b|\boperador(a)?\b|\bcon\s+(el|la)\s+(due\u00f1o|due\u00f1a|gerente)\b",
    re.IGNORECASE,
)
# Does this caller seem to be speaking Spanish to an English-recognition line? Deliberately conservative.
_SPANISH_WORDS = re.compile(
    r"\b(hola|buenas|buenos\s+d\u00edas|buenas\s+tardes|necesito|quiero|quisiera|ayuda|por\s+favor|gracias|tengo|espa\u00f1ol|"
    r"hablan?\s+espa\u00f1ol|plomero|electricista|funciona|estoy|est\u00e1|mi\s+casa|cu\u00e1nto|cuanto\s+cuesta|se\u00f1or|se\u00f1ora)\b",
    re.IGNORECASE,
)


def looks_spanish(text: str) -> bool:
    if re.search(r"[\u00f1\u00bf\u00a1]", text, re.IGNORECASE):
        return True
    if re.search(r"\b(espa[\u00f1n]ol|spanish)\b", text, re.IGNORECASE):
        return True
    if re.search(r"\bhola\b", text, re.IGNORECASE):
        return True
    return len({m.group(0).lower() for m in _SPANISH_WORDS.finditer(text)}) >= 2


# "Am I talking to a real person or a robot?" is a QUESTION about what the caller is talking to, not a request to be
# transferred. It must get an honest answer (it is an AI), not an instant ring to the owner.
_IDENTITY_QUESTION_RE = re.compile(
    r"\b(am\s+i\s+(talking|speaking)\s+(to|with)|are\s+you|is\s+this|is\s+that|who\s+am\s+i\s+talking)\b.{0,40}"
    r"\b(robot|bot|machine|computer|ai|a\.i\.|automated|recording|real\s+(person|human)|human|person|live)\b",
    re.IGNORECASE,
)
_EXPLICIT_TRANSFER_RE = re.compile(
    r"\b(i\s+(want|need|would\s+like|'d\s+like)\s+(to\s+)?(talk|speak)|let\s+me\s+(talk|speak)|put\s+me|connect\s+me|transfer\s+me|get\s+me|"
    r"can\s+i\s+(please\s+)?(talk|speak)|give\s+me\s+(a|the)\s+(person|human|owner|manager))\b",
    re.IGNORECASE,
)


def is_identity_question(text: str) -> bool:
    return bool(_IDENTITY_QUESTION_RE.search(text)) and not _EXPLICIT_TRANSFER_RE.search(text)


# ---------------------------------------------------------------------------------------------
# HARD INVARIANT: the caller is never told something was booked, moved or cancelled unless the server
# actually did it. The model writes the words; this code checks them against what really happened and
# replaces a false claim with an honest hand-off. Authorization and truth belong to deterministic code.
_BOOKED_CLAIM = re.compile(
    r"(you'?re|you\s+are)\s+(all\s+)?(set|booked|scheduled|confirmed)\b|"
    r"\b(appointment|visit|booking|slot|estimate|time)\s+(is|has\s+been|was|'s)\s+(now\s+)?(confirmed|booked|scheduled|set)\b|"
    r"\bi('ve|\s+have)\s+(booked|scheduled|confirmed)\b|\ball\s+set\s+for\b|\bbooked\s+you\b|"
    r"\b(su\s+cita|su\s+visita)\s+(est\u00e1|queda|ha\s+sido)\s+(confirmad|agendad|programad)|\bya\s+(est\u00e1|qued\u00f3)\s+(agendad|confirmad|programad)|\bagend\u00e9\b",
    re.IGNORECASE,
)
_CANCELLED_CLAIM = re.compile(
    r"\b(is|has\s+been|was|'s|got)\s+(now\s+)?cancel+ed\b|\bi('ve|\s+have)\s+cancel+ed\b|\ball\s+done.{0,40}cancel+ed\b|"
    r"\b(est\u00e1|ha\s+sido|qued\u00f3)\s+cancelad[ao]\b|\bcanc\u00e9l[eo]\b",
    re.IGNORECASE,
)
_MOVED_CLAIM = re.compile(
    r"\b(is|has\s+been|was|'s|got)\s+(now\s+)?(rescheduled|moved|changed)\b|\bi('ve|\s+have)\s+(rescheduled|moved|changed)\b|"
    r"\byou'?re\s+(now\s+)?(rescheduled|moved)\b|\b(est\u00e1|ha\s+sido|qued\u00f3)\s+(reprogramad|cambiad)[ao]\b|\breprogram\u00e9\b",
    re.IGNORECASE,
)
_SAFE_RETREAT = (
    "I'm sorry, I wasn't able to complete that on my end. I'll have someone from the team follow up with you shortly to take care of it.",
    "Lo siento, no pude completar eso de mi lado. Alguien del equipo le contactar\u00e1 pronto para resolverlo.",
)


def claimed_actions(text: str) -> set[str]:
    out = set()
    if _BOOKED_CLAIM.search(text):
        out.add("book")
    if _CANCELLED_CLAIM.search(text):
        out.add("cancel")
    if _MOVED_CLAIM.search(text):
        out.add("reschedule")
    return out


def unsupported_claims(session: "CallSession", text: str) -> set[str]:
    """Claims in `text` that nothing in this call backs up."""
    bad = set()
    for claim in claimed_actions(text):
        if claim == "book" and not (session.done & {"book", "reschedule", "lookup"}):
            bad.add(claim)
        elif claim in ("cancel", "reschedule") and claim not in session.done:
            bad.add(claim)
    return bad


def _fix_dates(session: "CallSession", text: str) -> str:
    """A weekday that does not match its date is corrected before the caller hears it (see app/datefix.py)."""
    try:
        fixed, n = datefix.fix_weekdays(text, datetime.now(ZoneInfo(session.config.timezone)))
    except Exception:
        logger.exception("Weekday check failed")
        return text
    if n:
        storage.record_metric("weekday_corrected", session.config.client_id, n)
        logger.warning("Corrected %d wrong weekday(s) on call %s", n, session.call_sid)
    return fixed


def _guard_claims(session: "CallSession", text: str) -> str:
    bad = unsupported_claims(session, text)
    if not bad:
        return text
    logger.warning("Blocked an unsupported claim (%s) on call %s", ",".join(sorted(bad)), session.call_sid)
    storage.record_metric("blocked_false_confirmation", session.config.client_id)
    tools.escalate_to_human(
        call_sid=session.call_sid, config=session.config, reason="blocked_false_confirmation", caller_name=None,
        caller_phone=session.caller_number,
        summary=f"The assistant was about to tell the caller something was {'/'.join(sorted(bad))} but the system had not done it, so the message was blocked. Please follow up with the caller.",
    )
    return _SAFE_RETREAT[1 if session.lang == "es" else 0]


def _record_outcome(session: "CallSession", tool: str, result: dict) -> None:
    ok = isinstance(result, dict) and (result.get("success") is True)
    if tool == "book_appointment" and ok:
        session.done.add("book")
    elif tool == "reschedule_appointment" and ok:
        session.done.add("reschedule")
    elif tool == "cancel_appointment" and ok:
        session.done.add("cancel")
    elif tool == "find_my_appointments" and isinstance(result, dict) and result.get("appointments"):
        session.done.add("lookup")


def _log_turn(*args, **kwargs) -> None:
    """The transcript is a record, not the call: a database problem must never stop the call, least of all a 911 message."""
    try:
        storage.log_turn(*args, **kwargs)
    except Exception:
        logger.exception("Could not write the transcript")


def _can_ring_owner(config: ClientConfig) -> bool:
    """Live transfer is allowed AND the destination is a number that can actually be dialed."""
    return bool(config.policy.can_transfer and twilio_utils.is_dialable(config.escalation_phone))


def _caller_gist(session: CallSession) -> str:
    """What the caller asked for, from this call's own words, for the owner's hand-off. Only for businesses that keep transcripts
    (otherwise caller words are not kept anywhere); scrubbed and short."""
    if not session.config.record_transcripts:
        return ""
    said = " / ".join(m["content"] for m in session.messages if m.get("role") == "user" and isinstance(m.get("content"), str))
    return f" The caller said: {said[:300]}" if said else ""


def run_turn(session: CallSession, caller_text: str) -> tuple[str, bool, str | None]:
    """Returns (reply_text, should_end, transfer_to). transfer_to is the phone
    number to <Dial> the caller into, or None for a normal reply/hangup."""
    with session.turn_lock:
        try:
            return _run_turn(session, caller_text)
        finally:
            session.messages = cost_guard.repair_history(session.messages)   # whatever way the turn ended, the next turn gets a history the model API accepts


def _run_turn(session: CallSession, caller_text: str) -> tuple[str, bool, str | None]:
    limit = cost_guard.check_limits(
        config=session.config, turn_count=session.turn_count, started_at=session.started_at
    )
    if limit.exceeded:
        tools.escalate_to_human(
            call_sid=session.call_sid,
            config=session.config,
            reason=limit.reason or "limit_reached",
            caller_name=None,
            caller_phone=_usable_caller_number(session.caller_number),
            summary="Call hit its length/turn limit; handing off to a human." + _caller_gist(session),
        )
        return (_m(session, "limit"), True, None)

    # A speech result is a few sentences. Anything far longer is abuse or a transcription runaway, and every
    # character costs model tokens on every later turn.
    caller_text = re.sub(r"[\x00-\x1f\x7f]+", " ", caller_text or "").strip()[:MAX_CALLER_CHARS]

    session.turn_count += 1
    store = session.config.record_transcripts
    _log_turn(session.call_sid, "caller", caller_text, store_text=store)

    english_emergency = bool(_EMERGENCY_RE.search(caller_text))
    spanish_emergency = bool(_EMERGENCY_ES_RE.search(caller_text))  # unambiguous phrases, so checked in every mode
    if english_emergency or spanish_emergency:
        # Safety can't depend on the model. Tell the caller to call 911, alert
        # the owner, and bridge the call in case they stay on the line.
        if session.lang == "es":
            message = _MSGS["emergency"][1]
        elif spanish_emergency and not english_emergency:
            message = _MSGS["emergency"][0] + " [es]" + _MSGS["emergency"][1] + "[/es]"  # heard in both languages
        else:
            message = _MSGS["emergency"][0]
        _log_turn(session.call_sid, "ai", re.sub(r"\[/?es\]", "", message), store_text=store)
        tools.escalate_to_human(
            call_sid=session.call_sid,
            config=session.config,
            reason="possible_emergency",
            caller_name=None,
            caller_phone=session.caller_number,
            summary="Caller described a possible emergency and was told to call 911. Follow up right away.",
        )
        return (message, True, session.config.escalation_phone if session.config.policy.emergency_action == "transfer" else None)

    if not is_identity_question(caller_text) and (
        _HUMAN_REQUEST_RE.search(caller_text) or (session.lang == "es" and _HUMAN_ES_RE.search(caller_text))
    ):
        if _can_ring_owner(session.config):
            message = _m(session, "human")
            _log_turn(session.call_sid, "ai", message, store_text=store)
            return (message, True, session.config.escalation_phone)
        session.session_note = (
            "The caller asked for a person, but this business does not connect calls live. Say so kindly, take their name and "
            "best number, then call escalate_to_human with reason callback_requested and end the call."
        )

    if session.lang == "en" and looks_spanish(caller_text):
        if session.config.spanish:
            session.offer_spanish = True
            message = SPANISH_OFFER + "I'm sorry, to continue in Spanish, press 2. Otherwise, how can I help you in English?"
            _log_turn(session.call_sid, "ai", re.sub(r"\[/?es\]", "", message), store_text=store)
            return (message, False, None)
        message = NON_ENGLISH_HANDOFF
        _log_turn(session.call_sid, "ai", re.sub(r"\[/?es\]", "", message), store_text=store)
        tools.escalate_to_human(
            call_sid=session.call_sid,
            config=session.config,
            reason="non_english_caller",
            caller_name=None,
            caller_phone=session.caller_number,
            summary="The caller appears to speak Spanish. They were told we can only help in English and that someone will call back. Please call them back, ideally in Spanish.",
        )
        return (message, True, None)

    session.messages.append({"role": "user", "content": caller_text})
    session.messages = cost_guard.repair_history(cost_guard.trim_history(session.messages))

    system_prompt = build_system_prompt(session.config, session.caller_number, session.session_note, session.lang)
    client = _anthropic_client()

    turn_began = time.perf_counter()
    for _ in range(cost_guard.MAX_TOOL_ITERATIONS_PER_TURN):
        if time.perf_counter() - turn_began > TURN_BUDGET_SECONDS:
            break                                   # another model round trip would pass Twilio's webhook deadline: hand off now
        try:
            session.messages = cost_guard.repair_history(session.messages)
            started = time.perf_counter()
            response = client.messages.create(
                model=session.config.model,
                max_tokens=MAX_REPLY_TOKENS,
                system=system_prompt,
                messages=session.messages,
                tools=tools_for(session.config),
            )
            record_usage(session.call_sid, response, started)
            if not isinstance(response.content, (list, tuple)) or not all(hasattr(b, "type") for b in response.content):
                raise ValueError("malformed model response")   # handled exactly like any other model failure
        except Exception:
            logger.exception("Anthropic API call failed for call_sid=%s", session.call_sid)
            from app import ops

            ops.alert_operator(
                "AI model error on a live call",
                f"{session.config.business_name}: the model API failed mid-call; the caller was put through to the owner's phone (or told a callback is coming). "
                "Check the Anthropic status page and your API credit balance.",
                key="model",
            )
            tools.escalate_to_human(
                call_sid=session.call_sid,
                config=session.config,
                reason="agent_error",
                caller_name=None,
                caller_phone=_usable_caller_number(session.caller_number),
                summary="The AI agent errored mid-call; the caller was put through to the owner (or told a callback is coming). Follow up.",
            )
            if _can_ring_owner(session.config):
                return (_m(session, "error_transfer"), True, session.config.escalation_phone)      # the promise on the website: if the AI fails, your phone rings
            return (_m(session, "error"), True, None)

        if response.stop_reason != "tool_use":
            text = _fix_dates(session, "".join(b.text for b in response.content if b.type == "text").strip())
            session.messages.append({"role": "assistant", "content": response.content})
            guarded = _guard_claims(session, text)
            _log_turn(session.call_sid, "ai", guarded, store_text=store)
            return (guarded or _m(session, "again"), guarded != text, None)

        session.messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        should_end = False
        end_message = ""
        transfer_to: str | None = None
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                result = _dispatch_tool(session, block.name, block.input)
            except Exception:
                logger.exception(
                    "Tool %s raised for call_sid=%s — degrading gracefully", block.name, session.call_sid
                )
                result = {"error": "that action failed on our end, apologize and offer to have a human follow up"}
            _record_outcome(session, block.name, result)
            if block.name == "end_call":
                should_end = True
                end_message = block.input.get("closing_message", "Thanks for calling — goodbye!")
            if block.name == "transfer_call" and tool_allowed(session.config, block.name):
                # A refused transfer (policy can_transfer false) must not ring the owner anyway: the model gets the refusal and carries on.
                should_end = True
                end_message = block.input.get("handoff_message", "One moment, connecting you now.")
                transfer_to = session.config.escalation_phone or "(none)"   # a blank destination still reaches the caller's fallback (a message), never a silent goodbye
            tool_results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)}
            )
        session.messages.append({"role": "user", "content": tool_results})

        if should_end:
            end_message = _guard_claims(session, _fix_dates(session, end_message))
            _log_turn(session.call_sid, "ai", end_message, store_text=store)
            return (end_message, True, transfer_to)

    logger.warning("Tool-call loop exhausted for call_sid=%s", session.call_sid)
    from app import ops

    storage.record_metric("tool_loop_exhausted", session.client_id)
    ops.alert_operator(
        "AI tool loop on a live call",
        f"{session.config.business_name}: the model kept calling tools without answering; the call was handed to the owner (or a callback was promised).",
        key="tool-loop",
    )
    tools.escalate_to_human(
        call_sid=session.call_sid,
        config=session.config,
        reason="agent_loop",
        caller_name=None,
        caller_phone=_usable_caller_number(session.caller_number),
        summary="The AI could not finish the request and handed the call off." + _caller_gist(session),
    )
    if _can_ring_owner(session.config):
        return (_m(session, "loop"), True, session.config.escalation_phone)                          # the message says "connecting you": actually do it
    return (_m(session, "error"), True, None)
