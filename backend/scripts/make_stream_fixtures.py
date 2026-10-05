"""Regenerate the synthetic stream-STT test clips (Windows SAPI voice + ffmpeg). Developer tool, no network, no real people.

    python scripts/make_stream_fixtures.py [outdir]

Each clip is 8 kHz mono mu-law WAV (what Twilio Media Streams carries), under 15 s. Gaps are exact silences inserted between
separately synthesised segments, so pause lengths are controlled. Needs powershell (System.Speech) and ffmpeg on PATH.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

CLIPS = {
    # name: list of segments; a number is a pause in seconds, a string is spoken text
    "sentence_pause": ["My air conditioner stopped cooling this morning.", 0.6, "It is making a loud noise too."],
    "phone_digits": ["This is John Smith.", 1.0, "My number is seven one three.", 0.7, "five five five.", 0.5, "zero one four two."],
    "clean_single": ["Hi, my water heater is leaking and I need someone to come out today."],
    "question_stop": ["Do you service Arlington?"],
}


def synth(text: str, path: Path) -> None:
    script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              f"$s.SetOutputToWaveFile('{path}'); $s.Speak('{text}'); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)


def build(name: str, parts: list, outdir: Path) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        inputs: list[Path] = []
        for i, part in enumerate(parts):
            p = tmp / f"{i}.wav"
            if isinstance(part, str):
                synth(part, p)
            else:
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"anullsrc=r=16000:cl=mono", "-t", str(part), str(p)], check=True)
            inputs.append(p)
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        for p in inputs:
            cmd += ["-i", str(p)]
        # SAPI pads every utterance with ~0.5 s of silence; trim both ends of each spoken segment so the inserted gaps are the real gaps
        trim = "silenceremove=start_periods=1:start_threshold=-45dB,areverse,silenceremove=start_periods=1:start_threshold=-45dB,areverse"
        graph = "".join(f"[{i}:a]aresample=16000,aformat=sample_fmts=s16:channel_layouts=mono" + ("," + trim if isinstance(parts[i], str) else "") + f"[a{i}];" for i in range(len(inputs)))
        graph += "".join(f"[a{i}]" for i in range(len(inputs))) + f"concat=n={len(inputs)}:v=0:a=1[out]"
        out = outdir / f"{name}.wav"
        subprocess.run(cmd + ["-filter_complex", graph, "-map", "[out]", "-ar", "8000", "-ac", "1", "-c:a", "pcm_mulaw", str(out)], check=True)
        return out


if __name__ == "__main__":
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "stream_stt"
    outdir.mkdir(parents=True, exist_ok=True)
    for clip, parts in CLIPS.items():
        print(build(clip, parts, outdir))
