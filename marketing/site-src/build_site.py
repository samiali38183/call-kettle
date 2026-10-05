"""Builds the public website (multi-page static site) into marketing/site/.

    ../../backend/.venv/Scripts/python.exe build_site.py

Brand, prices, phone numbers come from marketing/facts.json (via build.load_facts), so the
site can never disagree with the flyers. Demo conversations are REAL test calls between an
AI "customer" and the live receptionist (backend/qa/harness.py), labelled as such.
Deploy: cd ../site && vercel deploy --prod --yes
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKETING = HERE.parent
OUT = MARKETING / "site"
sys.path.insert(0, str(MARKETING))
import build  # noqa: E402

F = build.load_facts()
F["NEW_LINE"] = ("<b>Proof comes from the product.</b> We do not publish client names, ratings or quotes without written permission. "
                 f"You can hear it work yourself on the live demo line, {F['DEMO_PHONE']}, review the setup checks, and approve it before it handles your customers.")
F.setdefault("BRAND", F.get("brand", "Call Kettle"))
F.setdefault("BRAND_SHORT", F.get("brand_short", "Call Kettle"))
F["BRAND_MAIL"] = F["BRAND"].replace(" ", "%20")
F["CLIENT_PORTAL_URL"] = F.get("app_url", "https://app.callkettle.com").rstrip("/") + "/portal/login"
F["FRONT_DESK_SETUP_URL"] = F.get("app_url", "https://app.callkettle.com").rstrip("/") + "/start"
SITE = F["site_url"].rstrip("/")
DOMAIN = re.sub(r"^https?://", "", SITE)
# CSS/JS are referenced as /assets/x?v=<content hash> so a redeploy can never leave a browser on old CSS with new HTML.
ASSET_V = hashlib.sha256(b"".join((HERE / "assets" / n).read_bytes() for n in ("style.css", "call-demo.js"))).hexdigest()[:10]
TERMS_LINE = "No setup fee. Month-to-month. Cancel at any time, no cancellation fee."  # mirrors the hosted terms (sections 3 and 4)

ICONS = {
    "phone": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/></svg>',
    "calendar": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>',
    "mail": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 6L2 7"/></svg>',
    "shield": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>',
    "clock": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',
    "route": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"/><circle cx="18" cy="5" r="3"/></svg>',
    "chart": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 5-6"/></svg>',
    "user": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>',
    "alert": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m10.3 3.9-8.4 14.5A2 2 0 0 0 3.6 21.4h16.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>',
    "lock": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>',
}


def ico(name: str) -> str:
    return f'<div class="ico">{ICONS[name]}</div>'


def sub(text: str, extra: dict | None = None) -> str:
    """Fill @@KEY@@ placeholders from facts (and any page-specific extras)."""
    vals = {**F, **(extra or {})}
    return re.sub(r"@@([A-Z0-9_]+)@@", lambda m: str(vals[m.group(1)]) if m.group(1) in vals else m.group(0), text)


# ------------------------------------------------------------------ real test-call data
def load_calls() -> list[dict]:
    path = MARKETING / "samples" / "site_calls.json"
    if not path.exists():
        raise SystemExit("marketing/samples/site_calls.json is missing. Run backend/qa/harness.py then samples/make_site_calls.py.")
    calls = json.loads(path.read_text(encoding="utf-8"))
    # Transcripts come from a past harness run. Any that speak a calendar date ("Thursday, October first") are stale on the
    # public site, and we never edit a transcript, so those calls are left out rather than rewritten.
    dated = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\b")
    # Also left out: any call where the AI says "free" (we offer nothing free; "free estimate" is an unapproved claim).
    free = re.compile(r"\bfree\b", re.I)
    audio_path = HERE / "assets" / "call-audio.json"
    audio = json.loads(audio_path.read_text(encoding="utf-8")) if audio_path.exists() else {}
    saved = [{**c, "source": "saved-test", "audio": audio.get(c["slug"])} for c in calls if not any(dated.search(t["text"]) or free.search(t["text"]) for t in c["turns"])]
    scripted_path = MARKETING / "samples" / "scripted_examples.json"
    scripted = json.loads(scripted_path.read_text(encoding="utf-8")) if scripted_path.exists() else []
    have = {c["slug"] for c in saved}
    extra = []
    for c in scripted:
        if c["slug"] in have:
            raise SystemExit(f"scripted example duplicates a saved call: {c['slug']}")
        if any(dated.search(t["text"]) or free.search(t["text"]) for t in c["turns"]):
            raise SystemExit(f"scripted example {c['slug']} contains a month name or the word free")
        extra.append({**c, "source": "scripted", "audio": audio.get(c["slug"])})
    return saved + extra


CALLS = load_calls()


def calls_script(first: str | None = None) -> str:
    order = sorted(CALLS, key=lambda c: (c["slug"] != first, c.get("order", 99)))
    return '<script type="application/json" id="calls-data">' + json.dumps(order, ensure_ascii=False).replace("</", "<\\/") + "</script>"


def call_ui(first: str | None = None) -> str:
    order = sorted(CALLS, key=lambda c: (c["slug"] != first, c.get("order", 99)))
    tabs = "".join(
        f'<button class="cu-tab" role="tab" aria-selected="{"true" if i == 0 else "false"}">{html.escape(c["label"])}</button>'
        for i, c in enumerate(order)
    )
    noscript = "".join(
        f'<p><b>{"AI receptionist" if t["who"] == "ai" else "Customer"}:</b> {html.escape(t["text"])}</p>' for t in order[0]["turns"]
    )
    return f"""
<div class="callui" id="callui" aria-label="Replay of a test call between a customer and the AI receptionist">
  <div class="cu-tabs" role="tablist">{tabs}</div>
  <div class="cu-head">
    <div class="cu-avatar">{ICONS["phone"].replace('<svg', '<svg width="22" height="22"')}</div>
    <div><b data-cu-title>{html.escape(order[0]["business"])}</b><small data-cu-sub>Test call</small></div>
    <span class="wave" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></span>
    <span class="cu-status">Incoming call</span>
  </div>
  <div class="cu-audio"><audio data-cu-audio preload="metadata" aria-label="Narrated test-call example"></audio></div>
  <div class="cu-progress" title="Click to jump" aria-hidden="true"><i data-cu-progress></i></div>
  <div class="cu-body" role="region" aria-label="Test-call transcript"><noscript>{noscript}</noscript></div>
  <div class="cu-result"></div>
  <div class="cu-foot"><button type="button" data-cu-play>Play this call</button><span data-cu-time>0:00 / 0:00</span><button type="button" data-cu-replay>Replay</button><button type="button" data-cu-transcript>Read transcript</button></div>
  <p class="cu-note" data-cu-audio-note>Press play. The voice and the transcript move together. Call the live demo to hear the current phone voice.</p>
  <p class="cu-note"><b>Not a customer recording.</b> HVAC, plumbing and auto repair use saved test-call text; the other trades are scripted examples. Audio is synthetic narration, not the live phone voice.</p>
