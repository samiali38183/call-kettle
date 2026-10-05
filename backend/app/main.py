from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from urllib.parse import quote
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape as _h
from pathlib import Path
from zoneinfo import ZoneInfo

import time
from collections import OrderedDict, defaultdict, deque

from fastapi import BackgroundTasks, FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

_STATIC_DIR = Path(__file__).resolve().parent / "static"

from app import admin_onboarding, agent, billing, brand, costing, mailer, offsite, ops, outcomes, portal, storage, summary, tools, twilio_utils, webhooks
from app import frontdesk, frontdesk_storage, owner_activation, usage_trigger
from app import config as config_module
from app.config import ClientConfig, ClientNotFoundError, ConfigRejected, list_client_ids, load_client_config

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("callkettle.main")

MAX_SILENT_RETRIES = 1
MAX_CALLS_PER_NUMBER_10MIN = 6

# Applied to every client regardless of what's in their YAML — a call-recording
# disclosure shouldn't depend on someone remembering to type it into a config.
# Virginia is one-party consent (the business itself, as a party, can lawfully
# record) but clients and callers can be anywhere, and courts generally
# treat an AI transcribing/logging call content the same as recording for
# consent purposes. This is a cheap, standard precaution, not a substitute for
# real legal review — get an attorney's sign-off before onboarding a client in
# a strict two-party-consent state or one handling PHI/legally sensitive data.
CALL_DISCLOSURE = "This call may be recorded and monitored for quality. "


HOUSEKEEPING_EVERY_SECONDS = 6 * 3600
_started_at = time.time()


def _prune_rate_limiters() -> None:
    """The per-IP counters would otherwise grow with every stranger who ever visits."""
    now = time.monotonic()
    for table in (_booking_attempts, _lead_attempts, _intake_attempts):
        for ip in [ip for ip, dq in table.items() if not dq or now - dq[-1] > 3600]:
            table.pop(ip, None)


def _run_housekeeping() -> None:
    result = ops.housekeeping()
    _prune_rate_limiters()
    logger.info("Housekeeping done: %s", result)


async def _webhook_worker() -> None:
    """Delivers queued customer webhooks. Events live in the database, so a restart loses nothing."""
    await asyncio.sleep(20)
    while True:
        try:
            await run_in_threadpool(webhooks.process_outbox)
        except Exception:
            logger.exception("Webhook worker error")
        await asyncio.sleep(5)


async def _maintenance_loop() -> None:
    await asyncio.sleep(60)  # let the server settle after a deploy
    while True:
        try:
            await run_in_threadpool(_run_housekeeping)
        except Exception:
            logger.exception("Housekeeping loop error")
        await asyncio.sleep(HOUSEKEEPING_EVERY_SECONDS)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    storage.init_db()
    frontdesk_storage.init_db()
    owner_activation.init_db()
    ops.install_log_redaction()
    task = asyncio.create_task(_maintenance_loop())
    worker = asyncio.create_task(_webhook_worker())
    try:
        yield
    finally:
        task.cancel()
        worker.cancel()


