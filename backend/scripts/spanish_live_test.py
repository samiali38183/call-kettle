"""Real phone-network test of the Spanish beta.

    python scripts/spanish_live_test.py

Places a REAL call from one of our Twilio numbers to the demo line. The caller presses 2 (a real DTMF tone)
and then speaks Spanish using a synthesized voice. Twilio's own Spanish speech recognizer transcribes it and
the live AI answers. Afterwards it reads the call's transcript back from the server and judges it.

Costs a few cents of Twilio usage. The caller is a synthetic voice, so this proves the plumbing (key press,
es-US recognition, Spanish replies) but not how the system copes with a real person's accent or noise.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from twilio.rest import Client  # noqa: E402

load_dotenv(ROOT / ".env")
BASE = os.environ.get("APP_BASE_URL", "https://app.callkettle.com")
KEY = os.environ["REPORT_KEY"]
DEMO = "+15555550100"
FROM = "+15555550100"   # our own line, so the call stays inside our Twilio account
VOICE = 'voice="Polly.Lupe-Neural" language="es-US"'

TWIML = f"""<Response>
<Pause length="3"/>
<Play digits="2"/>
<Pause length="9"/>
<Say {VOICE}>Hola, buenas tardes. Mi aire acondicionado no está enfriando y necesito que alguien venga a revisarlo.</Say>
<Pause length="14"/>
<Say {VOICE}>Mañana por la mañana está bien.</Say>
<Pause length="14"/>
<Say {VOICE}>Muchas gracias.</Say>
<Pause length="6"/>
</Response>"""


def main() -> int:
    client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    call = client.calls.create(to=DEMO, from_=FROM, twiml=TWIML)
    print("placed call", call.sid)
    # the demo line's own call id differs from ours: find it by caller number and time
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(6)
        status = client.calls(call.sid).fetch().status
        if status in ("completed", "failed", "busy", "no-answer", "canceled"):
            break
    print("call status:", status)
    time.sleep(8)  # let the server finish logging the last turn
    data = httpx.get(f"{BASE}/admin/export", params={"key": KEY}, timeout=60).json()
    mine = [c for c in data["calls"] if c["client_id"] == "callkettle_demo" and c["from_number"] == FROM]
    if not mine:
        print("FAIL: the server logged no call from our number")
        return 1
    latest = sorted(mine, key=lambda c: c["started_at"])[-1]
    import json

    turns = json.loads(latest["transcript_json"])
    for t in turns:
        print(f"  {t['role']:6s}: {t['text'][:160]}")
    ai = " ".join(t["text"] for t in turns if t["role"] == "ai")
    caller = " ".join(t["text"] for t in turns if t["role"] == "caller")
    checks = {
        "pressing 2 switched the call to Spanish": "Con gusto le ayudo en espa" in ai,
        "speech recognition heard Spanish (es-US)": bool(re.search(r"aire|acondicionado|enfri|revis|hola|buenas", caller, re.I)),
        "the AI answered in Spanish after that": bool(re.search(r"[ñ¿áéíó]|usted|mañana|cita|nombre", ai.split("Con gusto", 1)[-1], re.I)),
    }
    for name, ok in checks.items():
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
