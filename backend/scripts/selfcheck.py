"""Checks the whole live service end to end and prints PASS / WARN / FAIL.

    python scripts/selfcheck.py

Run it before you sell, after any deploy, and whenever something feels off.
It uses real (signed) phone-webhook requests against production, but only
creates a few throwaway rows, which it deletes at the end. It never rings a
client's phone or sends a client a notification.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from twilio.request_validator import RequestValidator  # noqa: E402
from twilio.rest import Client  # noqa: E402

from app.config import ClientNotFoundError, load_client_config  # noqa: E402

BASE = os.environ.get("APP_BASE_URL", "https://app.callkettle.com")
KEY = os.environ.get("REPORT_KEY", "")
TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
STAMP = str(int(time.time()))
results: list[tuple[str, str, str]] = []


def record(level: str, name: str, detail: str = "") -> None:
    results.append((level, name, detail))
    icon = {"PASS": "  ok ", "WARN": " WARN", "FAIL": " FAIL", "INFO": " info"}[level]
    print(f"[{icon}] {name}" + (f": {detail}" if detail else ""))


def guard(name: str):
    def wrap(fn):
        try:
            fn()
        except AssertionError as exc:
            record("FAIL", name, str(exc))
        except Exception as exc:  # network, parsing, etc.
            record("FAIL", name, f"{type(exc).__name__}: {str(exc)[:140]}")
        return fn
    return wrap


def signed_post(path: str, form: dict) -> httpx.Response:
    url = BASE + path
    sig = RequestValidator(TOKEN).compute_signature(url, form)
    return httpx.post(url, data=form, headers={"X-Twilio-Signature": sig}, timeout=30)


def derived_key(client_id: str) -> str:
    import hashlib
    import hmac

    return hmac.new(KEY.encode(), client_id.encode(), hashlib.sha256).hexdigest()[:24]


print(f"Call Kettle self-check against {BASE}\n")
if not KEY or not TOKEN:
    sys.exit("REPORT_KEY and TWILIO_AUTH_TOKEN must be set in backend/.env")


# ---------------------------------------------------------------- server
@guard("Server health")
def _():
    r = httpx.get(f"{BASE}/health", timeout=15)
    assert r.status_code == 200 and r.json().get("status") == "ok", f"HTTP {r.status_code}"
    record("PASS", "Server health", "answering")


@guard("Server status")
def _():
    r = httpx.get(f"{BASE}/admin/status", params={"key": KEY, "deep": 1}, timeout=40)
    assert r.status_code == 200, f"HTTP {r.status_code}"
    s = r.json()
    assert s["database"]["ok"], f"database problem: {s['database']['error']}"
    record("PASS", "Database", f"writable, {s['database']['disk']['free_mb']} MB free")
    free = (s["database"]["disk"] or {}).get("free_mb", 9999)
    if free < 200:
        record("WARN", "Disk space", f"only {free} MB free on the data volume")
    if s["database"]["latest_backup"]:
        record("PASS", "Daily backups", f"latest {s['database']['latest_backup']} ({s['database']['backup_count']} kept)")
    else:
        record("WARN", "Daily backups", "none yet (the first runs about a minute after a deploy)")
    f = s["features"]
    assert f["signature_check_on"], "Twilio signature checking is OFF: anyone could fake a phone call"
    record("PASS", "Webhook security", "signature checking is on")
    assert f["twilio_configured"] and f["anthropic_configured"], "missing Twilio or Anthropic credentials"
    if s["model_reachable"] is True:
        record("PASS", "AI model", "reachable and the API key works")
    else:
        record("FAIL", "AI model", str(s["model_reachable"]))
    record("PASS", "Voice", f["voice"])
    record("PASS" if f["email_configured"] else "WARN", "Email alerts + calendar invites",
           "configured" if f["email_configured"] else "NOT set up yet (START-HERE step 2); clients won't get booking emails")
    record("PASS" if f["sms_enabled"] else "INFO", "Text messages", "on" if f["sms_enabled"] else "off until carrier registration is approved (expected)")
    record("INFO", "Google Calendar sync", "on" if f["google_calendar_enabled"] else "off (optional add-on; needs your Google service account)")
    record("PASS", "Live calls in progress", str(s["live_call_sessions"]))
    ob = s.get("webhook_outbox") or {}
    if ob.get("failed"):
        record("WARN", "Customer webhooks", f"{ob['failed']} event(s) permanently failed to deliver (see the status page)")
    elif ob.get("oldest_pending_seconds") and ob["oldest_pending_seconds"] > 3600:
        record("WARN", "Customer webhooks", f"an event has been waiting {ob['oldest_pending_seconds'] // 60} minutes")
    else:
        record("PASS", "Customer webhooks", f"{ob.get('pending', 0)} waiting, {ob.get('failed', 0)} failed")
    hk = s.get("housekeeping") or {}
    if hk.get("at"):
        from datetime import datetime, timezone

        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(hk["at"])).total_seconds() / 3600
        record("PASS" if age_h < 8 else "FAIL", "Maintenance loop", f"last ran {age_h:.1f} h ago (backups, recaps, balance and usage alerts)"
               + ("" if age_h < 8 else "  <- it should run every 6 hours"))
    elif s["uptime_seconds"] > 600:
        record("FAIL", "Maintenance loop", "has not run since the server started; backups, recaps and alerts are not happening")
    else:
        record("INFO", "Maintenance loop", "server just restarted; first run is about a minute after start")


@guard("Public pages")
def _():
    for path, needle in (("/terms", "Monthly fee"), ("/start", "set up your front desk"), ("/book", "Book")):
        r = httpx.get(BASE + path, timeout=15)
        assert r.status_code == 200 and needle.lower() in r.text.lower(), f"{path} HTTP {r.status_code} / missing '{needle}'"
    # the confidential price must not be on any public page (owner strategy: price is stated in conversation after the demo)
    import json as _json

    _price = os.environ.get("CALLKETTLE_PRIVATE_PRICE") or str(int(_json.loads((ROOT.parent / "marketing" / "facts.json").read_text(encoding="utf-8"))["price_monthly"]))
    for path in ("/terms", "/start", "/book"):
        body = httpx.get(BASE + path, timeout=15).text
        assert not re.search(rf"\$\s?{_price}(?!\d)|(?<![\d,.$]){_price}(?!\d)\s*(?:/|per|a)\s*(?:mo|month)", body, re.I), f"{path} shows the confidential price"
    record("PASS", "Pages", "/terms, /start and /book load, and none shows the confidential price")
    day = next((datetime.now() + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(1, 8)
               if (datetime.now() + timedelta(days=d)).weekday() < 5)
    slots = httpx.get(f"{BASE}/book/availability", params={"date": day, "limit": 5}, timeout=15).json().get("slots", [])
    assert slots, f"no booking slots on {day}"
    record("PASS", "Your booking page", f"shows open times ({', '.join(slots[:3])} on {day})")


# ---------------------------------------------------------------- phone numbers
numbers = []


@guard("Twilio account")
def _():
    global numbers
    client = Client(os.environ["TWILIO_ACCOUNT_SID"], TOKEN)
    acct = client.api.accounts(os.environ["TWILIO_ACCOUNT_SID"]).fetch()
    assert acct.status == "active", f"Twilio account is {acct.status}"
    record("PASS", "Twilio account", f"{acct.status}, {acct.type}")
    try:
        bal = client.balance.fetch()
        amount = float(bal.balance)
        level = "FAIL" if amount < 2 else "WARN" if amount < 10 else "PASS"
        record(level, "Twilio balance", f"${amount:.2f}" + ("  <- top up or turn on auto-recharge" if level != "PASS" else ""))
    except Exception:
        record("INFO", "Twilio balance", "couldn't read it; check the console")
    numbers = client.incoming_phone_numbers.list()
    assert numbers, "no phone numbers on the account"
    try:
        brands = client.messaging.v1.brand_registrations.list()
        record("INFO", "Text-message registration", f"{len(brands)} brand(s): " + (", ".join(b.status for b in brands) or "none submitted yet"))
    except Exception:
        record("INFO", "Text-message registration", "couldn't read status")


@guard("Phone numbers")
def _():
    clients_on_numbers = []
    for n in numbers:
        q = parse_qs(urlparse(n.voice_url or "").query)
        cid = (q.get("client_id") or [None])[0]
        label = f"{n.phone_number} ({cid})"
        assert cid, f"{n.phone_number}: voice URL has no client_id"
        try:
            cfg = load_client_config(cid)
        except ClientNotFoundError:
            record("FAIL", f"{label}", f"no clients/{cid}.yaml locally")
            continue
        clients_on_numbers.append(cid)
        probs = []
        if not (n.voice_url or "").startswith(BASE + "/voice/incoming"):
            probs.append("voice URL doesn't point at this server")
        if (n.status_callback or "") != BASE + "/voice/status":
            probs.append("status callback missing")
        want = "/api/fallback?to=" + quote(cfg.escalation_phone, safe="")
        if want not in (n.voice_fallback_url or ""):
            probs.append("fallback (ring owner if server is down) missing or wrong")
        record("FAIL" if probs else "PASS", label, "; ".join(probs) or "webhook, status callback and fallback all set")

        # is that client actually deployed, with its own dashboard?
        r = httpx.get(f"{BASE}/report/{cid}", params={"key": derived_key(cid)}, timeout=15)
        if r.status_code != 200:
            record("FAIL", f"{cid} deployed", f"dashboard HTTP {r.status_code}: run flyctl deploy")

        # a real signed incoming call, exactly as Twilio would send it
        sid = f"CA_SELFCHECK_{STAMP}_{cid}"
        r = signed_post(f"/voice/incoming?client_id={cid}", {"CallSid": sid, "From": "+15557770000", "To": n.phone_number})
        ok = r.status_code == 200 and "<Gather" in r.text and "recorded and monitored" in r.text and 'voice="Polly' in r.text
        record("PASS" if ok else "FAIL", f"{cid} answers a call", "greeting, AI disclosure and neural voice present" if ok else f"HTTP {r.status_code}")
        signed_post("/voice/status", {"CallSid": sid, "CallStatus": "completed"})

        # the LIVE config (not the local file): can the owner actually be told about a booking or an urgent call?
        live = httpx.get(f"{BASE}/admin/client/{cid}/config", params={"key": KEY}, timeout=15)
        if live.status_code == 200:
            text = live.text
            has_email = any(line.startswith("owner_email:") for line in text.splitlines())
            has_ntfy = any(line.startswith("ntfy_topic:") for line in text.splitlines())
            if not has_email and not has_ntfy:
                record("FAIL", f"{cid} owner alerts", "the live config has no owner_email and no ntfy_topic: bookings and callback requests alert NOBODY")
            elif not has_email:
                record("WARN", f"{cid} owner alerts", "no owner_email on the live config (no booking invites or weekly recap)")
            else:
                record("PASS", f"{cid} owner alerts", "owner_email is set on the live config")
    assert clients_on_numbers, "no number is pointed at a client"


@guard("Fallback function")
def _():
    fb = next((n.voice_fallback_url for n in numbers if n.voice_fallback_url), None)
    assert fb, "no number has a fallback URL"
    parsed = urlparse(fb)
    base = f"{parsed.scheme}://{parsed.netloc}"
    form = {"CallSid": "CA_SELFCHECK", "From": "+15557770000"}
    sig = RequestValidator(TOKEN).compute_signature(fb, form)
    good = httpx.post(fb, data=form, headers={"X-Twilio-Signature": sig}, timeout=20)
    assert good.status_code == 200 and "<Dial" in good.text, f"signed request -> HTTP {good.status_code}"
    bad = httpx.post(fb, data=form, timeout=20)
    assert bad.status_code == 403, f"unsigned request was accepted (HTTP {bad.status_code})"
    record("PASS", "Ring-the-owner fallback", f"{parsed.netloc} dials correctly and rejects unsigned requests")


@guard("Transfer flow")
def _():
    """A real (non-demo) client puts the caller through; the public demos only SIMULATE it. Uses a throwaway copy, never a customer's line."""
    cid = "zz_selfcheck_xfer"
    text = (ROOT / "clients" / "demo_riverside.yaml").read_text(encoding="utf-8")
    text = text.replace("client_id: demo_riverside", f"client_id: {cid}")
    text = chr(10).join(line for line in text.splitlines() if not line.startswith(("demo_mode:", "ntfy_topic:", "owner_email:")))
    up = httpx.post(f"{BASE}/admin/client/upload", params={"key": KEY}, content=text.encode(), timeout=30)
    assert up.status_code == 200, f"upload rejected: {up.status_code} {up.text[:200]}"
    sids = []
    try:
        sid = f"CA_SELFCHECK_{STAMP}_xfer"
        sids.append(sid)
        signed_post(f"/voice/incoming?client_id={cid}", {"CallSid": sid, "From": "+15557770001"})
        r = signed_post(f"/voice/gather?client_id={cid}&retry=0", {"CallSid": sid, "From": "+15557770001", "SpeechResult": "can I talk to a real person"})
        assert "<Dial" in r.text and "transfer-result" in r.text, "asking for a person did not transfer"
        r = signed_post(f"/voice/transfer-result?client_id={cid}", {"CallSid": sid, "From": "+15557770001", "DialCallStatus": "no-answer"})
        assert "<Gather" in r.text and "Hangup" not in r.text, "an unanswered transfer dead-ends"
        record("PASS", "Live transfer", "puts the caller through; if nobody answers, takes a message instead of hanging up")

        sid = f"CA_SELFCHECK_{STAMP}_911"
        sids.append(sid)
        signed_post(f"/voice/incoming?client_id={cid}", {"CallSid": sid, "From": "+15557770002"})
        r = signed_post(f"/voice/gather?client_id={cid}&retry=0", {"CallSid": sid, "From": "+15557770002", "SpeechResult": "I smell gas in the house"})
        assert "911" in r.text, "emergency did not produce 911 advice"
        record("PASS", "Emergency handling", "tells the caller to call 911 first")
    finally:
        for s_ in sids:
            signed_post("/voice/status", {"CallSid": s_, "CallStatus": "completed"})
        httpx.post(f"{BASE}/admin/client/{cid}/config-remove", params={"key": KEY, "confirm": cid}, timeout=30)

    # the public demo companies must never ring a real phone
    demo = "demo_nova_hvac"
    sid = f"CA_SELFCHECK_{STAMP}_demoxfer"
    signed_post(f"/voice/incoming?client_id={demo}", {"CallSid": sid, "From": "+15557770004"})
    r = signed_post(f"/voice/gather?client_id={demo}&retry=0", {"CallSid": sid, "From": "+15557770004", "SpeechResult": "can I talk to a real person"})
    assert "<Dial" not in r.text and "would ring" in r.text, "a demo company tried to ring a real phone"
    signed_post("/voice/status", {"CallSid": sid, "CallStatus": "completed"})
    record("PASS", "Demo hand-off is simulated", "the demo says what would happen and never dials")

    # the menu on the public demo line
    sid = f"CA_SELFCHECK_{STAMP}_menu"
    r = signed_post("/voice/incoming?client_id=callkettle_demo", {"CallSid": sid, "From": "+15557770005"})
    assert "demo-select" in r.text and "press 1" in r.text, "the demo line did not play its menu"
    r = signed_post("/voice/demo-select?client_id=callkettle_demo", {"CallSid": sid, "Digits": "2"})
    assert "client_id=demo_nova_garage" in r.text, "pressing 2 did not reach the garage door demo"
    signed_post("/voice/status", {"CallSid": sid, "CallStatus": "completed"})
    record("PASS", "Demo menu", "plays the menu and routes a key press to the right pretend company")


