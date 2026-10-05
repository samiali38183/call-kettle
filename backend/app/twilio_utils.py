from __future__ import annotations

import logging
import os
import re
from xml.sax.saxutils import escape

from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient

logger = logging.getLogger("callkettle.twilio")

_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")
_SKIP_SIGNATURE_CHECK = os.environ.get("CALLKETTLE_SKIP_SIGNATURE_CHECK", "0") == "1"


def validate_signature(*, url: str, form: dict[str, str], signature: str) -> bool:
    if _SKIP_SIGNATURE_CHECK:
        logger.warning("Twilio signature check skipped (CALLKETTLE_SKIP_SIGNATURE_CHECK=1)")
        return True
    if not _AUTH_TOKEN:
        logger.error(
            "Rejecting webhook to %s — TWILIO_AUTH_TOKEN is not set. "
            "Set it in .env, or set CALLKETTLE_SKIP_SIGNATURE_CHECK=1 for local testing only.",
            url,
        )
        return False
    if not signature:
        logger.warning("Rejecting webhook to %s — no X-Twilio-Signature header on the request", url)
        return False
    validator = RequestValidator(_AUTH_TOKEN)
    valid = validator.validate(url, form, signature)
    if not valid:
        logger.warning(
            "Rejecting webhook to %s — signature did not match. Usual cause: this URL doesn't "
            "exactly match what Twilio signed (scheme/host behind a proxy, or query string mismatch).",
            url,
        )
    return valid


# Twilio's default <Say> voice is the robotic "basic" one. Polly Neural sounds
# far more human for ~$0.0032 per 100 characters (a typical call is a few
# cents). Overridable without a redeploy: `fly secrets set TTS_VOICE=...`.
_VOICE = os.environ.get("TTS_VOICE", "Polly.Joanna-Neural")


NO_SPEECH_PROMPT = "Sorry, I didn't catch that."
NO_SPEECH_PROMPT_ES = "Perd\u00f3n, no le escuch\u00e9."

# Spanish (beta): a caller presses 2, then speech recognition switches to es-US and replies are
# spoken by a native Spanish neural voice. Overridable without a redeploy: TTS_VOICE_ES.
_VOICE_ES = os.environ.get("TTS_VOICE_ES", "Polly.Lupe-Neural")
LANG_CODES = {"en": "en-US", "es": "es-US"}

_DIGIT_WORDS = "zero one two three four five six seven eight nine".split()
_DIGIT_WORDS_ES = "cero uno dos tres cuatro cinco seis siete ocho nueve".split()
_PHONE_RE = re.compile(r"(?<![\w$])\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}(?!\d)")


def _spoken_phone(match: re.Match, lang: str = "en") -> str:
    digits = re.sub(r"\D", "", match.group())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return match.group()
    table = _DIGIT_WORDS_ES if lang == "es" else _DIGIT_WORDS
    words = [table[int(d)] for d in digits]
    return ", ".join(" ".join(words[i:j]) for i, j in ((0, 3), (3, 6), (6, 10)))


def speakable(text: str, lang: str = "en") -> str:
    """Phone numbers written as '+1-555-777-0101' get read aloud as symbols
    and one giant number. Deterministically respell them as digit groups —
    a backstop for when the model doesn't follow the prompt's speaking rules."""
    return _PHONE_RE.sub(lambda m: _spoken_phone(m, lang), text)


_ES_SEGMENT = re.compile(r"\[es\](.*?)\[/es\]", re.S)


def _say_one(text: str, lang: str) -> str:
    if lang == "es":
        return f'<Say voice="{escape(_VOICE_ES)}" language="es-US">{escape(speakable(text, "es"))}</Say>'
    return f'<Say voice="{escape(_VOICE)}">{escape(speakable(text))}</Say>'


def _say(text: str, lang: str = "en") -> str:
    """One <Say>, or several when the text carries [es]...[/es] segments: an English sentence followed by its
    Spanish twin is read by the English voice and then a native Spanish voice."""
    if lang == "en" and "[es]" in text:
        parts, pos = [], 0
        for m in _ES_SEGMENT.finditer(text):
            if text[pos:m.start()].strip():
                parts.append(_say_one(text[pos:m.start()].strip(), "en"))
            parts.append(_say_one(m.group(1).strip(), "es"))
            pos = m.end()
        if text[pos:].strip():
            parts.append(_say_one(text[pos:].strip(), "en"))
        return "".join(parts)
    return _say_one(text, lang)


