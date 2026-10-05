"""Customer-facing account and setup contract, exercised on real build output."""
from __future__ import annotations

import importlib.util
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest


class Links(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.stack = []
        self.links = []
        self.current = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.current = {"href": attrs.get("href"), "text": "", "ancestors": tuple(self.stack), "class": attrs.get("class", "")}
        if tag not in {"meta", "link", "input", "br", "img", "source", "hr"}:
            self.stack.append(tag)

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.links.append(self.current)
            self.current = None
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    source = Path(__file__).parent / "site-src" / "build_site.py"
    spec = importlib.util.spec_from_file_location("frontdesk_site", source)
    site = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(site)
    site.OUT = tmp_path_factory.mktemp("frontdesk-site")
    site.main()  # Includes real assets, metadata and placeholder verification; never writes marketing/site.
    return site, {rel: (site.OUT / rel).read_text(encoding="utf-8") for rel in site.PAGES}


@pytest.mark.parametrize("rel", ["index.html", "how-it-works/index.html", "pricing/index.html", "hvac/index.html", "garage-door/index.html", "plumbing/index.html", "electrical/index.html", "faq/index.html", "privacy/index.html", "404.html"])
def test_owner_sign_in_in_header_menu_and_footer(generated, rel):
    site, pages = generated
    links = Links(pages[rel]).links
    owner = [a for a in links if a["text"] == "Owner sign in"]
    assert all(a["href"] == site.F["CLIENT_PORTAL_URL"] for a in owner)
    assert any("header" in a["ancestors"] and "nav" not in a["ancestors"] for a in owner), "Mobile account access must not require opening Menu"
    assert any("nav" in a["ancestors"] for a in owner)
    assert any("footer" in a["ancestors"] for a in owner)
    assert "Client login" not in pages[rel]


def test_public_site_does_not_limit_service_to_northern_virginia(generated):
    _, pages = generated
    for rel, text in pages.items():
        assert "northern virginia" not in text.lower(), rel
    source = Path(__file__).parent / "site-src" / "make_og.py"
    assert "Northern Virginia" not in source.read_text(encoding="utf-8")


def test_account_links_have_responsive_touch_targets(generated):
    _, pages = generated
    home = pages["index.html"]
    assert ".account-link" in home
    assert "min-height:44px" in home
    assert "@media(max-width:1180px)" in home, "Expanded navigation must not overflow tablet/desktop widths"


@pytest.mark.parametrize("rel", ["index.html", "how-it-works/index.html", "pricing/index.html", "hvac/index.html", "faq/index.html", "privacy/index.html", "404.html"])
def test_setup_request_links_to_real_intake_without_instant_activation(generated, rel):
    site, pages = generated
    text = pages[rel]
    links = Links(text).links
    setup = [a for a in links if a["text"] == "Set up your front desk"]
    expected = site.F.get("app_url", "https://app.callkettle.com").rstrip("/") + "/start"
    assert setup, "New owners need a setup path separate from sign-in"
    assert all(a["href"] == expected for a in setup)
    assert any("nav" in a["ancestors"] for a in setup)
    assert any("footer" in a["ancestors"] for a in setup)
    assert "setup request, not instant account creation or service activation" in text
    assert "temporary password" in text
    assert "Nothing goes live until" in text
    assert "Create account" not in text and "Activate now" not in text


@pytest.mark.parametrize("rel", ["index.html", "how-it-works/index.html", "pricing/index.html"])
def test_managed_front_desk_explains_recorded_work_and_owner_action(generated, rel):
    _, pages = generated
    section = re.search(r'<section[^>]*id="managed-front-desk"[^>]*>(.*?)</section>', pages[rel], re.S)
    assert section, "Core decision pages must explain the front desk beyond call answering"
    text = section.group(1)
    for phrase in ["Bookings", "Messages", "Needs your attention", "Call back", "Mark handled", "Owner view", "Managed setup", "read-only", "Set up your front desk"]:
        assert phrase in text
    assert "your team" in text
    assert "not a promise that every caller will book" in text
    assert "setup request" in text
    assert "instant" in text
    assert "SMS" not in text and "human backup" not in text and "SLA" not in text


def test_public_output_preserves_price_brand_and_claim_audit_rules(generated):
    site, pages = generated
    import pricing_audit

    for rel, text in pages.items():
        assert not pricing_audit.scan_text(text, pricing_audit.private_price(), rel), rel
        assert not re.search(r"@@[A-Z0-9_]+@@", text), rel
        assert "deskline" not in text.lower(), rel
        assert not re.search(r"\$\s*\d|priceRange|\"@type\"\s*:\s*\"Offer\"", text), rel
        assert "one business day" not in text.lower(), rel
        assert "registration for texts is in progress" not in text.lower(), rel
        assert "ours is in progress" not in text.lower(), rel
    assert (site.OUT / "sitemap.xml").is_file()
    assert (site.OUT / "assets" / "call-demo.js").is_file()


def test_plans_renders_labeled_product_preview_instead_of_template_code(generated):
    _, pages = generated
    text = pages["pricing/index.html"]
    assert "{product_stage()}" not in text
    assert "Synthetic preview with sample data" in text


# ---------------------------------------------------------------- QA dogfood 2026-10-04 regressions
INTERNAL_PHRASES = ["make the plan feel worth it", "what the customer is paying for", "product_stage", "lorem ipsum", "todo", "fixme", "seller", "talk track"]


def template_remnants(page_html: str) -> list[str]:
    """Visible-text template leftovers such as {product_stage()} or {{x}}: scripts/styles legitimately contain braces."""
    visible = re.sub(r"<(script|style)\b.*?</\1>", "", page_html, flags=re.S | re.I)
    return re.findall(r"\{[^{}]*\}|\{\{|\}\}", visible)


def test_remnant_detector_catches_the_bug_that_shipped():
    assert template_remnants("<div>{product_stage()}</div>") == ["{product_stage()}"]
    assert template_remnants("<style>a{color:red}</style><script>var a={};</script><p>fine</p>") == []


def test_no_public_page_has_template_remnants_or_internal_coaching_copy(generated):
    _, pages = generated
    for rel, text in pages.items():
        assert template_remnants(text) == [], rel
        visible = re.sub(r"<(script|style)\b.*?</\1>", "", text, flags=re.S | re.I).lower()
        for phrase in INTERNAL_PHRASES:
            assert phrase not in visible, (rel, phrase)


TERMS_LINE = "No setup fee. Month-to-month. Cancel at any time, no cancellation fee."


def test_terms_line_matches_the_hosted_terms_exactly():
    terms = (Path(__file__).parent.parent / "backend" / "app" / "static" / "terms.html").read_text(encoding="utf-8")
    for fact in ["No setup fee.", "Month-to-month service", "cancel at any time", "There is no cancellation fee"]:
        assert fact in terms, fact


@pytest.mark.parametrize("rel", ["index.html", "pricing/index.html"])
def test_offer_states_terms_and_offers_a_one_tap_way_to_ask_for_the_price(generated, rel):
    site, pages = generated
    text = pages[rel]
    assert TERMS_LINE in text
    links = Links(text).links
    ask = [a for a in links if a["text"].strip() == "Ask for the price"]
    demo = [a for a in links if a["text"].strip() == "Get pricing on a 15-minute demo"]
    assert ask and all(a["href"].startswith("mailto:" + site.F["EMAIL"] + "?subject=") for a in ask)
    assert demo and all(a["href"] == site.F["BOOK_URL"] for a in demo)


def test_carrier_backup_is_not_promised_unconditionally(generated):
    _, pages = generated
    for rel, text in pages.items():
        assert "the carrier's backup rings your phone" not in text, rel
        assert "carrier's backup, which runs on separate infrastructure, rings your phone" not in text, rel
        assert "carrier's backup, on separate infrastructure, rings your phone" not in text, rel
    assert "depends on your carrier" in pages["index.html"]
    assert "depends on your carrier" in pages["how-it-works/index.html"]


def test_invoca_statistic_keeps_a_checkable_source_link(generated):
    _, pages = generated
    assert "https://www.invoca.com/reports/the-invoca-home-services-lead-conversion-benchmarks-report-2026" in pages["index.html"]


def test_pages_run_no_inline_script_or_event_handlers_so_a_strict_csp_works(generated):
    _, pages = generated
    for rel, text in pages.items():
        for m in re.finditer(r"<script\b([^>]*)>", text, flags=re.I):
            attrs = m.group(1)
            assert "src=" in attrs or "application/ld+json" in attrs or "application/json" in attrs, (rel, attrs)
        assert not re.search(r"\son[a-z]+\s*=", text, flags=re.I), rel
        assert "javascript:" not in text.lower(), rel


def test_vercel_config_has_safe_cache_csp_and_www_redirect(generated):
    import json

    site, _ = generated
    cfg = json.loads((site.OUT / "vercel.json").read_text(encoding="utf-8"))
    headers = {h["key"]: h["value"] for rule in cfg["headers"] for h in rule["headers"] if rule["source"] == "/(.*)"}
    asset_rule = next(r for r in cfg["headers"] if r["source"].startswith("/assets/"))
    cache = asset_rule["headers"][0]["value"]
    assert "immutable" not in cache and "must-revalidate" in cache and "max-age=31536000" not in cache
    csp = headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "base-uri 'none'" in csp
    script_src = re.search(r"script-src ([^;]*)", csp).group(1)
    assert "unsafe-inline" not in script_src and "unsafe-eval" not in script_src
    assert headers["Referrer-Policy"] and headers["X-Frame-Options"] == "DENY" and "camera=()" in headers["Permissions-Policy"]
    www = [r for r in cfg["redirects"] if any(h.get("value") == "www." + site.DOMAIN for h in r.get("has", []))]
    assert www and www[0]["destination"] == site.SITE + "/$1" and www[0]["permanent"] is True


def test_css_and_js_references_are_versioned_by_content(generated):
    site, pages = generated
    for rel, text in pages.items():
        assert re.search(r'href="/assets/style\.css\?v=[0-9a-f]{8,}"', text), rel
        assert re.search(r'src="/assets/call-demo\.js\?v=[0-9a-f]{8,}"', text), rel


def test_favicon_ico_and_share_metadata(generated):
    site, pages = generated
    ico = (site.OUT / "favicon.ico").read_bytes()
    assert ico[:4] == b"\x00\x00\x01\x00"
    for rel, text in pages.items():
        assert 'href="/favicon.ico"' in text, rel
        for needle in ['property="og:site_name"', 'property="og:image:alt"', 'name="twitter:title"', 'name="twitter:image"', 'name="description"']:
            assert needle in text, (rel, needle)


def test_top_bar_and_footer_links_have_44px_tap_targets():
    css = (Path(__file__).parent / "site-src" / "assets" / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\.callbar a\{[^}]*min-height:44px", css)
    assert re.search(r"\.cu-tab\{[^}]*min-height:44px", css) or re.search(r"\.cu-tab\{[^}]*padding:1[0-9]px", css)
