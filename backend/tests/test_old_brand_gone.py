"""The old product name must not appear anywhere a customer, prospect or visitor can see it (site, flyers, hosted pages, demo configs, app text, cold-call scripts).
What remains is infrastructure identity only, listed explicitly below: the calendar-invite UID suffix (calendars match updates by it), the secret push-notification
topic strings (renaming means re-subscribing a phone), the migration code that carries old data forward, and the legacy-name constants."""
import importlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "marketing"))
pa = importlib.import_module("pricing_audit")

OLD = re.compile("desk" + r"\s?line", re.I)
ALLOWED = {
    "backend/app/brand.py": ["ICS_UID_SUFFIX", "calendar-invite UID suffix"],
    "backend/app/storage.py": ["LEGACY_", "desk\" + \"line", "old product name"],
    "backend/app/__init__.py": ["DESK\" + \"LINE", "old product prefix"],
    "backend/app/ops.py": ["desk\" + \"line"],
}
TOPIC = re.compile(r"ntfy_topic:\s*\"?" + "desk" + "line-")


def test_no_customer_visible_surface_carries_the_old_name():
    offenders = []
    for f in pa.public_files(ROOT):
        text = pa._text_of(f)
        if not text:
            continue
        rel = str(f.relative_to(ROOT)).replace("\\", "/")
        for i, line in enumerate(text.splitlines(), 1):
            if OLD.search(line) and not TOPIC.search(line) and not any(a in line for a in ALLOWED.get(rel, [])):
                offenders.append(f"{rel}:{i}: {line.strip()[:100]}")
    assert not offenders, "\n".join(offenders)


def test_the_public_site_title_and_canonical_urls_use_the_new_brand():
    html = (ROOT / "marketing" / "site" / "index.html").read_text(encoding="utf-8")
    assert "<title>Call Kettle" in html and "https://callkettle.com" in html and not OLD.search(html)
    assert not re.search(r"fly\.dev", html)                         # customers never see the hosting hostname