def gather_twiml(
    *,
    say_text: str,
    action_url: str,
    timeout: int = 5,
    speech_timeout: str = "auto",
    hints: str = "",
    lang: str = "en",
    dtmf_prompt: str | None = None,
    accept_dtmf: bool = False,
) -> str:
    """dtmf_prompt: a Spanish sentence ("Para espa\u00f1ol, oprima dos.") spoken inside the Gather so a
    key press interrupts it. When set, the Gather also accepts one key press."""
    # speechTimeout="auto" ends at the first pause, which chops phone numbers
    # dictated in groups ("five seven one ... two nine zero ..."). Callers pass
    # a fixed number of seconds when a number is expected.
    hints_attr = f' hints="{escape(hints, {chr(34): "&quot;"})}"' if hints else ""
    press_key = bool(dtmf_prompt) or accept_dtmf
    input_mode = "speech dtmf" if press_key else "speech"
    digits_attr = ' numDigits="1"' if press_key else ""
    language = LANG_CODES.get(lang, "en-US")
    prompt_say = _say(dtmf_prompt, "es") if dtmf_prompt else ""
    no_speech = NO_SPEECH_PROMPT_ES if lang == "es" else NO_SPEECH_PROMPT
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Gather input="{input_mode}" action="{escape(action_url)}" method="POST" language="{language}" '
        f'speechTimeout="{escape(speech_timeout)}" timeout="{timeout}"{digits_attr}{hints_attr}>'
        f"{_say(say_text, lang)}{prompt_say}"
        "</Gather>"
        f"{_say(no_speech, lang)}"
        f'<Redirect method="POST">{escape(action_url)}</Redirect>'
        "</Response>"
    )


STREAM_NAME = "ck_stt"


def _stream_start_xml(stream_url: str, params: dict[str, str]) -> str:
    """Unidirectional (caller -> us) Media Stream. Custom parameters travel in the websocket `start` message, not in the URL."""
    parameters = "".join(f'<Parameter name="{escape(k, {chr(34): "&quot;"})}" value="{escape(v, {chr(34): "&quot;"})}"/>' for k, v in params.items())
    return f'<Start><Stream url="{escape(stream_url)}" track="inbound_track" name="{STREAM_NAME}">{parameters}</Stream></Start>'


def stream_reply_twiml(*, say_text: str, fallback_url: str, lang: str = "en", pause_s: int = 10) -> str:
    """Speak (Polly, same <Say> as Gather), then wait while the already-running stream listens. A new REST update replaces this
    TwiML when the caller finishes speaking; if nothing arrives the Redirect lands in the ordinary /voice/gather handler."""
    spoken = _say(say_text, lang) if say_text else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response>{spoken}<Pause length="{int(pause_s)}"/>'
        f'<Redirect method="POST">{escape(fallback_url)}</Redirect></Response>'
    )


def stream_greeting_twiml(*, say_text: str, stream_url: str, params: dict[str, str], fallback_url: str, lang: str = "en", pause_s: int = 10) -> str:
    return stream_reply_twiml(say_text=say_text, fallback_url=fallback_url, lang=lang, pause_s=pause_s).replace(
        "<Response>", "<Response>" + _stream_start_xml(stream_url, params), 1)


def with_stream_stop(twiml: str) -> str:
    """Stop the Media Stream (no more audio is sent to the STT provider) at the start of any TwiML."""
    return twiml.replace("<Response>", f'<Response><Stop><Stream name="{STREAM_NAME}"/></Stop>', 1)


