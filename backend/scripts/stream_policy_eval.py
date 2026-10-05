"""DEVELOPER-RUN endpointing-policy evaluation for stream STT. NOT part of CI.

`record` streams ONE short synthetic clip to REAL Deepgram (it MAY COST MONEY, a few seconds of audio) and saves the provider
event timeline as JSON. `replay` re-runs saved timelines through the Endpointer offline under any policy (no network, free).
Run from backend/:
    python scripts/stream_policy_eval.py record --i-understand-this-may-cost-money tests/fixtures/stream_stt/sentence_pause.wav out.json [--endpointing-ms 300]
    python scripts/stream_policy_eval.py replay out.json [out2.json ...]
The timeline stores provider message metadata and transcript text of SYNTHETIC clips only. The key is never printed or stored.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

CONFIRM_FLAG = "--i-understand-this-may-cost-money"
CHUNK_BYTES = 160
TAIL_SECONDS = 4.0


def check_preconditions(argv, env):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("wav", nargs="?")
    parser.add_argument("out", nargs="?")
    parser.add_argument(CONFIRM_FLAG, action="store_true", dest="confirmed")
    parser.add_argument("--endpointing-ms", type=int, default=None)
    parser.add_argument("--vad", action="store_true", dest="vad", help="also request SpeechStarted voice-onset events")
    args, _ = parser.parse_known_args(argv)
    if not args.confirmed:
        return None, f"refusing to run: this contacts a paid provider. Pass {CONFIRM_FLAG} to proceed."
    if not env.get("DEEPGRAM_API_KEY"):
        return None, "refusing to run: DEEPGRAM_API_KEY is not set (value is never printed)."
    if not args.wav or not args.out:
        return None, "refusing to run: give a mu-law WAV fixture and an output JSON path."
    return args, None


class _Tee:
    """Wraps a Deepgram connection and keeps (arrival time, parsed metadata) of every Results/UtteranceEnd message."""

    def __init__(self, conn, sink, clock):
        self._c, self._sink, self._clock = conn, sink, clock

    def send_bytes(self, d):
        return self._c.send_bytes(d)

    def send_text(self, t):
        return self._c.send_text(t)

    def close(self):
        return self._c.close()

    def recv(self, timeout=0.0):
        raw = self._c.recv(timeout)
        if raw is not None:
            try:
                msg = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
                kind = msg.get("type")
                if kind == "Results":
                    alt = ((msg.get("channel") or {}).get("alternatives") or [{}])[0]
                    words = alt.get("words") or []
                    self._sink.append({"t": round(self._clock(), 3), "kind": "results", "text": alt.get("transcript", ""),
                                       "is_final": bool(msg.get("is_final")), "speech_final": bool(msg.get("speech_final")),
                                       "audio_end": round(float(msg.get("start", 0)) + float(msg.get("duration", 0)), 3),
                                       "last_word_end": round(float(words[-1]["end"]), 3) if words else None})
                elif kind == "SpeechStarted":
                    self._sink.append({"t": round(self._clock(), 3), "kind": "speech_started"})
                elif kind == "UtteranceEnd":
                    self._sink.append({"t": round(self._clock(), 3), "kind": "utterance_end"})
            except Exception:
                pass
        return raw


def record(args) -> int:
    from stream_stt_smoke import read_mulaw_wav
    from app import stream_stt
    audio = read_mulaw_wav(args.wav)
    events: list = []
    t0 = [None]

    def clock():
        return (time.perf_counter() - t0[0]) if t0[0] is not None else 0.0

    def connect(url, headers):
        return _Tee(stream_stt.default_connect(url, headers), events, clock)

    kw = {"endpointing_ms": args.endpointing_ms} if args.endpointing_ms else {}
    kw["vad_events"] = bool(args.vad)
    stt = stream_stt.DeepgramSTT({"DEEPGRAM_API_KEY": os.environ["DEEPGRAM_API_KEY"], stream_stt.ENV_ENABLE: "1"}, connect=connect, **kw)
    frames = [audio[i:i + CHUNK_BYTES] for i in range(0, len(audio), CHUNK_BYTES)]
    frames += [bytes([255]) * CHUNK_BYTES] * int(TAIL_SECONDS / 0.02)
    t0[0] = time.perf_counter()
    try:
        for i, frame in enumerate(frames):
            delay = t0[0] + i * 0.02 - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            stt.feed(frame)
            stt.poll()
    except stream_stt.ProviderError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return 1
    finally:
        stt.close()
    usage = len(frames) * 0.02
    out = {"fixture": Path(args.wav).name, "endpointing_ms": args.endpointing_ms or stream_stt.DEFAULT_ENDPOINTING_MS, "vad_events": bool(args.vad),
           "speech_audio_s": round(len(audio) / 8000, 2), "usage_s": round(usage, 2), "events": events}
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"recorded {len(events)} events, usage {usage:.1f}s -> {args.out}")
    return 0


# --------------------------------------------------------------------------- offline replay
def speech_end(timeline: dict) -> float:
    """Ground truth end of the last spoken word, in stream seconds (provider word timings)."""
    ends = [e["last_word_end"] for e in timeline["events"] if e["kind"] == "results" and e.get("last_word_end")]
    return max(ends) if ends else 0.0


def replay(timeline: dict, endpointer, *, step: float = 0.02, expect=None):
    """Feed a recorded timeline through an Endpointer on a virtual 20 ms clock (one poll per media frame, like StreamCall).
    Returns [(emit_time, text)]."""
    from app.stream_stt import Transcript
    events = sorted(timeline["events"], key=lambda e: e["t"])
    if expect:
        endpointer.expect(expect)
    out, i, t = [], 0, 0.0
    end_t = (events[-1]["t"] if events else 0.0) + 5.0
    while t <= end_t:
        while i < len(events) and events[i]["t"] <= t + 1e-9:
            e = events[i]
            i += 1
            if e["kind"] == "utterance_end":
                endpointer.utterance_end(t)
            elif e["kind"] == "speech_started":
                endpointer.speech_started(t)
            elif e["text"].strip():
                endpointer.feed(Transcript(e["text"].strip(), e["is_final"], t, speech_final=e["speech_final"]), t)
        utt = endpointer.poll(t)
        if utt:
            out.append((round(t, 2), utt.text))
        t += step
    return out


def score(timeline: dict, endpointer, **kw) -> dict:
    got = replay(timeline, endpointer, **kw)
    end = speech_end(timeline)
    return {"utterances": len(got), "cut_offs": max(0, len(got) - 1), "latency_s": round(got[-1][0] - end, 2) if got else None,
            "texts": [g[1] for g in got]}


def main(argv=None, env=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    if argv and argv[0] == "replay":
        from app import stream_stt
        for path in argv[1:]:
            tl = json.loads(Path(path).read_text(encoding="utf-8"))
            print(path, json.dumps(score(tl, stream_stt.Endpointer()), indent=1))
        return 0
    if argv and argv[0] == "record":
        args, reason = check_preconditions(argv[1:], env)
        if reason:
            print(reason, file=sys.stderr)
            return 2
        return record(args)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