# The interactive API docs list every admin route; nobody but the operator needs them, and the operator has the source.
app = FastAPI(title=brand.get().name, lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class SecurityHeadersMiddleware:
    """Headers every response should carry. Frames are refused everywhere, including /book (the website links to it, it does not
    embed it, and an embeddable booking form can be clickjacked); Referrer-Policy keeps any URL (some still carry a key for
    scripts) from leaking to other sites."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {k.lower() for k, _ in headers}
                wanted = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                          (b"strict-transport-security", b"max-age=31536000"), (b"permissions-policy", b"camera=(), microphone=(), geolocation=()")]
                wanted.append((b"x-frame-options", b"DENY"))
                if path.startswith(("/admin", "/report", "/activate", "/portal")):
                    wanted.append((b"cache-control", b"no-store"))
                headers.extend((k, v) for k, v in wanted if k not in present)
            await send(message)

        await self.app(scope, receive, send_with_headers)



# CORS is a browser-only mechanism — it has no effect on Twilio's server-side
# webhook POSTs to /voice/*, which stay protected by signature validation
# regardless. This is here so the public booking widget (hosted on a
# different origin) can call /book/* from a prospect's browser at all.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def _voice_safety_net(request: Request, exc: Exception) -> Response:
    # Any unhandled error on a /voice/* route must never surface as a bare
    # 500 — Twilio has no TwiML to work with then and plays its generic
    # "application error" message to a real caller. A crashed demo call is a
    # lost lead; a crashed real call is a lost customer's trust in a business
    # that just started depending on this. Everything else keeps FastAPI's
    # normal error handling.
    if isinstance(exc, StarletteHTTPException):
        raise exc
    if request.url.path.startswith("/voice/"):
        logger.exception("Unhandled error on %s — degrading gracefully", request.url.path)
        ops.alert_operator(
            "Error on a live call",
            f"{request.url.path} for client {request.query_params.get('client_id', '?')} crashed; the caller was "
            "sent to the owner's phone instead. Check the Fly logs.",
            key="voice-crash",
        )
        # A broken AI must never strand a caller: ring the business's real
        # phone if we know whose call this is, otherwise apologize.
        client_id = request.query_params.get("client_id", "")
        try:
            owner_phone = load_client_config(client_id).escalation_phone
        except Exception:
            owner_phone = None
        if owner_phone and twilio_utils.is_dialable(owner_phone):
            twiml = twilio_utils.transfer_twiml(
                say_text="One moment, I'm connecting you with the team.", phone_number=owner_phone
            )
        else:
            twiml = twilio_utils.say_and_hangup_twiml(
                "Sorry, something went wrong on our end. Please try calling back in a moment."
            )
        return Response(content=twiml, media_type="application/xml")
    logger.exception("Unhandled error on %s", request.url.path)
    return Response(status_code=500)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def _page_csp(html_text: str) -> str:
    """Strict CSP for a static page: its own inline scripts are allowed by SHA-256 hash and nothing else may run.
    Styles need 'unsafe-inline' (inline <style> and style attributes); Google Fonts is the only third party."""
    hashes = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode() + "'" for body in _INLINE_SCRIPT.findall(html_text))
    return ("default-src 'none'; script-src " + (hashes or "'none'") + "; style-src 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")


def _branded_page(name: str) -> HTMLResponse:
    """A static page with the {{brand}} tokens filled in from app/brand.py (so a rename needs no code change)."""
    body = brand.render((_STATIC_DIR / name).read_text(encoding="utf-8"))
    return HTMLResponse(body, headers={"Content-Security-Policy": _page_csp(body)})


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response((_STATIC_DIR / "favicon.ico").read_bytes(), media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/book")
def book_page() -> HTMLResponse:
    return _branded_page("book.html")


def _public_base_url(request: Request) -> str:
    # Behind cloudflared/ngrok/any reverse proxy, request.url reflects the local
    # http://127.0.0.1:PORT view of the request, not the public https URL Twilio
    # actually POSTed to and signed. Signature validation — and any URL we hand
    # back to Twilio in TwiML for the next turn — must use the public one.
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _public_url(request: Request) -> str:
    path_and_query = request.url.path
    if request.url.query:
        path_and_query += f"?{request.url.query}"
    return _public_base_url(request) + path_and_query


async def _validated_form(request: Request) -> dict[str, str] | None:
    form = await request.form()
    form_dict = {k: str(v) for k, v in form.items()}
    ops.set_call_context(form_dict.get("CallSid"))
    signature = request.headers.get("X-Twilio-Signature", "")
    url = _public_url(request)
    if not twilio_utils.validate_signature(url=url, form=form_dict, signature=signature):
        logger.warning("Rejected webhook with invalid Twilio signature from %s (validated against %s)", request.client, url)
        return None
    return form_dict


def _hints(config: ClientConfig) -> str:
    """Words the speech recognizer should expect: the business and service names."""
    phrases = [config.business_name] + [s.name for s in config.services]
    return ",".join(p.replace(",", " ")[:100] for p in phrases if p)


# When the AI has just asked the caller to dictate a phone number, give them
# time to pause between digit groups instead of cutting them off mid-number.
_ASKS_FOR_NUMBER_RE = re.compile(r"(phone|number|digits|tel\u00e9fono|telefono|n\u00famero|numero|d\u00edgitos|digitos)", re.IGNORECASE)
SPANISH_PROMPT = "Para espa\u00f1ol, oprima dos."


# A reply that is only a sign-off ("...Goodbye!", "Have a great day!", "Adi\u00f3s.") and asks nothing: the caller has nothing left
# to answer, so the line is hung up instead of opening a Gather that can only time out (each Gather use is billed).
_CLOSING_TAIL_RE = re.compile(
    r"\b(good\s?bye|bye(\s+now)?|take\s+care|have\s+a\s+(great|good|nice|wonderful|lovely)(\s+\w+)?\s+(day|evening|night|one|weekend)|"
    r"adi\u00f3s|adios|hasta\s+luego|que\s+tenga\s+(un\s+)?(buen|excelente)\w*\s+d\u00eda|cu\u00eddese|cuidese)[\s.!,]*$",
    re.IGNORECASE,
)


def _is_closing_statement(reply: str) -> bool:
    text = (reply or "").strip()
    return bool(text) and "?" not in text and "\u00bf" not in text and len(text) <= 300 and bool(_CLOSING_TAIL_RE.search(text))


def _speech_timeout_for(reply: str) -> str:
    lowered = reply.lower()
    if "?" in reply and _ASKS_FOR_NUMBER_RE.search(reply) and "calling from" not in lowered and "est\u00e1 llamando" not in lowered:
        return "3"
    return "auto"


def _observe_emission(session, *, gathers: int = 0, say_text: str = "") -> None:
    """Private cost ledger: count the Gathers and <Say> characters the SERVER emitted for a live call. A Gather that times
    out silently may still bill, so gather_count is a server-measured upper bound (source 'server_emitted_gather'); tts_chars
    is the length of the text handed to <Say> (source 'server_say_text'). Never records caller text, never writes without a
    session, never raises, never alters a response."""
    try:
        if session is None:
            return
        evidence = {}
        if gathers:
            session.gather_count += gathers
            evidence["gather_count"] = {"value": session.gather_count, "status": "measured", "source": "server_emitted_gather"}
        if say_text:
            session.tts_chars += len(say_text)
            evidence["tts_chars"] = {"value": session.tts_chars, "status": "measured", "source": "server_say_text"}
        if evidence:
            session.obs_rev += 1
            agent.observe(session.client_id, session.call_sid, evidence, session.obs_rev)
    except Exception:
        logger.exception("Could not record private call emissions")


def _gather(
    config: ClientConfig, *, say_text: str, action_url: str, lang: str = "en", offer_spanish: bool = False, accept_dtmf: bool = False,
    session=None,
) -> str:
    twiml = twilio_utils.gather_twiml(
        say_text=say_text,
        action_url=action_url,
        speech_timeout=_speech_timeout_for(say_text),
        hints=_hints(config),
        lang=lang,
        dtmf_prompt=SPANISH_PROMPT if offer_spanish else None,
        accept_dtmf=accept_dtmf,
    )
    _observe_emission(session, gathers=1, say_text=say_text + (SPANISH_PROMPT if offer_spanish else ""))
    return twiml


@app.post("/voice/incoming")
async def voice_incoming(request: Request, client_id: str) -> Response:
    form = await _validated_form(request)
    if form is None:
        return Response(status_code=403)

    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")

    try:
        config = load_client_config(client_id)
    except ClientNotFoundError:
        logger.error("Unknown client_id in incoming call: %s", client_id)
        twiml = twilio_utils.say_and_hangup_twiml(
            "Sorry, this line isn't set up correctly. Please try again later."
        )
        return Response(content=twiml, media_type="application/xml")

    # Trial reservations precede every answer/transfer/model path. Retry of an
    # initial entry must not restart a session or emit another billed Gather.
    from app import trial
    decision = trial.admit(config, call_sid)
    if not decision.allowed or decision.reason == "duplicate":
        try:
            ops.alert_operator("Trial call declined",
                               f"{client_id}: {decision.reason}. Restore the customer's original forwarding or explicitly agree conversion; no automatic billing.",
                               key=f"trial-stop-{client_id}", min_interval=3600)
        except Exception:
            logger.exception("Could not alert operator about trial stop")
        return Response(content=twilio_utils.reject_twiml(), media_type="application/xml")

    # Worst-case loss cap: too many simultaneous calls are declined before they are answered (Twilio does not bill a <Reject>).
    try:
        crowded = storage.open_call_count(client_id, within_seconds=int(config.max_call_seconds) + 120, exclude_sid=call_sid) >= costing.MAX_CONCURRENT_CALLS
    except Exception:
        logger.exception("Could not count calls in flight; letting the call through")
        crowded = False
    if crowded:
        storage.record_metric("concurrency_rejected", client_id)
        return Response(content=twilio_utils.reject_twiml(), media_type="application/xml")

    # Margin protection: a robocaller or a stuck redial loop can burn real money
    # (speech recognition + voice + model, per turn). Repeat callers get a
    # short polite refusal instead of a fresh AI conversation.
    real_number = len(re.sub(r"\D", "", from_number)) >= 10
    if real_number and storage.recent_call_count(client_id, from_number, minutes=10) >= MAX_CALLS_PER_NUMBER_10MIN:
        logger.warning("Rate-limiting repeat caller %s on %s", from_number, client_id)
        twiml = twilio_utils.say_and_hangup_twiml(
            "We've already spoken with you a few times in the last few minutes. Please try again shortly."
        )
        return Response(content=twiml, media_type="application/xml")

    # Second margin brake: a client far past their included calls (usually a robocall
    # flood on spoofed numbers, which the per-number limit cannot catch) gets new calls
    # rung straight to their own phone, at zero AI cost, and we are alerted.
    try:
        usage = costing.status(config)
        if usage["level"] or usage["reason"]:
            ops.check_cost_levels(config, usage)
    except Exception:
        logger.exception("Could not evaluate the spending ceiling; letting the call through")
        usage = {"over": False}
    if usage["over"]:
        # Worst-case loss cap: degraded calls still cost carrier minutes, speech and texts. After the post-ceiling cap the
        # line stops answering altogether (unbilled <Reject>, no database row) until next month or until the operator acts.
        try:
            capped = costing.post_ceiling_calls(client_id) >= costing.post_ceiling_cap(config)
        except Exception:
            logger.exception("Could not read the post-ceiling call count; handling the call in degraded mode")
            capped = False
        if capped:
            storage.record_metric("post_ceiling_rejected", client_id)
            ops.alert_operator("Post-ceiling hard stop", f"{client_id} passed its post-ceiling call cap this month; new calls are being rejected.",
                               key=f"post-ceiling-{client_id}", min_interval=86400)
            return Response(content=twilio_utils.reject_twiml(), media_type="application/xml")
        storage.log_call_start(call_sid, client_id, from_number)
        storage.log_call_end(call_sid, "over_ceiling")
        storage.record_metric("over_ceiling_call", client_id)
        base = _public_base_url(request)
        if config.ceiling_mode == "transfer":
            twiml = twilio_utils.transfer_twiml(
                say_text="One moment, connecting you with the team.", phone_number=config.escalation_phone,
                action_url=base + f"/voice/transfer-result?client_id={client_id}", time_limit=config.ceiling_transfer_seconds)
        else:
            prompt = (f"Thanks for calling {config.business_name}. Our assistant is not available right now. "
                      "After the tone, please say your name, your phone number and what you need, and the team will call you back.")
            twiml = _gather(config, say_text=prompt, action_url=base + f"/voice/ceiling-message?client_id={client_id}&retry=0")
        return Response(content=twiml, media_type="application/xml")

    storage.log_call_start(call_sid, client_id, from_number)
    if config.demo_menu:
        return _demo_menu_response(request, config)
    if _owner_rings_first(config, from_number):
        # The owner's phone rings first; the assistant answers only if they do not pick up (see /voice/owner-first-result).
        storage.record_metric("owner_first_ring", client_id)
        base = _public_base_url(request)
        whisper = (base + f"/voice/whisper?client_id={client_id}&ctx=" + quote("A caller is on the line.")) if config.transfer_screening else None
        twiml = twilio_utils.transfer_twiml(
            say_text="", phone_number=config.escalation_phone, ring_seconds=config.owner_ring_seconds,
            action_url=base + f"/voice/owner-first-result?client_id={client_id}", whisper_url=whisper)
        return Response(content=twiml, media_type="application/xml")
    return _ai_greeting(request, config, call_sid, from_number)


def _demo_menu_response(request: Request, config: ClientConfig) -> Response:
    """The public demo line asks which pretend company the caller wants to hear. A key press (or silence, which means the first one)
    moves them to that demo. Everything after that is an ordinary call to a demo client with its own spend cap."""
    prompt = config.demo_menu_prompt or "Welcome to the demo line. This is an AI receptionist demo. Press a number to choose a company."
    if config.demo_private_codes:
        prompt += " If you were given a private demo code, press 9."
    action = _public_base_url(request) + f"/voice/demo-select?client_id={config.client_id}"
    twiml = twilio_utils.gather_twiml(say_text=CALL_DISCLOSURE + prompt, action_url=action, accept_dtmf=True, timeout=6)
    return Response(content=twiml, media_type="application/xml")


@app.post("/voice/demo-select")
async def voice_demo_select(request: Request, client_id: str) -> Response:
    form = await _validated_form(request)
    if form is None:
        return Response(status_code=403)
    try:
        config = load_client_config(client_id)
    except ClientNotFoundError:
        return Response(content=twilio_utils.say_and_hangup_twiml("Sorry, something went wrong on our end."), media_type="application/xml")
    menu = config.demo_menu or {}
    if not menu:
        return Response(status_code=404)
    digit = form.get("Digits", "").strip()
    if digit == "9" and config.demo_private_codes:
        action = _public_base_url(request) + f"/voice/demo-code?client_id={client_id}"
        return Response(content=twilio_utils.gather_digits_twiml(
            say_text="Please enter your six digit demo code. It is a private demonstration, not connected to any real business.",
            action_url=action, num_digits=storage.PRIVATE_DEMO_CODE_DIGITS), media_type="application/xml")
    target = menu.get(digit) or menu[sorted(menu)[0]]
    try:
        target_cfg = load_client_config(target)
    except ClientNotFoundError:
        target_cfg = None
    if target_cfg is None or not target_cfg.demo_mode:
        # the menu can only ever lead to a demo client, never to a customer's line
        logger.error("Demo menu %s points at %s, which is not a demo client", client_id, target)
        return Response(content=twilio_utils.say_and_hangup_twiml("Sorry, that demo is not available right now."), media_type="application/xml")
    storage.reassign_call(form.get("CallSid", ""), target)
    url = _public_base_url(request) + f"/voice/incoming?client_id={target}"
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><Response><Redirect method="POST">{url}</Redirect></Response>', media_type="application/xml")


@app.post("/voice/demo-code")
async def voice_demo_code(request: Request, client_id: str) -> Response:
    """A caller typed a private demo code. A valid, unexpired, unused-up code moves the call to that prospect's isolated demo client."""
    form = await _validated_form(request)
    if form is None:
        return Response(status_code=403)
    try:
        config = load_client_config(client_id)
    except ClientNotFoundError:
        return Response(content=twilio_utils.say_and_hangup_twiml("Sorry, something went wrong on our end."), media_type="application/xml")
    if not (config.demo_menu and config.demo_private_codes):
        return Response(status_code=404)
    status, demo = await run_in_threadpool(storage.claim_private_demo, form.get("Digits", ""), form.get("From"))
    messages = {"locked": "Too many attempts. Please try again later. Goodbye.", "expired": "That demo has expired. Goodbye.",
                "revoked": "That demo is no longer available. Goodbye.", "exhausted": "That demo has been used the maximum number of times. Goodbye."}
    if status != "ok" or demo is None:
        return Response(content=twilio_utils.say_and_hangup_twiml(messages.get(status, "That code was not recognized. Goodbye.")), media_type="application/xml")
    try:
        target_cfg = load_client_config(demo["client_id"])
    except ClientNotFoundError:
        target_cfg = None
    if target_cfg is None or not target_cfg.demo_mode:                    # a private demo can only ever be an isolated demo client
        logger.error("Private demo %s points at %s, which is not a demo client", demo["id"], demo["client_id"])
        return Response(content=twilio_utils.say_and_hangup_twiml("Sorry, that demo is not available right now."), media_type="application/xml")
    storage.reassign_call(form.get("CallSid", ""), demo["client_id"])
    url = _public_base_url(request) + f"/voice/incoming?client_id={demo['client_id']}"
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><Response><Redirect method="POST">{url}</Redirect></Response>', media_type="application/xml")


def _owner_rings_first(config: ClientConfig, from_number: str) -> bool:
    if from_number and from_number in config.always_ring_owner:
        return True
    if config.routing_mode == "owner_first":
        return True
    if config.routing_mode == "after_hours":
        return tools.is_open_now(config)
    return False


def _ai_greeting(request: Request, config: ClientConfig, call_sid: str, from_number: str) -> Response:
    agent.start_session(call_sid, config, caller_number=from_number)
    storage.log_turn(call_sid, "ai", config.opening_line, store_text=config.record_transcripts)
    action_url = _public_base_url(request) + f"/voice/gather?client_id={config.client_id}&retry=0"
    twiml = _gather(config, say_text=CALL_DISCLOSURE + config.opening_line, action_url=action_url, offer_spanish=config.spanish)
    if config.stt_mode == "stream":   # flag-off by default: gather clients never reach this line's body (docs/STREAM_STT_DESIGN.md)
        twiml = _stream_greeting_or(twiml, request, config, call_sid, action_url)
    _observe_emission(agent.get_session(call_sid), gathers=twiml.count("<Gather "),
                      say_text=CALL_DISCLOSURE + config.opening_line + (SPANISH_PROMPT if config.spanish and "<Gather " in twiml else ""))
    return Response(content=twiml, media_type="application/xml")


def _stream_greeting_or(default_twiml: str, request: Request, config: ClientConfig, call_sid: str, action_url: str) -> str:
    """Stream-mode greeting, or the unchanged Gather TwiML on ANY doubt (gate closed, Spanish beta needs key presses, any error)."""
    try:
        from app import stream_stt

        if config.spanish or not config.record_transcripts or not stream_stt.stream_mode_active(config):
            return default_twiml
        token = stream_stt.make_token(os.environ[stream_stt.ENV_TOKEN_SECRET], call_sid, config.client_id, now=time.time())
        stream_url = "wss://" + _public_base_url(request).split("://", 1)[1] + "/voice/stream"
        return twilio_utils.stream_greeting_twiml(
            say_text=CALL_DISCLOSURE + config.opening_line, stream_url=stream_url,
            params={"token": token, "client_id": config.client_id}, fallback_url=action_url)
    except Exception:
        logger.exception("Stream STT greeting failed; using Gather")
        return default_twiml


def _ws_public_base(websocket: WebSocket) -> str:
    proto = websocket.headers.get("x-forwarded-proto") or {"ws": "http", "wss": "https"}.get(websocket.url.scheme, "https")
    host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or websocket.url.netloc
    return f"{proto}://{host}"


def _stream_turn_handler(base: str, config: ClientConfig, session: "agent.CallSession", call_sid: str):
    """Blocking turn for a streamed utterance: the SAME agent.run_turn as /voice/gather; returns the TwiML for the live call."""
    from app import stream_stt

    client_id = config.client_id
    gather_url = base + f"/voice/gather?client_id={client_id}&retry=0"

    def handler(text: str) -> "stream_stt.TurnResult":
        turn_started = time.perf_counter()
        reply_text, should_end, transfer_to = agent.run_turn(session, text)
        storage.record_metric("turn_server_ms", client_id, round((time.perf_counter() - turn_started) * 1000, 1))
        lang = session.lang
        after = None
        if transfer_to:
            try:  # private cost observability; no gather_count: no Gather happened
                session.transfer_count += 1
                session.obs_rev += 1
                agent.observe(client_id, call_sid, {"transfer_count": {"value": session.transfer_count, "status": "measured", "source": "stream_turn"}}, session.obs_rev)
            except Exception:
                logger.exception("Could not record private call counts")
            storage.log_call_end(call_sid, "transferred")
            agent.end_session(call_sid)
            if session.config.demo_mode:
                note = " In a real setup, your own phone would ring right now. This is just a demonstration, so that is the end of the call."
                twiml = twilio_utils.say_and_hangup_twiml(reply_text + note, lang)
                _observe_emission(session, say_text=reply_text + note)
            else:
                _observe_emission(session, say_text=reply_text)
                whisper = None
                if session.config.transfer_screening:
                    ctx = _screening_context(call_sid, session.config, text)
                    whisper = base + f"/voice/whisper?client_id={client_id}&ctx={quote(ctx)}"
                twiml = twilio_utils.transfer_twiml(
                    say_text=reply_text, phone_number=transfer_to, action_url=base + f"/voice/transfer-result?client_id={client_id}",
                    lang=lang, whisper_url=whisper)
            return stream_stt.TurnResult(twilio_utils.with_stream_stop(twiml), reply_text, True)
        if should_end:
            storage.log_call_end(call_sid, "completed")
            agent.end_session(call_sid)
            after = lambda: summary.summarize_call(call_sid)  # noqa: E731
            _observe_emission(session, say_text=reply_text)
            return stream_stt.TurnResult(twilio_utils.say_and_hangup_twiml(reply_text, lang), reply_text, True, after=after)
        twiml = twilio_utils.stream_reply_twiml(say_text=reply_text, fallback_url=gather_url, lang=lang)
        _observe_emission(session, say_text=reply_text)
        return stream_stt.TurnResult(twiml, reply_text, False, _speech_timeout_for(reply_text))

    return handler


@app.websocket("/voice/stream")
async def voice_stream(websocket: WebSocket) -> None:
    """Twilio Media Streams (caller audio only). Flag-off: rejects everything unless stream mode is fully enabled. UNIT TESTED ONLY."""
    from app import stream_stt

    await websocket.accept()

    async def reject(why: str) -> None:
        logger.warning("Rejected media stream (%s)", why)
        await websocket.close(code=1008)

    start = None
    try:
        for _ in range(5):  # Twilio sends `connected` first, then `start`; anything else first is not Twilio
            msg = json.loads(await websocket.receive_text())
            if msg.get("event") == "start":
                start = msg.get("start") or {}
                break
            if msg.get("event") != "connected":
                break
    except WebSocketDisconnect:
        return
    except Exception:
        start = None
    if not isinstance(start, dict):
        await reject("no start event")
        return
    params = start.get("customParameters") or {}
    call_sid, client_id, token = str(start.get("callSid") or ""), str(params.get("client_id") or ""), params.get("token")
    config = session = None
    why = None
    try:
        config = load_client_config(client_id)
    except Exception:
        why = "unknown client"
    if why is None and not stream_stt.stream_mode_active(config):
        why = "stream mode off"
    if why is None and not stream_stt.verify_token(os.environ.get(stream_stt.ENV_TOKEN_SECRET, ""), token, call_sid, client_id, now=time.time()):
        why = "bad token"
    if why is None:
        session = agent.get_session(call_sid)
        if session is None or session.client_id != client_id:
            why = "no matching live call"
    if why is not None or config is None or session is None:
        await reject(why or "rejected")
        return

    ops.set_call_context(call_sid)
    base = _ws_public_base(websocket)
    retry_url = base + f"/voice/gather?client_id={client_id}&retry=0"
    spanish = session.lang == "es"

    def fallback_twiml() -> str:
        return twilio_utils.with_stream_stop(_gather(
            config, say_text="Perd\u00f3n, \u00bfpuede repetirlo?" if spanish else "Sorry, could you say that again?",
            action_url=retry_url, lang="es" if spanish else "en", session=session))

    def record_stt_seconds(seconds: float) -> None:
        session.stt_seconds += seconds
        session.obs_rev += 1
        agent.observe(client_id, call_sid, {"stt_seconds": {"value": round(session.stt_seconds, 3), "status": "measured", "source": "twilio_media_stream_bytes"}}, session.obs_rev)

    updater = stream_stt.get_call_updater()
    try:
        stt = stream_stt.PROVIDER_FACTORY(keyterms=stream_stt.derive_keyterms(config))
    except Exception as exc:
        logger.warning("Streaming STT provider unavailable (%s); falling back to Gather", type(exc).__name__)
        try:
            updater.update_call(call_sid, fallback_twiml())
        except Exception as fallback_exc:
            logger.warning("fallback update failed (%s)", type(fallback_exc).__name__)
        await websocket.close(code=1011)
        return

    call = stream_stt.StreamCall(
        call_sid=call_sid, client_id=client_id, stt=stt, updater=updater, turn_handler=_stream_turn_handler(base, config, session, call_sid),
        fallback_twiml=fallback_twiml, barge_in_twiml=twilio_utils.stream_reply_twiml(say_text="", fallback_url=retry_url),
        clock=lambda: stream_stt.monotonic(), max_audio_s=float(config.max_call_seconds) + 60.0, on_audio_seconds=record_stt_seconds)
    receiver = None
    try:
        while not call.done:
            if receiver is None:
                receiver = asyncio.ensure_future(websocket.receive_text())
            await asyncio.wait({receiver}, timeout=1.0)
            if not receiver.done():
                utterance = call.tick()
            else:
                task, receiver = receiver, None
                try:
                    msg = json.loads(task.result())
                except ValueError:
                    continue
                except Exception:   # WebSocketDisconnect or a transport error: the stream is gone
                    break
                utterance = call.handle_event(msg) if isinstance(msg, dict) else None
            if utterance is not None:
                await run_in_threadpool(call.run_utterance, utterance)
    finally:
        if receiver is not None and not receiver.done():
            receiver.cancel()
        call.on_disconnect()
        try:
            await websocket.close()
        except Exception:
            pass


@app.post("/voice/owner-first-result")
async def voice_owner_first_result(request: Request, background_tasks: BackgroundTasks, client_id: str) -> Response:
    """The owner's phone was rung first. If they took the call we are done; otherwise the assistant picks up the caller."""
    form = await _validated_form(request)
    if form is None:
        return Response(status_code=403)
    try:
        config = load_client_config(client_id)
    except ClientNotFoundError:
        return Response(content=twilio_utils.say_and_hangup_twiml("Sorry, something went wrong on our end."), media_type="application/xml")
    call_sid, from_number = form.get("CallSid", ""), form.get("From", "")
    status = form.get("DialCallStatus", "")
    duration = int(form["DialCallDuration"]) if form.get("DialCallDuration", "").isdigit() else None
    answered = status in {"completed", "answered"} and not (config.transfer_screening and duration is not None and duration < 8)
    if answered:
        storage.log_call_end(call_sid, "owner_answered")
        background_tasks.add_task(summary.summarize_call, call_sid)
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response/>', media_type="application/xml")
    storage.record_metric("owner_first_fell_back_to_ai", client_id)
    return _ai_greeting(request, config, call_sid, from_number)


# Twilio re-posts a webhook it thinks failed, with the SAME I-Twilio-Idempotency-Token. The first delivery's answer is replayed so a
# retry cannot start a second model turn (double cost, double hand-off, a history that no longer alternates).
_REPLAYS: "OrderedDict[str, bytes]" = OrderedDict()
_REPLAY_CAP = 512
_INFLIGHT: dict[str, "asyncio.Future"] = {}


async def _once_per_delivery(request: Request, call_sid: str, run) -> Response:
    token = request.headers.get("I-Twilio-Idempotency-Token", "").strip()[:128]
    if not token or not call_sid:
        return await run()
    key = f"{call_sid}:{token}"
    if key in _REPLAYS:
        storage.record_metric("twilio_retry_replayed")
        return Response(content=_REPLAYS[key], media_type="application/xml")
    pending = _INFLIGHT.get(key)
    if pending is not None:
        body = await asyncio.shield(pending)
        if body is not None:
            storage.record_metric("twilio_retry_replayed")
            return Response(content=body, media_type="application/xml")
        return await run()
    future = asyncio.get_running_loop().create_future()
    _INFLIGHT[key] = future
    body = None
    try:
        response = await run()
        if response.status_code == 200:
            body = bytes(response.body)
            _REPLAYS[key] = body
            while len(_REPLAYS) > _REPLAY_CAP:
                _REPLAYS.popitem(last=False)
        return response
    finally:
        _INFLIGHT.pop(key, None)
        future.set_result(body)


@app.post("/voice/gather")
async def voice_gather(
    request: Request, background_tasks: BackgroundTasks, client_id: str, retry: int = 0
) -> Response:
    form = await _validated_form(request)
    if form is None:
        return Response(status_code=403)
    return await _once_per_delivery(request, form.get("CallSid", ""), lambda: _voice_gather_turn(request, background_tasks, client_id, retry, form))


def _end_call_record(call_sid: str, outcome: str) -> None:
    """The call record is bookkeeping: a database problem must never turn a goodbye (or a hand-off) into a different TwiML."""
    try:
        storage.log_call_end(call_sid, outcome)
    except Exception:
        logger.exception("Could not record the end of the call (%s)", outcome)


async def _voice_gather_turn(request: Request, background_tasks: BackgroundTasks, client_id: str, retry: int, form: dict) -> Response:
    call_sid = form.get("CallSid", "")
    speech = re.sub(r"[\x00-\x1f\x7f]+", " ", form.get("SpeechResult", "")).strip()
    action_url = _public_base_url(request) + f"/voice/gather?client_id={client_id}&retry=0"

    try:
        config = load_client_config(client_id)
    except ClientNotFoundError:
        twiml = twilio_utils.say_and_hangup_twiml("Sorry, something went wrong on our end.")
        return Response(content=twiml, media_type="application/xml")

    digits = form.get("Digits", "").strip()
    if digits == "2" and config.spanish and not speech:
        session = agent.get_session(call_sid) or agent.start_session(call_sid, config, caller_number=form.get("From"))
        if session.lang == "en":
            greeting = agent.switch_to_spanish(session)
            return Response(content=_gather(config, say_text=greeting, action_url=action_url, lang="es", session=session), media_type="application/xml")

    if not speech:
        if retry >= MAX_SILENT_RETRIES:
            session = agent.get_session(call_sid)
            # The caller is promised a follow-up, so the owner is told even when the in-memory session is gone (a restart mid-call).
            tools.escalate_to_human(
                call_sid=call_sid,
                config=session.config if session is not None else config,
                reason="no_speech_detected",
                caller_name=None,
                caller_phone=form.get("From"),
                summary="Caller didn't respond after repeated prompts.",
            )
            _end_call_record(call_sid, "no_input")
            agent.end_session(call_sid)
            goodbye = (
                "No escucho nada. Alguien del equipo le llamar\u00e1. Adi\u00f3s."
                if session is not None and session.lang == "es"
                else "I'm not hearing anything — I'll have someone from the team follow up. Goodbye."
            )
            twiml = twilio_utils.say_and_hangup_twiml(goodbye, session.lang if session is not None else "en")
            _observe_emission(session, say_text=goodbye)
            return Response(content=twiml, media_type="application/xml")

        retry_url = _public_base_url(request) + f"/voice/gather?client_id={client_id}&retry={retry + 1}"
        live = agent.get_session(call_sid)
        spanish = live is not None and live.lang == "es"
        twiml = _gather(
            config, say_text="Perd\u00f3n, \u00bfpuede repetirlo?" if spanish else "Sorry, could you say that again?",
            action_url=retry_url, lang="es" if spanish else "en", session=live,
        )
        return Response(content=twiml, media_type="application/xml")

    session = agent.get_session(call_sid)
    if session is None:
        # The process restarted (or was redeployed) mid-call: pick the call back up from what the database knows.
        session = agent.recover_session(call_sid, config, caller_number=form.get("From"))

    # run_turn blocks on the model API (1-3s). Run it off the event loop, or
    # every other caller on the server freezes while one caller's turn thinks.
    turn_started = time.perf_counter()
    reply_text, should_end, transfer_to = await run_in_threadpool(agent.run_turn, session, speech)
    # How long the server kept the caller waiting for this turn (model + tools + queueing). The caller's full silence also
    # includes Twilio's end-of-speech wait and voice start-up, which only a real call can show (docs/VOICE_BENCHMARK.md).
    storage.record_metric("turn_server_ms", client_id, round((time.perf_counter() - turn_started) * 1000, 1))
    try:  # private cost observability; gathers are counted where emitted (_gather), here only transfers; never affects the call
        if transfer_to:
            session.transfer_count += 1
            session.obs_rev += 1
            evidence = {"transfer_count": {"value": session.transfer_count, "status": "measured", "source": "twilio_gather_callback"}}
            agent.observe(session.client_id, call_sid, evidence, session.obs_rev)
    except Exception:
        logger.exception("Could not record private call counts")

    lang = session.lang
    if transfer_to and not twilio_utils.is_dialable(transfer_to):
        # The configured destination cannot be dialed: ringing it would strand the caller. Take a message instead and tell the owner.
        logger.error("Transfer destination for %s is not a dialable number; taking a message instead", client_id)
        storage.record_metric("transfer_destination_invalid", client_id)
        ops.alert_operator("Transfer number is not dialable",
                           f"{client_id}: a caller asked to be connected but the escalation phone is blank or malformed; a message was taken instead. Fix the client's escalation phone.",
                           key=f"bad-transfer-{client_id}")
        tools.escalate_to_human(call_sid=call_sid, config=config, reason="transfer_unavailable", caller_name=None,
                                caller_phone=form.get("From"), summary="The caller asked for a person but the transfer number is not dialable. Call them back.")
        prompt = ("Sorry, I can't connect you right now, but I've let the team know you called. "
                  "Can I take your name and the best number to reach you?")
        session.session_note = ("The live transfer is unavailable and the team has been notified. You asked for the caller's name and best number. "
                                "Once they give them, call escalate_to_human with reason callback_requested and then end_call saying someone will call back soon.")
        session.messages.append({"role": "assistant", "content": prompt})
        return Response(content=_gather(config, say_text=prompt, action_url=action_url, lang=lang, session=session), media_type="application/xml")
    if transfer_to:
        _end_call_record(call_sid, "transferred")
        agent.end_session(call_sid)
        base = _public_base_url(request)
        if session.config.demo_mode:
            # A demo never rings a real phone: it says what would happen, so the hand-off can be heard without disturbing anyone.
            note = " In a real setup, your own phone would ring right now. This is just a demonstration, so that is the end of the call."
            _observe_emission(session, say_text=reply_text + note)
            return Response(content=twilio_utils.say_and_hangup_twiml(reply_text + note, lang), media_type="application/xml")
        result_url = base + f"/voice/transfer-result?client_id={client_id}"
        whisper = None
        if session.config.transfer_screening:
            ctx = _screening_context(call_sid, session.config, speech)
            whisper = base + f"/voice/whisper?client_id={client_id}&ctx={quote(ctx)}"
        twiml = twilio_utils.transfer_twiml(
            say_text=reply_text, phone_number=transfer_to, action_url=result_url, lang=lang, whisper_url=whisper
        )
        _observe_emission(session, say_text=reply_text)
    elif should_end or _is_closing_statement(reply_text):
        _end_call_record(call_sid, "completed")
        agent.end_session(call_sid)
        background_tasks.add_task(summary.summarize_call, call_sid)
        twiml = twilio_utils.say_and_hangup_twiml(reply_text, lang)
        _observe_emission(session, say_text=reply_text)
    else:
        offer, session.offer_spanish = session.offer_spanish, False
        twiml = _gather(config, say_text=reply_text, action_url=action_url, lang=lang, accept_dtmf=offer, session=session)

    return Response(content=twiml, media_type="application/xml")


def _screening_context(call_sid: str, config: ClientConfig, speech: str) -> str:
    """One short sentence for the owner's whisper. Plain words only (it is read aloud and travels in a signed URL)."""
    if not config.record_transcripts:
        return "A caller asked to be put through."
    lines: list[str] = []
    try:
        call = storage.get_call(call_sid) or {}
        lines = [t["text"] for t in json.loads(call.get("transcript_json") or "[]") if t.get("role") == "caller"][-2:]
    except Exception:
        logger.exception("Could not read the call for the whisper")
    said = " ".join(lines) or speech
    said = re.sub(r"[^A-Za-z0-9 ,.'-]", " ", said)
    said = re.sub(r"\s+", " ", said).strip()[:140]
    urgent = bool(agent._EMERGENCY_RE.search(speech or ""))
    return ("Urgent. " if urgent else "") + (f"The caller said: {said}." if said else "A caller asked to be put through.")


@app.post("/voice/whisper")
async def voice_whisper(request: Request, client_id: str, ctx: str = "") -> Response:
    """Twilio plays this to the OWNER's phone when they answer a screened transfer."""
    form = await _validated_form(request)
    if form is None:
        return Response(status_code=403)
    try:
        config = load_client_config(client_id)
    except ClientNotFoundError:
        return Response(content=twilio_utils.say_and_hangup_twiml("Sorry, something went wrong."), media_type="application/xml")
    text = re.sub(r"[^A-Za-z0-9 ,.'-]", " ", ctx)[:200]
    announcement = f"Call from {config.business_name} line. {text}"
    return Response(content=twilio_utils.whisper_twiml(announcement=announcement, accept_url=_public_base_url(request) + f"/voice/whisper-accept?client_id={client_id}"),
                    media_type="application/xml")


@app.post("/voice/whisper-accept")
async def voice_whisper_accept(request: Request, client_id: str) -> Response:
    """Only the owner pressing 1 connects the caller. Anything else hangs up the owner's leg, which Twilio reports as unanswered."""
    form = await _validated_form(request)
    if form is None:
        return Response(status_code=403)
    if form.get("Digits", "").strip() == "1":
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response/>', media_type="application/xml")
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>', media_type="application/xml")


@app.post("/voice/ceiling-message")
async def voice_ceiling_message(request: Request, client_id: str, retry: int = 0) -> Response:
    """Degraded mode after a spending ceiling: no AI. Take a message, tell the owner, and still honor a real emergency."""
    form = await _validated_form(request)
    if form is None:
        return Response(status_code=403)
    try:
        config = load_client_config(client_id)
    except ClientNotFoundError:
        return Response(content=twilio_utils.say_and_hangup_twiml("Sorry, something went wrong on our end."), media_type="application/xml")

    call_sid = form.get("CallSid", "")
    caller = form.get("From")
    speech = re.sub(r"[\x00-\x1f\x7f]+", " ", form.get("SpeechResult", "")).strip()[:agent.MAX_CALLER_CHARS]
    if speech and (agent._EMERGENCY_RE.search(speech) or agent._EMERGENCY_ES_RE.search(speech)):
        tools.escalate_to_human(call_sid=call_sid, config=config, reason="possible_emergency", caller_name=None, caller_phone=caller,
                                summary=f"Caller said (while the assistant was over its limit): {speech[:300]}. Follow up right away.")
        if config.policy.emergency_action != "transfer":
            return Response(content=twilio_utils.say_and_hangup_twiml(agent._MSGS["emergency"][0]), media_type="application/xml")
        return Response(content=twilio_utils.transfer_twiml(say_text=agent._MSGS["emergency"][0], phone_number=config.escalation_phone,
                                                            time_limit=config.ceiling_transfer_seconds), media_type="application/xml")
    if not speech and retry < 1:
        action = _public_base_url(request) + f"/voice/ceiling-message?client_id={client_id}&retry=1"
        return Response(content=_gather(config, say_text="Sorry, I didn't catch that. Please say your name, number and what you need.",
                                        action_url=action), media_type="application/xml")
    tools.escalate_to_human(
        call_sid=call_sid, config=config, reason="over_limit_message", caller_name=None, caller_phone=caller,
        summary=(f"Message left while the assistant was over its monthly limit: {speech}" if speech
                 else "Caller reached the assistant while it was over its monthly limit and left no message. Call them back."))
    storage.log_turn(call_sid, "caller", speech or "(no message)", store_text=config.record_transcripts)
    return Response(content=twilio_utils.say_and_hangup_twiml("Thank you. We have your message and someone will call you back soon. Goodbye."),
                    media_type="application/xml")


def _observe_twilio_seconds(client_id: str, call_sid: str, raw: str, metric: str, source: str) -> None:
    """Record a duration ONLY when Twilio actually sent a numeric value. Never raises."""
    try:
        if not (raw.isascii() and raw.isdigit()):
            return
        seconds = int(raw)
        value = [seconds] if metric == "transfer_seconds" else seconds
        agent.observe(client_id, call_sid, {metric: {"value": value, "status": "measured", "source": source}}, agent.FINAL_REVISION)
    except Exception:
        logger.exception("Could not record %s", metric)


@app.post("/voice/transfer-result")
async def voice_transfer_result(
    request: Request, background_tasks: BackgroundTasks, client_id: str
) -> Response:
    """Twilio calls this when the live transfer's <Dial> ends. If nobody
    answered, the caller must not be left with dead air — hand them back to
    the AI to take a message, and tell the owner right away."""
    form = await _validated_form(request)
    if form is None:
        return Response(status_code=403)

    call_sid = form.get("CallSid", "")
    dial_status = form.get("DialCallStatus", "")
    _observe_twilio_seconds(client_id, call_sid, form.get("DialCallDuration", ""), "transfer_seconds", "twilio_dial_callback")

    # A screened transfer that the owner declined or let time out can still be reported as "completed" with a very short
    # duration (their leg hung up before bridging). A real conversation is never under 8 seconds.
    try:
        screened = load_client_config(client_id).transfer_screening
    except Exception:
        screened = False
    if screened and dial_status in {"completed", "answered"} and (form.get("DialCallDuration", "").isdigit() and int(form["DialCallDuration"]) < 8):
        dial_status = "no-answer"

    if dial_status in {"completed", "answered"}:
        background_tasks.add_task(summary.summarize_call, call_sid)
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response/>', media_type="application/xml")

    try:
        config = load_client_config(client_id)
    except ClientNotFoundError:
        twiml = twilio_utils.say_and_hangup_twiml("Sorry, something went wrong on our end.")
        return Response(content=twiml, media_type="application/xml")

    caller_phone = form.get("From")
    over_ceiling = (storage.get_call(call_sid) or {}).get("outcome") == "over_ceiling"
    storage.log_call_end(call_sid, "transfer_unanswered")
    tools.escalate_to_human(
        call_sid=call_sid,
        config=config,
        reason="transfer_unanswered",
        caller_name=None,
        caller_phone=caller_phone,
        summary=f"Caller asked for a live person but the transfer wasn't answered ({dial_status or 'no status'}). Call them back.",
    )
    if over_ceiling:
        # ceiling_mode "transfer" rang the owner because the client is over its spending ceiling: the message is taken
        # without the model, exactly like ceiling_mode "message", instead of starting the AI the ceiling exists to stop.
        prompt = ("I'm sorry, nobody was able to pick up just now. After the tone, please say your name, your phone number "
                  "and what you need, and the team will call you back.")
        action = _public_base_url(request) + f"/voice/ceiling-message?client_id={client_id}&retry=0"
        return Response(content=_gather(config, say_text=prompt, action_url=action), media_type="application/xml")

    previous = agent.get_session(call_sid)
    spanish = previous is not None and previous.lang == "es"
    lang = "es" if spanish else "en"
    prompt = (
        "Lo siento, nadie pudo contestar en este momento. Ya avis\u00e9 al equipo que usted llam\u00f3. "
        "\u00bfMe puede dar su nombre y el mejor n\u00famero para llamarle?"
        if spanish else
        "I'm sorry, nobody was able to pick up just now. I've let them know you called. "
        "Can I take your name and the best number to reach you?"
    )
    session = agent.start_session(call_sid, config, caller_number=caller_phone)
    session.lang = lang
    session.session_note = (
        "The caller asked for a live person but nobody answered the transfer, and the team has already been "
        "notified. You have asked for their name and best phone number. Once they give them (the caller-ID "
        "number counts if they say it's fine), call escalate_to_human with reason 'callback_requested' and "
        "their details, then call end_call with a closing_message that says someone will call them back soon "
        "(for example: 'Thanks, Dana. Someone from the team will call you back as soon as they can. Goodbye.'). "
        "Do not start a new topic or ask what else they need."
    )
    session.messages = [
        {"role": "user", "content": "(The caller asked for a live person, but nobody answered the transfer.)"},
        {"role": "assistant", "content": prompt},
    ]
    storage.log_turn(call_sid, "ai", prompt, store_text=config.record_transcripts)
    action_url = _public_base_url(request) + f"/voice/gather?client_id={client_id}&retry=0"
    return Response(content=_gather(config, say_text=prompt, action_url=action_url, lang=lang, session=session), media_type="application/xml")


@app.post("/voice/status")
async def voice_status(request: Request, background_tasks: BackgroundTasks) -> PlainTextResponse:
    form = await _validated_form(request)
    if form is None:
        return PlainTextResponse("", status_code=403)

    call_sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")
    if call_status in {"completed", "busy", "failed", "no-answer", "canceled"}:
        try:
            owner = (storage.get_call(call_sid) or {}).get("client_id")
            if owner:
                _observe_twilio_seconds(owner, call_sid, form.get("CallDuration", ""), "carrier_seconds", "twilio_status_callback")
        except Exception:
            logger.exception("Could not record carrier seconds")
        outcome = "caller_hung_up" if call_status == "completed" else call_status
        storage.log_call_end_if_open(call_sid, outcome)
        agent.end_session(call_sid)
        background_tasks.add_task(summary.summarize_call, call_sid)
    return PlainTextResponse("")


# --- Sales consultation booking (Call Kettle's own lead-gen calendar) ---
# Hardcoded to exactly one client_id, never taken as a path/query parameter.
# These are plain JSON endpoints, not Twilio webhooks, so no signature check
# applies — but that's also why the client_id can't be caller-supplied: an
# open `/book/{client_id}/confirm` would let anyone create bogus bookings on
# a real client's calendar just by guessing their client_id. Reusing the same
# conflict-checked storage.create_booking a live phone call would use, so two
# prospects genuinely can't double-book the same slot — this is a real
# calendar, not a contact form pretending to be one.
_SALES_CLIENT_ID = "callkettle_sales"


class BookingRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=7, max_length=30)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    time: str = Field(pattern=r"^\d{2}:\d{2}$")

    @field_validator("phone")
    @classmethod
    def _phone_needs_digits(cls, v: str) -> str:
        if len(re.sub(r"\D", "", v)) < 7:
            raise ValueError("phone number needs at least 7 digits")
        return v