</div>{calls_script(first)}"""


# ------------------------------------------------------------------ shared layout
NAV = [("/how-it-works", "How it works"), ("/hvac", "HVAC"), ("/garage-door", "Garage door"), ("/plumbing", "Plumbing"), ("/electrical", "Electrical"), ("/pricing", "Plans"), ("/faq", "FAQ")]


def layout(*, path: str, title: str, desc: str, body: str, ld: list | None = None, hero_first: str | None = None) -> str:
    links = "".join(f'<a href="{h}">{t}</a>' for h, t in NAV)
    nav = (f'<header class="nav"><div class="wrap"><a class="brand" href="/"><i></i>@@BRAND@@</a>'
           f'<div class="account-actions"><a class="account-link" href="@@CLIENT_PORTAL_URL@@">Owner sign in</a>'
           f'<button class="menu-btn" id="menu-btn" aria-expanded="false" aria-controls="site-nav">Menu</button></div>'
           f'<nav id="site-nav" aria-label="Main">{links}<a class="account-link" href="@@CLIENT_PORTAL_URL@@">Owner sign in</a><a href="@@FRONT_DESK_SETUP_URL@@">Set up your front desk</a><a class="btn btn-primary" href="tel:@@DEMO_TEL@@">Hear it live</a></nav></div></header>')
    url = SITE + (path if path != "/" else "/")
    ld_tags = "".join(f'<script type="application/ld+json">{json.dumps(o, ensure_ascii=False)}</script>' for o in (ld or []))
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(desc)}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="website"><meta property="og:title" content="{html.escape(title)}"><meta property="og:description" content="{html.escape(desc)}">
<meta property="og:url" content="{url}"><meta property="og:site_name" content="@@BRAND@@"><meta property="og:image" content="{SITE}/assets/og.png"><meta property="og:image:alt" content="@@BRAND@@: overflow and after-hours call coverage for local service companies">
<meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{html.escape(title)}"><meta name="twitter:description" content="{html.escape(desc)}"><meta name="twitter:image" content="{SITE}/assets/og.png">
<meta name="theme-color" content="#0F1B2D">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%230F1B2D'/%3E%3Ccircle cx='16' cy='16' r='6' fill='%2366E0C0'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Source+Code+Pro:wght@400;500;600&display=swap">
<link rel="stylesheet" href="/assets/style.css?v={ASSET_V}">
<style>
/* Account access stays outside the collapsed menu; existing site tokens remain authoritative. */
.account-actions{{display:none;align-items:center;gap:8px}}
.account-link,footer a{{display:inline-flex;align-items:center;min-height:44px}}
.nav nav{{gap:10px}}
.nav nav a{{font-size:13.5px;white-space:nowrap}}
.nav nav a.btn{{font-size:13.5px;padding-inline:12px}}
@media(max-width:1180px){{
  .account-actions{{display:flex}}
  .account-actions .account-link{{white-space:nowrap;font-size:14px;font-weight:600}}
  .menu-btn{{display:block;min-height:44px}}
  .nav nav{{display:none;position:absolute;left:0;right:0;top:66px;background:#fff;border-bottom:1px solid var(--line);flex-direction:column;align-items:stretch;gap:0;padding:8px 20px 16px;max-height:calc(100dvh - 100px);overflow-y:auto}}
  .nav nav.open{{display:flex}}
  .nav nav a{{padding:14px 4px;border-bottom:1px solid var(--line);font-size:17px;min-height:44px}}
  .nav nav a.btn{{margin-top:12px;text-align:center}}
}}
@media(max-width:380px){{.nav .wrap{{gap:8px;padding-inline:12px}}.nav .brand{{font-size:19px}}.account-actions{{gap:6px}}.menu-btn{{font-size:14px;padding-inline:8px}}}}
</style>
{ld_tags}
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<div class="callbar">Hear it work right now: call <a href="tel:@@DEMO_TEL@@"><b>@@DEMO_PHONE@@</b></a> and pretend to be a customer</div>
{nav}
<main id="main">
{body}
</main>
<div class="sticky-cta"><a class="btn btn-green" href="tel:@@DEMO_TEL@@">Hear it live</a><a class="btn btn-ghost" href="@@BOOK_URL@@">Book a demo</a></div>
<footer>
<div class="wrap">
  <div class="cols">
    <div><a class="brand" href="/" style="color:#fff"><i></i>@@BRAND@@</a>
      <p style="margin-top:12px;max-width:34ch">Overflow and after-hours call coverage for local service companies, set up for you by a real person.</p></div>
    <div><h4>Product</h4><ul><li><a href="/how-it-works">How it works</a></li><li><a href="/pricing">Plans</a></li><li><a href="/faq">FAQ</a></li><li><a href="@@FRONT_DESK_SETUP_URL@@">Set up your front desk</a></li></ul></div>
    <div><h4>Trades</h4><ul><li><a href="/hvac">HVAC</a></li><li><a href="/garage-door">Garage door</a></li><li><a href="/plumbing">Plumbing</a></li><li><a href="/electrical">Electrical</a></li></ul></div>
    <div><h4>Talk to us</h4><ul><li><a href="tel:@@PHONE_TEL@@">@@PHONE@@</a></li><li><a href="mailto:@@EMAIL@@">@@EMAIL@@</a></li><li><a href="@@BOOK_URL@@">Book a demo</a></li><li><a class="account-link" href="@@CLIENT_PORTAL_URL@@">Owner sign in</a></li><li><a href="@@TERMS_URL@@">Terms</a></li><li><a href="/privacy">Privacy</a></li></ul></div>
  </div>
  <p class="fine">New here? <a href="@@FRONT_DESK_SETUP_URL@@">Set up your front desk</a> sends your business details for review: it is a setup request, not instant account creation or service activation. We review your hours, services and call rules with you, then build and test the setup. Nothing goes live until you approve it and test forwarding. Already set up? Use Owner sign in with the email and temporary password we give you during managed setup; choose your own password on first sign-in. For access help, <a href="mailto:@@EMAIL@@">contact @@OWNER@@</a>.</p>
  <p class="fine">@@BRAND@@ is operated by @@OWNER@@. Every call opens by telling the caller they are speaking with an AI. The service answers in English. Not for healthcare or legal offices: the service is not set up for protected health information.</p>
</div>
</footer>
<script src="/assets/call-demo.js?v={ASSET_V}" defer></script>
</body>
</html>"""
    return sub(page)


def offer_block() -> str:
    """Terms in plain words (mirrors the hosted terms) and a one-tap way to ask for the price. States no price."""
    trial_ask = "mailto:@@EMAIL@@?subject=One-week%20trial%20of%20@@BRAND_MAIL@@&body=Hi%20@@OWNER@@%2C%20I%20would%20like%20to%20start%20the%20one-week%20trial%20for%20my%20business.%20My%20business%20and%20best%20number%3A%20"
    ask = "mailto:@@EMAIL@@?subject=Price%20for%20@@BRAND_MAIL@@&body=Hi%20@@OWNER@@%2C%20what%20does%20@@BRAND_MAIL@@%20cost%20for%20my%20business%3F%20My%20business%20and%20best%20number%3A%20"
    return f"""<section class="offer-strip" id="price"><div class="wrap"><div class="offer-card">
  <div><span class="eyebrow">One-week trial for new customers</span><h2>Try it for a week. Then decide.</h2>
  <p class="lede" style="margin-top:8px">We set up your line and it answers your calls for up to <b>7 days or 30 calls</b>, whichever comes first. No card is needed and nothing is charged or renewed automatically. During the trial it answers, books and takes messages, but it does not transfer callers to a person. <b>{TERMS_LINE}</b> Ask and @@OWNER@@ will put the trial terms and the price in writing before anything starts.</p></div>
  <div class="btn-row"><a class="btn btn-primary" href="{trial_ask}">Ask about the one-week trial</a><a class="btn btn-ghost" href="{ask}">Ask for the price</a><a class="btn btn-ghost" href="@@BOOK_URL@@">Get pricing on a 15-minute demo</a></div>
</div></div></section>"""


def cta_band(heading: str = "Hear it handle a call before you decide.", text: str | None = None) -> str:
    text = text or "Call the demo line and talk to it like a customer. If it looks useful, book 15 minutes and we will show you how it would handle your calls. Nothing goes live until you approve it and test it."
    return f"""<section><div class="wrap"><div class="band"><h2>{heading}</h2><p>{text}</p>
<div class="btn-row"><a class="btn btn-green" href="tel:@@DEMO_TEL@@">Hear it on the live demo: @@DEMO_PHONE@@</a><a class="btn btn-outline-light" href="@@BOOK_URL@@">Get pricing on a 15-minute demo</a><a class="btn btn-outline-light" href="/how-it-works">See how it works</a></div></div></div></section>"""


def managed_front_desk() -> str:
    """Describe implemented call records and owner actions, not a future dispatch system."""
    return """<section id="managed-front-desk"><div class="wrap two">
  <div><span class="eyebrow">Your managed front desk</span><h2>Not just an answered call. A clear next step.</h2>
    <p class="lede" style="margin-top:14px">The receptionist handles the call; your team stays responsible for the work and the callbacks. Your private owner portal shows what was recorded so you can see what happened and what still needs you.</p>
    <p>This is call coverage and follow-up visibility, not a promise that every caller will book or that a technician has been dispatched.</p>
    <div class="btn-row"><a class="btn btn-primary" href="@@FRONT_DESK_SETUP_URL@@">Set up your front desk</a><a class="btn btn-ghost" href="@@CLIENT_PORTAL_URL@@">Owner sign in</a></div>
    <p class="disclaimer" style="margin-top:12px">The form sends a setup request for review, not instant account creation or activation. We give you owner access during managed setup. Nothing goes live until you have tested and approved it.</p>
  </div>
  <div><dl>
    <dt><h3>Bookings</h3></dt><dd><p>Appointments are made only in configured booking hours and available slots. The owner sees upcoming appointments in a read-only booking calendar.</p></dd>
    <dt><h3>Messages</h3></dt><dd><p>When a caller needs a callback, the receptionist takes their name, number and reason. Call history keeps the recorded outcome and, when enabled, a summary and text transcript.</p></dd>
    <dt><h3>Needs your attention</h3></dt><dd><p>Flagged calls appear in the owner view. Use Call back to dial the caller from your phone, then Mark handled after your team follows up. The AI does not make that callback for you.</p></dd>
    <dt><h3>Owner view</h3></dt><dd><p>Sign in for your own calls, bookings, setup details and CSV exports. See recorded activity, not estimated revenue.</p></dd>
    <dt><h3>Managed setup</h3></dt><dd><p>We review your hours, services, answers and hand-off rules, build the receptionist, and test it with you. Forwarding is tested on your actual phone setup before customer calls are routed.</p></dd>
  </dl></div>
</div></section>"""


