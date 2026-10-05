"""DEVELOPER-RUN local end-to-end check of stream-STT mode: REAL Deepgram, FAKE Twilio. NOT part of CI.

Real network + real provider usage = it MAY COST MONEY (about one clip's worth of Deepgram streaming). It refuses to run unless
DEEPGRAM_API_KEY is set AND you pass --i-understand-this-may-cost-money. Run from backend/:
    python scripts/stream_e2e_local.py --i-understand-this-may-cost-money path/to/fixture.wav
    python scripts/stream_e2e_local.py --i-understand-this-may-cost-money --bad-key path/to/fixture.wav     # failure -> Gather fallback

What it does (all inside this one process; nothing outside it is touched):
  * temp SQLite database and a temp clients folder holding ONE throwaway client (stt_mode: stream); production data is untouched
  * gate env vars (CALLKETTLE_STREAM_STT_ENABLED, a random CALLKETTLE_STREAM_TOKEN_SECRET) are set only in this process
  * fake Twilio: POST /voice/incoming, read the stream token from the TwiML, open /voice/stream, send `connected`/`start`, then
    the fixture as real-time 20 ms base64 `media` events, then 20 ms of silence frames (Twilio keeps sending) until a reply arrives
  * the Twilio REST client is replaced by a recorder, so calls.update TwiML is captured and nothing reaches Twilio
  * the AI model is an OFFLINE FAKE (same fake-client technique as tests/test_agent.py): run_turn really runs (prompt, history,
    TwiML building) but no model latency or Anthropic cost is included in the latency figure
  * --bad-key replaces the Deepgram key with a deliberately wrong value (the real key is not sent) to prove the Gather fallback
Prints transcripts (use a synthetic fixture, never a real customer). The key is never printed; the report states whether it
leaked into logs, the ledger, the database file or any captured TwiML.
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

CONFIRM_FLAG = "--i-understand-this-may-cost-money"
CLIENT_ID = "e2e_stream_hvac"
CALL_SID = "CA_E2E_LOCAL_0001"
FRAME_BYTES = 160                       # 20 ms of 8 kHz mu-law
FRAME_SECONDS = 0.02
REPLY_TIMEOUT_S = 25.0
WRONG_KEY = "deliberately-wrong-key-for-failure-test"
FAKE_REPLY = "Thanks John, I have your number as seven one three, five five five, zero one four two. Is the unit blowing warm air?"


def check_preconditions(argv: list[str], env) -> tuple[argparse.Namespace | None, str | None]:
    """Pure refusal logic (unit tested). Returns (args, None) when allowed, else (None, reason)."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("wav", nargs="?")
    parser.add_argument(CONFIRM_FLAG, action="store_true", dest="confirmed")
    parser.add_argument("--bad-key", action="store_true", dest="bad_key")
    args, _ = parser.parse_known_args(argv)
    if not args.confirmed:
        return None, f"refusing to run: this contacts a paid provider. Pass {CONFIRM_FLAG} to proceed."
    if not env.get("DEEPGRAM_API_KEY"):
        return None, "refusing to run: DEEPGRAM_API_KEY is not set (value is never printed)."
    if not args.wav:
        return None, "refusing to run: give the path of an 8 kHz mono mu-law WAV fixture."
    return args, None


# --------------------------------------------------------------------------- fakes
@dataclass
class _Block:
    text: str
    type: str = "text"


@dataclass
class _Response:
    content: list
    stop_reason: str = "end_turn"


class FakeAnthropic:
    """Offline stand-in for the Anthropic client (same shape tests/test_agent.py uses). Records what the model was asked."""

    def __init__(self, reply: str):
        self.reply, self.calls = reply, []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))     # snapshot: the agent keeps mutating its history list
        return _Response([_Block(self.reply)])


class FakeTwilioRest:
    """Stands in for twilio.rest.Client: client.calls(sid).update(twiml=...) is recorded with a monotonic timestamp."""

    def __init__(self):
        self.updates: list[tuple[float, str, str]] = []
        self.event = threading.Event()

    def calls(self, sid):
        outer = self

        class _Call:
            def update(self, twiml=None, **_):
                outer.updates.append((time.perf_counter(), sid, twiml))
                outer.event.set()

        return _Call()


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:
            self.lines.append(str(record.msg))