@guard("Private demos")
def _():
    """A prospect's private demo: created through the admin API, reached with a code on the public menu, refused with a wrong one, then revoked."""
    import yaml

    cid = "prep_selfcheck"
    text = (ROOT / "clients" / "demo_nova_hvac.yaml").read_text(encoding="utf-8").replace("client_id: demo_nova_hvac", f"client_id: {cid}")
    sid = f"CA_SELFCHECK_{STAMP}_pd"
    caller = f"+1555777{int(STAMP) % 10000:04d}"          # a fresh number every run, so the wrong-code lockout (5 an hour per caller) never trips a repeat run
    made = None
    try:
        r = httpx.post(f"{BASE}/admin/private-demo", params={"key": KEY, "label": "selfcheck", "hours": 1, "max_calls": 2}, content=text.encode(), timeout=30)
        assert r.status_code == 200, f"could not create a private demo: {r.status_code} {r.text[:150]}"
        made = r.json()
        assert len(made["code"]) == 6 and made["client_id"] == cid
        menu = signed_post("/voice/incoming?client_id=callkettle_demo", {"CallSid": sid, "From": caller})
        assert "press 9" in menu.text, "the menu does not offer the private code"
        ask = signed_post("/voice/demo-select?client_id=callkettle_demo", {"CallSid": sid, "Digits": "9"})
        assert 'input="dtmf"' in ask.text and 'numDigits="6"' in ask.text, "pressing 9 did not ask for digits only"
        bad = signed_post("/voice/demo-code?client_id=callkettle_demo", {"CallSid": sid, "From": caller, "Digits": "000000" if made["code"] != "000000" else "000001"})
        assert "<Hangup" in bad.text and f"client_id={cid}" not in bad.text, "a wrong code was accepted"
        good = signed_post("/voice/demo-code?client_id=callkettle_demo", {"CallSid": sid, "From": caller, "Digits": made["code"]})
        assert f"client_id={cid}" in good.text, "the right code did not reach the private demo"
        talk = signed_post(f"/voice/incoming?client_id={cid}", {"CallSid": sid, "From": caller})
        assert "<Gather" in talk.text and "AI" in talk.text, "the private demo did not answer"
        unsigned = httpx.post(f"{BASE}/voice/demo-code?client_id=callkettle_demo", data={"Digits": made["code"]}, timeout=20)
        assert unsigned.status_code == 403, "an unsigned request was accepted"
        denied = httpx.post(f"{BASE}/admin/private-demo", params={"key": "wrong"}, content=text.encode(), timeout=20)
        assert denied.status_code == 403, "the admin endpoint accepted a wrong key"
        listing = httpx.get(f"{BASE}/admin/private-demos", params={"key": KEY}, timeout=20).text
        assert made["code"] not in listing, "the plain code is visible in the admin listing"
        record("PASS", "Private demos", "created, reached with its code on the public menu, wrong code refused, unsigned and wrong-key requests refused, code not stored in the clear")
    finally:
        signed_post("/voice/status", {"CallSid": sid, "CallStatus": "completed"})
        httpx.post(f"{BASE}/admin/private-demo/revoke", params={"key": KEY, "client_id": cid}, timeout=20)
        httpx.post(f"{BASE}/admin/client/{cid}/config-remove", params={"key": KEY, "confirm": cid}, timeout=20)