FAQS = [
    ("Does it work with my existing business number?",
     "Yes, and you do not change it. You keep the number your customers already know. Your carrier or phone system forwards calls to the number we set up for you: all calls, only the calls you don't answer, only after hours, or when you are on another call. What works depends on your carrier or phone system, so we confirm it on your phone with a real test call before anything goes live. <a href=\"/how-it-works\">The details and tradeoffs are here.</a>"),
    ("When does the AI answer, and when does my team?",
     "You choose. <b>Overflow:</b> your phone rings first and the AI answers only if nobody picks up. <b>Busy:</b> the AI answers when you are already on another call. <b>After hours:</b> your team answers during the day, the AI covers nights and weekends. <b>Everything:</b> the AI answers every call. <b>Selective:</b> certain numbers, such as family or your best customers, always ring you first. Overflow or after hours is the usual place to start; you can widen it later if you like what you hear."),
    ("Does it replace my staff?",
     "No. It is overflow and after-hours coverage, and your team stays primary. In overflow mode your phone rings first and the AI answers only when nobody picks up; in after-hours mode your team answers during the day. It is there for the calls nobody can get to, not to take over the ones your people already handle."),
    ("What if it books something wrong?",
     "Booking rules are enforced by our server, not left to the AI. It only books inside your hours, only for services you offer, and never twice in the same slot, and it can only change an appointment made from the phone number calling in. You get an email and a dashboard entry for every booking, so you see each one. A caller can always ask for a person, and you can reach us directly: call @@OWNER@@ at @@PHONE@@ or email @@EMAIL@@. Before a business goes live we place test calls against its setup (booking, rescheduling, cancelling, handing off to a person, after hours), and it does not go live until they pass."),
    ("How do I turn it off?",
     "Switch call forwarding off on your phone or phone system. On most mobile carriers that takes about ten seconds, and your line rings you again immediately with nothing needed from us. Business phone systems differ, so before go-live we confirm how it works on yours and test turning it off. <a href=\"/how-it-works\">Common carrier codes are here.</a>"),
    ("What if the caller wants a person?",
     "They get one. A caller who asks for you, the office or the dispatcher is put through to the number you choose. If nobody answers, the AI takes a message and alerts you. A caller who only asks whether they are talking to an AI gets a straight answer, not a transfer."),
    ("Will customers know it's an AI?",
     "Yes, always. Every call opens by saying the caller is speaking with an AI receptionist. It then talks in short, plain sentences. You can hear exactly how it sounds on the demo line, @@DEMO_PHONE@@."),
    ("What does it actually do on a call?",
     "It answers with your business name, finds out what the caller needs, answers questions using the hours, services and answers you gave us, and books an appointment in a time you are actually open. It can move or cancel the appointment the caller booked. It never guesses a price you did not give it, and it never gives repair or safety advice."),
    ("Is there a one-week trial?",
     "Yes, every new customer can start with one. We set up your line, test it with you, and it answers your calls for up to 7 days or 30 calls, whichever comes first. No card is needed, nothing is charged, and nothing renews automatically. During the trial it answers, answers questions, books appointments and takes messages, but it does not transfer callers to a person. When the trial ends it stops answering, so you turn forwarding off; we remind you before then. We agree the setup and these limits in writing before any of your customer calls are forwarded. Ask @@OWNER@@ at @@PHONE@@ or @@EMAIL@@ and you will get those terms in writing. The monthly price stays off this page until you ask."),
    ("How much does it cost, and why isn't the price on this site?",
     "The price depends on when you want calls covered and roughly how many calls you get, so we recommend a setup first and then give you the number plainly, in writing, after the 15-minute demo and before anything starts. You never have to book a demo to find out: if you want the number first, call or email @@OWNER@@ and you will get it. Your phone carrier may charge separately for call forwarding, which is between you and them."),
    ("What if a caller has an emergency?",
     "If someone may be in danger (a gas smell, fire, a carbon monoxide alarm, an injury), the AI tells them to hang up and call 911 right away and alerts you. For urgent service calls that are not life-threatening, you decide what happens: ring your on-call phone, or take the details and alert you. The AI is not an emergency service and does not give safety advice."),
    ("What if the AI or your servers go down?",
     "There are two safety nets. If the AI itself errors on a call, the call rings your phone. If our servers are unreachable, calls can fall back to your own phone through your carrier's forwarding. That depends on your carrier or phone system, so we set it up and test it with you before go-live. Nothing is 100 percent, so we do not promise perfection."),
    ("Does the AI ever say something is booked when it is not?",
     "It is not allowed to. The AI cannot tell a caller something is booked, moved or cancelled unless our system actually did it."),
    ("Does it connect to Google Calendar or my scheduling software?",
     "Not as a two-way link. Every booking appears on your private dashboard immediately, and each one is emailed to you as a calendar invite that lands on Google, Outlook or Apple Calendar. On request the assistant can also read your calendar as busy time through your private calendar link, so a job you already have blocks that time. That is read-only and not instant: it never writes to your calendar, and your provider decides how quickly a change you make shows up (minutes to hours). We test the link with you before relying on it."),
    ("Can it send bookings to my other software?",
     "Yes, through a webhook: each new, moved or cancelled booking and each callback request is sent as signed data to an address you choose. Zapier, Make and many CRMs can receive that. We set it up for you. We have not built native connections to specific apps yet, and we will tell you plainly if yours is not supported."),
    ("Does it send text messages to my customers?",
     "No automated customer text messages are included today. The AI can book an appointment, confirm it on the call, and send the owner an email alert. Do not rely on customer texting as part of your setup."),
    ("Does it speak Spanish?",
     "Spanish is in beta. On lines where we turn it on, the greeting ends with \"para español, oprima dos\": the caller presses 2 and the rest of the call runs in Spanish. It is more likely to make mistakes than the English service, so we turn it on only if you want it. Other languages are not supported: the caller is told so politely, their details are taken, and you are alerted to call them back."),
    ("How fast can I be live?",
     "We do not promise a date. Setup is hands-on: we build it from your hours, services and answers, call it ourselves, then you call it, and nothing goes live until you approve it. We would rather take longer than turn on something you have not tested."),
    ("Who else uses it?",
     "We do not publish client names, ratings or quotes without written permission. What we can offer before you decide is the live demo line, @@DEMO_PHONE@@, a setup you approve yourself, and a monitored start where we review early calls and fix what needs fixing."),
    ("Can I stop using it any time?",
     "Yes. Forwarding is yours to switch off at any moment, and we delete your call data on request. The terms are in writing before anything starts."),
    ("Who sees my customers' information, and are calls recorded?",
     "You do, on a private link that only opens your data. We do not record call audio. We keep a text transcript and a short summary of each call, which delete automatically after 90 days, unless you ask us not to keep transcripts at all. Speech is processed by our phone and AI providers; we never sell data. The service is not set up for medical or legal records. <a href=\"/privacy\">Read the privacy details.</a>"),
    ("Is this for my kind of business?",
     "It fits appointment-based local service companies where a missed call is a lost job: HVAC, plumbing, electrical, garage doors, and similar. It is not for doctors, dentists, therapists, home health or law firms."),
    ("We already answer our calls, or we already use an answering service.",
     "Then you may not need this. The case for it is narrow: calls that still go unanswered when everyone is busy, on a job, or the office is closed, or an answering service that takes messages but cannot book. If your current setup already handles those well, tell us and we will say so."),
    ("Why not just use a cheap do-it-yourself AI app?",
     "Those are real options and some owners are happy with them. They are software: you set up the answers, connect the phone line and fix problems yourself. Here a person does that for you, tests it with you before it goes live, changes it when your hours or services change, and answers when you call. If you like configuring software, use an app. If you would rather not think about it, that is what this is."),
]


