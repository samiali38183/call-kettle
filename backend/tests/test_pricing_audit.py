"""The pricing-leak audit: config-driven (not 'block 297 forever'), reports file and line, tolerates unrelated numbers, and the real repository is clean."""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "marketing"))
pa = importlib.import_module("pricing_audit")


def _scan_text(text, price=497):
    return pa.scan_text(text, price, "x.html")


def test_the_real_repository_has_no_public_price():
    findings, scanned, _ = pa.scan(ROOT)
    assert scanned > 50
    assert findings == [], "\n".join(str(f) for f in findings)


def test_it_catches_the_price_in_every_form_a_page_could_carry_it():
    for price in (297, 497):                                               # the retired price and the approved one are both caught when configured
        for line in (f"<p>${price}/month</p>", f"<b>$ {price} a month</b>", f"Plans start at {price} per month", f'<meta name="description" content="Only ${price}.00">',
                     f"The monthly price is {price} dollars", f"pricing: {price}", '{"@type":"Offer","price":"%d"}' % price, f'og:price:amount" content="{price}"'):
            assert _scan_text(line, price), (price, line)
    for line in ('"priceRange": "$$"', "starting at $49", "<span>${{PRICE}}</span>", "price_monthly"):
        assert _scan_text(line), line


def test_it_reports_the_file_the_line_and_why():
    f = pa.scan_text("ok line\nsecond line\nOur plan is $••• a month\n", 497, "marketing/site/index.html")[0]
    assert (f.file, f.line) == ("marketing/site/index.html", 3) and "exact monthly price" in f.why and "$•••" in f.context
    assert "marketing/site/index.html:3" in str(f)


def test_unrelated_numbers_are_not_flagged():
    for line in ("Trusted by 297 reviews on Google", "Since 1975, 2970 jobs", "Call+15555550100", "Invoice #297", "Room 2297 and $1,297 deposit", "$•••0"):
        assert not _scan_text(line), line


def test_the_audit_follows_the_configured_price_not_the_number_297():
    assert not _scan_text("The plan is $••• a month", price=349)          # 297 is not the private price in this scenario
    assert not _scan_text("The plan is $••• a month", price=349)
    assert not _scan_text("The plan is $••• a month")                      # the retired $••• is no longer the configured price
    assert not _scan_text("Trusted by 497 reviews on Google") and not _scan_text("$•••0")
    assert _scan_text("The plan is $349 a month", price=349)
    assert not _scan_text("$3497/month", price=349)


def test_environment_override_wins_over_facts_json(monkeypatch):
    monkeypatch.setenv("CALLKETTLE_PRIVATE_PRICE", "411")
    assert pa.private_price() == 411
    monkeypatch.delenv("CALLKETTLE_PRIVATE_PRICE")
    assert pa.private_price() == int(__import__("json").loads((ROOT / "marketing" / "facts.json").read_text())["price_monthly"])


def test_a_planted_leak_in_a_built_page_and_in_json_ld_fails_the_scan(tmp_path):
    (tmp_path / "marketing" / "site").mkdir(parents=True)
    (tmp_path / "marketing" / "site" / "index.html").write_text('<html><script type="application/ld+json">{"@type":"Offer","price":"497"}</script><p>hi</p></html>', encoding="utf-8")
    (tmp_path / "marketing" / "site" / "ok.html").write_text("<p>Call the live demo</p>", encoding="utf-8")
    findings, scanned, _ = pa.scan(tmp_path, 497)
    assert scanned == 2 and [f.file for f in findings] == ["marketing/site/index.html"]


def test_internal_files_are_inventoried_not_failed(tmp_path):
    (tmp_path / "marketing" / "proposals").mkdir(parents=True)
    (tmp_path / "marketing" / "proposals" / "q.md").write_text("The plan is $••• a month", encoding="utf-8")
    assert pa.scan(tmp_path, 497)[0] == []
    assert pa.internal_inventory(tmp_path, 497) == {"marketing/proposals/q.md": 1}


def test_the_public_demo_ai_and_hosted_terms_do_not_state_the_price():
    from app.config import load_client_config

    for cid in ("demo_riverside", "callkettle_demo", "demo_nova_hvac", "demo_nova_garage", "demo_nova_plumbing"):
        cfg = load_client_config(cid)
        spoken = cfg.extra_instructions + cfg.opening_line + " ".join(f.a for f in cfg.faqs)
        assert "297" not in spoken and "497" not in spoken, cid
    terms = (ROOT / "backend" / "app" / "static" / "terms.html").read_text(encoding="utf-8")
    assert "297" not in terms and "497" not in terms and "Monthly fee" in terms