@guard("New-client pipeline")
def _():
    """The path every new client takes: upload a config, answer a call as them, remove them."""
    cid = "zz_selfcheck"
    text = (ROOT / "clients" / "callkettle_demo.yaml").read_text(encoding="utf-8")
    text = text.replace("client_id: callkettle_demo", f"client_id: {cid}").replace("Riverside Home Services", "Selfcheck Plumbing")
    up = httpx.post(f"{BASE}/admin/client/upload", params={"key": KEY}, content=text.encode(), timeout=30)
    assert up.status_code == 200, f"upload rejected: {up.status_code} {up.text[:200]}"
    try:
        r = signed_post(f"/voice/incoming?client_id={cid}", {"CallSid": f"CA_SELFCHECK_{STAMP}_new", "From": "+15557770003"})
        assert "<Gather" in r.text and "AI" in r.text, "a freshly uploaded client did not answer correctly"
        bad = httpx.post(f"{BASE}/admin/client/upload", params={"key": KEY}, content=b"client_id: zz_bad\nbusiness_name: x\n", timeout=30)
        assert bad.status_code == 422, "a broken config was accepted"
        signed_post("/voice/status", {"CallSid": f"CA_SELFCHECK_{STAMP}_new", "CallStatus": "completed"})
    finally:
        rm = httpx.post(f"{BASE}/admin/client/{cid}/config-remove", params={"key": KEY, "confirm": cid}, timeout=30)
    assert rm.status_code == 200 and rm.json().get("removed"), "could not remove the test client"
    gone = signed_post(f"/voice/incoming?client_id={cid}", {"CallSid": f"CA_SELFCHECK_{STAMP}_gone", "From": "+15557770004"})
    assert "Selfcheck Plumbing" not in gone.text, "a removed client is still being served"
    record("PASS", "New-client pipeline", "upload a config, it answers calls at once, bad configs are refused, removal works (no restart)")