_FIRST = ["How much does it cost", "Is there a one-week", "Will customers know", "What if a caller has an emergency", "Does it work with my existing",
          "When does the AI answer", "Does it replace my staff", "How do I turn it off", "What if the caller wants a person"]
FAQS = sorted(FAQS, key=lambda qa: next((i for i, k in enumerate(_FIRST) if qa[0].startswith(k)), len(_FIRST)))


def faq_html(items=None) -> str:
    items = items if items is not None else FAQS
    return "".join(f'<details><summary>{q}</summary><div class="a"><p>{a}</p></div></details>' for q, a in items)


def faq_ld(items=None) -> dict:
    items = items if items is not None else FAQS
    clean = lambda t: html.unescape(re.sub(r"<[^>]+>", "", sub(t)))
    return {"@context": "https://schema.org", "@type": "FAQPage",
            "mainEntity": [{"@type": "Question", "name": clean(q), "acceptedAnswer": {"@type": "Answer", "text": clean(a)}} for q, a in items]}


def org_ld() -> dict:
    # Deliberately no price, priceRange or Offer: the exact price is presented in a conversation, not published.
    return {"@context": "https://schema.org", "@type": "ProfessionalService", "name": F["BRAND"], "url": SITE,
            "telephone": F["phone_e164"], "email": F["email"], "areaServed": "United States",
            "description": "Managed overflow and after-hours call coverage for local service companies, set up and supported by a real person."}


MODES = [
    ("Overflow", "Your phone rings first. If nobody picks up, the AI answers instead of voicemail.", "A common place to start."),
    ("After hours", "Your team answers during the day. Nights, weekends and holidays go to the AI.", "Keeps the office in charge."),
    ("When you're busy", "If you are already on another call, the next caller gets the AI instead of a busy signal.", "For peak days."),
    ("Everything", "The AI answers every call and puts callers through when they ask.", "For owners who are rarely at a desk."),
    ("Selective", "Certain numbers, such as family or your best customers, always ring you first.", "Add it to any mode."),
]


def modes_grid() -> str:
    return '<div class="grid g3">' + "".join(
        f'<div class="card"><h3>{a}</h3><p>{b}</p><p class="disclaimer" style="margin-top:8px">{c}</p></div>' for a, b, c in MODES) + "</div>"



def product_stage() -> str:
    """Animated, synthetic product view for the public site. Uses fake sample data only."""
    return """
<div class="stage" aria-label="Synthetic product preview: live call, booking and owner portal">
  <div class="orb orb-a"></div><div class="orb orb-b"></div>
  <div class="stage-top"><span class="dot on"></span><span>Live sample call</span><b>00:42</b></div>
  <div class="stage-grid">
    <div class="phone-card floaty">
      <div class="mini-label">Caller</div>
      <h3>No heat upstairs</h3>
      <p>AI asks the right questions, refuses repair advice, and checks open times.</p>
      <div class="bars"><i></i><i></i><i></i><i></i><i></i></div>
    </div>
    <div class="booking-card floaty delay-1">
      <div class="mini-label">Booked</div>
      <h3>Heating repair visit</h3>
      <p>Tomorrow · 10:30 AM · inside your allowed hours</p>
      <ul><li>No double-booking</li><li>Calendar invite sent</li></ul>
    </div>
    <div class="owner-card floaty delay-2">
      <div class="mini-label">Owner portal</div>
      <div class="portal-row"><b>Needs attention</b><span>1</span></div>
      <div class="portal-row"><b>Booked this week</b><span>4</span></div>
      <div class="portal-row"><b>CSV export</b><span>Ready</span></div>
    </div>
  </div>
  <p class="stage-note">Synthetic preview with sample data. Real calls appear in the private owner portal.</p>
</div>
"""

