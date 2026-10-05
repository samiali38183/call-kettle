"""Stream STT: an OFFLINE, FLAG-OFF alternative to Twilio <Gather> speech recognition.

STATUS: UNIT TESTED ONLY (fake connection). NEVER RUN WITH REAL AUDIO. No tests contact any provider.
See docs/STREAM_STT_DESIGN.md (private) for the design, the pilot procedure and the open questions.

What lives here (all pure Python, no network at import or in tests):
  * StreamingSTT   - the provider-agnostic interface (Twilio mu-law 8 kHz bytes in, interim/final transcripts out)
  * FakeSTT        - an in-memory implementation used by tests
  * DeepgramSTT    - Deepgram live-streaming adapter over an injectable connection (default: websockets sync client); UNVERIFIED live
  * stream_mode_active() - the single gate (per-client flag AND env AND ready provider AND no kill switch)
  * make_token/verify_token - HMAC token binding a stream to one call_sid + client_id
  * Endpointer     - pure end-of-utterance / barge-in logic
  * StreamCall     - transport-independent processor for one Twilio Media Streams connection, with fallback to Gather

This module is imported lazily by app.main (never at startup) and only for clients whose stt_mode is "stream".
Nothing here logs transcript text, audio, tokens or keys.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import json
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlencode

logger = logging.getLogger("callkettle.stream_stt")

# Environment variable NAMES only. Values are never stored in code, logs or the ledger.
ENV_ENABLE = "CALLKETTLE_STREAM_STT_ENABLED"       # explicit global enable flag
ENV_KILL = "CALLKETTLE_STREAM_STT_KILL"            # global kill switch: any value except 0/false/no/off disables stream mode
ENV_PROVIDER_KEY = "DEEPGRAM_API_KEY"
ENV_TOKEN_SECRET = "CALLKETTLE_STREAM_TOKEN_SECRET"  # signs the per-call stream token

MULAW_BYTES_PER_SECOND = 8000                       # Twilio Media Streams: 8 kHz, 8-bit mu-law, mono
STREAM_NAME = "ck_stt"                              # the <Stream name=...> a later <Stop> refers to
TOKEN_TTL_SECONDS = 3600


class NotConfigured(RuntimeError):
    """The streaming provider is not enabled/configured. Never carries a secret value."""


# --------------------------------------------------------------------------- interface + fake + provider skeleton
@dataclass(frozen=True)
class Transcript:
    text: str
    is_final: bool
    at: float | None = None
    speech_final: bool = False        # provider says the speaker paused (Deepgram speech_final); Endpointer treats it as end-of-utterance evidence


class StreamingSTT:
    """feed() must not block on the network for long; poll() returns whatever transcripts are ready. Both may raise."""

    transport_implemented = False
    utterance_ends = 0          # running count of provider UtteranceEnd signals (providers that have none leave it at 0)
    speech_starts = 0           # running count of provider SpeechStarted (voice onset) signals; 0 for providers without it

    def feed(self, audio: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def poll(self) -> list[Transcript]:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class FakeSTT(StreamingSTT):
    """Scripted in-memory provider for tests: script maps the Nth feed() call (1-based) to transcripts returned by the next poll()."""

    transport_implemented = True

    def __init__(self, script: dict[int, list[Transcript]] | None = None, *, fail_on_feed: bool = False, poll_error: bool = False):
        self.script = dict(script or {})
        self.fail_on_feed, self.poll_error = fail_on_feed, poll_error
        self.feeds = 0
        self.bytes_fed = 0
        self.closed = False
        self._pending: list[Transcript] = []

    def feed(self, audio: bytes) -> None:
        if self.fail_on_feed:
            raise RuntimeError("fake stt failure")
        self.feeds += 1
        self.bytes_fed += len(audio)
        self._pending.extend(self.script.get(self.feeds, []))

    def poll(self) -> list[Transcript]:
        if self.poll_error:
            raise RuntimeError("fake stt poll failure")
        out, self._pending = self._pending, []
        return out

    def close(self) -> None:
        self.closed = True


DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"
DEFAULT_MODEL = "nova-3"
DEFAULT_ENDPOINTING_MS = 300
DEFAULT_UTTERANCE_END_MS = 1000       # provider minimum is 1000 (needs interim_results=true)
DEFAULT_VAD_EVENTS = True             # SpeechStarted voice-onset events: lets the Endpointer notice a caller resuming within its grace
# Endpointing policy defaults, chosen from recorded real-Deepgram timelines of SYNTHETIC audio (docs/STREAM_STT_DESIGN.md):
# after the provider's speech_final the Endpointer still waits this long before taking the turn (statement / question / text that
# ends open: comma, no final punctuation, a digit run short of 10 digits), and holds the turn this long after a SpeechStarted.
DEFAULT_GRACE_S = 0.8
DEFAULT_QUESTION_GRACE_S = 0.5
DEFAULT_OPEN_GRACE_S = 1.5
DEFAULT_SPEECH_HOLD_S = 2.0
KEEPALIVE_SECONDS = 4.0               # Deepgram: KeepAlive every 3-5 s; 10 s without audio/KeepAlive closes the socket
MAX_MESSAGE_BYTES = 65536             # provider messages larger than this are ignored, never parsed
MAX_MESSAGES_PER_POLL = 100
MAX_KEYTERMS = 50                     # our own bound (provider limit not verified)
MAX_KEYTERM_CHARS = 100
CONNECT_TIMEOUT_SECONDS = 5.0


class ProviderError(RuntimeError):
    """The streaming provider failed. Messages carry a code or a type name only: never the key, headers or transcript text."""


class ProviderConnectionClosed(Exception):
    """The provider connection closed. Carries the close code only (the close reason may echo request data and is dropped)."""

    def __init__(self, code: int | None = None, reason: str | None = None):   # reason is accepted and deliberately discarded
        super().__init__(f"connection closed (code={code})")
        self.code = code


def _clean_terms(terms) -> list[str]:
    out: list[str] = []
    for term in terms or ():
        text = " ".join(str(term or "").split())[:MAX_KEYTERM_CHARS].strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_KEYTERMS:
            break
    return out


def build_listen_url(*, model: str = DEFAULT_MODEL, keyterms=(), endpointing_ms: int = DEFAULT_ENDPOINTING_MS,
                     utterance_end_ms: int = DEFAULT_UTTERANCE_END_MS, vad_events: bool = False) -> str:
    """Query string for Twilio Media Streams audio (8 kHz mono mu-law). No secret is ever part of the URL.

    Nova-3 takes repeated `keyterm=` params (plain terms only); older models take `keywords=`. [Per Deepgram docs; not run live.]"""
    params: list[tuple[str, str]] = [
        ("model", model), ("encoding", "mulaw"), ("sample_rate", "8000"), ("channels", "1"),
        ("interim_results", "true"), ("punctuate", "true"), ("smart_format", "true"),
        ("endpointing", str(int(endpointing_ms))), ("utterance_end_ms", str(int(utterance_end_ms))),
    ]
    if vad_events:
        params.append(("vad_events", "true"))      # SpeechStarted messages: voice onset, much earlier than the first recognised words
    hint = "keyterm" if str(model).startswith("nova-3") else "keywords"
    params += [(hint, term) for term in _clean_terms(keyterms)]
    return f"{DEEPGRAM_WS_URL}?{urlencode(params)}"


class _WebsocketsConnection:
    """Default connection: the `websockets` sync client (installed transitively by uvicorn[standard]; see the design doc)."""

    def __init__(self, ws):
        self._ws = ws

    def _guard(self, fn, *args, **kwargs):
        from websockets.exceptions import ConnectionClosed
        try:
            return fn(*args, **kwargs)
        except ConnectionClosed as exc:
            frame = getattr(exc, "rcvd", None) or getattr(exc, "sent", None)
            raise ProviderConnectionClosed(getattr(frame, "code", 1006)) from None

    def send_bytes(self, data: bytes) -> None:
        self._guard(self._ws.send, bytes(data))

    def send_text(self, text: str) -> None:
        self._guard(self._ws.send, str(text))

    def recv(self, timeout: float = 0.0):
        try:
            return self._guard(self._ws.recv, timeout=timeout)
        except TimeoutError:
            return None

    def close(self) -> None:
        self._ws.close()


def default_connect(url: str, headers: dict):
    from websockets.sync import client as ws_client          # imported only when a real connection is made
    ws = ws_client.connect(url, additional_headers=dict(headers), open_timeout=CONNECT_TIMEOUT_SECONDS, max_size=1 << 20)
    return _WebsocketsConnection(ws)


class DeepgramSTT(StreamingSTT):
    """Deepgram live-streaming transport. Synchronous (matches StreamingSTT): feed() sends audio, poll() drains what arrived.

    Connects lazily on the FIRST feed() (constructing is inert and offline). The API key is read from DEEPGRAM_API_KEY at connect
    time only and is sent only in the Authorization header; it is not stored on the instance. No reconnect is ever attempted: any
    failure raises ProviderError and the adapter stays failed so StreamCall falls back to Gather.

    UNVERIFIED with real audio / a real account (docs/STREAM_STT_DESIGN.md). Connection interface (injectable for tests):
    connect(url, headers) -> conn with send_bytes(bytes), send_text(str), recv(timeout) -> str|bytes|None, close().
    """

    transport_implemented = True

    def __init__(self, env: dict | None = None, *, connect: Callable | None = None, clock: Callable[[], float] | None = None,
                 keyterms=(), model: str = DEFAULT_MODEL, endpointing_ms: int = DEFAULT_ENDPOINTING_MS,
                 utterance_end_ms: int = DEFAULT_UTTERANCE_END_MS, keepalive_s: float = KEEPALIVE_SECONDS,
                 vad_events: bool = DEFAULT_VAD_EVENTS):
        source = os.environ if env is None else env
        missing = [name for name in (ENV_PROVIDER_KEY,) if not source.get(name)]
        if missing:
            raise NotConfigured(f"streaming STT is not configured: set {', '.join(missing)} (value never logged)")
        if not _truthy(source.get(ENV_ENABLE)):
            raise NotConfigured(f"streaming STT is not enabled: set {ENV_ENABLE}=1 (explicit opt-in)")
        # A closure, not a value: the key is looked up when connecting and is invisible to repr()/vars().
        self._read_key = lambda: (os.environ if env is None else env).get(ENV_PROVIDER_KEY)
        self._connect = connect or default_connect
        self._clock = clock or (lambda: monotonic())
        self._url = build_listen_url(model=model, keyterms=keyterms, endpointing_ms=endpointing_ms, utterance_end_ms=utterance_end_ms,
                                     vad_events=vad_events)
        self._keepalive_s = keepalive_s
        self._conn = None
        self._closed = False
        self._failed = False
        self._last_sent = 0.0
        self.utterance_ends = 0
        self.speech_starts = 0
        self.speech_finals = 0

    def __repr__(self) -> str:
        return "DeepgramSTT()"

    # -- internals
    def _fail(self, message: str) -> "ProviderError":
        self._failed = True
        return ProviderError(message)

    def _ensure(self):
        if self._closed:
            raise ProviderError("streaming STT adapter is closed")
        if self._failed:
            raise ProviderError("streaming STT adapter has failed (no reconnect is attempted)")
        if self._conn is None:
            key = self._read_key()
            if not key:
                raise self._fail("streaming STT key is not configured")
            try:
                self._conn = self._connect(self._url, {"Authorization": f"Token {key}"})
            except Exception as exc:
                raise self._fail(f"could not connect to the streaming provider ({type(exc).__name__})") from None
            self._last_sent = self._clock()
        return self._conn

    def _send(self, conn, fn_name: str, payload) -> None:
        try:
            getattr(conn, fn_name)(payload)
        except ProviderConnectionClosed as exc:
            raise self._fail(f"streaming provider closed the connection (code={exc.code})") from None
        except Exception as exc:
            raise self._fail(f"send to the streaming provider failed ({type(exc).__name__})") from None
        self._last_sent = self._clock()

    # -- StreamingSTT
    def feed(self, audio: bytes) -> None:
        conn = self._ensure()
        self._send(conn, "send_bytes", audio)

    def poll(self) -> list[Transcript]:
        if self._conn is None:
            if self._closed or self._failed:
                raise ProviderError("streaming STT adapter is not usable")
            return []
        conn = self._ensure()
        out: list[Transcript] = []
        for _ in range(MAX_MESSAGES_PER_POLL):
            try:
                raw = conn.recv(0.0)
            except ProviderConnectionClosed as exc:
                raise self._fail(f"streaming provider closed the connection (code={exc.code})") from None
            except Exception as exc:
                raise self._fail(f"receive from the streaming provider failed ({type(exc).__name__})") from None
            if raw is None:
                break
            transcript = self._handle(raw)
            if transcript is not None:
                out.append(transcript)
        if self._clock() - self._last_sent >= self._keepalive_s:
            self._send(conn, "send_text", json.dumps({"type": "KeepAlive"}))
        return out

    def _handle(self, raw) -> Transcript | None:
        """Parse one provider message. Anything malformed, oversized or unknown is ignored; only an Error message raises."""
        try:
            if len(raw) > MAX_MESSAGE_BYTES:
                return None
            msg = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
        except Exception:
            return None
        if not isinstance(msg, dict):
            return None
        kind = msg.get("type")
        if kind == "Error":                          # message shape [UNVERIFIED]; never echo its content
            raise self._fail("streaming provider reported an error")
        if kind == "UtteranceEnd":
            self.utterance_ends += 1
            return None
        if kind == "SpeechStarted":
            self.speech_starts += 1
            return None
        if kind != "Results":                        # Metadata, anything new
            return None
        try:
            alternatives = (msg.get("channel") or {}).get("alternatives") or []
            text = alternatives[0].get("transcript") if alternatives else None
        except Exception:
            return None
        if msg.get("speech_final") is True:
            self.speech_finals += 1
        if not isinstance(text, str) or not text.strip():
            return None
        return Transcript(text.strip(), msg.get("is_final") is True, self._clock(), speech_final=msg.get("speech_final") is True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.send_text(json.dumps({"type": "CloseStream"}))
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- keyterms derived from the client's config
MAX_DERIVED_TERM_CHARS = 60
_TERM_ALLOWED = re.compile(r"[^A-Za-z0-9 &'.\-/]")
_TERM_SENSITIVE = re.compile(
    r"@|\$|%|https?|www\.|\.(com|net|org|io|co|us)\b|\d{3,}|\d\.\d|\b\d+\s*(dollars?|bucks?|usd|cents?|percent|off)\b|\b(dollars?|bucks?|usd|price|prices|cost|costs|costing|fee|fees|free|discount)\b",
    re.IGNORECASE)
_AREA_QUESTION = re.compile(r"\b(area|areas|serve|serves|service area|located|location|coverage|cover)\b", re.IGNORECASE)
_AREA_LEAD = re.compile(r"\b(?:including|such as|serves?|covers?|throughout|across)\b", re.IGNORECASE)
_PLACE = re.compile(r"[A-Z][A-Za-z.'\-]*(?: [A-Z][A-Za-z.'\-]*){0,3}")
_NOT_PLACES = {"This", "That", "We", "I", "Yes", "No", "It", "Our", "The", "A", "An", "If", "For", "Call", "Please", "Sample"}


def _sanitize_term(raw) -> str | None:
    """One safe keyterm or None. Control characters/markup are removed; anything that looks like a phone number, email, URL,
    price or discount is DROPPED (not trimmed): the exact price and contact details must never reach a third-party provider."""
    if not isinstance(raw, str):
        return None
    text = re.sub(r"<[^>]*>", " ", raw)
    text = _TERM_ALLOWED.sub(" ", text)
    text = " ".join(text.split())
    if not text or _TERM_SENSITIVE.search(raw) or _TERM_SENSITIVE.search(text):
        return None
    if len(text) > MAX_DERIVED_TERM_CHARS:
        text = text[:MAX_DERIVED_TERM_CHARS].rsplit(" ", 1)[0]
    text = text.strip(" .-/'&")
    return text if len(text) >= 2 else None


def _areas_from_faqs(faqs) -> list[str]:
    """Place names from the answer to a 'what area do you serve' FAQ: the list after 'including/such as/serves', or 'the X area'.
    Only text from an area-type question is read, and only capitalised short phrases are taken (never sentences or numbers)."""
    out: list[str] = []
    if not isinstance(faqs, (list, tuple)):
        return out
    for faq in faqs:
        question, answer = getattr(faq, "q", None), getattr(faq, "a", None)
        if not isinstance(question, str) or not isinstance(answer, str) or not _AREA_QUESTION.search(question):
            continue
        sentence = re.split(r"(?<=[a-z)])\.\s", answer.strip(), maxsplit=1)[0]
        match = _AREA_LEAD.search(sentence)
        if not match:
            continue
        tail = sentence[match.end():]
        for chunk in re.split(r",|\band\b|\bor\b|;", tail):
            chunk = re.sub(r"^\s*((including|such as|like|and|or|the|in|of)\s+)+", "", chunk.strip(), flags=re.IGNORECASE)
            chunk = re.sub(r"\s+area\s*$", "", chunk, flags=re.IGNORECASE)
            place = _PLACE.fullmatch(chunk.strip())
            if place and place.group(0) not in _NOT_PLACES:
                out.append(place.group(0))
    return out


def derive_keyterms(config) -> list[str]:
    """Bounded, de-duplicated, sanitised recognition hints: business name first, then service names, then service-area place names
    (an explicit `service_areas` list if the config has one, else the answer to the area FAQ). Never phone numbers, emails, URLs,
    prices or FAQ sentences. Never raises: a malformed config yields what could be read. Cap: MAX_KEYTERMS terms."""
    candidates: list = []
    try:
        candidates.append(getattr(config, "business_name", None))
        for service in getattr(config, "services", None) or ():
            candidates.append(getattr(service, "name", None))
        explicit = getattr(config, "service_areas", None)
        if isinstance(explicit, (list, tuple)):
            candidates.extend(explicit)
        candidates.extend(_areas_from_faqs(getattr(config, "faqs", None)))
    except Exception:
        pass
    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        term = _sanitize_term(raw)
        if term and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
            if len(out) >= MAX_KEYTERMS:
                break
    return out


PROVIDER_FACTORY: Callable[..., StreamingSTT] = DeepgramSTT   # tests replace this; production keeps the skeleton
monotonic: Callable[[], float] = time.monotonic              # tests replace this with a deterministic clock


# --------------------------------------------------------------------------- the gate
def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _killed(value) -> bool:
    return str(value or "").strip().lower() not in ("", "0", "false", "no", "off")


def stream_mode_active(config, env: dict | None = None) -> bool:
    """True only when ALL hold: the client's stt_mode is "stream", the global enable flag is set, no kill switch is set, the
    provider key and token secret env vars are present, and the provider class has a working transport."""
    env = os.environ if env is None else env
    if getattr(config, "stt_mode", "gather") != "stream":
        return False
    if _killed(env.get(ENV_KILL)):
        return False
    if not _truthy(env.get(ENV_ENABLE)):
        return False
    if not env.get(ENV_PROVIDER_KEY) or not env.get(ENV_TOKEN_SECRET):
        return False
    return bool(getattr(PROVIDER_FACTORY, "transport_implemented", False))


# --------------------------------------------------------------------------- signed token (call_sid + client_id bound)
def _mac(secret: str, call_sid: str, client_id: str, exp: int) -> str:
    return hmac.new(secret.encode(), f"ck-stream-v1|{call_sid}|{client_id}|{exp}".encode(), hashlib.sha256).hexdigest()


def make_token(secret: str, call_sid: str, client_id: str, *, now: float, ttl: int = TOKEN_TTL_SECONDS) -> str:
    if not secret:
        raise ValueError("a token secret is required")
    exp = int(now + ttl)
    return f"{exp}.{_mac(secret, call_sid, client_id, exp)}"


def verify_token(secret: str, token, call_sid: str, client_id: str, *, now: float) -> bool:
    try:
        if not secret or not isinstance(token, str) or not call_sid or not client_id:
            return False
        exp_text, _, mac = token.partition(".")
        if not exp_text.isdigit() or not mac:
            return False
        exp = int(exp_text)
        if now > exp:
            return False
        return hmac.compare_digest(mac, _mac(secret, call_sid, client_id, exp))
    except Exception:
        return False


# --------------------------------------------------------------------------- endpointing (pure)
@dataclass(frozen=True)
class Utterance:
    text: str
    barge_in: bool = False


def est_speech_seconds(text: str) -> float:
    """ESTIMATE of how long the caller hears a spoken reply (about 14 characters a second, never under 2 s). Used only to
    decide whether caller speech counts as barge-in. Not measured."""
    return max(2.0, len(text or "") / 14.0)


_SPOKEN_DIGITS = {"zero", "oh", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"}
_DANGLING_WORDS = {"and", "but", "so", "because", "or", "my", "is", "the", "a", "an", "to", "with", "for", "at", "in", "of", "then", "also",
                   "um", "uh", "its", "it's", "that", "which"}
PHONE_DIGITS = 10


def count_digits(text: str) -> int:
    """Digits in a transcript, written (713) or spoken (seven one three)."""
    return sum(c.isdigit() for c in text) + sum(1 for w in re.findall(r"[A-Za-z']+", text.lower()) if w in _SPOKEN_DIGITS)


def utterance_shape(last_text: str, whole_text: str = "") -> str:
    """"question" (ends in ?), "complete" (a finished sentence or a whole phone number) or "open" (comma, no terminal
    punctuation, a dangling conjunction/article, or a number still short of ten digits): open text is likely mid-thought."""
    text = (last_text or "").strip()
    if not text:
        return "open"
    if text.endswith("?"):
        return "question"
    core = text.rstrip(" .!?,;:-")
    words = re.findall(r"[A-Za-z0-9']+", core.lower())
    last_word = words[-1] if words else ""
    if core[-1:].isdigit() or last_word in _SPOKEN_DIGITS:
        return "complete" if count_digits(whole_text or text) >= PHONE_DIGITS else "open"
    if text[-1] in ",;:-" or text[-1] not in ".!" or last_word in _DANGLING_WORDS:
        return "open"
    return "complete"


class Endpointer:
    """Decides when the caller has finished an utterance.

    Final transcripts accumulate; the utterance is complete once a final transcript is the latest thing heard and `patience`
    seconds passed without new speech. patience follows Gather's speechTimeout semantics via expect(): "3" (the AI just asked for a
    phone number) waits 3 s so a pause between digit groups is not the end; anything else uses the short default. If the provider
    never finalises the last words, the text so far is used after twice the patience. A hard cap flushes a runaway utterance.

    Provider evidence (Deepgram `speech_final` on a final transcript, or an `UtteranceEnd` signal) shortens the wait: in the default
    patience the utterance is complete after a short GRACE since the last transcript (0.8 s statement, 0.5 s question, 1.5 s when the
    text ends open: comma / no final punctuation / a digit run short of 10 digits) so a caller who only paused is not answered early;
    grace_s=0 restores "complete at once". A provider SpeechStarted (voice onset) while an utterance is pending holds the turn open
    for speech_hold_s until the new words arrive (docs/STREAM_STT_DESIGN.md, measured on synthetic audio only). When the AI just asked for a phone number the longer patience is KEPT (providers
    endpoint on the pause between digit groups), evidence only lets interim-only text use 1x instead of 2x patience. New speech
    clears the evidence. With no evidence at all the behaviour is exactly the silence-gap logic above.
    """

    def __init__(self, *, silence_s: float = 1.0, number_silence_s: float = 3.0, max_utterance_s: float = 30.0,
                 grace_s: float = DEFAULT_GRACE_S, question_grace_s: float | None = DEFAULT_QUESTION_GRACE_S,
                 open_grace_s: float | None = DEFAULT_OPEN_GRACE_S, speech_hold_s: float = DEFAULT_SPEECH_HOLD_S):
        self._default, self._number = silence_s, number_silence_s
        self._patience = silence_s
        self._max = max_utterance_s
        # Grace after provider end-of-utterance evidence (default patience only). 0 = complete at once (the old behaviour).
        self._grace, self._question_grace, self._open_grace = grace_s, question_grace_s, open_grace_s
        self._hold = speech_hold_s
        self._speaking_until = 0.0
        self._reset()

    def _reset(self) -> None:
        self._finals: list[str] = []
        self._interim = ""
        self._first: float | None = None
        self._last: float | None = None
        self._barge = False
        self._barge_reported = False
        self._ended = False
        self._hold_until = 0.0

    def expect(self, speech_timeout: str) -> None:
        text = str(speech_timeout or "auto").strip().lower()
        self._patience = self._number if text == "3" else self._default

    def ai_speaking_until(self, t: float) -> None:
        self._speaking_until = t

    def feed(self, transcript: Transcript, now: float) -> bool:
        """Record a transcript. Returns True exactly once per utterance when the caller starts talking over the AI (barge-in)."""
        text = (transcript.text or "").strip()
        if not text:
            return False
        new_barge = False
        if now < self._speaking_until:
            self._barge = True
            if not self._barge_reported:
                self._barge_reported = new_barge = True
        if self._first is None:
            self._first = now
        self._last = now
        self._hold_until = 0.0
        self._ended = False                       # new speech makes earlier end-of-utterance evidence stale
        if transcript.is_final:
            self._finals.append(text)
            self._interim = ""
            self._ended = bool(transcript.speech_final)
        else:
            self._interim = text
        return new_barge

    def utterance_end(self, now: float) -> None:
        """The provider reported UtteranceEnd (a quiet gap after words). Ignored when nothing is pending, so a stale signal can
        never end a later utterance."""
        if self._last is not None and self._first is not None:
            self._ended = True

    def speech_started(self, now: float) -> None:
        """The provider heard voice onset (SpeechStarted). Words take a second or so to be recognised, so while an utterance is
        pending this holds the turn open for speech_hold_s (cleared by the next transcript). Ignored when nothing is pending, and
        never a barge-in by itself (no words yet)."""
        if self._first is not None and self._last is not None and self._hold > 0:
            self._hold_until = now + self._hold

    def _grace_needed(self) -> float:
        if self._grace <= 0:
            return 0.0
        text = " ".join(self._finals + ([self._interim] if self._interim and not self._finals else []))
        shape = utterance_shape(self._finals[-1] if self._finals else self._interim, text)
        if shape == "question" and self._question_grace is not None:
            return self._question_grace
        if shape == "open" and self._open_grace is not None:
            return self._open_grace
        return self._grace

    def poll(self, now: float) -> Utterance | None:
        if self._last is None or self._first is None:
            return None
        if now < self._hold_until and now - self._first < self._max:
            return None
        idle = now - self._last
        needed = self._patience if self._finals and not self._interim else self._patience * 2
        if self._ended:
            needed = self._grace_needed() if self._patience == self._default else self._patience
        ready = idle >= needed
        if not ready and now - self._first < self._max:
            return None
        text = " ".join(self._finals + ([self._interim] if self._interim else [])).strip()
        barge = self._barge
        self._reset()
        return Utterance(text, barge) if text else None


# --------------------------------------------------------------------------- one Twilio Media Streams connection
@dataclass
class TurnResult:
    twiml: str                        # what to send to the live call
    reply_text: str                   # what the AI said (only its length is used here, never logged)
    ended: bool                       # the call is over or handed off: stop listening
    speech_timeout: str = "auto"      # "3" when the AI just asked for a phone number
    after: Callable[[], None] | None = field(default=None, repr=False)


class StreamCall:
    """Transport-independent: the websocket route feeds it Twilio events; tests feed it dicts. Every failure ends in the
    fallback TwiML (the ordinary Gather) so the caller is never dead-ended."""

    def __init__(self, *, call_sid: str, client_id: str, stt: StreamingSTT, updater, turn_handler: Callable[[str], TurnResult],
                 fallback_twiml: Callable[[], str], barge_in_twiml: str | None = None, clock: Callable[[], float] = time.monotonic,
                 endpointer: Endpointer | None = None, no_audio_s: float = 8.0, max_audio_s: float = 1800.0,
                 on_audio_seconds: Callable[[float], None] | None = None):
        self.call_sid, self.client_id = call_sid, client_id
        self._stt, self._updater, self._turn = stt, updater, turn_handler
        self._fallback_twiml, self._barge_in_twiml = fallback_twiml, barge_in_twiml
        self._clock = clock
        self._ep = endpointer or Endpointer()
        self._no_audio_s, self._max_audio_s = no_audio_s, max_audio_s
        self._on_audio_seconds = on_audio_seconds
        self._bytes = 0
        self._utterance_ends_seen = 0
        self._speech_starts_seen = 0
        self._last_audio: float | None = None
        self._closed = False
        self.stopped = False
        self.finished = False
        self.fallback_reason: str | None = None

    @property
    def audio_seconds(self) -> float:
        return self._bytes / MULAW_BYTES_PER_SECOND

    @property
    def done(self) -> bool:
        return self.finished or self.fallback_reason is not None or self.stopped

    # -- events
    def handle_event(self, msg: dict) -> Utterance | None:
        if self.finished or self.fallback_reason:
            return None
        event = msg.get("event")
        now = self._clock()
        if event == "stop":
            self.stopped = True
            self._close()
            return None
        if event != "media":
            return None
        try:
            audio = base64.b64decode((msg.get("media") or {}).get("payload", ""), validate=True)
        except Exception:
            return None
        self._bytes += len(audio)
        self._last_audio = now
        if self.audio_seconds > self._max_audio_s:
            self._fail("audio_cap")
            return None
        try:
            self._stt.feed(audio)
            results = self._stt.poll()
        except Exception as exc:
            self._fail("stt_error", exc)
            return None
        barge = False
        for transcript in results:
            barge = self._ep.feed(transcript, now) or barge
        ends = getattr(self._stt, "utterance_ends", 0)
        if isinstance(ends, int) and ends > self._utterance_ends_seen:
            self._utterance_ends_seen = ends
            self._ep.utterance_end(now)
        starts = getattr(self._stt, "speech_starts", 0)
        if isinstance(starts, int) and starts > self._speech_starts_seen:
            self._speech_starts_seen = starts
            self._ep.speech_started(now)
        if barge:
            self._cut_ai()
        return self._ep.poll(now)

    def tick(self) -> Utterance | None:
        """Call periodically while no message arrives: detects a stream that stopped delivering audio."""
        if self.done:
            return None
        now = self._clock()
        if self._last_audio is None:
            self._last_audio = now
            return None
        if now - self._last_audio >= self._no_audio_s:
            self._fail("no_audio")
            return None
        return self._ep.poll(now)

    def run_utterance(self, utterance: Utterance) -> None:
        """Blocking (model call + REST). Run it in a worker thread."""
        if self.finished or self.fallback_reason:
            return
        try:
            result = self._turn(utterance.text)
        except Exception as exc:
            self._fail("turn_error", exc)
            return
        try:
            self._updater.update_call(self.call_sid, result.twiml)
        except Exception as exc:
            self._fail("rest_error", exc)
            return
        now = self._clock()
        if result.ended:
            self.finished = True
            self._close()
        else:
            self._ep.expect(result.speech_timeout)
            self._ep.ai_speaking_until(now + est_speech_seconds(result.reply_text))
        if result.after is not None:
            try:
                result.after()
            except Exception as exc:
                logger.warning("post-turn step failed (%s)", type(exc).__name__)

    def on_disconnect(self) -> None:
        """The websocket is gone. Without a clean stop event that means the stream broke: fall back to Gather."""
        if not (self.stopped or self.finished or self.fallback_reason):
            self._fail("ws_drop")
        self._close()

    def close(self) -> None:
        self._close()

    # -- internals
    def _cut_ai(self) -> None:
        self._ep.ai_speaking_until(0.0)
        if self._barge_in_twiml:
            try:
                self._updater.update_call(self.call_sid, self._barge_in_twiml)
            except Exception as exc:
                logger.warning("barge-in update failed (%s)", type(exc).__name__)

    def _fail(self, reason: str, exc: Exception | None = None) -> None:
        if self.fallback_reason:
            return
        self.fallback_reason = reason
        # Reason and exception TYPE only: provider/Twilio messages can echo request data.
        logger.warning("stream STT fell back to Gather: reason=%s error=%s call=%s", reason, type(exc).__name__ if exc else "-", self.call_sid)
        try:
            self._updater.update_call(self.call_sid, self._fallback_twiml())
        except Exception as fallback_exc:
            logger.warning("fallback update failed (%s)", type(fallback_exc).__name__)
        self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stt.close()
        except Exception as exc:
            logger.warning("stt close failed (%s)", type(exc).__name__)
        if self._on_audio_seconds is not None and self._bytes > 0:
            try:
                self._on_audio_seconds(self.audio_seconds)
            except Exception as exc:
                logger.warning("could not record stt seconds (%s)", type(exc).__name__)


# --------------------------------------------------------------------------- REST updater (injectable; tests replace _UPDATER)
class TwilioRestUpdater:
    """Replaces the TwiML of a live call via Twilio's REST calls.update. Never exercised by tests."""

    def update_call(self, call_sid: str, twiml: str) -> None:
        from app import twilio_utils
        twilio_utils.update_call_twiml(call_sid, twiml)


_UPDATER = None


def get_call_updater():
    return _UPDATER if _UPDATER is not None else TwilioRestUpdater()