@guard("Dashboard security")
def _():
    a = httpx.get(f"{BASE}/report/sample_homecare", params={"key": derived_key("demo_dental")}, timeout=15)
    b = httpx.get(f"{BASE}/report/sample_homecare", timeout=15)
    c = httpx.get(f"{BASE}/admin/export", timeout=15)
    assert a.status_code == 403 and b.status_code == 403 and c.status_code == 403, "a private page opened without the right key"
    record("PASS", "Private pages", "one client's key can't open another's dashboard; admin pages need your key")


@guard("Sales documents")
def _():
    audit = ROOT.parent / "marketing" / "audit.py"
    if not audit.exists():
        record("INFO", "Sales documents", "marketing folder not found next to backend")
        return
    r = subprocess.run([sys.executable, str(audit)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, "the flyer/offer/terms/site disagree:\n" + r.stdout[-600:]
    record("PASS", "Sales documents", "flyer, offer, terms, playbook, plan and both websites agree with each other")


# ---------------------------------------------------------------- clean up
try:
    r = httpx.post(f"{BASE}/admin/purge-test-data", params={"key": KEY}, timeout=20)
    record("INFO", "Cleanup", f"removed {r.json().get('removed', '?')} throwaway test rows")
except Exception:
    record("WARN", "Cleanup", "couldn't delete the test rows; run it again")

fails = [r for r in results if r[0] == "FAIL"]
warns = [r for r in results if r[0] == "WARN"]
print("\n" + "=" * 60)
print(f"{len([r for r in results if r[0]=='PASS'])} passed, {len(warns)} warnings, {len(fails)} failed")
if fails:
    print("\nFIX THESE BEFORE SELLING:")
    for _, name, detail in fails:
        print(f"  - {name}: {detail}")
if warns:
    print("\nWORTH DOING SOON:")
    for _, name, detail in warns:
        print(f"  - {name}: {detail}")
if not fails:
    print("\nReady for customers.")
sys.exit(1 if fails else 0)