# ------------------------------------------------------------------ pages
def page_home() -> str:
    body = f"""
<section class="hero"><div class="wrap hero-grid">
  <div>
    <span class="eyebrow">Overflow and after-hours call coverage</span>
    <h1>When your techs can't pick up, we answer and book the job.</h1>
    <p class="lede" style="margin-top:18px">@@BRAND@@ is an AI receptionist, set up for you by a real person, that picks up the calls your office misses: after hours, when every line is busy, when everyone is on a job. It books only inside your hours, puts callers through to a person when they ask, and shows you every call. You keep your number, and nothing goes live until you have heard it and approved it.</p>
    <div class="btn-row">
      <a class="btn btn-green" href="tel:@@DEMO_TEL@@">Call the live demo: @@DEMO_PHONE@@</a>
      <a class="btn btn-outline-light" href="@@BOOK_URL@@">Or book a 15-minute demo</a>
    </div>
    <p class="fine">Call from your phone, right now. The demo line uses fictional companies. It tells you it is an AI, then answers, books, moves and cancels like it would for your business. Try to trip it up.</p>
    <div class="proof-row"><span>Keep your existing number</span><span>Your team stays in control</span><span>One-week trial</span><span>Tested before it goes live</span><span>Set up by a real person</span></div>
  </div>
  <div>{product_stage()}</div>
</div></section>

{offer_block()}

{managed_front_desk()}

<section class="demo-show"><div class="wrap hero-grid">
  <div>{call_ui()}</div>
  <div><span class="eyebrow">Listen &amp; follow</span><h2>Press play. Hear the call.</h2>
  <p class="lede" style="margin-top:12px">Nine trades each have their own spoken example: HVAC, plumbing, electrical, garage door, roofing, auto repair, landscaping, cleaning and contractor. Press play and the transcript moves with the voice. Tap any message to jump to it.</p>
  <p class="disclaimer">HVAC, plumbing and auto repair use saved test-call text. The other trades are scripted examples. All audio is synthetic narration, not a customer recording. Call the live demo to hear the current phone voice and ask your own questions.</p>
  <ul class="ticks" style="grid-template-columns:1fr;margin-top:18px"><li>The voice and the transcript stay on the same example</li><li>The booking outcome appears when the call ends</li><li>Switch trades and each one has its own call</li></ul>
  <div class="btn-row"><a class="btn btn-primary" href="tel:@@DEMO_TEL@@">Call it now</a><a class="btn btn-ghost" href="/how-it-works">See the flow</a></div></div>
</div></section>

<section class="alt trust-honest"><div class="wrap">
  <div class="trust-card"><div><span class="eyebrow">Built to be verified</span><h2>A serious phone setup should prove itself before it goes live.</h2><p class="lede" style="margin-top:10px">Call the live demo, listen to the voice, see the owner portal, and approve your exact setup before it handles customer calls.</p></div><div class="star-box" aria-label="How you can check it"><b>How you can check it</b><span>Live demo · test calls · your approval</span></div></div>
</div></section>

<section class="alt"><div class="wrap two">
  <div><span class="eyebrow">The problem</span><h2>When nobody can answer, the caller moves on</h2>
  <p class="lede" style="margin-top:12px">When you are under a sink, on a roof or at the end of a long day, the phone keeps ringing. A call that reaches voicemail is one you may never find out about.</p></div>
  <div class="stats" style="grid-template-columns:1fr"><div class="stat"><div class="n red">52%</div><p>of callers to home-services businesses spoke with a person in Invoca's July 2026 benchmark. The other 48% did not reach one.</p><small>Source: <a href="https://www.invoca.com/reports/the-invoca-home-services-lead-conversion-benchmarks-report-2026" rel="noopener">Invoca, Home Services Lead Conversion Benchmarks Report 2026</a>. Averages across Invoca's own customer base (businesses that track calls from marketing), so your numbers will differ. Ask yourself what yours are.</small></div></div>
</div></section>

<section><div class="wrap">
  <div class="center"><span class="eyebrow">You choose when it answers</span><h2>Start with the calls your team can't get to</h2>
  <p class="lede" style="margin-top:12px">No one has to hand their phone over. Overflow or after hours is the usual place to start; widen it later only if you like what you hear.</p></div>
  <div style="margin-top:30px">{modes_grid()}</div>
  <p class="center" style="margin-top:20px"><a class="btn btn-primary" href="/how-it-works">See how it works with your phone</a></p>
</div></section>

<section class="alt" id="setup"><div class="wrap">
  <div class="center"><span class="eyebrow">How setup works</span><h2>Three steps, and you stay in charge</h2>
  <p class="lede" style="margin-top:12px">You do not need to learn software. Nothing reaches your customers until you have approved it.</p></div>
  <div class="steps s3" style="margin-top:30px">
    <div class="step"><h3>1. Hear it, then talk for 15 minutes</h3><p>Call the demo line, then book a short demo. We look at how your phones work today and recommend when it should answer: overflow, after hours, or both.</p></div>
    <div class="step"><h3>2. We build it and test it with you</h3><p>We set it up from your hours, services and answers, place test calls against it, then you call it yourself. It goes live only when you approve it.</p></div>
    <div class="step"><h3>3. You turn on call forwarding</h3><p>With us on the phone, you forward calls from the number your customers already know. You can turn forwarding off yourself at any time, usually in about ten seconds. We watch the first calls with you.</p></div>
  </div>
  <p class="center" style="margin-top:20px"><a class="btn btn-primary" href="/how-it-works">See the full setup details</a></p>
</div></section>

<section><div class="wrap">
  <div class="center"><span class="eyebrow">Try to break it</span><h2>Four calls worth making</h2>
  <p class="lede" style="margin-top:12px">Call <b>@@DEMO_PHONE@@</b> and choose a pretend company. Talk to it the way a real customer would. Nothing you say reaches a real business.</p></div>
  <div style="margin-top:28px;display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px">
    <div class="card"><h3>1. A real problem</h3><p>"My furnace is blowing cold air." Press 1 for heating and air. It asks what it needs, checks real open times and books one.</p></div>
    <div class="card"><h3>2. A change of mind</h3><p>"Actually, can we move that to a different day?" Then cancel it. It only says it is done when the booking system says so.</p></div>
    <div class="card"><h3>3. A person, please</h3><p>"Can I speak with a person?" In your setup it rings your phone; in the demo it tells you what would happen and ends the call.</p></div>
    <div class="card"><h3>4. A question it must not guess</h3><p>"What will the repair cost?" It will not invent a price or give repair advice. It offers a technician instead.</p></div>
  </div>
</div></section>

<section class="alt"><div class="wrap" style="max-width:980px">
  <div class="center"><span class="eyebrow">Watch it in under a minute</span><h2>From a ringing phone to a booked job</h2>
  <p class="lede" style="margin-top:12px">A test call with our live receptionist, from the ring to the owner's phone.</p></div>
  <div style="margin-top:28px;border-radius:20px;overflow:hidden;box-shadow:var(--shadow);background:#0F1B2D">
    <video controls playsinline preload="none" poster="/assets/demo-poster.jpg" style="display:block;width:100%;height:auto;aspect-ratio:16/9" aria-label="Product demo video: a customer calls a business, the AI receptionist books the job, and the owner is notified">
      <source src="/assets/demo.mp4" type="video/mp4">
      <a href="/assets/demo.mp4">Download the demo video</a>
    </video>
  </div>
  <p class="disclaimer center" style="margin-top:12px">The customer in this video is an AI playing a caller. The receptionist is our real system. It is not a customer recording.</p>
</div></section>

<section><div class="wrap">
  <div class="center"><span class="eyebrow">What happens to a call</span><h2>From ring to a result the owner can trust</h2></div>
  <div class="steps" style="margin-top:32px">
    <div class="step"><h3>It answers</h3><p>With your business name, in plain sentences, and says up front that it is an AI receptionist.</p></div>
    <div class="step"><h3>It helps</h3><p>It answers from the hours, services and answers you gave us, and never invents a price or gives repair advice.</p></div>
    <div class="step"><h3>It books or hands off</h3><p>It books a real open time, moves or cancels the caller's own appointment, or puts them through to a person.</p></div>
    <div class="step"><h3>You see what happened</h3><p>An email with a calendar invite, the call on your dashboard, anything that needs you flagged, and a recap every Monday.</p></div>
  </div>
</div></section>

<section class="alt"><div class="wrap">
  <div class="center"><span class="eyebrow">What it handles</span><h2>The routine calls, with a person one step away</h2></div>
  <div class="grid g3" style="margin-top:32px">
    <div class="card">{ico("clock")}<h3>After-hours calls</h3><p>Nights, weekends and holidays, in the hours you choose.</p></div>
    <div class="card">{ico("calendar")}<h3>Booking, moving and cancelling</h3><p>Books only inside the hours you set and for services you offer, never twice in one slot. A caller can move or cancel their own appointment.</p></div>
    <div class="card">{ico("route")}<h3>Hand-off to a person</h3><p>Callers who ask for you ring your phone. If nobody answers, it takes a message and alerts you right then.</p></div>
    <div class="card">{ico("mail")}<h3>Instant details</h3><p>Every booking and callback is emailed to you with a calendar invite for Google, Outlook or Apple Calendar.</p></div>
    <div class="card">{ico("chart")}<h3>Private owner portal</h3><p>Sign in to see your calls, summaries, follow-ups, booking calendar, settings and CSV exports in one place.</p></div>
    <div class="card">{ico("shield")}<h3>Safe when something breaks</h3><p>If the AI fails, your phone rings. If our servers fail, calls can fall back to your own phone where your carrier or phone system supports it; we set that up and test it with you. Gas or fire: callers are told to call 911.</p></div>
  </div>
</div></section>

<section><div class="wrap two">
  <div><span class="eyebrow">Tested before it touches a customer</span><h2>Your setup has to pass before it answers</h2>
  <p class="lede" style="margin-top:14px">We build the receptionist around your actual hours, services and rules. Then we place test calls against it, and it does not go live until they pass.</p></div>
  <div><ul class="ticks" style="grid-template-columns:1fr">
    <li>A normal booking is made correctly, once</li><li>A caller moves, then cancels, their own appointment</li>
    <li>A caller asks for a person and is put through</li><li>Nobody answers the transfer: it takes a message</li>
    <li>An emergency phrase gets the 911 message first</li><li>Questions it should not answer are refused, not invented</li>
    <li>Then you call it yourself and approve it</li></ul></div>
</div></section>

<section class="alt"><div class="wrap">
  <div class="center"><span class="eyebrow">Honest limits</span><h2>What it is, and what it is not</h2></div>
  <div class="grid g3" style="margin-top:32px">
    <div class="card">{ico("user")}<h3>Always says it is an AI</h3><p>Every call opens with the disclosure. It cannot be turned off.</p></div>
    <div class="card">{ico("lock")}<h3>Your data stays yours</h3><p>A private link opens only your data. We do not record call audio. Transcripts delete after 90 days, and we never sell data.</p></div>
    <div class="card">{ico("alert")}<h3>Not an emergency service</h3><p>It sends danger calls to 911 and alerts you. It does not diagnose, give safety advice or guess prices.</p></div>
    <div class="card">{ico("calendar")}<h3>Calendar, said plainly</h3><p>Bookings reach your calendar as emailed invites. It can read your calendar as busy time on request. It is not a two-way sync.</p></div>
    <div class="card">{ico("shield")}<h3>No customer texting</h3><p>Automated customer text messages are not included today. Confirmations are spoken on the call; owner alerts use email.</p></div>
    <div class="card">{ico("user")}<h3>Not for every business</h3><p>Not for healthcare or legal offices. If your current setup already covers your calls well, we will tell you.</p></div>
  </div>
</div></section>

<section><div class="wrap two">
  <div><span class="eyebrow">Who is behind it</span><h2>A real person, not a call center</h2></div>
  <div><p class="lede">@@BRAND@@ is run by @@OWNER@@. The service is available to companies across the United States, and I personally manage each setup: I build your receptionist, test it with you, and change it when your hours or services change. You can call or email me directly.</p>
  <p>@@NEW_LINE@@</p>
  <p><b>@@PHONE@@</b> &middot; <a href="mailto:@@EMAIL@@">@@EMAIL@@</a></p></div>
</div></section>

<section class="alt"><div class="wrap">
  <div class="center"><span class="eyebrow">Questions</span><h2>What owners ask first</h2></div>
  <div style="max-width:820px;margin:30px auto 0">{faq_html(FAQS[:8])}<p style="margin-top:18px"><a href="/faq">See all questions</a></p></div>
</div></section>
{cta_band()}"""
    return layout(path="/", title="@@BRAND@@: overflow and after-hours call coverage for local trades",
                  desc="Don't let a good call die in voicemail. It answers when your team can't, books the appointment, and brings in a person. Keep your number. Hear it live.",
                  body=body, ld=[org_ld(), faq_ld(FAQS[:8])])