# --------------------------------------------------------------------------- run
def _prepare_environment(workdir: Path, deepgram_key: str) -> None:
    """Everything is set BEFORE app modules are imported (storage/config read these at import time)."""
    (workdir / "clients").mkdir()
    for name in ("SMS_ENABLED", "REPORT_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "CALLKETTLE_STREAM_STT_KILL"):
        os.environ.pop(name, None)
    os.environ.update({
        "CALLKETTLE_DB_PATH": str(workdir / "e2e.db"),
        "CALLKETTLE_CLIENTS_DIR": str(workdir / "clients"),
        "CALLKETTLE_SKIP_SIGNATURE_CHECK": "1",       # the fake Twilio sends no signature
        "CALLKETTLE_DISABLE_PUSH": "1",
        "ANTHROPIC_API_KEY": "offline-fake",
        "CALLKETTLE_STREAM_STT_ENABLED": "1",
        "CALLKETTLE_STREAM_TOKEN_SECRET": secrets.token_hex(24),
        "DEEPGRAM_API_KEY": deepgram_key,
    })


def _write_test_client(workdir: Path) -> None:
    import yaml
    from app import config as cfg
    raw = yaml.safe_load((cfg.CLIENTS_DIR / "demo_hvac.yaml").read_text(encoding="utf-8"))
    raw["client_id"], raw["stt_mode"] = CLIENT_ID, "stream"
    raw["demo_mode"] = False
    (workdir / "clients" / f"{CLIENT_ID}.yaml").write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _send(ws, obj: dict) -> None:
    ws.send_text(json.dumps(obj))


def run(args, report: dict) -> int:
    from stream_stt_smoke import read_mulaw_wav
    audio = read_mulaw_wav(args.wav)
    audio_seconds = len(audio) / 8000
    report["fixture_seconds"] = round(audio_seconds, 2)

    workdir = Path(tempfile.mkdtemp(prefix="ck_stream_e2e_"))
    real_key = os.environ["DEEPGRAM_API_KEY"]
    sent_key = WRONG_KEY if args.bad_key else real_key
    _prepare_environment(workdir, sent_key)
    report["workdir"] = str(workdir)
    report["mode"] = "bad-key" if args.bad_key else "real-key"

    capture = _LogCapture()
    capture.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    from app import agent, config as cfg, cost_observability as co, main, storage, stream_stt, twilio_utils
    logging.getLogger().addHandler(capture)
    logging.getLogger().setLevel(logging.INFO)
    cfg.load_client_config.cache_clear()
    _write_test_client(workdir)
    storage.init_db()

    fake_llm = FakeAnthropic(FAKE_REPLY)
    agent._anthropic_client = lambda: fake_llm
    rest = FakeTwilioRest()
    twilio_utils._client = lambda: rest

    from fastapi.testclient import TestClient
    transcripts: list[tuple[float, bool, bool, str]] = []
    real_factory = stream_stt.PROVIDER_FACTORY

    def recording_factory(**kw):
        stt = real_factory(**kw)
        original_poll = stt.poll
        t_origin = time.perf_counter()

        def poll():
            out = original_poll()
            for tr in out:
                transcripts.append((round(time.perf_counter() - t_origin, 2), tr.is_final, tr.speech_final, tr.text))
            return out
        stt.poll = poll
        report["provider_class"] = type(stt).__name__
        report["_stt"] = stt
        return stt

    stream_stt.PROVIDER_FACTORY = recording_factory
    recording_factory.transport_implemented = True

    utterances: list[str] = []
    real_run_turn = agent.run_turn

    def spying_run_turn(session, caller_text):
        utterances.append(caller_text)
        return real_run_turn(session, caller_text)
    main.agent.run_turn = spying_run_turn

    t_last_audio = None
    t_start_holder: list[float] = []
    with TestClient(main.app) as client:
        r = client.post(f"/voice/incoming?client_id={CLIENT_ID}", data={"CallSid": CALL_SID, "From": "+15555550100", "To": "+15555550199"})
        greeting = r.text
        report["incoming_status"] = r.status_code
        report["greeting_has_stream"] = "<Start><Stream" in greeting
        report["greeting_has_gather"] = "<Gather" in greeting
        token = re.search(r'<Parameter name="token" value="([^"]+)"', greeting)
        url = re.search(r'<Stream url="([^"]+)"', greeting)
        if not (token and url):
            report["error"] = "greeting TwiML had no stream url/token (gate closed?)"
            return 1
        path = "/" + url.group(1).split("://", 1)[1].split("/", 1)[1]
        with client.websocket_connect(path) as ws:
            _send(ws, {"event": "connected", "protocol": "Call", "version": "1.0.0"})
            _send(ws, {"event": "start", "start": {"callSid": CALL_SID, "streamSid": "MZE2E", "accountSid": "ACE2E",
                                                    "tracks": ["inbound"], "customParameters": {"token": token.group(1), "client_id": CLIENT_ID}}})
            frames = [audio[i:i + FRAME_BYTES] for i in range(0, len(audio), FRAME_BYTES)]
            t0 = time.perf_counter()
            t_start_holder.append(t0)
            seq = 0

            def media(payload: bytes):
                nonlocal seq
                seq += 1
                _send(ws, {"event": "media", "sequenceNumber": str(seq), "streamSid": "MZE2E",
                           "media": {"track": "inbound", "chunk": str(seq), "timestamp": str(seq * 20), "payload": base64.b64encode(payload).decode()}})

            def fell_back() -> bool:
                return any("<Gather" in u[2] for u in rest.updates)

            for i, frame in enumerate(frames):
                if fell_back():
                    break
                delay = t0 + i * FRAME_SECONDS - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                media(frame)
            t_last_audio = time.perf_counter()
            # Twilio keeps sending (silent) frames while the caller is quiet; the endpointer needs them to keep polling the provider.
            # Stop once an AI reply arrives AFTER the last speech byte (the answer to the final utterance) or the Gather fallback fired.
            i = len(frames)
            while not fell_back() and not any(ts > t_last_audio and "<Say" in tw for ts, _, tw in rest.updates)                     and time.perf_counter() - t_last_audio < REPLY_TIMEOUT_S:
                delay = t0 + i * FRAME_SECONDS - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                media(bytes([255]) * FRAME_BYTES)
                i += 1
            _send(ws, {"event": "stop", "streamSid": "MZE2E"})
            try:
                while True:
                    ws.receive_text()
            except Exception:
                pass

    report["transcripts"] = transcripts
    report["utterances_sent_to_run_turn"] = utterances
    stt = report.pop("_stt", None)
    report["speech_final_count"] = getattr(stt, "speech_finals", None)
    report["utterance_end_count"] = getattr(stt, "utterance_ends", None)
    report["model_calls"] = len(fake_llm.calls)
    report["model_saw_user_texts"] = [
        next((m.get("content") for m in reversed(c.get("messages") or []) if m.get("role") == "user"), None) for c in fake_llm.calls]
    updates = rest.updates
    report["update_count"] = len(updates)
    report["updates"] = [{"call_sid": sid, "seconds_after_last_audio_byte": round(ts - t_last_audio, 3),
                          "seconds_after_stream_start": round(ts - t_start_holder[0], 3), "twiml": tw} for ts, sid, tw in updates]
    report["update_kinds"] = ["fallback_gather" if "<Gather" in tw else "ai_reply" if "<Say" in tw else "silence_pause" for _, _, tw in updates]
    after = [ts - t_last_audio for ts, _, tw in updates if ts > t_last_audio and "<Say" in tw]
    report["latency_last_audio_byte_to_reply_twiml_s"] = round(after[0], 3) if after else None

    evidence = co.read_evidence(storage.DB_PATH, CLIENT_ID, CALL_SID)
    report["ledger_evidence"] = {k: v for k, v in evidence.items()}
    with sqlite3.connect(storage.DB_PATH) as conn:
        dump = "\n".join(json.dumps(conn.execute(f'SELECT * FROM "{t}"').fetchall(), default=str)
                         for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
    db_bytes = Path(storage.DB_PATH).read_bytes() if Path(storage.DB_PATH).exists() else b""
    logs = "\n".join(capture.lines)
    haystacks = {"logs": logs, "ledger_and_tables": dump, "captured_twiml": "\n".join(u[2] for u in updates),
                 "greeting_twiml": greeting, "report": json.dumps(report, default=str)}
    leaked = sorted(name for name, text in haystacks.items() if real_key in text)
    if real_key.encode() in db_bytes:
        leaked.append("db_file")
    leaked += sorted(f"{n}:token_secret" for n, t in haystacks.items() if os.environ["CALLKETTLE_STREAM_TOKEN_SECRET"] in t)
    report["key_leaked_into"] = leaked
    report["log_lines"] = len(capture.lines)
    report["log_sample"] = [l for l in capture.lines if "stream" in l.lower() or "fell back" in l.lower()][:10]
    return 0


def main(argv: list[str] | None = None, env=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    args, reason = check_preconditions(argv, env)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    report: dict = {}
    try:
        code = run(args, report)
    except (OSError, ValueError) as exc:
        print(f"cannot run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    printable = json.loads(json.dumps(report, default=str))
    print(json.dumps(printable, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
