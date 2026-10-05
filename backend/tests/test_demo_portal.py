"""SHOW-ME owner portal for sales demos (scripts/seed_demo_portal.py + portal_sample): seeding is idempotent, the sample banner is on every
page, the sample portal is read-only, and sample data and real clients' data never cross. Offline: temp SQLite, no network."""
import importlib.util
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "seed_demo_portal.py"
SAMPLE = "sample_portal_hvac"
REAL = "demo_hvac"                       # stands in for a real customer: a normal client without portal_sample
REAL_EMAIL, REAL_PW = "owner@example.com", "correct horse battery 42"
BANNER = "Sample business - demo data"


def _load_script():
    spec = importlib.util.spec_from_file_location("seed_demo_portal", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def _login(browser: TestClient, email: str, password: str):
    page = browser.get("/portal/login")
    return browser.post("/portal/login", data={"email": email, "password": password, "csrf": _csrf(page.text)}, follow_redirects=False)


@pytest.fixture
def env(app_client):
    client, main = app_client
    from app import owner_auth, storage

    seeder = _load_script()
    conn = sqlite3.connect(storage.DB_PATH)
    conn.execute("INSERT INTO calls (call_sid, client_id, from_number, started_at, outcome, summary, transcript_json, outcome_class, needs_attention) "
                 "VALUES ('CA_REAL_1', ?, '+15555550100', ?, 'completed', 'REAL-CUSTOMER-SECRET furnace call', '[]', 'CALLBACK_REQUESTED', 1)",
                 (REAL, datetime.now().astimezone().isoformat()))
    conn.execute("INSERT INTO bookings (call_sid, client_id, caller_name, caller_phone, service, slot_start, slot_end, created_at) "
                 "VALUES ('CA_REAL_1', ?, 'REAL-CUSTOMER-NAME', '+15555550100', 'Repair', '2099-01-05T09:00', '2099-01-05T10:00', '2026-01-01T00:00:00+00:00')", (REAL,))
    conn.commit()
    conn.close()
    temp = owner_auth.create_user(REAL, REAL_EMAIL)
    owner_auth.set_password(conn_id(storage, REAL_EMAIL), REAL_PW)

    class E:
        pass

    e = E()
    e.client, e.main, e.owner_auth, e.storage, e.seeder, e.temp = client, main, owner_auth, storage, seeder, temp
    return e


def conn_id(storage, email):
    conn = sqlite3.connect(storage.DB_PATH)
    try:
        return conn.execute("SELECT id FROM owner_users WHERE email = ?", (email,)).fetchone()[0]
    finally:
        conn.close()


def _rows(e, sql, args=()):
    conn = sqlite3.connect(e.storage.DB_PATH)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _sample_browser(e, result=None):
    result = result or e.seeder.seed()
    b = TestClient(e.main.app)
    r = _login(b, result["email"], result["password"])
    assert r.status_code == 303 and r.headers["location"] == "/portal/overview", "the demo login must go straight in (no forced change)"
    return b, result


def _real_browser(e):
    b = TestClient(e.main.app)
    assert _login(b, REAL_EMAIL, REAL_PW).status_code == 303
    return b


# ------------------------------------------------------------ the sample business itself
def test_sample_client_is_fictional_flagged_and_not_reachable_by_phone():
    from app.config import list_client_ids, load_client_config

    cfg = load_client_config(SAMPLE)
    assert cfg.portal_sample and cfg.demo_mode and cfg.business_name.startswith("Sample ")
    assert not (cfg.owner_email or cfg.ntfy_topic or cfg.webhook_url or cfg.google_calendar_id or cfg.calendar_ical_url)
    assert cfg.escalation_phone.startswith("+1571555")
    assert SAMPLE not in load_client_config("callkettle_demo").demo_menu.values()
    assert not [c for c in list_client_ids() if c != SAMPLE and load_client_config(c).portal_sample], "only the fictional sample may be flagged"


def test_portal_sample_requires_demo_mode():
    from app.config import ClientConfig, load_client_config

    raw = load_client_config(SAMPLE).model_dump()
    raw["demo_mode"] = False
    with pytest.raises(ValueError):
        ClientConfig(**raw)
    raw["demo_mode"], raw["owner_email"] = True, "owner@example.com"
    with pytest.raises(ValueError):
        ClientConfig(**raw)


# ------------------------------------------------------------ seeding
def test_seeding_is_idempotent_and_touches_only_the_sample(env):
    first = env.seeder.seed()
    snapshot = lambda: (_rows(env, "SELECT call_sid, outcome_class, needs_attention FROM calls WHERE client_id = ? ORDER BY call_sid", (SAMPLE,)),
                        _rows(env, "SELECT service, caller_name FROM bookings WHERE client_id = ? ORDER BY slot_start", (SAMPLE,)))
    calls1, bookings1 = snapshot()
    second = env.seeder.seed()
    assert snapshot() == (calls1, bookings1)
    assert len(calls1) == first["calls"] == 6 and len(bookings1) == first["bookings"] == 2 and first["attention"] == 3
    assert {c[1] for c in calls1} == {"BOOKED", "CALLBACK_REQUESTED", "OUTSIDE_SERVICE_AREA", "EMERGENCY_ESCALATED", "AFTER_HOURS_MESSAGE"}
    assert all("(sample)" in b[1] for b in bookings1)
    assert all(s.startswith("SAMPLE:") for (s,) in _rows(env, "SELECT summary FROM calls WHERE client_id = ?", (SAMPLE,)))
    assert all(f[0].startswith("+1571555") for f in _rows(env, "SELECT from_number FROM calls WHERE client_id = ?", (SAMPLE,)))
    assert _rows(env, "SELECT COUNT(*) FROM owner_users WHERE client_id = ?", (SAMPLE,)) == [(1,)]
    # the real client's data is untouched
    assert _rows(env, "SELECT call_sid FROM calls WHERE client_id = ?", (REAL,)) == [("CA_REAL_1",)]
    assert _rows(env, "SELECT caller_name FROM bookings WHERE client_id = ?", (REAL,)) == [("REAL-CUSTOMER-NAME",)]
    # a reseed rotates the password; --keep-login keeps it
    assert first["password"] != second["password"]
    assert env.owner_auth.authenticate(first["email"], first["password"], "1.1.1.1")[0] == "bad"
    assert env.owner_auth.authenticate(second["email"], second["password"], "1.1.1.2")[0] == "ok"
    kept = env.seeder.seed(reset_login=False)
    assert kept["password"] is None and env.owner_auth.authenticate(second["email"], second["password"], "1.1.1.3")[0] == "ok"


def test_seed_refuses_an_email_that_belongs_to_a_real_client(env):
    before = _rows(env, "SELECT pw_hash, client_id FROM owner_users WHERE email = ?", (REAL_EMAIL,))
    with pytest.raises(RuntimeError):
        env.seeder.seed(email=REAL_EMAIL)
    assert _rows(env, "SELECT pw_hash, client_id FROM owner_users WHERE email = ?", (REAL_EMAIL,)) == before
    assert _rows(env, "SELECT COUNT(*) FROM calls WHERE client_id = ?", (SAMPLE,)) == [(0,)]


def test_script_refuses_off_fly_and_prints_password_once_locally(env, monkeypatch, capsys):
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    assert env.seeder.main([]) == 1
    assert "fly ssh console --app deskline-ai -C 'python scripts/seed_demo_portal.py'" in capsys.readouterr().out
    assert _rows(env, "SELECT COUNT(*) FROM calls WHERE client_id = ?", (SAMPLE,)) == [(0,)]
    assert env.seeder.main(["--allow-local"]) == 0
    out = capsys.readouterr().out
    pw = re.search(r"Password \(shown once[^:]*: (\S+)", out).group(1)
    assert out.count(pw) == 1
    assert env.owner_auth.authenticate(env.seeder.DEFAULT_EMAIL, pw, "1.1.1.4")[0] == "ok"


def test_daily_demo_reset_keeps_the_sample_bookings(env):
    from app import ops

    env.seeder.seed()
    ops.reset_demo_data(hours=0)
    assert _rows(env, "SELECT COUNT(*) FROM bookings WHERE client_id = ?", (SAMPLE,)) == [(2,)]


# ------------------------------------------------------------ portal: banner and read-only
def test_banner_on_every_sample_page_and_sample_content_shown(env):
    b, _ = _sample_browser(env)
    month = _rows(env, "SELECT substr(MIN(slot_start), 1, 7) FROM bookings WHERE client_id = ?", (SAMPLE,))[0][0]
    for path in ("/portal/overview", "/portal/calendar", f"/portal/calendar?month={month}", "/portal/calls", "/portal/settings", "/portal/password"):
        r = b.get(path)
        assert r.status_code == 200 and BANNER in r.text, path
    over = b.get("/portal/overview").text
    assert "Needs your attention (3)" in over and over.count("disabled>Mark handled") == 3 and "Sample Heating &amp; Air" in over
    assert "gas" in over and "911" in over
    assert "(sample)" in b.get(f"/portal/calendar?month={month}").text
    calls = b.get("/portal/calls").text
    assert "6 call(s) match" in calls and "Read the full conversation" in calls and "Richmond" in calls
    csv = b.get("/portal/export/calls.csv")
    assert 'filename="SAMPLE-calls.csv"' in csv.headers["content-disposition"] and "SAMPLE:" in csv.text
    assert 'SAMPLE-bookings.csv' in b.get("/portal/export/bookings.csv").headers["content-disposition"]


def test_sample_portal_cannot_be_changed(env):
    b, result = _sample_browser(env)
    page = b.get("/portal/overview").text
    token = _csrf(page)
    r = b.post("/portal/handled", data={"csrf": token, "call": "SAMPLE-PORTAL-05"}, follow_redirects=False)
    assert r.status_code == 403 and BANNER in r.text
    assert _rows(env, "SELECT COUNT(*) FROM calls WHERE client_id = ? AND needs_attention = 1 AND attention_resolved_at IS NULL", (SAMPLE,)) == [(3,)]
    r = b.post("/portal/password", data={"csrf": token, "current": result["password"], "new": "another long password 9", "new2": "another long password 9"},
               follow_redirects=False)
    assert r.status_code == 403
    assert env.owner_auth.authenticate(result["email"], result["password"], "1.1.1.5")[0] == "ok", "the founder's demo login must still work"
    assert b.get("/portal/overview").status_code == 200, "still signed in"


# ------------------------------------------------------------ isolation both ways
def test_real_owner_never_sees_sample_data_or_banner(env):
    env.seeder.seed()
    real = _real_browser(env)
    pages = [real.get(p).text for p in ("/portal/overview", "/portal/calls", "/portal/settings", "/portal/export/calls.csv", "/portal/export/bookings.csv")]
    for text in pages:
        assert "SAMPLE" not in text and BANNER not in text and "(sample)" not in text
    assert "REAL-CUSTOMER-SECRET" in pages[1]
    # the real owner can still mark their own call handled (read-only applies only to the sample)
    r = real.post("/portal/handled", data={"csrf": _csrf(pages[0]), "call": "CA_REAL_1"}, follow_redirects=False)
    assert r.status_code == 303
    # and cannot touch a sample call through the form
    r = real.post("/portal/handled", data={"csrf": _csrf(pages[0]), "call": "SAMPLE-PORTAL-05"}, follow_redirects=False)
    assert _rows(env, "SELECT attention_resolved_at FROM calls WHERE call_sid = 'SAMPLE-PORTAL-05'") == [(None,)]


def test_sample_owner_never_sees_a_real_client(env):
    b, _ = _sample_browser(env)
    for path in ("/portal/overview", "/portal/calls", "/portal/calls?page=1&outcome=CALLBACK_REQUESTED", "/portal/calendar?month=2099-01",
                 "/portal/settings", "/portal/export/calls.csv", "/portal/export/bookings.csv"):
        text = b.get(path).text
        assert "REAL-CUSTOMER" not in text and "CoolFlow" not in text, path
    assert json.loads(_rows(env, "SELECT transcript_json FROM calls WHERE call_sid = 'SAMPLE-PORTAL-01'")[0][0])