def page_how() -> str:
    body = """
<section class="phero"><div class="wrap"><div class="crumbs"><a href="/">Home</a> / How it works</div>
<h1>How it works, and what happens to your existing number</h1>
<p class="lede" style="margin-top:14px">The short version: you keep your number, we set up a receptionist around your business, and you choose when calls go to it.</p></div></section>

<section><div class="wrap">
  <h2>From first call to live</h2>
  <div class="steps s3" style="margin-top:26px">
    <div class="step"><h3>A short demo</h3><p>15 minutes. You call the demo line, we look at how your phones work today, and we recommend a coverage mode.</p></div>
    <div class="step"><h3>A one-page proposal</h3><p>What it will handle, what stays with your team, how it connects, the price, the terms, and a review date.</p></div>
    <div class="step"><h3>We build and test it</h3><p>From your hours, services and answers. We call it ourselves and run our booking, hand-off and emergency checks, then you call it and change whatever you do not like.</p></div>
    <div class="step"><h3>You switch on forwarding</h3><p>We walk you through it on your phone, live, and place a test call together. You can turn it off yourself at any time.</p></div>
    <div class="step"><h3>We watch the first calls</h3><p>We read the first calls with you, fix what needs fixing, and send a weekly recap.</p></div>
    <div class="step"><h3>We review it together</h3><p>At the agreed date we look at what it handled, what went wrong and what we changed, and you decide whether to continue.</p></div>
  </div>
</div></section>

""" + managed_front_desk() + """
<section class="alt"><div class="wrap">
  <span class="eyebrow">When the AI answers</span><h2>Five coverage modes</h2>
  <p class="lede" style="margin-top:12px">Pick one, or combine them. Changing it later is a setting, not a project.</p>
  <div style="margin-top:26px">""" + modes_grid() + """</div>
</div></section>

<section><div class="wrap">
  <span class="eyebrow">Your existing number</span><h2>Ways to connect it, and the tradeoffs</h2>
  <p class="lede" style="margin-top:12px">What works depends on your phone carrier or phone system, so we always confirm it on your phone and with a real test call before we promise anything.</p>
  <div class="tw" style="margin-top:26px"><table>
  <thead><tr><th>Option</th><th>What you do</th><th>What callers see</th><th>Good for</th><th>Watch out for</th></tr></thead><tbody>
  <tr><td><b>Forward when you don't answer or are busy</b> (conditional forwarding)</td><td>Turn on "no answer" and "busy" forwarding.</td><td>It rings you first. If you miss it, the AI picks up.</td><td>Overflow: keeping your personal touch and catching the misses.</td><td>The number of rings before it forwards is set by your carrier. Your voicemail may answer first; we test for this.</td></tr>
  <tr><td><b>Forward after hours</b></td><td>Turn forwarding on in the evening and off in the morning, or use your phone system's schedule.</td><td>Daytime calls reach you. Nights and weekends reach the AI.</td><td>Businesses with staff during the day.</td><td>Manual on/off on plain mobile plans. Scheduled on many business phone systems.</td></tr>
  <tr><td><b>Forward all calls</b></td><td>Turn on call forwarding to your new number.</td><td>They dial your number and the AI answers.</td><td>Owners who are rarely at a desk.</td><td>Your phone will not ring at all while it is on. Ringing you from our side is not possible in this setup, so we use a dedicated line.</td></tr>
  <tr><td><b>Ring both at once</b> (simultaneous ring)</td><td>Set it up in your VoIP or business phone system.</td><td>You and the AI both hear the ring. Whoever answers first wins.</td><td>Offices with a phone system that supports it.</td><td>Not available on most mobile plans.</td></tr>
  <tr><td><b>Publish a new number</b></td><td>Put the new number on your website, Google profile and trucks.</td><td>The AI answers that number directly.</td><td>A second line for online leads.</td><td>Your old number is not covered unless you also forward it.</td></tr>
  </tbody></table></div>
  <div class="note" style="margin-top:18px"><b>Porting your number to us</b> (moving it entirely) is possible in some cases but we do not recommend it to start. It takes days, cannot always be undone quickly, and forwarding gives you the same result while you keep full control.</div>
</div></section>

<section class="alt"><div class="wrap">
  <h2>Common forwarding codes</h2>
  <p class="lede" style="margin-top:10px">Codes vary by carrier and plan. We try them with you on the phone, and use your phone's own Settings, Phone, Call Forwarding if a code does not work.</p>
  <div class="tw" style="margin-top:22px"><table>
  <thead><tr><th>Carrier</th><th>Forward all calls</th><th>Only if no answer</th><th>Turn off</th></tr></thead><tbody>
  <tr><td>Verizon</td><td><span class="kbd">*72</span> then our number</td><td><span class="kbd">*71</span> then our number</td><td><span class="kbd">*73</span></td></tr>
  <tr><td>AT&amp;T (mobile)</td><td><span class="kbd">*21*</span>number<span class="kbd">#</span></td><td>Use the phone's Call Forwarding setting, or ask AT&amp;T. Codes for this vary by plan.</td><td><span class="kbd">#21#</span></td></tr>
  <tr><td>T-Mobile</td><td><span class="kbd">**21*</span>1number<span class="kbd">#</span></td><td><span class="kbd">**61*</span>number<span class="kbd">#</span></td><td><span class="kbd">##21#</span></td></tr>
  <tr><td>Other carriers and VoIP or office phones</td><td colspan="3">Use the phone's Call Forwarding setting, or your phone system's admin page (call forwarding or call routing). Turn-off: <span class="kbd">##002#</span> works on many carriers.</td></tr>
  </tbody></table></div>
</div></section>

<section><div class="wrap two">
  <div><h2>What if it cannot answer, or something breaks?</h2>
  <p class="lede" style="margin-top:12px">The design goal is that a caller reaches a person, not an error message.</p></div>
  <div class="grid">
    <div class="card"><h3>Caller asks for you</h3><p>It rings your phone. No answer in about 25 seconds: it takes a message and alerts you.</p></div>
    <div class="card"><h3>The AI errors mid-call</h3><p>The call rings your phone instead.</p></div>
    <div class="card"><h3>Our servers are unreachable</h3><p>Calls can fall back to your own phone through your carrier's forwarding. This depends on your carrier or phone system, so we set it up and test it with you before go-live.</p></div>
    <div class="card"><h3>You want out</h3><p>Turn forwarding off (about ten seconds). Your line rings you again immediately, with nothing needed from us.</p></div>
  </div>
</div></section>
""" + cta_band()

    return layout(path="/how-it-works", title="How it works and how to use your existing business number | @@BRAND@@",
                  desc="You keep your number. Choose overflow, after hours, busy or everything. How setup, testing and go-live work, carrier codes, tradeoffs and what happens if something breaks.",
                  body=body, ld=[org_ld()])


def page_pricing() -> str:
    body = f"""
<section class="phero pricing-hero"><div class="wrap two">
  <div><div class="crumbs"><a href="/">Home</a> / Plans</div><span class="eyebrow">Plans after a real demo</span>
  <h1>A managed receptionist, not a do-it-yourself bot.</h1>
  <p class="lede" style="margin-top:14px">Pricing is tied to the coverage you choose: overflow, after-hours, busy-line coverage, or full-time answering. You hear the demo, we scope the setup, then you get the number plainly in writing before anything starts.</p>
  <div class="btn-row"><a class="btn btn-green" href="tel:@@DEMO_TEL@@">Hear the live demo</a><a class="btn btn-primary" href="@@BOOK_URL@@">Book 15 minutes</a></div></div>
  <div>{product_stage()}</div>
</div></section>

{offer_block()}

""" + managed_front_desk() + """
<section><div class="wrap">
  <div class="center"><span class="eyebrow">Managed service</span><h2>What your setup includes</h2><p class="lede" style="margin-top:12px">The value is not just answering the phone. It is setup, safe booking rules, owner visibility, and a person responsible for fixing it when the calls expose something messy.</p></div>
  <div class="value-grid" style="margin-top:30px">
    <div class="value-card"><b>01</b><h3>Configured around your business</h3><p>Hours, services, call-handling rules, hand-off number, booking windows and questions are written for you.</p></div>
    <div class="value-card"><b>02</b><h3>Tested before customers hear it</h3><p>We place setup calls first: booking, move, cancel, human hand-off, emergency phrase and questions it must not answer.</p></div>
    <div class="value-card"><b>03</b><h3>Owner portal included</h3><p>Call history, summaries, needs-attention queue, booking calendar, settings and CSV exports stay in one private place.</p></div>
    <div class="value-card"><b>04</b><h3>Monitored start</h3><p>The first calls are reviewed with you so weak answers, missed details and edge cases get fixed quickly.</p></div>
  </div>
</div></section>

<section class="alt"><div class="wrap two">
  <div><span class="eyebrow">Every plan includes</span><h2>Coverage, proof and control</h2></div>
  <div><ul class="ticks" style="grid-template-columns:1fr">
    <li>Setup around your hours, services and answers, tested with you</li><li>Appointment booking, moving and cancelling inside your hours</li>
    <li>Hand-off to a person, and message taking when nobody answers</li><li>Email alerts with calendar invites</li>
    <li>A private dashboard, call summaries and a Monday recap</li><li>Changes when your hours or services change</li>
    <li>A review with you at an agreed date</li></ul></div>
</div></section>

<section><div class="wrap two">
  <div><span class="eyebrow">How we quote</span><h2>No surprise checkout page</h2></div>
  <div class="quote-steps"><p><b>1. A 15-minute demo.</b> You call the demo line and we look at how your phones work today.</p>
  <p><b>2. A one-page proposal.</b> The coverage we recommend, what the AI handles and what stays with your team, how it connects, the price, the terms and a review date.</p>
  <p><b>3. A monitored start.</b> We set it up, test it, and review the first weeks with you. Your phone carrier may charge for call forwarding, which is between you and them.</p>
  <p>If you would rather know the number before a demo, ask us. We will tell you.</p></div>
</div></section>
""" + cta_band("See what it would do on your calls.")

    return layout(path="/pricing", title="Plans: how coverage and plans work | @@BRAND@@",
                  desc="Plans depend on how you want calls covered and your call volume. What every plan includes and how we quote after a short demo.",
                  body=body, ld=[org_ld()])