# This endpoint is public and writes to a real calendar, so bound the damage a
# bot could do: a handful of bookings per IP per hour, and only the next 60 days.
_BOOKING_WINDOW_DAYS = 60
_BOOKINGS_PER_IP_PER_HOUR = 5
_booking_attempts: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("fly-client-ip") or request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def _booking_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    attempts = _booking_attempts[ip]
    while attempts and now - attempts[0] > 3600:
        attempts.popleft()
    if len(attempts) >= _BOOKINGS_PER_IP_PER_HOUR:
        return True
    attempts.append(now)
    return False


def _business_today(config):
    return datetime.now(ZoneInfo(config.timezone)).date()


@app.get("/book/next-open")
def book_next_open() -> dict:
    """The first date (today or later, within two weeks) that has an open slot, so the booking page never opens on a closed day."""
    config = load_client_config(_SALES_CLIENT_ID)
    today = _business_today(config)
    for offset in range(15):
        day = today + timedelta(days=offset)
        if tools.check_availability(config=config, date=day.isoformat(), limit=1).get("slots"):
            return {"date": day.isoformat(), "timezone": config.timezone}
    return {"date": today.isoformat(), "timezone": config.timezone}


@app.get("/book/availability")
def book_availability(date: str, limit: int = 3) -> dict:
    config = load_client_config(_SALES_CLIENT_ID)
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return {"error": f"'{date}' is not a valid date, expected YYYY-MM-DD"}
    today = datetime.now(ZoneInfo(config.timezone)).date()
    if day > today + timedelta(days=_BOOKING_WINDOW_DAYS):
        return {"slots": [], "note": f"Bookings open up to {_BOOKING_WINDOW_DAYS} days ahead."}
    return tools.check_availability(config=config, date=date, limit=max(1, min(limit, 24)))


