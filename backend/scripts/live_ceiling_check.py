"""Production verification of the spending-ceiling degraded mode, through the real signed phone-webhook interface.

Creates a disposable client (zz_ceiling_check) allowed ONE call per month, then:
  call 1  -> the AI answers normally
  call 2  -> over the limit: no AI, a message is taken and lands in the escalations table
  call 3  -> over the limit and the caller describes an emergency: 911 message + transfer to the owner
Deletes everything afterwards. Sends the operator one "Spending ceiling" alert (that is the alert being verified).

    python scripts/live_ceiling_check.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from xml.etree import ElementTree

import httpx
from dotenv import load_dotenv
from twilio.request_validator import RequestValidator

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
BASE = os.environ.get("APP_BASE_URL", "https://app.callkettle.com")
KEY, TOKEN = os.environ["REPORT_KEY"], os.environ["TWILIO_AUTH_TOKEN"]
CID = "zz_ceiling_check"
CALLER = "+15555550100"


def config_yaml() -> str:
    text = (ROOT / "clients" / "demo_riverside.yaml").read_text(encoding="utf-8")
    text = text.replace("client_id: demo_riverside", f"client_id: {CID}").replace("Riverside Home Services", "Ceiling Check Plumbing")
    text = re.sub(r"^ntfy_topic:.*\n", "", text, flags=re.M)
    text = re.sub(r"^owner_email:.*\n", "", text, flags=re.M)
    text = re.sub(r"(?s)extra_instructions: \|.*", "", text)
    return text.rstrip() + "\nmonthly_call_ceiling: 1\nceiling_mode: message\n"


def post(path: str, form: dict) -> ElementTree.Element:
    url = BASE + path
    sig = RequestValidator(TOKEN).compute_signature(url, form)
    r = httpx.post(url, data=form, headers={"X-Twilio-Signature": sig}, timeout=30)
    r.raise_for_status()
    return ElementTree.fromstring(r.text) if r.text.strip() else ElementTree.Element("Empty")


def has(root, tag: str) -> bool:
    return root.find(f".//{tag}") is not None


def says(root) -> str:
    return " ".join((e.text or "") for e in root.iter("Say"))


def main() -> int:
    up = httpx.post(f"{BASE}/admin/client/upload", params={"key": KEY}, content=config_yaml().encode(), timeout=30)
    if up.status_code != 200:
        print("could not create the test client:", up.status_code, up.text[:300])
        return 2
    results: list[tuple[bool, str]] = []
    try:
        first = post(f"/voice/incoming?client_id={CID}", {"CallSid": "CAzzceil1", "From": CALLER})
        results.append((has(first, "Gather") and "ceiling-message" not in ElementTree.tostring(first, encoding="unicode"), "call 1: the AI answers normally"))
        post("/voice/status", {"CallSid": "CAzzceil1", "CallStatus": "completed"})

        second = post(f"/voice/incoming?client_id={CID}", {"CallSid": "CAzzceil2", "From": CALLER})
        results.append((has(second, "Gather") and "ceiling-message" in ElementTree.tostring(second, encoding="unicode") and not has(second, "Dial"),
                        "call 2: over the limit -> asks for a message (no AI, no transfer)"))
        done = post(f"/voice/ceiling-message?client_id={CID}&retry=0",
                    {"CallSid": "CAzzceil2", "From": CALLER, "SpeechResult": "This is Alex, call me back about a leaking sink"})
        results.append((has(done, "Hangup") and "call you back" in says(done), "call 2: message accepted, caller told someone will call back"))

        third = post(f"/voice/incoming?client_id={CID}", {"CallSid": "CAzzceil3", "From": CALLER})
        emergency = post(f"/voice/ceiling-message?client_id={CID}&retry=0",
                         {"CallSid": "CAzzceil3", "From": CALLER, "SpeechResult": "I smell gas in my house"})
        results.append((has(emergency, "Dial") and "911" in says(emergency), "call 3: emergency while over the limit -> 911 message + transfer"))

        data = httpx.get(f"{BASE}/admin/export", params={"key": KEY}, timeout=60).json()
        mine = [e for e in data["escalations"] if e["client_id"] == CID]
        reasons = sorted(e["reason"] for e in mine)
        results.append(("over_limit_message" in reasons and "possible_emergency" in reasons, f"database holds the message and the emergency record ({reasons})"))
        calls = [c for c in data["calls"] if c["client_id"] == CID]
        results.append((sum(c["outcome"] == "over_ceiling" for c in calls) == 2, "over-limit calls are logged as over_ceiling"))
    finally:
        httpx.post(f"{BASE}/admin/client/{CID}/delete", params={"key": KEY, "confirm": CID}, timeout=30)
        httpx.post(f"{BASE}/admin/client/{CID}/config-remove", params={"key": KEY, "confirm": CID}, timeout=30)
        left = [c for c in httpx.get(f"{BASE}/admin/export", params={"key": KEY}, timeout=60).json()["calls"] if c["client_id"] == CID]
        results.append((not left, f"cleanup: {len(left)} test rows left"))
    for ok, what in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {what}")
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
