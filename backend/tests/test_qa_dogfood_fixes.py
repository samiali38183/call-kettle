"""QA dogfood 2026-10-04 regressions for the app host: /book, /start, /terms, security headers, favicon."""
import base64
import hashlib
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
ROOT = Path(__file__).resolve().parents[2]


def _future_sunday():
    d = date.today() + timedelta(days=8)
    while d.weekday() != 6:
        d += timedelta(days=1)
    return d


def test_next_open_skips_a_closed_sunday_to_monday(app_client, monkeypatch):
    client, main = app_client
    sunday = _future_sunday()
    monkeypatch.setattr(main, "_business_today", lambda config: sunday)
    body = client.get("/book/next-open").json()
    assert body["date"] == (sunday + timedelta(days=1)).isoformat()
    assert body["timezone"] == "America/New_York"


def test_next_open_keeps_today_when_open(app_client, monkeypatch):
    client, main = app_client
    monday = _future_sunday() + timedelta(days=1)
    monkeypatch.setattr(main, "_business_today", lambda config: monday)
    assert client.get("/book/next-open").json()["date"] == monday.isoformat()


def test_next_open_falls_back_to_today_when_nothing_is_open(app_client, monkeypatch):
    client, main = app_client
    sunday = _future_sunday()
    monkeypatch.setattr(main, "_business_today", lambda config: sunday)
    monkeypatch.setattr(main.tools, "check_availability", lambda **kw: {"slots": []})
    assert client.get("/book/next-open").json()["date"] == sunday.isoformat()


def test_book_page_promises_what_the_site_promises_and_links_back(app_client):
    client, _ = app_client
    page = client.get("/book").text
    assert "15-minute demo" in page
    assert "30 min consultation" not in page.lower()
    assert "30-minute slot" in page                      # the calendar really holds 30 minutes: say so
    assert 'href="https://callkettle.com"' in page      # brand and a way back
    assert "Back to callkettle.com" in page
    assert "Eastern Time" in page
    assert 'href="tel:' in page and 'href="mailto:' in page
    assert "/book/next-open" in page                    # default date is the next open day, not a closed Sunday


def test_booking_slot_really_is_thirty_minutes():
    """The page text above is only honest while the sales calendar reserves 30 minutes."""
    import yaml
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "clients" / "callkettle_sales.yaml").read_text(encoding="utf-8"))
    assert cfg["slot_minutes"] == 30 and cfg["services"][0]["duration_minutes"] == 30


@pytest.mark.parametrize("path", ["/book", "/start", "/terms"])
def test_app_pages_refuse_framing_and_carry_a_strict_csp(app_client, path):
    client, _ = app_client
    r = client.get(path)
    assert r.headers["x-frame-options"] == "DENY"
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "base-uri 'none'" in csp and "default-src 'none'" in csp
    script_src = re.search(r"script-src ([^;]*)", csp).group(1)
    assert "unsafe-inline" not in script_src and "unsafe-eval" not in script_src
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", r.text, flags=re.S | re.I)
    for body in inline:  # every inline script is allowed by hash, nothing else runs
        digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
        assert f"'sha256-{digest}'" in script_src, path
    if not inline:
        assert script_src.strip() == "'none'"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_favicon_is_served_so_pages_do_not_404_in_the_console(app_client):
    client, _ = app_client
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.content[:4] == b"\x00\x00\x01\x00"
    assert r.headers["content-type"].startswith("image/")


def test_start_page_hours_rows_fit_a_320px_phone():
    css = (STATIC / "start.html").read_text(encoding="utf-8")
    assert "grid-template-columns:64px 1fr 1fr auto" not in css       # the row that forced 530px
    assert re.search(r"input\[type=time\][^}]*min-width:0|\.day input\{[^}]*min-width:0", css)
    assert "overflow-x:hidden" not in css                             # fixed by layout, not by hiding the overflow
    assert re.search(r"@media \(max-width:(4[0-9]{2}|5[0-9]{2})px\)\{[^}]*\.day", css)


def test_terms_recording_wording_is_consistent_with_what_the_code_does():
    """No audio recording anywhere in app code; the spoken notice is a precaution. Say exactly that, in every copy of the terms."""
    for path in (STATIC / "terms.html", ROOT / "marketing" / "service-agreement.html", ROOT / "marketing" / "templates" / "agreement.html"):
        text = path.read_text(encoding="utf-8")
        assert "Provider does not record or store call audio" in text, path
        assert "precaution" in text, path
        assert "may be recorded and monitored" in text, path


def test_no_call_audio_recording_exists_in_app_code():
    for py in (Path(__file__).resolve().parent.parent / "app").glob("*.py"):
        src = py.read_text(encoding="utf-8")
        assert not re.search(r"<Record\b|\.recordings|record=True|recording_status_callback|RecordingUrl", src), py.name