@app.post("/book/confirm")
def book_confirm(booking: BookingRequest, request: Request) -> JSONResponse:
    if _booking_rate_limited(_client_ip(request)):
        return JSONResponse({"success": False, "error": "Too many attempts — please try again later."}, status_code=429)
    config = load_client_config(_SALES_CLIENT_ID)
    if datetime.strptime(booking.date, "%Y-%m-%d").date() > datetime.now(ZoneInfo(config.timezone)).date() + timedelta(days=_BOOKING_WINDOW_DAYS):
        return JSONResponse({"success": False, "error": f"Bookings open up to {_BOOKING_WINDOW_DAYS} days ahead."}, status_code=409)
    service = config.services[0].name
    result = tools.book_appointment(
        call_sid=None,
        config=config,
        caller_name=booking.name,
        caller_phone=booking.phone,
        service=service,
        date=booking.date,
        time=booking.time,
    )
    status_code = 200 if result.get("success") else 409
    return JSONResponse(result, status_code=status_code)


# --- Website "request a call back" form ---
# Public, so it's narrowly scoped: only clients that opt in (web_leads: true),
# only name + phone + a best-time choice (no free text, so nobody types health
# details into a website form), a honeypot field, and a per-IP rate limit.
_LEADS_PER_IP_PER_HOUR = 6
_lead_attempts: dict[str, deque] = defaultdict(deque)


class LeadRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=7, max_length=30)
    best_time: str = Field(default="Anytime", max_length=20)
    website: str = ""  # honeypot: real visitors never see or fill this

    @field_validator("phone")
    @classmethod
    def _phone_needs_digits(cls, v: str) -> str:
        if len(re.sub(r"\D", "", v)) < 7:
            raise ValueError("phone number needs at least 7 digits")
        return v


def _lead_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    attempts = _lead_attempts[ip]
    while attempts and now - attempts[0] > 3600:
        attempts.popleft()
    if len(attempts) >= _LEADS_PER_IP_PER_HOUR:
        return True
    attempts.append(now)
    return False


_BEST_TIMES = {"Morning", "Afternoon", "Evening", "Anytime"}


@app.post("/lead")
def website_lead(lead: LeadRequest, request: Request) -> JSONResponse:
    if lead.website:  # a bot filled the honeypot; pretend success, do nothing
        return JSONResponse({"success": True})
    if _lead_rate_limited(_client_ip(request)):
        return JSONResponse({"success": False, "error": "Too many requests. Please call us instead."}, status_code=429)
    try:
        config = load_client_config(lead.client_id)
    except ClientNotFoundError:
        return JSONResponse({"success": False, "error": "Unknown business."}, status_code=404)
    if not config.web_leads:
        return JSONResponse({"success": False, "error": "Not available."}, status_code=404)
    best = lead.best_time if lead.best_time in _BEST_TIMES else "Anytime"
    tools.escalate_to_human(
        call_sid=None,
        config=config,
        reason="website_request",
        caller_name=lead.name.strip(),
        caller_phone=lead.phone.strip(),
        summary=f"Asked for a call back through the website. Best time to call: {best}.",
    )
    return JSONResponse({"success": True})


# --- Terms of service and client intake ---
# /terms is what the Stripe payment link points at ("I agree to the terms"), so
# paying is agreeing and nobody prints or signs anything. /start is the intake
# form a new client fills in (or Sami fills in with them on the phone); it turns
# into their receptionist config with `scripts/onboard_client.py --intake <id>`.
@app.get("/terms")
def terms_page() -> HTMLResponse:
    return _branded_page("terms.html")


@app.get("/start")
def intake_page() -> HTMLResponse:
    return _branded_page("start.html")


_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME_RE = r"^([01]\d|2[0-3]):[0-5]\d$"


class IntakeService(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    minutes: int = Field(default=60, ge=5, le=480)


class IntakeFaq(BaseModel):
    q: str = Field(min_length=1, max_length=200)
    a: str = Field(min_length=1, max_length=500)


class IntakeRequest(BaseModel):
    business_name: str = Field(min_length=1, max_length=100)
    owner_name: str = Field(min_length=1, max_length=80)
    owner_phone: str = Field(min_length=7, max_length=30)
    owner_email: str = Field(default="", max_length=120)
    trade: str = Field(min_length=1, max_length=60)
    phone_provider: str = Field(default="", max_length=60)
    hours: dict[str, str] = Field(default_factory=dict)  # day -> "closed" or "HH:MM-HH:MM"
    services: list[IntakeService] = Field(min_length=1, max_length=8)
    faqs: list[IntakeFaq] = Field(default_factory=list, max_length=8)
    never_say: str = Field(default="", max_length=500)
    google_calendar_email: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=500)
    website: str = ""  # honeypot

    @field_validator("owner_phone")
    @classmethod
    def _phone_needs_digits(cls, v: str) -> str:
        if len(re.sub(r"\D", "", v)) < 10:
            raise ValueError("phone number needs 10 digits")
        return v

    @field_validator("owner_email", "google_calendar_email")
    @classmethod
    def _email_shape(cls, v: str) -> str:
        if v and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("that doesn't look like an email address")
        return v

    @field_validator("hours")
    @classmethod
    def _hours_shape(cls, v: dict[str, str]) -> dict[str, str]:
        for day, value in v.items():
            if day not in _DAYS:
                raise ValueError(f"unknown day {day!r}")
            if value != "closed" and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d", value):
                raise ValueError(f"hours for {day} must be 'closed' or 'HH:MM-HH:MM'")
        return v


