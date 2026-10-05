"""Offline-built narrated examples for the website call player. NOT telephone recordings.

Each call is narrated turn by turn with Microsoft Edge neural voices (edge-tts, free, no account, no API key)
and joined with ffmpeg. The receptionist always uses the same voice; callers vary. Cue times let the page
reveal each message exactly when it is spoken. Run:  python make_call_audio.py [slug ...]

edge-tts is not part of the app. Install it into scratch only:
    pip install --target C:/Users/samis/AppData/Local/hermes/cache/scratch/edgetts edge-tts
"""
from pathlib import Path
import asyncio
import importlib.util
import json
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "marketing/site-src/assets"
SCRATCH = Path("C:/Users/samis/AppData/Local/hermes/cache/scratch")
sys.path.insert(0, str(SCRATCH / "edgetts"))

AI_VOICE = "en-US-JennyNeural"
CALLER_VOICES = {
    "hvac": "en-US-GuyNeural", "plumbing": "en-US-RogerNeural", "electrical": "en-US-AndrewNeural",
    "garage-door": "en-US-AriaNeural", "roofing": "en-US-BrianNeural", "auto": "en-US-ChristopherNeural",
    "landscaping": "en-US-MichelleNeural", "cleaning": "en-US-AvaNeural", "contractor": "en-US-EricNeural",
}
GAP_SECONDS = 0.45


def duration(path):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)], text=True))


async def speak(text, voice, path):
    import edge_tts
    for attempt in range(3):
        try:
            await edge_tts.Communicate(text, voice, rate="-4%").save(str(path))
            return
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(2)


def main(only=None):
    spec = importlib.util.spec_from_file_location("site_audio_builder", ROOT / "marketing/site-src/build_site.py")
    assert spec is not None and spec.loader is not None
    site = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(site)
    records = json.loads((OUT / "call-audio.json").read_text(encoding="utf-8")) if (OUT / "call-audio.json").exists() else {}
    with tempfile.TemporaryDirectory(prefix="call-narration-", dir=SCRATCH) as temp:
        temp = Path(temp)
        gap = temp / "gap.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", str(GAP_SECONDS), str(gap)], check=True)
        for call in site.load_calls():
            slug = call["slug"]
            if only and slug not in only:
                continue
            caller_voice = CALLER_VOICES[slug]
            parts, cues, elapsed = [], [], 0.0
            for i, turn in enumerate(call["turns"]):
                src = temp / f"{slug}-{i}.mp3"
                asyncio.run(speak(turn["text"].replace("\n", " "), AI_VOICE if turn["who"] == "ai" else caller_voice, src))
                wav = temp / f"{slug}-{i}.wav"
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ar", "44100", "-ac", "1", str(wav)], check=True)
                cues.append(round(elapsed, 3))
                elapsed += duration(wav) + GAP_SECONDS
                parts += [wav, gap]
            listing = temp / f"{slug}.txt"
            listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
            name = f"call-example-{slug}.mp3"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                            "-c:a", "libmp3lame", "-b:a", "64k", "-ar", "44100", "-ac", "1", str(OUT / name)], check=True)
            kind = "a scripted example" if call.get("source") == "scripted" else "unedited saved test-call text"
            total = duration(OUT / name)
            records[slug] = {"src": f"/assets/{name}", "duration": round(total, 3), "cues": cues,
                             "provenance": f"Synthetic neural-voice narration of {kind}; not a phone recording or the current phone voice.",
                             "voices": {"ai": AI_VOICE, "caller": caller_voice}}
            assert len(cues) == len(call["turns"]) and cues[-1] < total
            print(slug, len(cues), "turns", round(total, 1), "seconds")
    (OUT / "call-audio.json").write_text(json.dumps(records, indent=2), encoding="utf-8", newline="\n")
    print("PASS audio + turn cues generated; no paid provider used")


if __name__ == "__main__":
    main(sys.argv[1:] or None)
