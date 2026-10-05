"""Consistency audit: every sales document and website must agree with facts.json
and must not contain a claim the product can't back up.  Run after any change:

    ../backend/.venv/Scripts/python.exe audit.py
Exit code 1 if anything is wrong.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from html import unescape
from pathlib import Path

HERE = Path(__file__).resolve().parent
FACTS = json.loads((HERE / "facts.json").read_text(encoding="utf-8"))

DOCS = {
    "flyer": HERE / "flyer.html",
    "offer": HERE / "offer.html",
    "agreement": HERE / "service-agreement.html",
    "landing page": HERE / "site" / "index.html",
    "playbook": HERE / "field-playbook.html",
    "business plan": HERE / "business-plan.html",
    "start-here": HERE / "START-HERE.html",
    "what you're selling": HERE / "what-youre-selling.html",
    "Sample Home Care Co site": HERE.parent.parent / "sample_homecare-site" / "index.html",
}
for _p in sorted((HERE / "site").glob("*/index.html")):
    DOCS[f"site/{_p.parent.name}"] = _p
for _f in sorted((HERE / "flyers").glob("flyer-*.html")):
    DOCS[f"flyer ({_f.stem[6:]})"] = _f
PUBLIC = {n for n in DOCS if n.startswith(("flyer", "site/"))} | {"flyer", "offer", "agreement", "landing page", "Sample Home Care Co site"}  # prospects/clients see these


def text_of(path: Path) -> str:
    html = path.read_text(encoding="utf-8")
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))


def digits(e164: str) -> str:
    return re.sub(r"\D", "", e164)[-10:]


ALLOWED_PHONES = {
    digits(FACTS["phone_e164"]): "Sami",
    digits(FACTS["mom_line_e164"]): "Sample Home Care Co care line",
    "+15555550100": "Sample Home Care Co general line",
}
if FACTS.get("demo_number_e164"):
    ALLOWED_PHONES[digits(FACTS["demo_number_e164"])] = "demo line"

# Phrases that would be untrue today, or that we corrected during the audit.
FORBIDDEN = {
    r"\bunlimited\b": "we cap at fair use; never say unlimited",
    r"syncs? (with|to) (your|their) (google|outlook|calendar)|sync(ed|ing)? (with|to) your (google|outlook|calendar)": "no live calendar sync exists",
    r"(we|it|deskline|call kettle)\s+(will\s+)?(text|texts)\s+(you|your|customers|callers)": "texting is off until carrier registration",
    r"hipaa[- ]compliant|hipaa compliance": "not HIPAA compliant",
    r"SB 243|HB 2225": "those laws don't apply to customer-service bots (only allowed in playbook/legal notes)",
    r"\$500[- ]\$1,500|\$1,500|(?<!to )(?<!- )\$199\b|\$99\b|\$197\b": "old pricing (setup fee / founding tier no longer exist)",
    r"founding (client|offer|rate)|setup fee of|one-time setup|\+ ?\$\d+ setup": "setup fee / founding tier no longer exist",
    r"reply to this text": "nothing handles text replies",
    r"\b(48|forty-eight)[- ]hours?\b": "go-live promise is two business days",
    r"spanish|bilingual|multilingual": "English only (allowed only where we say so)",
    r"guarantee[sd]? (results|bookings|revenue)": "no guarantees",
}
FORBIDDEN_ALLOWED_IN = {
    r"SB 243|HB 2225": {"playbook"},
    r"spanish|bilingual|multilingual": {"playbook", "business plan", "offer", "landing page", "agreement", "what you're selling"},
    r"\bunlimited\b": {"playbook", "what you're selling"},  # used only in "never say" lists
    r"(we|it|deskline|call kettle)\s+(will\s+)?(text|texts)\s+(you|your|customers|callers)": {"playbook"},
    r"syncs? (with|to) (your|their) (google|outlook|calendar)|sync(ed|ing)? (with|to) your (google|outlook|calendar)": {"playbook"},
    r"hipaa[- ]compliant|hipaa compliance": {"playbook"},
}

problems: list[str] = []
notes: list[str] = []
texts = {}
for name, path in DOCS.items():
    if not path.exists():
        problems.append(f"MISSING FILE: {name} ({path})")
        continue
    texts[name] = text_of(path)


def expect(doc: str, pattern: str, why: str) -> None:
    if not re.search(pattern, texts[doc], re.I):
        problems.append(f"{doc}: expected {why} (pattern {pattern!r} not found)")


price = FACTS["price_monthly"]
fair = FACTS["fair_use_calls"]

# 1. THE EXACT PRICE IS NOT PUBLIC (owner strategy, 2026-10-01): it is presented in a conversation, after the demo.
#    Quotes (the offer, the agreement/terms) are where it lives; every PUBLIC surface must be free of it.
QUOTE_DOCS = {"agreement"}                       # the signed contract: private, after the demo. The offer sheet is price-free (it is a pre-demo hand-out).
FLYERS = [n for n in DOCS if n.startswith("flyer")]
NO_PRICE = {n for n in PUBLIC if n not in QUOTE_DOCS}          # website pages, flyers, the landing page, the client's own site
for d in QUOTE_DOCS:
    expect(d, rf"\${price}", "monthly price (a private contract)")
for n in FLYERS:
    if n in texts:
        # 2026-10-03 flyer redesign dropped the Invoca/52% stat and the old exact
        # AI-disclosure sentence in favor of shorter honest copy; still require an
        # AI-disclosure mention and the site address.
        expect(n, r"(?i)\bis an AI\b", "an AI-disclosure mention")
        expect(n, re.escape(FACTS["site_url"].split("//")[1]), "the website address")
# 2026-10-04 (QA dogfood H3): the Plans page and the home offer may state the plain-words terms that the hosted /terms page already
# publishes, in exactly this sentence (mirrored by TERMS_LINE in site-src/build_site.py). It contains no price, no call count, no rate.
APPROVED_TERMS_LINE = "No setup fee. Month-to-month. Cancel at any time, no cancellation fee."
for n in sorted(NO_PRICE - {"Sample Home Care Co site"}):
    if n not in texts:
        continue
    for m in re.finditer(r"\$\s?\d", texts[n]):
        problems.append(f"{n}: a dollar figure on a public surface (price is presented in conversation) ...{texts[n][max(0, m.start()-40):m.end()+30]}...")
    for pat, why in ((rf"\b{price}\b", "the exact price"), (rf"\b{fair}\s+(AI-handled\s+)?calls?\b", "the included-call count"),
                     (r"no setup fee", "pricing terms (belong in the proposal)"), (r"per month|a month\b|/month|/mo\b", "a per-month rate")):
        body = texts[n].replace(APPROVED_TERMS_LINE, "") if why.startswith("pricing terms") else texts[n]  # only the sanctioned sentence is exempt
        for m in re.finditer(pat, body, re.I):
            problems.append(f"{n}: {why} on a public surface ...{body[max(0, m.start()-40):m.end()+30]}...")

# 1b. The raw files (HTML, JSON-LD, meta tags, JS, sitemap, flyers) must not carry the price either: text extraction drops them.
RAW_PUBLIC = list((HERE / "site").rglob("*")) + list((HERE / "flyers").glob("*.html"))
for f in RAW_PUBLIC:
    if not f.is_file() or f.suffix.lower() not in {".html", ".js", ".json", ".xml", ".txt", ".css", ".webmanifest"} or ".vercel" in f.parts:
        continue
    raw_text = f.read_text(encoding="utf-8", errors="ignore")
    for pat, why in ((rf"(?<![\d.]){price}(?![\d])", "the exact price"), (r"priceRange|\"price\"\s*:|\"@type\"\s*:\s*\"Offer\"|priceCurrency", "price/Offer structured data"),
                     (r"data-price", "a price attribute")):
        if re.search(pat, raw_text):
            problems.append(f"{f.relative_to(HERE)}: {why} in the raw file")
# the video poster/description text and OG tags live in the pages above; the video itself is rendered without a price (scene.html)
if re.search(r"PRICE|price_monthly", (HERE / "video" / "scene.html").read_text(encoding="utf-8")):
    problems.append("video/scene.html: references the price")

# 1b2. Built pages must not show unrendered template code or internal sales-coaching copy (QA 2026-10-04 H1/M1: "{product_stage()}" shipped).
_COACHING = ("make the plan feel worth it", "what the customer is paying for", "product_stage")
for f in (HERE / "site").rglob("*.html"):
    if ".vercel" in f.parts:
        continue
    _vis = re.sub(r"<(script|style)\b.*?</\1>", "", f.read_text(encoding="utf-8", errors="ignore"), flags=re.S | re.I)
    for _m in re.findall(r"\{[^{}]*\}|\{\{|\}\}", _vis):
        problems.append(f"{f.relative_to(HERE)}: unrendered template text {_m!r} is visible on the page")
    for _p in _COACHING:
        if _p in _vis.lower():
            problems.append(f"{f.relative_to(HERE)}: internal coaching copy {_p!r} is visible on the page")

# 1c. Claims registry (docs/CLAIMS_REGISTRY.md): every percentage on a public surface must be registered, and no registered claim may be stale.
from datetime import date as _date

REGISTRY = HERE.parent / "docs" / "CLAIMS_REGISTRY.md"
registered_patterns: list[str] = []
if not REGISTRY.exists():
    problems.append("docs/CLAIMS_REGISTRY.md is missing")
else:
    for line in REGISTRY.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 11 and re.match(r"C-\d+", cells[0]):
            pats = re.findall(r"`([^`]+)`", cells[1])
            registered_patterns += pats
            try:
                due = _date.fromisoformat(cells[8])
            except ValueError:
                problems.append(f"claims registry {cells[0]}: 'Re-verify by' is not a date")
                continue
            if due < _date.today():
                problems.append(f"claims registry {cells[0]} ({pats[0] if pats else '?'}): re-verification was due {due}; check the source, then update 'Last verified' and 'Re-verify by'")
            if cells[9].lower() == "yes" and cells[3] == "WEAK":
                problems.append(f"claims registry {cells[0]}: a WEAK claim is marked public")
for n in sorted(NO_PRICE - {"Sample Home Care Co site"}):
    if n not in texts:
        continue
    for m in re.finditer(r"\d+(?:\.\d+)?\s?%", texts[n]):
        token = m.group(0).replace(" ", "")
        if not any(token == pat.replace(" ", "") or token.startswith(pat.replace(" ", "")) for pat in registered_patterns):
            problems.append(f"{n}: percentage {token} is not in docs/CLAIMS_REGISTRY.md ...{texts[n][max(0, m.start()-40):m.end()+30]}...")

# 2. Dollar figures in the quote documents must be known figures
KNOWN_DOLLARS = {price, FACTS["receptionist_monthly_wage"], 3100, 37230, 37, 290, 235, 1640, 29, 299, 249, 129, 79, 49, 149, 98, 293, 4, 15, 2, 21, 300, 1200, 18, 900, 603}
for name in QUOTE_DOCS:
    for m in re.finditer(r"\$\s?([\d,]+)(?:\.\d+)?", texts[name]):
        val = int(m.group(1).replace(",", "") or 0)
        if val not in KNOWN_DOLLARS and val not in {1, 0}:
            problems.append(f"{name}: unexpected dollar figure ${m.group(1)} near ...{texts[name][max(0, m.start()-40):m.end()+30]}...")

# 3. Phone numbers must all be ones we own or the client's
for name, t in texts.items():
    for m in re.finditer(r"\(?(\d{3})\)?[\s.\-]?(\d{3})[\s.\-](\d{4})", t):
        num = m.group(1) + m.group(2) + m.group(3)
        if num not in ALLOWED_PHONES and not re.search(r"\+1 ?555|555-?0", m.group(0)):
            problems.append(f"{name}: unknown phone number {m.group(0)}")
if FACTS.get("demo_number_e164"):
    for d in FLYERS + ["landing page", "playbook", "start-here"]:
        if d in texts and digits(FACTS["demo_number_e164"]) not in re.sub(r"\D", "", texts[d]):
            problems.append(f"{d}: demo number is missing")

# 4. Contact details on client-facing docs
for d in FLYERS + ["offer", "agreement", "landing page"]:
    if d not in texts:
        continue
    expect(d, re.escape(FACTS["email"]), "Sami's email")
    if digits(FACTS["phone_e164"]) not in re.sub(r"\D", "", texts[d]):
        problems.append(f"{d}: Sami's phone number is missing")
expect("landing page", r"Invoca", "the statistic's source")
expect("landing page", r"52%", "the sourced statistic")

# 5. Timeframes must agree across documents
for d, pat in (("offer", r"2 business days"), ("playbook", r"two business days")):
    expect(d, pat, "go-live promise of two business days")
if re.search(r"live in (about )?(2|two) (business )?days", texts["landing page"], re.I):
    problems.append("landing page: promises a go-live time; the public site must not (only the proposal does)")
expect("offer", r"one business day", "change-request turnaround")
expect("playbook", r"one business day", "change-request turnaround")
for d in ("offer", "landing page", "playbook"):
    expect(d, r"ten seconds", "forwarding-off time")
# the public site leads with the demo
expect("landing page", r"Call the live demo", "the primary demo call-to-action")
expect("landing page", r"Book a 15-minute demo", "the secondary call-to-action")

# 6. Forbidden claims
for pat, why in FORBIDDEN.items():
    for name, t in texts.items():
        allowed = FORBIDDEN_ALLOWED_IN.get(pat, set())
        if name in allowed or (name.startswith("site/") and "landing page" in allowed):
            continue
        for m in re.finditer(pat, t, re.I):
            ctx = t[max(0, m.start() - 60): m.end() + 60]
            problems.append(f"{name}: '{m.group(0)}' ({why}) ...{ctx}...")

# 7. Verticals: healthcare/legal must never be pitched as a target
for name in FLYERS + ["landing page", "offer"]:
    if name not in texts:
        continue
    for m in re.finditer(r"dental|medical|law firm|attorney|therap|med spa", texts[name], re.I):
        ctx = texts[name][max(0, m.start() - 70): m.end() + 50]
        if not re.search(r"not (yet|built|for)|don't have|isn'?t|no HIPAA|skip|can't|Is this a fit", ctx, re.I):
            problems.append(f"{name}: mentions '{m.group(0)}' without saying we don't serve it ...{ctx}...")

# 8. Sample Home Care Co site: no claims beyond the client's own facts, and the contact details match
ht = texts.get("Sample Home Care Co site", "")
for phone in ("+15555550100", "+15555550100"):
    if phone not in ht:
        problems.append(f"Sample Home Care Co site: missing {phone}")
if "9681 Main St" not in ht:
    problems.append("Sample Home Care Co site: missing office address")
if re.search(r"licensed|certified|accredited|medicare[- ]certified|joint commission|award", ht, re.I):
    problems.append("Sample Home Care Co site: makes a licensing/certification claim we can't verify")

import pricing_audit  # noqa: E402  (config-driven: the private price must not leak; reports FILE:LINE and why)

_leaks, _scanned, _unreadable = pricing_audit.scan(HERE.parent, pricing_audit.private_price())
for _f in _leaks:
    problems.append(f"PRICING LEAK {_f}")
for _u in _unreadable:
    notes.append(f"{_u} could not be text-scanned")

print("=" * 72)
print(f"Audited {len(texts)} documents against facts.json; pricing-leak scan covered {_scanned} public/pre-demo files")
print("=" * 72)
if problems:
    print(f"\n{len(problems)} PROBLEM(S):\n")
    for p in problems:
        print(" -", p)
    sys.exit(1)
print("\nNo discrepancies found.")