_INTAKES_PER_IP_PER_HOUR = 4
_intake_attempts: dict[str, deque] = defaultdict(deque)


@app.post("/intake")
def submit_intake(intake: IntakeRequest, request: Request) -> JSONResponse:
    if intake.website:
        return JSONResponse({"success": True})
    ip = _client_ip(request)
    now = time.monotonic()
    attempts = _intake_attempts[ip]
    while attempts and now - attempts[0] > 3600:
        attempts.popleft()
    if len(attempts) >= _INTAKES_PER_IP_PER_HOUR:
        return JSONResponse({"success": False, "error": "Too many submissions. Please text us instead."}, status_code=429)
    attempts.append(now)

    payload = intake.model_dump()
    payload.pop("website", None)
    intake_id = storage.create_intake(payload)
    try:
        from app import notify

        notify.notify_owner(
            load_client_config(_SALES_CLIENT_ID),
            title="New client intake",
            body=f"#{intake_id} {intake.business_name} ({intake.trade}), {intake.owner_name} {intake.owner_phone}. "
                 "Open your admin page and tap Review & go live.",
        )
    except Exception:
        logger.exception("Intake notification failed for #%s (intake is saved)", intake_id)
    return JSONResponse({"success": True, "id": intake_id})


def _master_key_ok(key: str) -> bool:
    return bool(_REPORT_KEY) and bool(key) and hmac.compare_digest(key, _REPORT_KEY)


@app.get("/admin/intakes")
def admin_intakes(key: str = "") -> JSONResponse:
    if not _master_key_ok(key):
        return JSONResponse({"error": "Not authorized."}, status_code=403)
    return JSONResponse({"intakes": storage.list_intakes()})


@app.get("/admin/intake/{intake_id}")
def admin_intake(intake_id: int, key: str = "") -> JSONResponse:
    if not _master_key_ok(key):
        return JSONResponse({"error": "Not authorized."}, status_code=403)
    found = storage.get_intake(intake_id)
    if found is None:
        return JSONResponse({"error": "Not found."}, status_code=404)
    return JSONResponse(found)


# --- Operator tools: status, data export, offboarding ---
_TEST_SID_PREFIXES = ("CA_SELFCHECK", "CA_LOAD", "CA_E2E", "CA_LIVE_VERIFY", "CA_FIN")


