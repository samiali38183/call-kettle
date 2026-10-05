"""DEVELOPER-RUN live smoke test for the Deepgram streaming adapter. NOT part of CI. NEVER run by an agent.

Real network + real provider usage = it MAY COST MONEY. It refuses to run unless DEEPGRAM_API_KEY is set AND you pass
--i-understand-this-may-cost-money. You supply the audio: an 8 kHz, mono, 8-bit mu-law WAV (WAVE format tag 7), e.g.
    ffmpeg -i in.wav -ar 8000 -ac 1 -c:a pcm_mulaw fixture.wav
Run from backend/:
    python scripts/stream_stt_smoke.py --i-understand-this-may-cost-money path/to/fixture.wav
Prints transcripts (so do not use a recording of a real customer). The key is never printed.
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

CONFIRM_FLAG = "--i-understand-this-may-cost-money"
CHUNK_BYTES = 160            # 20 ms of 8 kHz mu-law, like Twilio Media Streams


def read_mulaw_wav(path: str) -> bytes:
    """Return the raw mu-law payload of an 8 kHz mono 8-bit mu-law WAV; raise ValueError on anything else."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    pos, fmt, payload = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            payload = body
        pos += 8 + size + (size & 1)
    if fmt is None or payload is None:
        raise ValueError("missing fmt or data chunk")
    tag, channels, rate, _, _, bits = fmt
    if (tag, channels, rate, bits) != (7, 1, 8000, 8):
        raise ValueError(f"need 8 kHz mono 8-bit mu-law (format tag 7); got tag={tag} channels={channels} rate={rate} bits={bits}")
    return payload


def check_preconditions(argv: list[str], env) -> tuple[argparse.Namespace | None, str | None]:
    """Pure refusal logic (unit tested). Returns (args, None) when allowed, else (None, reason)."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("wav", nargs="?")
    parser.add_argument(CONFIRM_FLAG, action="store_true", dest="confirmed")
    parser.add_argument("--keyterm", action="append", default=[])
    args, _ = parser.parse_known_args(argv)
    if not args.confirmed:
        return None, f"refusing to run: this contacts a paid provider. Pass {CONFIRM_FLAG} to proceed."
    if not env.get("DEEPGRAM_API_KEY"):
        return None, "refusing to run: DEEPGRAM_API_KEY is not set (value is never printed)."
    if not args.wav:
        return None, "refusing to run: give the path of an 8 kHz mono mu-law WAV fixture."
    return args, None


def main(argv: list[str] | None = None, env=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    args, reason = check_preconditions(argv, env)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    try:
        audio = read_mulaw_wav(args.wav)
    except (OSError, ValueError) as exc:
        print(f"cannot use fixture: {exc}", file=sys.stderr)
        return 2
    from app import stream_stt
    run_env = {"DEEPGRAM_API_KEY": env["DEEPGRAM_API_KEY"], stream_stt.ENV_ENABLE: "1"}
    stt = stream_stt.DeepgramSTT(run_env, keyterms=args.keyterm)
    print(f"streaming {len(audio) / stream_stt.MULAW_BYTES_PER_SECOND:.1f}s of audio in real time...")
    try:
        for i in range(0, len(audio), CHUNK_BYTES):
            stt.feed(audio[i:i + CHUNK_BYTES])
            for t in stt.poll():
                print(("FINAL   " if t.is_final else "interim ") + t.text)
            time.sleep(CHUNK_BYTES / stream_stt.MULAW_BYTES_PER_SECOND)
        deadline = time.monotonic() + 5.0                     # let trailing finals arrive
        while time.monotonic() < deadline:
            for t in stt.poll():
                print(("FINAL   " if t.is_final else "interim ") + t.text)
            time.sleep(0.1)
    except stream_stt.ProviderError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return 1
    finally:
        stt.close()
    print(f"done. utterance_end events: {stt.utterance_ends}, speech_final results: {stt.speech_finals}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