TRADE_PAGES = {
    "hvac": dict(
        name="HVAC", title="Overflow and after-hours call coverage for HVAC companies across the United States",
        h1="Your techs are on a roof. The service call is still ringing.",
        lede="The worst calls come at the worst times: a dead furnace at 6 a.m., a no-cool call on a hot Friday night, every truck out on the same day. A call that reaches voicemail is one you may never hear about. @@BRAND@@ answers when your office can't, books the visit inside your hours, and brings in a person when one is needed.",
        pains=[("Peak-season overload", "Every tech is on a job and the office line rings out. The AI keeps answering while you are slammed."),
               ("After-hours calls", "A caller with no heat or no cooling reaches something that answers at 9 p.m., takes the details or books a visit, and alerts you."),
               ("Estimate requests", "New-system inquiries are captured or booked as estimates without anyone stopping a job to pick up.")],
        handles=["Books repair visits and estimate appointments inside your hours", "Answers service area, hours and anything you have given it, from your words", "Puts a caller through to your on-call phone when they ask, or when you set urgent calls to ring you", "Tells callers with a gas smell or carbon monoxide alarm to call 911 and alerts you", "Never quotes a price you did not give it and never gives repair advice"],
        faq=[("What about no-heat calls in winter?", "You decide. It can book the soonest slot you allow and alert you, or ring your on-call phone right away. Anything involving gas, fire or carbon monoxide gets the 911 message first."),
             ("Can it tell callers my diagnostic fee?", "Only if you give us the exact wording. Otherwise it offers a visit or a callback instead of guessing."),
             ("Will it replace my office staff?", "No. Your team stays primary. It covers the calls your team cannot get to: after hours, busy periods and overflow.")],
        slug="hvac"),
    "garage-door": dict(
        name="Garage door", title="Overflow and after-hours call coverage for garage door companies across the United States",
        h1="A door stuck open at 7 p.m. will not wait for tomorrow.",
        lede="Garage door calls are urgent and simple: it will not open, a spring snapped, the door is stuck with a car inside. If the office has closed, that customer reaches voicemail. @@BRAND@@ answers when you can't and gets the details or the appointment.",
        pains=[("Calls after the office closes", "If the office closes at 4 or 5 but a door is stuck at 7, the AI answers after that and takes the job details."),
               ("Calls while the crew is out", "Everyone is on a repair and the line rings out. The AI keeps answering."),
               ("Estimate requests", "New-door and opener quotes are captured or booked without interrupting a job.")],
        handles=["Captures what is wrong (will not open, stuck, off track, opener) and books a visit inside your hours", "Answers service area and hours from your words", "Puts a caller through to your on-call number when they ask", "Never gives repair or safety instructions, including for springs and cables, and never promises an arrival time it has not booked", "Alerts you the moment a lead comes in"],
        faq=[("Will it tell callers how to fix a stuck door?", "No. It does not give repair or safety advice. It takes the details and books a visit or alerts you."),
             ("Can it promise a technician tonight?", "Only if you have configured and it has actually booked a slot. It never promises dispatch it has not confirmed.")],
        slug="garage-door"),
    "plumbing": dict(
        name="Plumbing", title="Overflow and after-hours call coverage for plumbers across the United States",
        h1="You can't answer the phone from under a sink.",
        lede="A leaking pipe does not wait for a callback. @@BRAND@@ answers when you can't, books the visit or takes the details, and brings in a person when one is needed.",
        pains=[("Calls during the job", "Hands wet, head under a cabinet: the call goes to voicemail."),
               ("Burst pipe and leak calls", "Callers with an active leak reach something that answers, takes the details and alerts you."),
               ("Drain and water heater quotes", "It books an estimate visit instead of guessing a price over the phone.")],
        handles=["Books leak, drain and water heater visits inside your hours", "Answers hours, service area and common questions from your words", "Transfers to your phone when a caller asks for you", "Tells callers who describe a gas smell, a fire or an injury to call 911, and alerts you", "Never quotes a price you did not give it"],
        faq=[("What if it is a real plumbing emergency?", "You decide: book the soonest slot and alert you, or ring your on-call phone. A gas smell always gets the 911 message first."),
             ("Will it give quotes?", "No. It will not invent prices. It books an estimate or takes the details so you can quote properly.")],
        slug="plumbing"),
    "electrical": dict(
        name="Electrical", title="Overflow and after-hours call coverage for electricians across the United States",
        h1="You're inside a panel. The phone is still ringing.",
        lede="Electricians are in panels and attics, not at the phone, and a voicemail box is a poor answer to a dead outlet or a remodel request. @@BRAND@@ answers when you can't, books the estimate or repair, and brings in a person when one is needed.",
        pains=[("Estimate requests", "New wiring, panel upgrades and added outlets start with a call. Capturing it on the first call is the win."),
               ("Safety situations", "A caller who describes a fire or an injury is told to call 911, and you are alerted. It does not give electrical safety advice."),
               ("Repeat and referral callers", "Returning customers get a fast, consistent answer even when you are on site.")],
        handles=["Books estimate visits and repair appointments inside your hours", "Answers service area and hours from your words", "Transfers to you when a caller asks", "Tells callers who describe a fire or an injury to call 911, and alerts you", "Never gives electrical or safety advice"],
        faq=[("What about electrical emergencies?", "A caller who describes a fire or someone being hurt gets the 911 message immediately, and you are alerted. It does not give electrical safety advice. For urgent but not dangerous calls, we can ring your on-call phone."),
             ("Does it know my licensing or insurance details?", "Only what you put in its instructions. We add what you want callers to hear and leave out anything you do not.")],
        slug="electrical"),
}


def trade_noun(name: str) -> str:
    """Lower-case a trade name for mid-sentence use, but never HVAC."""
    return name if name.isupper() else name.lower()