@app.get("/admin/status")
def admin_status(key: str = "", deep: int = 0) -> JSONResponse:
    """Health of everything the service depends on, as booleans and counts only
    (no secrets). `deep=1` also makes one tiny model call to prove the key works."""
    if not _master_key_ok(key):
        return JSONResponse({"error": "Not authorized."}, status_code=403)
    from app import gcal, notify

    db_ok, db_error = True, None
    try:
        with storage._conn() as conn:
            conn.execute("SELECT 1 FROM calls LIMIT 1")
    except Exception as exc:  # pragma: no cover - only when the database is broken
        db_ok, db_error = False, str(exc)[:120]
    disk = None
    try:
        import shutil

        usage = shutil.disk_usage(Path(storage.DB_PATH).parent)
        disk = {"free_mb": usage.free // 1_000_000, "total_mb": usage.total // 1_000_000}
    except Exception:
        pass
    _bf = Path(storage.DB_PATH).parent / "backups"
    backups = sorted(list(_bf.glob("callkettle-*.db")) + list(_bf.glob(ops.OLD_BACKUP_GLOB)), key=lambda p: p.name[-13:]) if _bf.exists() else []
    model_ok = None
    if deep:
        try:
            agent._anthropic_client().messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=5, messages=[{"role": "user", "content": "ok"}]
            )
            model_ok = True
        except Exception as exc:
            model_ok = f"failed: {str(exc)[:100]}"
    return JSONResponse({
        "ok": db_ok,
        "database": {"ok": db_ok, "error": db_error, "disk": disk, "latest_backup": backups[-1].name if backups else None,
                     "backup_count": len(backups), "sqlite": storage.sqlite_inspection(deep=bool(deep)) if db_ok else None},
        "uptime_seconds": int(time.time() - _started_at),
        "housekeeping": dict(ops.LAST_HOUSEKEEPING),
        "billing": billing.summary(),
        "email": {**mailer.health(), "events_url": f"/email/events/<provider>?token={mailer.events_token()}"},
        "offsite_backup": {**offsite.status_from(), "last": (ops.LAST_HOUSEKEEPING.get("result") or {}).get("offsite")},
        "webhook_outbox": webhooks.outbox_health(),
        "turn_server_ms_24h": storage.metric_percentiles("turn_server_ms", (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()),
        "live_call_sessions": agent.session_count(),
        "features": {
            "sms_enabled": notify.sms_enabled(),
            "email_configured": mailer.configured(),
            "google_calendar_enabled": gcal.enabled(),
            "twilio_configured": bool(os.environ.get("TWILIO_ACCOUNT_SID") and os.environ.get("TWILIO_AUTH_TOKEN")),
            "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "signature_check_on": os.environ.get("CALLKETTLE_SKIP_SIGNATURE_CHECK", "0") != "1",
            "voice": twilio_utils._VOICE,
        },
        "model_reachable": model_ok,
        "clients": len(list_client_ids()),
    })


# --- Admin login: a signed cookie instead of a secret in every link. The master key is typed once; pages never contain it.
ADMIN_COOKIE = "dl_admin"
ADMIN_SESSION_SECONDS = 12 * 3600
_login_attempts: dict[str, deque] = defaultdict(deque)


def _admin_token(expiry: int) -> str:
    sig = hmac.new(_REPORT_KEY.encode(), f"admin|{expiry}".encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def _admin_cookie_ok(value: str) -> bool:
    try:
        expiry, sig = value.split(".", 1)
        return _REPORT_KEY != "" and int(expiry) > time.time() and hmac.compare_digest(value, _admin_token(int(expiry)))
    except (ValueError, AttributeError):
        return False


class AdminCookieMiddleware:
    """If a request to /admin carries a valid login cookie, behave as if it carried the master key.
    The key is added in memory only; it is never written into a page or a link."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/admin") and not scope["path"].startswith("/admin/login"):
            cookies = dict(c.split("=", 1) for c in (dict(scope["headers"]).get(b"cookie", b"").decode("latin-1")).split("; ") if "=" in c)
            if _admin_cookie_ok(cookies.get(ADMIN_COOKIE, "")):
                if scope.get("method") not in ("GET", "HEAD", "OPTIONS"):
                    request = Request(scope)
                    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
                    expected_origin = f"{scheme}://{request.headers.get('host', '')}"
                    if request.headers.get("origin", "") != expected_origin:
                        response = HTMLResponse("<p>Request origin could not be verified. Reload the admin page and try again.</p>", status_code=403,
                                                headers={"Cache-Control": "no-store"})
                        await response(scope, receive, send)
                        return
                if b"key=" not in scope.get("query_string", b""):
                    extra = b"key=" + _REPORT_KEY.encode()
                    qs = scope.get("query_string", b"")
                    scope = dict(scope, query_string=(qs + b"&" + extra) if qs else extra)
        await self.app(scope, receive, send)


app.add_middleware(AdminCookieMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(failed: int = 0) -> HTMLResponse:
    msg = '<p style="color:#8A2620">That key did not work.</p>' if failed else ""
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow"><title>Admin sign-in</title></head>'
        '<body style="font-family:-apple-system,Segoe UI,sans-serif;max-width:420px;margin:60px auto;padding:0 16px;font-size:18px">'
        f'<h1>Admin sign-in</h1>{msg}<form method="post" action="/admin/login"><p><input type="password" name="key" autofocus autocomplete="current-password" '
        'style="font-size:18px;padding:10px;width:100%;box-sizing:border-box" placeholder="Master key"></p>'
        '<p><button style="font-size:18px;padding:12px 22px;background:#2358D6;color:#fff;border:0;border-radius:10px">Sign in</button></p></form></body></html>',
        headers={"Cache-Control": "no-store"},
    )


@app.post("/admin/login")
async def admin_login(request: Request):
    ip = _client_ip(request)
    attempts = _login_attempts[ip]
    now = time.monotonic()
    while attempts and now - attempts[0] > 600:
        attempts.popleft()
    if len(attempts) >= 10:
        return HTMLResponse("<p>Too many attempts. Try again in a few minutes.</p>", status_code=429)
    form = await request.form()
    if not _master_key_ok(str(form.get("key", ""))):
        attempts.append(now)
        return RedirectResponse("/admin/login?failed=1", status_code=303)
    resp = RedirectResponse("/admin", status_code=303)
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(ADMIN_COOKIE, _admin_token(int(time.time()) + ADMIN_SESSION_SECONDS), max_age=ADMIN_SESSION_SECONDS,
                    httponly=True, secure=secure, samesite="strict", path="/")
    return resp


@app.get("/admin/logout")
def admin_logout() -> RedirectResponse:
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE, path="/")
    return resp


app.include_router(portal.router)
app.include_router(frontdesk.router)
app.include_router(owner_activation.router)
app.include_router(usage_trigger.router)
admin_onboarding.install(app, master_key_ok=_master_key_ok, report_key_for=lambda client_id: report_key_for(client_id))


def _export_everything() -> dict:
    conn = sqlite3.connect(storage.DB_PATH)
    conn.row_factory = sqlite3.Row
    data = {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in ("calls", "bookings", "cancelled_bookings", "escalations", "intakes")}
    conn.close()
    live = config_module.LIVE_CLIENTS_DIR
    data["client_configs"] = (
        {p.stem: p.read_text(encoding="utf-8") for p in sorted(live.glob("*.yaml"))} if live and live.exists() else {}
    )
    data["exported_at"] = datetime.now(timezone.utc).isoformat()
    return data


@app.get("/admin/export")
def admin_export(key: str = "") -> JSONResponse:
    if not _master_key_ok(key):
        return JSONResponse({"error": "Not authorized."}, status_code=403)
    return JSONResponse(_export_everything(), headers={"Cache-Control": "no-store"})


@app.post("/admin/client/upload")
async def admin_upload_client(request: Request, key: str = "") -> JSONResponse:
    """Start serving a client's config right now, with no deploy and no restart.
    The body is the client's YAML. It is validated exactly like a baked-in config,
    so a typo is rejected here instead of breaking a live call."""
    if not _master_key_ok(key):
        return JSONResponse({"error": "Not authorized."}, status_code=403)
    body = await request.body()
    if len(body) > config_module.MAX_CONFIG_BYTES:
        return JSONResponse({"error": "Config is too large."}, status_code=413)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return JSONResponse({"error": "Config must be UTF-8 text."}, status_code=400)
    try:
        config, created = await run_in_threadpool(config_module.save_live_config, text)
    except ConfigRejected as exc:
        return JSONResponse({"error": str(exc)[:1500]}, status_code=422)
    logger.info("Client config %s for %s", "created" if created else "updated", config.client_id)
    return JSONResponse({
        "ok": True, "client_id": config.client_id, "created": created, "business_name": config.business_name,
        "dashboard": f"/report/{config.client_id}?key={report_key_for(config.client_id)}",
    })


PRIVATE_DEMO_STRIPPED = ("owner_email", "ntfy_topic", "webhook_url", "webhook_secret", "google_calendar_id", "calendar_ical_url", "always_ring_owner",
                         "demo_menu", "demo_menu_prompt", "demo_private_codes")


@app.post("/admin/private-demo")
async def admin_create_private_demo(request: Request, key: str = "", label: str = "", hours: int = 72, max_calls: int = 10) -> JSONResponse:
    """Create (or refresh) one prospect's private demo and return its one-time code. The body is the demo's YAML.
    The server forces the isolation: client id must start with prep_, demo_mode on, no owner email/ntfy/webhook/calendar, spend capped,
    and the plain code is shown only in this response."""
    if not _master_key_ok(key):
        return JSONResponse({"error": "Not authorized."}, status_code=403)
    import yaml

    body = await request.body()
    if len(body) > config_module.MAX_CONFIG_BYTES:
        return JSONResponse({"error": "Config is too large."}, status_code=413)
    try:
        raw = yaml.safe_load(body.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return JSONResponse({"error": "Config must be valid UTF-8 YAML."}, status_code=400)
    if not isinstance(raw, dict) or not str(raw.get("client_id", "")).startswith("prep_"):
        return JSONResponse({"error": "A private demo's client_id must start with prep_."}, status_code=422)
    for field in PRIVATE_DEMO_STRIPPED:
        raw.pop(field, None)
    raw["demo_mode"] = True
    raw["ceiling_mode"] = "message"
    raw["monthly_cost_ceiling_usd"] = min(float(raw.get("monthly_cost_ceiling_usd") or 10), 10.0)
    raw["record_transcripts"] = True
    try:
        config, _created = await run_in_threadpool(config_module.save_live_config, yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    except ConfigRejected as exc:
        return JSONResponse({"error": str(exc)[:1500]}, status_code=422)
    demo = await run_in_threadpool(storage.create_private_demo, config.client_id, label or config.business_name, hours=hours, max_calls=max_calls)
    return JSONResponse({"ok": True, **demo, "business_name": config.business_name,
                         "how": "Call the demo number, press 9, type the code. Shown once; only a keyed hash is stored."})


@app.post("/admin/private-demo/revoke")
def admin_revoke_private_demo(key: str = "", client_id: str = "") -> JSONResponse:
    if not _master_key_ok(key):
        return JSONResponse({"error": "Not authorized."}, status_code=403)
    if not client_id.startswith("prep_"):
        return JSONResponse({"error": "Only prep_ demos can be revoked here."}, status_code=422)
    n = storage.revoke_private_demo(client_id)
    cleaned = ops.clean_private_demos()["cleaned"]                      # stop serving it and wipe its data now, not at the next maintenance run
    return JSONResponse({"revoked": n, "cleaned": cleaned})


@app.get("/admin/private-demos")
def admin_list_private_demos(key: str = "") -> JSONResponse:
    if not _master_key_ok(key):
        return JSONResponse({"error": "Not authorized."}, status_code=403)
    return JSONResponse({"demos": storage.list_private_demos()})


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request) -> JSONResponse:
    """Stripe events (subscription and payment state). Verified with the endpoint secret; replays are ignored; nothing here switches a phone line off."""
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        return JSONResponse({"error": "Billing webhooks are not configured."}, status_code=403)
    raw = await request.body()
    if not billing.verify_signature(raw, request.headers.get("Stripe-Signature", ""), secret):
        return JSONResponse({"error": "Bad signature."}, status_code=400)
    event = billing.parse(raw)
    if event is None:
        return JSONResponse({"error": "Bad JSON."}, status_code=400)
    result = await run_in_threadpool(billing.handle_event, event)
    if result.get("attention"):
        ops.alert_operator("Payment needs attention", f"Subscription {result.get('subscription')} is now {result.get('status')}. Decide with the customer; the phone line is NOT switched off automatically.",
                           key=f"billing-{result.get('subscription')}-{result.get('status')}", min_interval=6 * 3600)
    return JSONResponse({"received": True, **{k: v for k, v in result.items() if k in ("applied", "reason")}})


@app.post("/email/events/{provider}")
async def email_events(provider: str, request: Request, token: str = "") -> JSONResponse:
    """Delivery, bounce and complaint events from the email provider. Authorized by a secret in the URL (derived from the master key)."""
    if provider not in ("postmark", "resend") or not hmac.compare_digest(token or "", mailer.events_token()):
        return JSONResponse({"error": "Not authorized."}, status_code=403)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "Bad JSON."}, status_code=400)
    parsed = mailer.parse_event(provider, payload if isinstance(payload, dict) else {})
    if parsed is None:
        return JSONResponse({"ok": True, "tracked": False})
    event, message_id, recipient, detail = parsed
    if event in ("delivered", "bounced", "complained"):
        await run_in_threadpool(mailer.record_event, provider, event, message_id, recipient, detail)
    if event in ("bounced", "complained"):
        ops.alert_operator("Email bounced or complained", f"{recipient or 'a recipient'}: {event}. That address is now suppressed; the owner may not be receiving alerts.", key=f"email-{event}-{recipient}", min_interval=3600)
    return JSONResponse({"ok": True, "tracked": True, "event": event})


@app.get("/admin/client/{client_id}/config")
def admin_get_client_config(client_id: str, key: str = "") -> Response:
    """The configuration this server is actually using for a client (what a call would see), as YAML. Contains the
    client's webhook secret and calendar link, so it is master-key only and never cached. Used by scripts/certify_client.py."""
    if not _master_key_ok(key):
        return JSONResponse({"error": "Not authorized."}, status_code=403)
    try:
        cfg = load_client_config(client_id)
    except ClientNotFoundError:
        return JSONResponse({"error": "No such client."}, status_code=404)
    import yaml

    text = yaml.safe_dump(cfg.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True)
    return Response(content=text, media_type="text/yaml", headers={"Cache-Control": "no-store"})


@app.post("/admin/client/{client_id}/config-remove")
def admin_remove_client_config(client_id: str, key: str = "", confirm: str = "") -> JSONResponse:
    """Stop serving an uploaded client config (used when offboarding a client)."""
    if not _master_key_ok(key):
        return JSONResponse({"error": "Not authorized."}, status_code=403)
    if confirm != client_id:
        return JSONResponse({"error": "Pass confirm=<client_id> to proceed."}, status_code=400)
    removed = config_module.remove_live_config(client_id)
    still_served = client_id in list_client_ids()
    return JSONResponse({"removed": removed, "still_served_from_image": still_served})


@app.post("/admin/client/{client_id}/delete")
def admin_delete_client(client_id: str, key: str = "", confirm: str = "") -> JSONResponse:
    """Erase everything stored for one client (the promise made in the terms).
    Requires confirm=<client_id> so it can't be triggered by accident."""
    if not _master_key_ok(key):
        return JSONResponse({"error": "Not authorized."}, status_code=403)
    if confirm != client_id:
        return JSONResponse({"error": "Pass confirm=<client_id> to proceed."}, status_code=400)
    with storage._conn() as conn:
        counts = {t: conn.execute(f"DELETE FROM {t} WHERE client_id = ?", (client_id,)).rowcount
                  for t in ("calls", "bookings", "cancelled_bookings", "escalations", "owner_users")}
    return JSONResponse({"deleted": counts, "client_id": client_id})


@app.post("/admin/purge-test-data")
def admin_purge_test_data(key: str = "") -> JSONResponse:
    """Remove rows created by the self-check and load tests (call SIDs with known test prefixes)."""
    if not _master_key_ok(key):
        return JSONResponse({"error": "Not authorized."}, status_code=403)
    removed = 0
    with storage._conn() as conn:
        for prefix in _TEST_SID_PREFIXES:
            for table in ("escalations", "bookings", "calls"):
                removed += conn.execute(f"DELETE FROM {table} WHERE call_sid LIKE ?", (prefix + "%",)).rowcount
    return JSONResponse({"removed": removed})


# --- Owner dashboard: bookings, calls, summaries, handoffs — viewable in a browser ---
# It shows real customer names and phone numbers, so it's protected by a
# secret in the URL. Each client gets its own key derived from one master
# secret (REPORT_KEY), so giving a client their link never exposes anyone
# else's data. This is a shared-link scheme, not a login system — appropriate
# for one owner checking a private bookmark; move to real auth before a client
# has multiple staff who need separate access.
_REPORT_KEY = os.environ.get("REPORT_KEY", "")

_OUTCOME_LABELS = {
    "completed": "Handled by AI",
    "transferred": "Transferred to you",
    "transfer_unanswered": "Transfer not answered",
    "caller_hung_up": "Caller hung up",
    "no_input": "No response from caller",
}


def report_key_for(client_id: str) -> str:
    return hmac.new(_REPORT_KEY.encode(), client_id.encode(), hashlib.sha256).hexdigest()[:24]


def _report_authorized(client_id: str, key: str) -> bool:
    if not _REPORT_KEY or not key:
        return False
    return hmac.compare_digest(key, _REPORT_KEY) or hmac.compare_digest(key, report_key_for(client_id))


def _fmt_utc(iso: str | None, tz: ZoneInfo) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
    except ValueError:
        return iso


def _fmt_phone(number: str | None) -> str:
    """+15555550100 ->+15555550100 for display; anything else is shown as it came."""
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else (number or "")


def _fmt_slot(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M").strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
    except ValueError:
        return iso


@app.post("/report/{client_id}/handled")
async def report_handled(client_id: str, request: Request) -> Response:
    """The owner marks a follow-up as handled. Authorized by that client's own dashboard key; scoped to that client's calls."""
    form = await request.form()
    key, call_sid = str(form.get("key", "")), str(form.get("call", ""))
    if not _report_authorized(client_id, key):
        return HTMLResponse("<p>Not authorized.</p>", status_code=403)
    storage.resolve_attention(client_id, call_sid)
    return RedirectResponse(f"/report/{quote(client_id)}?key={quote(key)}#attention", status_code=303)


@app.get("/report/{client_id}", response_class=HTMLResponse)
def report_page(client_id: str, key: str = "") -> HTMLResponse:
    if not _report_authorized(client_id, key):
        return HTMLResponse("<p>Not authorized.</p>", status_code=403)

    try:
        config = load_client_config(client_id)
    except ClientNotFoundError:
        return HTMLResponse("<p>Unknown client.</p>", status_code=404)

    tz = ZoneInfo(config.timezone)
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    now_local = datetime.now(tz).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M")

    conn = sqlite3.connect(storage.DB_PATH)
    calls = conn.execute(
        "SELECT from_number, started_at, outcome, summary, transcript_json, outcome_class FROM calls "
        "WHERE client_id = ? AND started_at >= ? ORDER BY started_at DESC LIMIT 100",
        (client_id, since),
    ).fetchall()
    week_rows = conn.execute(
        "SELECT started_at, outcome_class, needs_attention FROM calls WHERE client_id = ? AND started_at >= ?",
        (client_id, (datetime.now(timezone.utc) - timedelta(weeks=9)).isoformat()),
    ).fetchall()
    attention = conn.execute(
        "SELECT call_sid, from_number, started_at, outcome_class, summary FROM calls "
        "WHERE client_id = ? AND started_at >= ? AND needs_attention = 1 AND attention_resolved_at IS NULL "
        "ORDER BY started_at DESC LIMIT 50",
        (client_id, since),
    ).fetchall()
    bookings = conn.execute(
        "SELECT caller_name, caller_phone, service, slot_start, status FROM bookings "
        "WHERE client_id = ? AND created_at >= ? ORDER BY slot_start",
        (client_id, since),
    ).fetchall()
    escalations = conn.execute(
        "SELECT reason, caller_phone, summary, created_at FROM escalations "
        "WHERE client_id = ? AND created_at >= ? ORDER BY created_at DESC",
        (client_id, since),
    ).fetchall()
    conn.close()

    upcoming = [b for b in bookings if b[3] >= now_local and b[4] == "confirmed"]
    past = [b for b in bookings if b[3] < now_local or b[4] != "confirmed"]

    def booking_rows(rows: list) -> str:
        return "".join(
            f"<tr><td>{_h(_fmt_slot(slot))}</td><td>{_h(name)}</td>"
            f'<td><a href="tel:{_h(phone)}">{_h(_fmt_phone(phone))}</a></td><td>{_h(service)}</td></tr>'
            for name, phone, service, slot, _status in rows
        )

    upcoming_html = booking_rows(upcoming) or '<tr><td colspan="4" class="empty">No upcoming bookings.</td></tr>'
    past_html = booking_rows(past) or '<tr><td colspan="4" class="empty">Nothing yet.</td></tr>'

    esc_html = "".join(
        f"<tr><td>{_h(_fmt_utc(created_at, tz))}</td><td>{_h(reason.replace('_', ' '))}</td>"
        f'<td><a href="tel:{_h(phone or "")}">{_h(_fmt_phone(phone))}</a></td><td>{_h(summary_text or "")}</td></tr>'
        for reason, phone, summary_text, created_at in escalations
    ) or '<tr><td colspan="4" class="empty">No callbacks needed.</td></tr>'

    weeks = outcomes.weekly_summary(week_rows, tz)
    weeks_html = "".join(
        f"<tr><td>{_h(w['week_start'])}</td><td>{w['calls']}</td><td>{w['booked']}</td><td>{w['rescheduled_or_cancelled']}</td><td>{w['callbacks_and_leads']}</td>"
        f"<td>{w['put_through']}</td><td>{w['answered_questions']}</td><td>{w['after_hours']}</td><td>{w['needs_attention']}</td></tr>" for w in weeks
    )
    attention_html = "".join(
        f'<div class="call attn"><div class="meta"><b>{_h(_fmt_utc(started_at, tz))}</b> &middot; '
        f'<a href="tel:{_h(from_number or "")}">{_h(_fmt_phone(from_number) or "unknown")}</a> &middot; '
        f'<span class="tag warn">{_h(outcomes.LABELS.get(cls or "", "Needs a look"))}</span></div>'
        f"<p>{_h(call_summary) if call_summary else 'Open the call below for details.'}</p>"
        f'<form method="post" action="/report/{_h(quote(client_id))}/handled"><input type="hidden" name="key" value="{_h(key)}">'
        f'<input type="hidden" name="call" value="{_h(sid)}"><button type="submit">Mark handled</button></form></div>'
        for sid, from_number, started_at, cls, call_summary in attention
    ) or '<p class="empty">Nothing waiting on you.</p>'

    call_items = []
    for from_number, started_at, outcome, call_summary, transcript_json, outcome_class in calls:
        label = outcomes.LABELS.get(outcome_class or "") or _OUTCOME_LABELS.get(outcome or "", (outcome or "In progress").replace("_", " ").title())
        transcript_html = ""
        if config.record_transcripts:
            try:
                turns = json.loads(transcript_json)
            except ValueError:
                turns = []
            if turns:
                lines = "".join(
                    f"<p><b>{'Caller' if t.get('role') == 'caller' else 'AI'}:</b> {_h(t.get('text', ''))}</p>"
                    for t in turns
                )
                transcript_html = f"<details><summary>Full transcript</summary>{lines}</details>"
        body = _h(call_summary) if call_summary else ("" if config.record_transcripts else "Details not recorded for privacy.")
        call_items.append(
            f'<div class="call"><div class="meta"><b>{_h(_fmt_utc(started_at, tz))}</b> &middot; '
            f'{_h(_fmt_phone(from_number) or "unknown")} &middot; <span class="tag">{_h(label)}</span></div>'
            f"<p>{body}</p>{transcript_html}</div>"
        )
    calls_html = "".join(call_items) or '<p class="empty">No calls yet.</p>'

    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<meta http-equiv="refresh" content="120">
<title>{_h(config.business_name)} — Dashboard</title>
<style>
body{{font-family:-apple-system,Segoe UI,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#20293A;background:#F9FAFA;}}
h1{{font-size:22px;margin-bottom:4px;}} h2{{font-size:15px;margin:30px 0 8px;text-transform:uppercase;letter-spacing:.06em;color:#5B6472;}}
.sub{{color:#5B6472;font-size:13px;margin:0 0 16px;}}
table{{width:100%;border-collapse:collapse;font-size:14px;background:#fff;border:1px solid #DEE3E8;border-radius:8px;}}
th,td{{text-align:left;padding:9px 11px;border-bottom:1px solid #EEF1F5;vertical-align:top;}}
th{{color:#5B6472;font-size:11px;text-transform:uppercase;}}
.stats{{display:flex;gap:10px;flex-wrap:wrap;}}
.stat{{background:#fff;border:1px solid #DEE3E8;border-radius:8px;padding:10px 16px;min-width:110px;}}
.stat b{{display:block;font-size:22px;}}
.call{{background:#fff;border:1px solid #DEE3E8;border-radius:8px;padding:12px 14px;margin-bottom:8px;font-size:14px;}}
.call p{{margin:6px 0;}} .meta{{font-size:13px;color:#5B6472;}} .tag{{background:#E4F5EF;color:#0F7B63;border-radius:10px;padding:1px 8px;font-size:12px;}}
details{{margin-top:6px;font-size:13px;}} summary{{cursor:pointer;color:#2F6FED;}}
.empty{{color:#8A93A3;}} a{{color:#2F6FED;text-decoration:none;}}
.tag.warn{{background:#FCEBD2;color:#8A4B00;}} .attn{{border-left:4px solid #E08A00;}}
button{{font:inherit;font-size:13px;padding:5px 12px;border:1px solid #2F6FED;background:#fff;color:#2F6FED;border-radius:6px;cursor:pointer;}}
</style></head><body>
<h1>{_h(config.business_name)}</h1>
<p class="sub">Last 30 days &middot; times shown in {_h(config.timezone)} &middot; refreshes every 2 minutes</p>
<div class="stats">
  <div class="stat"><b>{len(calls)}</b>calls answered</div>
  <div class="stat"><b>{len(upcoming)}</b>upcoming bookings</div>
  <div class="stat"><b>{len(escalations)}</b>callbacks needed</div>
</div>
<h2 id="attention">Needs your attention ({len(attention)})</h2>
{attention_html}
<h2>Week by week</h2>
<table><tr><th>Week of</th><th>Calls</th><th>Booked</th><th>Moved / cancelled</th><th>Callbacks &amp; leads</th><th>Put through</th><th>Questions answered</th><th>After hours*</th><th>Needed attention</th></tr>{weeks_html}</table>
<p class="sub">Counts the system recorded. *After hours = before 8, after 6 or on a weekend. A booking, a lead or a callback is not revenue: nothing here estimates income.</p>
<h2>Upcoming bookings</h2>
<table><tr><th>When</th><th>Name</th><th>Phone</th><th>Service</th></tr>{upcoming_html}</table>
<h2>Callbacks &amp; handoffs</h2>
<table><tr><th>When</th><th>Why</th><th>Phone</th><th>Details</th></tr>{esc_html}</table>
<h2>Recent calls</h2>
{calls_html}
<h2>Past bookings</h2>
<table><tr><th>When</th><th>Name</th><th>Phone</th><th>Service</th></tr>{past_html}</table>
<p class="sub" style="margin-top:34px">Questions or want something changed? Text {_h(brand.get().contact_name)} at {_h(brand.get().support_phone)}. To turn forwarding off yourself: Verizon *73, AT&amp;T #21#, T-Mobile ##21#, or ##002# on most other carriers.</p>
</body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.get("/admin", response_class=HTMLResponse)
def admin_index(key: str = "") -> HTMLResponse:
    """One page for the operator: every client, 30-day activity, and each
    client's private dashboard link. Master key only."""
    if not _REPORT_KEY or not key or not hmac.compare_digest(key, _REPORT_KEY):
        return HTMLResponse("<p>Not authorized.</p>", status_code=403)

    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    conn = sqlite3.connect(storage.DB_PATH)
    call_counts = dict(conn.execute("SELECT client_id, COUNT(*) FROM calls WHERE started_at >= ? GROUP BY client_id", (since,)))
    booking_counts = dict(conn.execute("SELECT client_id, COUNT(*) FROM bookings WHERE created_at >= ? GROUP BY client_id", (since,)))
    esc_counts = dict(conn.execute("SELECT client_id, COUNT(*) FROM escalations WHERE created_at >= ? GROUP BY client_id", (since,)))
    conn.close()

    rows = []
    for client_id in list_client_ids():
        try:
            config = load_client_config(client_id)
        except Exception:
            continue
        link = f"/report/{_h(client_id)}?key={report_key_for(client_id)}"
        rows.append(
            f"<tr><td><b>{_h(config.business_name)}</b><br><span class='id'>{_h(client_id)}</span></td>"
            f"<td>{call_counts.get(client_id, 0)}</td><td>{booking_counts.get(client_id, 0)}</td>"
            f"<td>{esc_counts.get(client_id, 0)}</td><td><a href=\"{link}\">Dashboard</a> &middot; "
            f"<a href=\"/admin/client/{_h(client_id)}/number\">Number</a> &middot; "
            f"<a href=\"/admin/client/{_h(client_id)}/owner\">Owner login</a> &middot; "
            f"<a href=\"/activate/{_h(client_id)}?key={report_key_for(client_id)}\">Client setup page</a></td></tr>"
        )
    intake_rows = "".join(
        f"<tr><td>{r['id']}</td><td><b>{_h(r['business_name'] or '')}</b><br><span class='id'>{_h(r['trade'] or '')}</span></td>"
        f"<td>{_h(r['owner_name'] or '')}<br><a href=\"tel:{_h(r['owner_phone'] or '')}\">{_h(r['owner_phone'] or '')}</a></td>"
        f"<td>{_h(r['created_at'][:16])}</td><td>{_h(r['status'])}</td>"
        f"<td><a href=\"/admin/intake/{r['id']}/review\">Review &amp; go live</a></td></tr>"
        for r in storage.list_intakes(10)
    )
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{_h(brand.get().name)} admin</title>
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#20293A;background:#F9FAFA}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #DEE3E8}}th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid #EEF1F5;font-size:14px}}
th{{font-size:11px;text-transform:uppercase;color:#5B6472}}.id{{font-size:12px;color:#8A93A3}}a{{color:#2F6FED}}</style></head><body>
<h1>{_h(brand.get().name)} clients</h1><p>Last 30 days. Each dashboard link uses that client's own key; send a client only their own link.</p>
<table><tr><th>Client</th><th>Calls</th><th>Bookings</th><th>Callbacks</th><th></th></tr>{''.join(rows)}</table>
<h2 style="margin-top:34px">New client intakes</h2>
<p>Clients fill in <a href="/start">/start</a>. Tap <b>Review &amp; go live</b> on a new one: check the receptionist it built, go live, then get them a number.</p>
<table><tr><th>#</th><th>Business</th><th>Contact</th><th>Received</th><th>Status</th><th></th></tr>{intake_rows or '<tr><td colspan="6">None yet.</td></tr>'}</table>
<p style="margin-top:24px;font-size:13px">Pages: <a href="/start">intake form</a> &middot; <a href="/terms">terms</a> &middot; <a href="/book">booking page</a></p></body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