def gather_digits_twiml(*, say_text: str, action_url: str, num_digits: int, timeout: int = 10) -> str:
    """Key presses only (a code). No speech recognition, so nothing the caller says is transcribed."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Gather input="dtmf" action="{escape(action_url)}" method="POST" numDigits="{int(num_digits)}" timeout="{int(timeout)}" finishOnKey="#">'
        f"{_say(say_text, 'en')}"
        "</Gather>"
        f"{_say('We did not get a code. Goodbye.', 'en')}<Hangup/>"
        "</Response>"
    )


def reject_twiml() -> str:
    """Decline the call before answering it. Twilio does not bill a call whose FIRST verb is <Reject> (docs: twiml/reject),
    which makes it the only truly zero-cost way to stop an abusive flood."""
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Reject reason="busy"/></Response>'


def say_and_hangup_twiml(text: str, lang: str = "en") -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{_say(text, lang)}<Hangup/></Response>'


def is_dialable(number) -> bool:
    """True only for a number Twilio can <Dial>: an optional +, then 7 to 15 digits, with nothing but spacing and punctuation.
    A blank or garbled transfer destination must never become a <Dial> that rings nothing and strands the caller."""
    if not isinstance(number, str):
        return False
    text = number.strip()
    if not re.fullmatch(r"\+?[\d\s().-]+", text):
        return False
    return 7 <= len(re.sub(r"\D", "", text)) <= 15


def transfer_twiml(
    *, say_text: str, phone_number: str, action_url: str | None = None, ring_seconds: int = 25, lang: str = "en",
    whisper_url: str | None = None, time_limit: int | None = None,
) -> str:
    # time_limit (seconds) caps the bridged call (Dial timeLimit); None keeps Twilio's own default of 4 hours.
    limit = f' timeLimit="{int(time_limit)}"' if time_limit else ""
    # With an action URL, Twilio asks us what to do when the Dial ends — so an
    # unanswered transfer becomes "take a message" instead of dead air.
    action = f' action="{escape(action_url)}" method="POST"' if action_url else ""
    # With a whisper URL the owner is screened first: Twilio plays that TwiML to the owner's leg and only bridges the
    # caller if it finishes without hanging up.
    number = (f'<Number url="{escape(whisper_url)}" method="POST">{escape(phone_number)}</Number>' if whisper_url
              else escape(phone_number))
    spoken = _say(say_text, lang) if say_text else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response>{spoken}<Dial timeout="{ring_seconds}"{limit}{action}>{number}</Dial></Response>'
    )


def whisper_twiml(*, announcement: str, accept_url: str) -> str:
    """Played to the owner when they pick up a transferred call: say who is calling, press 1 to accept."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Gather input="dtmf" numDigits="1" timeout="8" action="{escape(accept_url)}" method="POST">'
        f'{_say(announcement + " Press 1 to take the call.", "en")}</Gather>'
        f'{_say("No answer received. Goodbye.", "en")}<Hangup/></Response>'
    )


def say_and_continue_twiml(*, say_text: str, action_url: str, timeout: int = 5) -> str:
    return gather_twiml(say_text=say_text, action_url=action_url, timeout=timeout)


_twilio_client: TwilioClient | None = None


def _client() -> TwilioClient | None:
    global _twilio_client
    if not _ACCOUNT_SID or not _AUTH_TOKEN:
        return None
    if _twilio_client is None:
        _twilio_client = TwilioClient(_ACCOUNT_SID, _AUTH_TOKEN)
    return _twilio_client


def update_call_twiml(call_sid: str, twiml: str) -> None:
    """Replace the TwiML of a LIVE call (Twilio REST calls.update). Raises on any failure so the caller can fall back to Gather."""
    client = _client()
    if client is None:
        raise RuntimeError("Twilio REST is not configured")
    client.calls(call_sid).update(twiml=twiml)


def send_sms(*, to: str, body: str) -> bool:
    client = _client()
    if client is None or not _FROM_NUMBER:
        logger.warning("Twilio SMS not configured — would have sent to %s: %s", to, body)
        return False
    try:
        client.messages.create(to=to, from_=_FROM_NUMBER, body=body)
        return True
    except Exception:
        # A trial account sending to an unverified number, missing A2P 10DLC
        # registration, or a transient Twilio error should never take the
        # call down with it — the booking already succeeded, this is just
        # the confirmation text.
        logger.exception("Failed to send SMS to %s — booking still stands", to)
        return False
