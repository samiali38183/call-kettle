"""Renaming the business is configuration, not a code change (docs/BRAND_CLEARANCE.md)."""
from datetime import datetime

import pytest

NEW = "Northline Desk"


def _cfg():
    from app.config import load_client_config

    return load_client_config("demo_hvac")


def test_defaults_are_unchanged(app_client):
    client, main = app_client
    terms = client.get("/terms").text
    assert "Call Kettle Terms of Service" in terms and "Sami Ali, doing business as Call Kettle" in terms
    assert "{{" not in terms and "{{" not in client.get("/start").text and "{{" not in client.get("/book").text


def test_a_rename_reaches_every_public_page(app_client, monkeypatch):
    client, main = app_client
    monkeypatch.setenv("BRAND_NAME", NEW)
    monkeypatch.setenv("BRAND_SUPPORT_EMAIL", "owner@example.com")
    monkeypatch.setenv("SUPPORT_PHONE", "(555) 010-0000")
    for path in ("/terms", "/start", "/book"):
        text = client.get(path).text
        assert NEW in text and "Call Kettle" not in text and "CALL KETTLE" not in text, path
    terms = client.get("/terms").text
    assert f"Sami Ali, doing business as {NEW} (owner@example.com, (555) 010-0000)" in terms


def test_the_legal_entity_can_be_set_separately(app_client, monkeypatch):
    client, main = app_client
    monkeypatch.setenv("BRAND_NAME", NEW)
    monkeypatch.setenv("BRAND_LEGAL_ENTITY", "Northline Desk LLC, a Virginia limited liability company")
    assert "Provider\" is Northline Desk LLC, a Virginia limited liability company" in client.get("/terms").text


def test_a_hostile_brand_name_cannot_inject_html(app_client, monkeypatch):
    client, main = app_client
    monkeypatch.setenv("BRAND_NAME", '<script>alert(1)</script> & "Co"')
    for path in ("/terms", "/start", "/book"):
        assert "<script>alert(1)" not in client.get(path).text
    client.post("/admin/login", data={"key": "master_key_for_tests"})
    assert "<script>alert(1)" not in client.get("/admin").text


def test_the_admin_page_uses_the_brand(app_client, monkeypatch):
    client, main = app_client
    monkeypatch.setenv("BRAND_NAME", NEW)
    client.post("/admin/login", data={"key": "master_key_for_tests"})
    page = client.get("/admin").text
    # (the client list below may still show a client whose own configured business name is "Call Kettle": that is data)
    assert f"<title>{NEW} admin</title>" in page and f"<h1>{NEW} clients</h1>" in page and "<title>Call Kettle" not in page


def test_the_weekly_recap_uses_the_brand_and_contact(monkeypatch):
    from app import digest

    monkeypatch.setenv("BRAND_NAME", NEW)
    monkeypatch.setenv("BRAND_CONTACT_NAME", "Dana")
    monkeypatch.setenv("SUPPORT_PHONE", "(555) 010-0000")
    stats = {"calls": 3, "booked": 1, "after_hours": 0, "callbacks": 0, "transferred": 0}
    subject, body = digest.compose(_cfg(), stats, datetime(2026, 1, 5), datetime(2026, 1, 12), [])
    assert f"Your week with {NEW}" in subject
    assert "text Dana at (555) 010-0000" in body and body.rstrip().endswith(NEW) and "Call Kettle" not in body + subject


def test_calendar_invites_carry_the_new_name_but_keep_a_stable_uid_suffix(monkeypatch):
    from app import notify

    monkeypatch.setenv("BRAND_NAME", 'Northline, "Desk"')
    ics = notify.build_ics(config=_cfg(), caller_name="Pat", caller_phone="+15555550100", service="Repair",
                           start=datetime(2026, 1, 12, 10), end=datetime(2026, 1, 12, 11), organizer="bookings@example.com", uid="abc123")
    assert "PRODID:-//Northline, \"Desk\"//Bookings//EN" in ics or "PRODID:-//Northline, " in ics
    assert 'ORGANIZER;CN="Northline, Desk":mailto:bookings@example.com' in ics        # quotes stripped, comma safe inside a quoted param
    assert "UID:abc123@deskline-ai" in ics                                             # identity of existing events must not change


def test_fallback_organizer_follows_the_brand(monkeypatch):
    from app import brand

    monkeypatch.setenv("BRAND_NAME", "Northline Desk")
    assert brand.get().fallback_sender == "owner@example.com"
    monkeypatch.setenv("BRAND_NAME", "!!!")
    assert brand.get().fallback_sender == "owner@example.com"


def test_blank_and_oversized_values_fall_back_safely(monkeypatch):
    from app import brand

    monkeypatch.setenv("BRAND_NAME", "   ")
    assert brand.get().name == "Call Kettle"
    monkeypatch.setenv("BRAND_NAME", "x" * 500)
    assert len(brand.get().name) == 60


def test_no_runtime_code_hardcodes_the_current_brand():
    """Static guard: a rename must not need code edits. Allowed: the defaults inside brand.py, comments, logger names,
    file names and the client config ids."""
    import re
    from pathlib import Path

    offenders = []
    for path in sorted((Path(__file__).resolve().parents[1] / "app").rglob("*")):
        if path.suffix not in {".py", ".html"} or path.name == "brand.py":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0] if path.suffix == ".py" else line
            if re.search(r"Call Kettle|CALL KETTLE|Call Kettle admin|Call Kettle clients|Your week with Call Kettle", code):
                offenders.append(f"{path.name}:{n}: {line.strip()[:90]}")
    assert not offenders, offenders


def test_outgoing_email_shows_the_brand_as_the_sender_name(monkeypatch):
    import smtplib

    from app import notify

    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "me@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "x")
    monkeypatch.setenv("SMTP_FROM", "bookings@example.com")
    monkeypatch.setenv("BRAND_NAME", "Northline Desk")
    assert notify._send_email("owner@example.com", "hi", "body") is True
    assert sent[0]["From"] == "Northline Desk <bookings@example.com>"
    monkeypatch.setenv("SMTP_FROM", "Someone Else <bookings@example.com>")       # an explicit display name is respected
    notify._send_email("owner@example.com", "hi", "body")
    assert sent[1]["From"] == "Someone Else <bookings@example.com>"
