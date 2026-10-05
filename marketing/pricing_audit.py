"""Pricing-leak audit: the confidential monthly subscription price must never appear on a public or pre-demo, prospect-facing surface.

    python marketing/pricing_audit.py            # exit 1 on any leak; prints FILE:LINE, the context and WHY
    python marketing/pricing_audit.py --inventory  # also lists the intentional INTERNAL occurrences (allowed)

Owner strategy (final): PROBLEM -> INTEREST -> LIVE DEMO -> DISCOVERY -> VALUE -> RECOMMENDED PLAN -> PRICE -> CLOSE. The exact price is stated in a qualified
conversation (or at once if a prospect asks directly), in a private proposal or contract. It is never advertised.

This is deliberately NOT "block the number 297 forever": the price lives in ONE private configuration value (marketing/facts.json `price_monthly`, or the
environment variable CALLKETTLE_PRIVATE_PRICE, which wins) and the audit checks that THAT value does not leak. Change the price and the audit follows. A bare
number that is not used as a price (for example "297 reviews") is not flagged: a hit needs a currency sign, a per-month phrase, a price word nearby, or a
template token / structured-data key that would render the price.

What counts as public or pre-demo: the website (source and BUILD OUTPUT), flyers, the video scene, the hosted terms/start/book pages, the Vercel fallback,
the demo configs the AI speaks from, cold-call/voicemail/email/warm-intro scripts, the prospect sheet and call queue. PDFs are text-extracted and scanned.
Internal files (economics, playbooks, proposals, contracts, billing, tests) are explicitly allowed and are only inventoried.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# Public or pre-demo prospect-facing: a price here is a failure. Globs are relative to the repository root.
PUBLIC_GLOBS = [
    "marketing/site/**/*", "marketing/site-src/**/*", "marketing/flyers/**/*", "marketing/flyer.html", "marketing/flyers.json", "marketing/offer.html",
    "marketing/video/scene.html", "marketing/video/*.py", "marketing/pdf/flyer*.pdf", "marketing/pdf/offer*.pdf",
    "marketing/templates/flyer.html", "marketing/templates/offer.html",
    "backend/app/static/**/*", "backend/app/*.py", "backend/clients/*.yaml", "fallback/**/*",
    "marketing/owner_kit/01_CALL_SHEET.md", "marketing/owner_kit/02_GATEKEEPER.md", "marketing/owner_kit/03_DISCOVERY.md", "marketing/owner_kit/06_FOLLOW_UP_EMAILS.md",
    "marketing/warm_intro.md", "marketing/today.csv", "marketing/prospects.csv", "marketing/TOP_25_NOVA*.md", "marketing/sales.py", "marketing/triggers.py",
    "marketing/build_prospects.py", "marketing/check_prospects.py",
]
SKIP_PARTS = {".git", ".vercel", "node_modules", "__pycache__", ".venv"}
TEXT_SUFFIXES = {".html", ".htm", ".js", ".mjs", ".json", ".xml", ".txt", ".css", ".md", ".csv", ".py", ".yaml", ".yml", ".webmanifest", ".svg", ".ts"}


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    context: str
    why: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.why}\n    ...{self.context}..."


def private_price() -> int:
    env = os.environ.get("CALLKETTLE_PRIVATE_PRICE")
    if env:
        return int(float(env))
    return int(float(json.loads((HERE / "facts.json").read_text(encoding="utf-8"))["price_monthly"]))


def _price_patterns(price: int) -> list[tuple[re.Pattern, str]]:
    p = re.escape(str(price))
    num = rf"(?<![\d,.$]){p}(?:\.00)?(?![\d,]|\.\d)"
    return [
        (re.compile(rf"\$\s?{p}(?:\.00)?(?![\d,]|\.\d)"), "the exact monthly price with a dollar sign"),
        (re.compile(rf"{num}\s*(?:/|per|a|each|every)\s*(?:mo\b|month)", re.I), "the exact monthly price followed by a per-month phrase"),
        (re.compile(rf"(?:price|pricing|plan|cost|fee|rate|subscription|billed|charge|usd|monthly)\b[^\n]{{0,30}}?{num}", re.I), "the exact monthly price near a price word"),
        (re.compile(rf"{num}[^\n]{{0,30}}?\b(?:price|pricing|subscription|monthly|usd|dollars)\b", re.I), "the exact monthly price near a price word"),
    ]


GENERIC_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\{\{\s*PRICE\s*\}\}|\$\{\{PRICE\}\}|price_monthly|PRICE_MONTHLY|facts\[[\"']PRICE", re.I), "a template token or key that would render the private price"),
    (re.compile(r"\"priceRange\"|\"price\"\s*:|\"lowPrice\"|\"highPrice\"|priceCurrency|\"@type\"\s*:\s*\"(?:Offer|AggregateOffer)\"|itemprop=[\"']price|data-price|og:price|product:price", re.I),
     "price/Offer structured data or a price attribute"),
    (re.compile(r"\b(?:starting|starts|starting at|starts at|from)\s+(?:at\s+)?\$\s?\d", re.I), "advertised 'starting at' pricing"),
]


def _text_of(path: Path) -> str | None:
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader

            return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
        except Exception:
            return None
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return None
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def scan_text(text: str, price: int, label: str) -> list[Finding]:
    out = []
    pats = _price_patterns(price) + GENERIC_RULES
    for i, line in enumerate(text.splitlines(), 1):
        for pat, why in pats:
            m = pat.search(line)
            if m:
                out.append(Finding(label, i, line[max(0, m.start() - 50): m.end() + 40].strip(), why))
                break
    return out


def public_files(root: Path = REPO, globs: list[str] | None = None) -> list[Path]:
    seen, out = set(), []
    for g in globs or PUBLIC_GLOBS:
        for p in sorted(root.glob(g)):
            if p.is_file() and not (set(p.parts) & SKIP_PARTS) and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def scan(root: Path = REPO, price: int | None = None, globs: list[str] | None = None) -> tuple[list[Finding], int, list[str]]:
    """(findings, files scanned, files that could not be read as text, e.g. an unreadable PDF)."""
    price = price if price is not None else private_price()
    findings, scanned, unreadable = [], 0, []
    for f in public_files(root, globs):
        text = _text_of(f)
        if text is None:
            if f.suffix.lower() in {".pdf"}:
                unreadable.append(str(f.relative_to(root)))
            continue
        scanned += 1
        findings += scan_text(text, price, str(f.relative_to(root)).replace("\\", "/"))
    return findings, scanned, unreadable


# Intentional internal occurrences (allowed). Reported for the record, never a failure.
INTERNAL_GLOBS = [
    "marketing/facts.json", "marketing/economics.py", "marketing/discovery_calc.py", "marketing/practice.py", "marketing/build.py", "marketing/audit.py",
    "marketing/proposals/**/*", "marketing/templates/*.html", "marketing/service-agreement.html", "marketing/field-playbook.html", "marketing/cheat-sheet.html",
    "marketing/START-HERE.html", "marketing/business-plan.html", "marketing/what-youre-selling.html", "marketing/prospect-log.html",
    "marketing/owner_kit/0[4-9]*.md", "marketing/owner_kit/10*.md", "marketing/START_HERE.md", "docs/**/*", "*.md", "backend/tests/**/*", "backend/scripts/**/*",
]


def internal_inventory(root: Path = REPO, price: int | None = None) -> dict[str, int]:
    price = price if price is not None else private_price()
    public = {p for p in public_files(root)}
    counts: dict[str, int] = {}
    for g in INTERNAL_GLOBS:
        for p in sorted(root.glob(g)):
            if not p.is_file() or p in public or (set(p.parts) & SKIP_PARTS):
                continue
            text = _text_of(p)
            if text:
                n = len(scan_text(text, price, "x"))
                if n:
                    counts[str(p.relative_to(root)).replace("\\", "/")] = n
    return counts


def main() -> int:
    price = private_price()
    findings, scanned, unreadable = scan(REPO, price)
    print("=" * 72)
    print(f"PRICING-LEAK AUDIT: {scanned} public / pre-demo files scanned for the configured private price (and price structured data)")
    print("=" * 72)
    for u in unreadable:
        print(f"WARNING: could not read {u} as text; it was built from audited HTML but is not itself verified")
    if "--inventory" in sys.argv:
        inv = internal_inventory(REPO, price)
        print(f"\nINTENTIONAL INTERNAL OCCURRENCES (allowed): {sum(inv.values())} in {len(inv)} files")
        for f, n in inv.items():
            print(f"  {n:>3}  {f}")
    if findings:
        print(f"\nFAIL: {len(findings)} LEAK(S). The exact price is forbidden on public and pre-demo surfaces (owner strategy: price is stated in conversation after the demo).\n")
        for f in findings:
            print(" -", f)
        return 1
    print("\nPASS: the configured price does not appear on any public or pre-demo surface.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