def page_trade(key: str) -> str:
    t = TRADE_PAGES[key]
    noun = trade_noun(t["name"])
    pains = "".join(f'<div class="card"><h3>{a}</h3><p>{b}</p></div>' for a, b in t["pains"])
    handles = "".join(f"<li>{h}</li>" for h in t["handles"])
    hero_call = t["slug"] if any(c["slug"] == t["slug"] for c in CALLS) else None
    body = f"""
<section class="hero"><div class="wrap hero-grid">
  <div><span class="eyebrow">{t["name"]} &middot; Call coverage</span>
  <h1>{t["h1"]}</h1>
  <p class="lede" style="margin-top:18px">{t["lede"]}</p>
  <div class="btn-row"><a class="btn btn-green" href="tel:@@DEMO_TEL@@">Hear it handle a call: @@DEMO_PHONE@@</a><a class="btn btn-outline-light" href="@@BOOK_URL@@">Get pricing on a 15-minute demo</a></div>
  <div class="proof-row"><span>Keep your existing number</span><span>Overflow or after hours</span><span>One-week trial</span><span>Tested before it goes live</span></div></div>
  <div>{call_ui(hero_call)}</div>
</div></section>
<section class="alt"><div class="wrap"><div class="center"><h2>Where {noun} calls get missed</h2></div>
<div class="grid g3" style="margin-top:28px">{pains}</div></div></section>
<section><div class="wrap two"><div><h2>What it handles for {noun} companies</h2></div>
<div><ul class="ticks" style="grid-template-columns:1fr">{handles}</ul>
<p class="disclaimer" style="margin-top:14px">It always tells callers it is an AI, and a caller can ask for a person at any point.</p>
<p class="disclaimer" style="margin-top:8px">@@NEW_LINE@@</p></div></div></section>
<section class="alt"><div class="wrap"><div class="center"><h2>You choose when it answers</h2></div><div style="margin-top:26px">{modes_grid()}</div></div></section>
<section><div class="wrap"><div class="center"><h2>{t["name"]} questions</h2></div>
<div style="max-width:820px;margin:26px auto 0">{faq_html(t["faq"])}<p style="margin-top:14px"><a href="/faq">All questions</a> &middot; <a href="/how-it-works">Using your existing number</a> &middot; <a href="/pricing">How plans work</a></p></div></div></section>
{cta_band()}"""
    return layout(path=f"/{key}", title=f'{t["name"]} overflow call coverage across the United States | @@BRAND@@',
                  desc=f'Don\'t let {noun} calls die in voicemail. It answers when your team can\'t, books the appointment and brings in a person when needed. Hear it live.',
                  body=body, ld=[org_ld(), faq_ld(t["faq"])], hero_first=hero_call)


def page_faq() -> str:
    body = """
<section class="phero"><div class="wrap"><div class="crumbs"><a href="/">Home</a> / FAQ</div><h1>Frequently asked questions</h1></div></section>
<section><div class="wrap" style="max-width:860px">""" + faq_html() + """</div></section>""" + cta_band("Still have a question?", "Call @@OWNER@@ at @@PHONE@@, email @@EMAIL@@, or hear the receptionist for yourself on the demo line.")
    return layout(path="/faq", title="FAQ: overflow call coverage questions answered | @@BRAND@@",
                  desc="Phone numbers, when the AI answers, human hand-off, emergencies, calendars, texting, languages and cancelling: straight answers.", body=body, ld=[faq_ld()])


def page_privacy() -> str:
    body = """
<section class="phero"><div class="wrap"><div class="crumbs"><a href="/">Home</a> / Privacy</div><h1>Privacy</h1>
<p class="lede" style="margin-top:12px">What we collect, why, how long we keep it, and who handles it. Last updated September 2026.</p></div></section>
<section><div class="wrap" style="max-width:820px">
<h2>On this website</h2>
<p>This site does not use advertising trackers. If you book a demo or use a contact form, we receive the name, phone number and details you type, and we use them only to contact you about the service.</p>
<h2 style="margin-top:28px">For our clients' callers</h2>
<p>When a customer calls a business that uses @@BRAND@@, the call is answered by an AI receptionist that tells the caller it is an AI and that the call may be recorded and monitored. We store the booking details the caller gives (name, phone number, what they need) and, unless the client has turned it off, a text transcript and a short summary of the call. We do not record call audio ourselves.</p>
<ul>
<li><b>Retention:</b> transcripts and summaries are deleted automatically after 90 days. Booking and call-count records are kept for the client's history.</li>
<li><b>Who can see it:</b> the business owner, through a private link that opens only their data, and us, to support and improve the service.</li>
<li><b>Who processes it:</b> our phone provider (Twilio) for the call and speech recognition and voice; our AI provider (Anthropic) to generate replies; our hosting provider (Fly.io); our email provider for alerts; Stripe for billing. Each handles data only to provide its service to us.</li>
<li><b>We do not sell personal data.</b></li>
<li><b>Deletion:</b> a client can ask us to delete their data at any time, and we will.</li>
</ul>
<h2 style="margin-top:28px">Not for protected health information</h2>
<p>@@BRAND@@ isn't set up for medical or legal records and doesn't have the agreements those require. We do not take healthcare or legal clients, and the receptionist isn't allowed to ask for or repeat medical details.</p>
<h2 style="margin-top:28px">Contact</h2>
<p>Questions or deletion requests: <a href="mailto:@@EMAIL@@">@@EMAIL@@</a> or @@PHONE@@. Full service terms: <a href="@@TERMS_URL@@">@@TERMS_URL@@</a>.</p>
<p class="disclaimer">This summary is written in plain language to describe how the service works today. It is not legal advice.</p>
</div></section>"""
    return layout(path="/privacy", title="Privacy | @@BRAND@@", desc="What @@BRAND@@ collects from callers and clients, how long it is kept, and who handles it.", body=body)


def page_404() -> str:
    body = """<section><div class="wrap center" style="padding-block:60px"><h1>That page isn't here</h1><p class="lede" style="margin-top:14px">Try the <a href="/">home page</a>, or call the demo line: <a href="tel:@@DEMO_TEL@@">@@DEMO_PHONE@@</a>.</p></div></section>"""
    return layout(path="/404", title="Page not found | @@BRAND@@", desc="Page not found.", body=body)


PAGES = {"index.html": page_home, "how-it-works/index.html": page_how, "pricing/index.html": page_pricing,
         "hvac/index.html": lambda: page_trade("hvac"), "plumbing/index.html": lambda: page_trade("plumbing"),
         "electrical/index.html": lambda: page_trade("electrical"), "garage-door/index.html": lambda: page_trade("garage-door"), "faq/index.html": page_faq,
         "privacy/index.html": page_privacy, "404.html": page_404}


# Conservative CSP: no inline <script> exists on any page (JSON-LD / call-data blocks are inert data), so script-src is 'self' only.
# style-src keeps 'unsafe-inline' because pages carry a small inline <style> and style="" attributes; Google Fonts is the only third party.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' data:; media-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'")
VERCEL_CONFIG = {
    "cleanUrls": True, "trailingSlash": False,
    "redirects": [{"source": "/(.*)", "has": [{"type": "host", "value": "www." + DOMAIN}], "destination": SITE + "/$1", "permanent": True}],
    "headers": [
        # Asset file names are not hashed, so never "immutable": browsers revalidate after an hour (HTML links CSS/JS with ?v=<hash>).
        {"source": "/assets/(.*)", "headers": [{"key": "Cache-Control", "value": "public, max-age=3600, must-revalidate"}]},
        {"source": "/favicon.ico", "headers": [{"key": "Cache-Control", "value": "public, max-age=86400, must-revalidate"}]},
        {"source": "/(.*)", "headers": [{"key": "X-Content-Type-Options", "value": "nosniff"}, {"key": "Referrer-Policy", "value": "strict-origin-when-cross-origin"},
                                        {"key": "X-Frame-Options", "value": "DENY"}, {"key": "Content-Security-Policy", "value": CSP},
                                        {"key": "Permissions-Policy", "value": "camera=(), microphone=(), geolocation=()"}]}],
}


def main() -> None:
    if OUT.exists():  # clear the contents, not the folder: a local preview server may be holding it open (Windows)
        for child in OUT.iterdir():
            if child.name == ".vercel":  # keeps the folder linked to the live Vercel project
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    (OUT / "assets").mkdir(parents=True, exist_ok=True)
    for f in (HERE / "assets").iterdir():
        shutil.copy2(f, OUT / "assets" / f.name)
    shutil.copy2(HERE / "favicon.ico", OUT / "favicon.ico")
    for rel, fn in PAGES.items():
        dest = OUT / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(fn(), encoding="utf-8")
        print("built", rel)
    urls = ["/", "/how-it-works", "/pricing", "/hvac", "/garage-door", "/plumbing", "/electrical", "/faq", "/privacy"]
    (OUT / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>{SITE}{u}</loc></url>" for u in urls) + "</urlset>", encoding="utf-8")
    (OUT / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE}/sitemap.xml\n", encoding="utf-8")
    (OUT / "vercel.json").write_text(json.dumps(VERCEL_CONFIG, indent=1), encoding="utf-8")
    left = []
    for p in OUT.rglob("*.html"):
        left += re.findall(r"@@[A-Z0-9_]+@@", p.read_text(encoding="utf-8"))
    if left:
        raise SystemExit(f"Unfilled placeholders: {sorted(set(left))}")


if __name__ == "__main__":
    main()
