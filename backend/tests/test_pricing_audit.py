"""The pricing-leak audit: config-driven (not 'block one number forever'), reports file and line, tolerates unrelated numbers, and the real repository is clean.

All prices in this file are made-up numbers; the real private price is never stored in the repository."""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "marketing"))
pa = importlib.import_module("pricing_audit")

FAKE_PRICE = 1234
OTHER_PRICE = 777


def _scan_text(text, price=FAKE_PRICE):
    return pa.scan_text(text, price, "x.html")


def test_the_real_repository_has_no_public_price(monkeypatch):
    monkeypatch.setenv("CALLKETTLE_PRIVATE_PRICE", str(FAKE_PRICE))
    findings, scanned, _ = pa.scan(ROOT)
    assert scanned > 50
    assert findings == [], "\n".join(str(f) for f in findings)


def test_it_catches_the_price_in_every_form_a_page_could_carry_it():
    for price in (OTHER_PRICE, FAKE_PRICE):                                # any configured price is caught
        for line in (f"<p>${price}/month</p>", f"<b>$ {price} a month</b>", f"Plans start at {price} per month", f'<meta name="description" content="Only ${price}.00">',
                     f"The monthly price is {price} dollars", f"pricing: {price}", '{"@type":"Offer","price":"%d"}' % price, f'og:price:amount" content="{price}"'):
            assert _scan_text(line, price), (price, line)
    for line in ('"priceRange": "$$"', "starting at $49", "<span>${{PRICE}}</span>", "price_monthly"):
        assert _scan_text(line), line


def test_it_reports_the_file_the_line_and_why():
    f = pa.scan_text(f"ok line\nsecond line\nOur plan is ${FAKE_PRICE} a month\n", FAKE_PRICE, "marketing/site/index.html")[0]
    assert (f.file, f.line) == ("marketing/site/index.html", 3) and "exact monthly price" in f.why and f"${FAKE_PRICE}" in f.context
    assert "marketing/site/index.html:3" in str(f)


def test_unrelated_numbers_are_not_flagged():
    for line in (f"Trusted by {FAKE_PRICE} reviews on Google", "Since 1975, 29700 jobs", "Call+15555550100", f"Invoice #{FAKE_PRICE}", f"Room 2{FAKE_PRICE} and $1,{FAKE_PRICE} deposit",
                 f"${FAKE_PRICE}0"):
        assert not _scan_text(line), line


def test_the_audit_follows_the_configured_price_not_one_hardcoded_number():
    assert not _scan_text(f"The plan is ${FAKE_PRICE} a month", price=OTHER_PRICE)   # a different price is configured in this scenario
    assert _scan_text(f"The plan is ${OTHER_PRICE} a month", price=OTHER_PRICE)
    assert not _scan_text(f"Trusted by {FAKE_PRICE} reviews on Google", price=OTHER_PRICE)
    assert not _scan_text(f"${OTHER_PRICE}9/month", price=OTHER_PRICE)


def test_environment_override_wins_over_facts_json(monkeypatch):
    monkeypatch.setenv("CALLKETTLE_PRIVATE_PRICE", "411")
    assert pa.private_price() == 411


def test_a_planted_leak_in_a_built_page_and_in_json_ld_fails_the_scan(tmp_path):
    (tmp_path / "marketing" / "site").mkdir(parents=True)
    (tmp_path / "marketing" / "site" / "index.html").write_text(
        '<html><script type="application/ld+json">{"@type":"Offer","price":"%d"}</script><p>hi</p></html>' % FAKE_PRICE, encoding="utf-8")
    (tmp_path / "marketing" / "site" / "ok.html").write_text("<p>Call the live demo</p>", encoding="utf-8")
    findings, scanned, _ = pa.scan(tmp_path, FAKE_PRICE)
    assert scanned == 2 and [f.file for f in findings] == ["marketing/site/index.html"]


def test_internal_files_are_inventoried_not_failed(tmp_path):
    (tmp_path / "marketing" / "proposals").mkdir(parents=True)
    (tmp_path / "marketing" / "proposals" / "q.md").write_text(f"The plan is ${FAKE_PRICE} a month", encoding="utf-8")
    assert pa.scan(tmp_path, FAKE_PRICE)[0] == []
    assert pa.internal_inventory(tmp_path, FAKE_PRICE) == {"marketing/proposals/q.md": 1}


def test_the_public_demo_ai_and_hosted_terms_do_not_state_a_price():
    import re

    from app.config import load_client_config

    for cid in ("demo_riverside", "callkettle_demo", "demo_nova_hvac", "demo_nova_garage", "demo_nova_plumbing"):
        cfg = load_client_config(cid)
        spoken = cfg.extra_instructions + cfg.opening_line + " ".join(f.a for f in cfg.faqs)
        assert not re.search(r"\$\s?\d", spoken), cid
    terms = (ROOT / "backend" / "app" / "static" / "terms.html").read_text(encoding="utf-8")
    assert not re.search(r"\$\s?\d", terms) and "Monthly fee" in terms
