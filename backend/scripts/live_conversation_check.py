"""Production verification: real conversations against the LIVE server through its real phone-webhook
interface (signed Twilio-style requests), with an AI playing the caller. Uses a disposable client
(zz_live_check) with no alert channels, verifies what landed in the database, then deletes everything.

    python scripts/live_conversation_check.py                 # book -> move, book -> cancel
    python scripts/live_conversation_check.py --scenario book

It does not exercise Twilio speech recognition or voice (those need a real phone call), only the server,
the AI, the booking rules and the database, exactly as a call would.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402
import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from twilio.request_validator import RequestValidator  # noqa: E402

load_dotenv(ROOT / ".env")
BASE = os.environ.get("APP_BASE_URL", "https://app.callkettle.com")
KEY, TOKEN = os.environ["REPORT_KEY"], os.environ["TWILIO_AUTH_TOKEN"]
CID = "zz_live_check"
CALLER_ID = "+15555550100"
MODEL = "claude-haiku-4-5-20251001"

SCENARIOS = {
    "book": "Cooperative. Books the first time offered, gives the name 'Alex Rivera', confirms the number you're calling from. After it is confirmed, says thanks and goodbye.",
    "move": "Books the first time offered with the name 'Alex Rivera' and confirms the number you're calling from. After it is confirmed, says 'actually, can we move it to a different time?' and accepts the first different time offered. Then says thanks and goodbye.",
    "cancel": "Books the first time offered with the name 'Alex Rivera' and confirms the number you're calling from. After it is confirmed, says 'sorry, please cancel that appointment', confirms yes if asked, then says thanks and goodbye.",
}


def config_yaml() -> str:
    text = (ROOT / "clients" / "demo_riverside.yaml").read_text(encoding="utf-8")
    text = text.replace("client_id: demo_riverside", f"client_id: {CID}").replace("Riverside Home Services", "Live Check Plumbing")
    text = re.sub(r"^ntfy_topic:.*\n", "", text, flags=re.M)
    text = re.sub(r"^owner_email:.*\n", "", text, flags=re.M)
    text = re.sub(r"(?s)extra_instructions: \|.*", "", text)
    return text.rstrip() + "\n"


def signed_post(path: str, form: dict) -> str:
    url = BASE + path
    sig = RequestValidator(TOKEN).compute_signature(url, form)
    r = httpx.post(url, data=form, headers={"X-Twilio-Signature": sig}, timeout=30)
    r.raise_for_status()
    return r.text


def spoken(twiml: str) -> tuple[str, str]:
    """(text the caller hears, what the call does next: gather | hangup | dial)"""
    root = ElementTree.fromstring(twiml)
    text = " ".join((e.text or "") for e in root.iter("Say")).strip()
    kind = "dial" if root.find(".//Dial") is not None else "hangup" if root.find(".//Hangup") is not None else "gather"
    return text, kind


def caller_says(client: anthropic.Anthropic, behaviour: str, transcript: list[tuple[str, str]]) -> str:
    system = ("You are role-playing a customer phoning a local business about an air conditioner that stopped cooling. You are a real person, "
              "never mention being an AI. Reply with ONLY what you would say out loud: one or two short natural sentences. "
              f"How you behave: {behaviour}")
    msgs = [{"role": "user", "content": f"(The call connected.) Receptionist: {transcript[0][1]}"}]
    for who, text in transcript[1:]:
        msgs.append({"role": "assistant" if who == "caller" else "user", "content": text})
    if msgs[-1]["role"] == "assistant":
        msgs.append({"role": "user", "content": "(silence)"})
    out = client.messages.create(model=MODEL, max_tokens=100, system=system, messages=msgs)
    return "".join(b.text for b in out.content if b.type == "text").strip().strip('"')


def hold_call(scenario: str) -> tuple[str, list[tuple[str, str]]]:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=30)
    sid = f"CA_LIVECHECK_{uuid.uuid4().hex[:10]}"
    path = f"/voice/incoming?client_id={CID}"
    twiml = signed_post(path, {"CallSid": sid, "From": CALLER_ID})
    text, kind = spoken(twiml)
    transcript = [("ai", text)]
    for _ in range(14):
        if kind != "gather":
            break
        said = caller_says(client, SCENARIOS[scenario], transcript)
        transcript.append(("caller", said))
        twiml = signed_post(f"/voice/gather?client_id={CID}&retry=0", {"CallSid": sid, "From": CALLER_ID, "SpeechResult": said})
        text, kind = spoken(twiml)
        transcript.append(("ai", text))
    signed_post("/voice/status", {"CallSid": sid, "CallStatus": "completed"})
    return sid, transcript


def export() -> dict:
    return httpx.get(f"{BASE}/admin/export", params={"key": KEY}, timeout=60).json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=list(SCENARIOS) + ["all"], default="all")
    args = ap.parse_args()
    chosen = ["move", "cancel"] if args.scenario == "all" else [args.scenario]
    up = httpx.post(f"{BASE}/admin/client/upload", params={"key": KEY}, content=config_yaml().encode(), timeout=30)
    if up.status_code != 200:
        print("could not create the test client:", up.status_code, up.text[:300])
        return 2
    failures, report = [], []
    try:
        for name in chosen:
            sid, transcript = hold_call(name)
            data = export()
            live = [b for b in data["bookings"] if b["client_id"] == CID and b["call_sid"] == sid]
            gone = [b for b in data["cancelled_bookings"] if b["client_id"] == CID and (b["call_sid"] == sid or b.get("cancelled_by_call_sid") == sid)]
            ok = {"book": len(live) == 1 and not gone, "move": len(live) == 1 and not gone, "cancel": not live and len(gone) == 1}[name]
            if name == "move" and ok:
                ok = live[0]["slot_start"] != ""          # exactly one live booking after a move
            report.append(f"[{'PASS' if ok else 'FAIL'}] {name}: live bookings={len(live)} cancelled={len(gone)} turns={len(transcript)}")
            if not ok:
                failures.append(name)
                for who, text in transcript:
                    report.append(f"    {who:6s}: {text[:140]}")
    finally:
        httpx.post(f"{BASE}/admin/client/{CID}/delete", params={"key": KEY, "confirm": CID}, timeout=30)
        httpx.post(f"{BASE}/admin/client/{CID}/config-remove", params={"key": KEY, "confirm": CID}, timeout=30)
        left = [b for b in export()["bookings"] if b["client_id"] == CID]
        report.append(f"cleanup: {len(left)} test bookings left" + ("" if not left else "  <- CHECK"))
    print("\n".join(report))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
