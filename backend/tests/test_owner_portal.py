"""Owner portal: login, sessions, CSRF, lockout, strict client isolation, pages, CSV. Offline and deterministic (temp SQLite, no network)."""
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape as _h
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

A_ID, B_ID = "demo_hvac", "demo_nova_plumbing"
A_EMAIL, B_EMAIL = "owner@example.com", "owner@example.com"
NEW_PW = "correct horse battery 42"


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def _now_iso(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat()


@pytest.fixture
def env(app_client):
    client, main = app_client
    from app import owner_auth, portal, storage

    def new_browser(**kw) -> TestClient:
        return TestClient(main.app, **kw)

    def login(browser: TestClient, email: str, password: str):
        page = browser.get("/portal/login")
        return browser.post("/portal/login", data={"email": email, "password": password, "csrf": _csrf(page.text)}, follow_redirects=False)

    def ready(browser: TestClient, email: str, temp: str, password: str = NEW_PW) -> str:
        """Sign in with the temporary password and complete the forced password change."""
        r = login(browser, email, temp)
        assert r.status_code == 303 and r.headers["location"] == "/portal/password"
        page = browser.get("/portal/password")
        r = browser.post("/portal/password", data={"current": temp, "new": password, "new2": password, "csrf": _csrf(page.text)}, follow_redirects=False)
        assert r.status_code == 303, r.text
        return password

    class E:
        pass

    e = E()
    e.client, e.main, e.owner_auth, e.portal, e.storage = client, main, owner_auth, portal, storage
    e.new_browser, e.login, e.ready = new_browser, login, ready
    e.temp_a = owner_auth.create_user(A_ID, A_EMAIL)
    e.temp_b = owner_auth.create_user(B_ID, B_EMAIL)
    e.a, e.b = client, new_browser()
    ready(e.a, A_EMAIL, e.temp_a)
    ready(e.b, B_EMAIL, e.temp_b)
    return e


def _db(e):
    return sqlite3.connect(e.storage.DB_PATH)


def _seed_call(e, sid, client_id, *, outcome_class="BOOKED", attention=0, summary="Booked a tune-up", frm="+15555550100", started=None, transcript=None):
    conn = _db(e)
    conn.execute("INSERT INTO calls (call_sid, client_id, from_number, started_at, outcome, summary, transcript_json, outcome_class, needs_attention) VALUES (?,?,?,?,?,?,?,?,?)",
                 (sid, client_id, frm, started or _now_iso(), "completed", summary, json.dumps(transcript or []), outcome_class, attention))
    conn.commit()
    conn.close()


def _seed_booking(e, client_id, slot_start, *, name="Pat Caller", service="AC tune-up", phone="+15555550100"):
    end = (datetime.strptime(slot_start, "%Y-%m-%dT%H:%M") + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    conn = _db(e)
    conn.execute("INSERT INTO bookings (call_sid, client_id, caller_name, caller_phone, service, slot_start, slot_end, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 ("sid-" + slot_start, client_id, name, phone, service, slot_start, end, _now_iso()))
    conn.commit()
    conn.close()


def _month_slot(day=15, hh="10:00"):
    return f"{datetime.now(timezone.utc):%Y-%m}-{day:02d}T{hh}"


# ------------------------------------------------------------ passwords and accounts
def test_scrypt_hash_has_per_user_salt_and_verifies(app_client):
    from app import owner_auth

    h1, h2 = owner_auth.hash_password("a-long-password-1"), owner_auth.hash_password("a-long-password-1")
    assert h1 != h2 and h1.startswith("scrypt$") and "a-long-password-1" not in h1
    assert owner_auth.verify_password("a-long-password-1", h1)
    assert not owner_auth.verify_password("a-long-password-2", h1)
    assert not owner_auth.verify_password("x", "garbage") and not owner_auth.verify_password("x", "scrypt$1$2$3$zz$zz")


def test_create_user_stores_only_a_hash_and_forces_a_change(app_client):
    from app import owner_auth, storage

    temp = owner_auth.create_user(A_ID, "  owner@example.com ")
    raw = Path(storage.DB_PATH).read_bytes()
    assert temp.encode() not in raw                                   # plaintext never reaches the database file
    conn = sqlite3.connect(storage.DB_PATH)
    row = conn.execute("SELECT email, must_change, pw_hash FROM owner_users").fetchone()
    assert row[0] == "owner@example.com" and row[1] == 1 and row[2].startswith("scrypt$")
    with pytest.raises(ValueError):
        owner_auth.create_user(B_ID, "owner@example.com")            # one email, one account (and one client)
    with pytest.raises(ValueError):
        owner_auth.create_user(A_ID, "not-an-email")


def test_operator_cli_prints_the_password_once_and_never_logs_or_stores_it(app_client, capsys, caplog):
    import importlib
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    cli = importlib.import_module("create_owner_user")
    from app import storage

    caplog.set_level(logging.DEBUG)
    assert cli.main([A_ID, "owner@example.com"]) == 0
    out = capsys.readouterr().out
    temp = re.search(r"change required at first sign-in\): (\S+)", out).group(1)
    assert len(temp) >= 16 and temp not in caplog.text
    assert temp.encode() not in Path(storage.DB_PATH).read_bytes()
    assert cli.main([A_ID, "owner@example.com"]) == 1 and "already exists" in capsys.readouterr().out
    assert cli.main(["no_such_client", "owner@example.com"]) == 1 and "Unknown client" in capsys.readouterr().out
    assert cli.main(["--reset", "owner@example.com"]) == 0
    assert re.search(r"\): (\S+)", capsys.readouterr().out).group(1) != temp
    assert cli.main(["--reset", "owner@example.com"]) == 1
    assert cli.main([]) == 2


def test_recovery_does_not_reactivate_a_disabled_customer(env):
    with env.storage._conn() as conn:
        conn.execute("UPDATE owner_users SET disabled_at = ? WHERE email = ?", (env.owner_auth._now(), A_EMAIL))
    with pytest.raises(ValueError, match="disabled"):
        env.owner_auth.reset_user(A_EMAIL)
    with env.storage._conn() as conn:
        assert conn.execute("SELECT disabled_at FROM owner_users WHERE email = ?", (A_EMAIL,)).fetchone()[0] is not None


def test_password_rules(app_client):
    from app import owner_auth

    assert owner_auth.password_problem("short") and owner_auth.password_problem("aaaaaaaaaaaaaaaa")
    assert owner_auth.password_problem("sami.owner-password", "owner@example.com")       # contains the email local part
    assert owner_auth.password_problem(NEW_PW, "owner@example.com") is None


# ------------------------------------------------------------ login behaviour
def test_first_login_forces_a_password_change_before_any_page(app_client):
    client, main = app_client
    from app import owner_auth

    temp = owner_auth.create_user(A_ID, A_EMAIL)
    page = client.get("/portal/login")
    r = client.post("/portal/login", data={"email": A_EMAIL, "password": temp, "csrf": _csrf(page.text)}, follow_redirects=False)
    assert r.headers["location"] == "/portal/password"
    for path in ("/portal/overview", "/portal/calendar", "/portal/calls", "/portal/settings", "/portal/export/calls.csv"):
        assert client.get(path, follow_redirects=False).headers["location"] == "/portal/password"


def test_unknown_email_and_wrong_password_are_indistinguishable(env):
    anon1, anon2 = env.new_browser(), env.new_browser()
    r1 = env.login(anon1, A_EMAIL, "wrong password entirely")
    r2 = env.login(anon2, "owner@example.com", "wrong password entirely")
    assert r1.status_code == r2.status_code == 200
    strip = lambda t: re.sub(r'value="[^"]+"', "", t)
    assert strip(r1.text) == strip(r2.text) and env.portal.GENERIC_LOGIN_ERROR in r1.text
    assert "ck_portal" not in r1.headers.get("set-cookie", "") + r2.headers.get("set-cookie", "")


def test_account_lockout_then_recovers_and_hides_that_it_is_locked(env, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(env.owner_auth, "_now", lambda: clock[0])
    anon = env.new_browser()
    for _ in range(env.owner_auth.MAX_FAILS_PER_ACCOUNT):
        env.login(anon, A_EMAIL, "nope nope nope nope")
    r = env.login(anon, A_EMAIL, NEW_PW)                               # right password, but the account is locked
    assert r.status_code == 200 and env.portal.GENERIC_LOGIN_ERROR in r.text and "ck_portal" not in r.headers.get("set-cookie", "")
    clock[0] += env.owner_auth.ACCOUNT_LOCK_SECONDS + 1
    assert env.login(anon, A_EMAIL, NEW_PW).status_code == 303


def test_a_success_resets_the_failure_count(env):
    anon = env.new_browser()
    for _ in range(env.owner_auth.MAX_FAILS_PER_ACCOUNT - 1):
        env.login(anon, A_EMAIL, "nope nope nope nope")
    assert env.login(anon, A_EMAIL, NEW_PW).status_code == 303
    anon = env.new_browser()
    for _ in range(env.owner_auth.MAX_FAILS_PER_ACCOUNT - 1):
        env.login(anon, A_EMAIL, "nope nope nope nope")
    assert env.login(anon, A_EMAIL, NEW_PW).status_code == 303


def test_per_ip_limit_blocks_guessing_across_many_accounts(env):
    anon = env.new_browser()
    for i in range(env.owner_auth.MAX_FAILS_PER_IP):
        env.login(anon, f"user{i}@example.test", "nope nope nope nope")
    r = env.login(anon, A_EMAIL, NEW_PW)                               # even the real password is refused from a blocked address
    assert r.status_code == 429 and "ck_portal" not in r.headers.get("set-cookie", "")


def test_session_cookie_flags(env):
    anon = env.new_browser()
    http_cookie = env.login(anon, A_EMAIL, NEW_PW).headers["set-cookie"].lower()
    assert "httponly" in http_cookie and "samesite=lax" in http_cookie and "secure" not in http_cookie
    anon2 = env.new_browser()
    p = anon2.get("/portal/login")
    r = anon2.post("/portal/login", data={"email": A_EMAIL, "password": NEW_PW, "csrf": _csrf(p.text)}, headers={"x-forwarded-proto": "https"}, follow_redirects=False)
    assert "secure" in r.headers["set-cookie"].lower()


def test_login_post_requires_its_csrf_token(env):
    anon = env.new_browser()
    anon.get("/portal/login")
    r = anon.post("/portal/login", data={"email": A_EMAIL, "password": NEW_PW, "csrf": "forged"}, follow_redirects=False)
    assert r.status_code == 403 and "ck_portal" not in r.headers.get("set-cookie", "")
    r = anon.post("/portal/login", data={"email": A_EMAIL, "password": NEW_PW}, follow_redirects=False)
    assert r.status_code == 403
    assert env.new_browser().post("/portal/login", data={"email": A_EMAIL, "password": NEW_PW, "csrf": "x"}, follow_redirects=False).status_code == 403


# ------------------------------------------------------------ sessions and CSRF
def test_every_post_needs_the_session_csrf_token(env):
    _seed_call(env, "c1", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1)
    for path, data in (("/portal/handled", {"call": "c1"}), ("/portal/logout", {}), ("/portal/password", {"current": NEW_PW, "new": "another long pass 9", "new2": "another long pass 9"})):
        assert env.a.post(path, data=data, follow_redirects=False).status_code == 403, path
        assert env.a.post(path, data={**data, "csrf": "forged"}, follow_redirects=False).status_code == 403, path
    assert _db(env).execute("SELECT attention_resolved_at FROM calls WHERE call_sid='c1'").fetchone()[0] is None
    assert env.a.get("/portal/overview").status_code == 200                       # still signed in
    # the other user's CSRF token is not valid for this session
    other = _csrf(env.b.get("/portal/overview").text)
    assert env.a.post("/portal/handled", data={"call": "c1", "csrf": other}, follow_redirects=False).status_code == 403


def test_logout_destroys_the_session_server_side(env):
    token = env.a.cookies.get("ck_portal")
    page = env.a.get("/portal/overview")
    r = env.a.post("/portal/logout", data={"csrf": _csrf(page.text)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/portal/login"
    replay = env.new_browser()
    replay.cookies.set("ck_portal", token, path="/portal")
    assert replay.get("/portal/overview", follow_redirects=False).headers["location"] == "/portal/login"
    assert _db(env).execute("SELECT COUNT(*) FROM owner_sessions WHERE user_id=(SELECT id FROM owner_users WHERE email=?)", (A_EMAIL,)).fetchone()[0] == 0


def test_sessions_expire_absolutely_and_when_idle(env, monkeypatch):
    base = env.owner_auth._now()
    clock = [base]
    monkeypatch.setattr(env.owner_auth, "_now", lambda: clock[0])
    token, _ = env.owner_auth.create_session(1)
    assert env.owner_auth.get_session(token)
    clock[0] = base + env.owner_auth.SESSION_IDLE_SECONDS - 10
    assert env.owner_auth.get_session(token)                                       # activity refreshes the idle timer
    clock[0] += env.owner_auth.SESSION_IDLE_SECONDS + 10
    assert env.owner_auth.get_session(token) is None
    clock[0] = base
    token2, _ = env.owner_auth.create_session(1)
    for step in range(1, 7):                                                         # stay active, still hit the absolute limit
        clock[0] = base + step * 2 * 3600 - 60
        if step * 2 * 3600 - 60 < env.owner_auth.SESSION_ABSOLUTE_SECONDS:
            assert env.owner_auth.get_session(token2)
    clock[0] = base + env.owner_auth.SESSION_ABSOLUTE_SECONDS + 1
    assert env.owner_auth.get_session(token2) is None
    assert env.owner_auth.get_session("") is None and env.owner_auth.get_session("x" * 500) is None and env.owner_auth.get_session("nope") is None


def test_session_token_is_stored_only_as_a_hash(env):
    token = env.a.cookies.get("ck_portal")
    assert token and token.encode() not in Path(env.storage.DB_PATH).read_bytes()


def test_login_issues_a_fresh_session_token_each_time(env):
    other = env.new_browser()
    env.login(other, A_EMAIL, NEW_PW)
    assert other.cookies.get("ck_portal") != env.a.cookies.get("ck_portal")


def test_password_change_validates_and_signs_every_other_session_out(env):
    second = env.new_browser()
    assert env.login(second, A_EMAIL, NEW_PW).status_code == 303
    page = env.a.get("/portal/password")
    tok = _csrf(page.text)

    def change(cur, new, new2):
        return env.a.post("/portal/password", data={"current": cur, "new": new, "new2": new2, "csrf": tok}, follow_redirects=False)

    assert "current password" in change("wrong", "another long pass 9", "another long pass 9").text
    assert "did not match" in change(NEW_PW, "another long pass 9", "different long pass 9").text
    assert "at least" in change(NEW_PW, "short", "short").text
    assert "different from the current" in change(NEW_PW, NEW_PW, NEW_PW).text
    assert change(NEW_PW, "another long pass 9", "another long pass 9").status_code == 303
    assert second.get("/portal/overview", follow_redirects=False).headers["location"] == "/portal/login"      # the other device is signed out
    assert env.a.get("/portal/overview").status_code == 200                                                  # this one continues
    assert env.login(env.new_browser(), A_EMAIL, NEW_PW).status_code == 200                                  # old password is dead


def test_disabled_account_and_offboarded_client_lose_access(env, monkeypatch):
    conn = _db(env)
    conn.execute("UPDATE owner_users SET disabled_at = 1 WHERE email = ?", (A_EMAIL,))
    conn.commit()
    assert env.a.get("/portal/overview", follow_redirects=False).headers["location"] == "/portal/login"
    assert env.login(env.new_browser(), A_EMAIL, NEW_PW).status_code == 200
    from app.config import ClientNotFoundError

    def gone(cid):
        raise ClientNotFoundError(cid)

    monkeypatch.setattr(env.portal, "load_client_config", gone)
    assert env.b.get("/portal/overview", follow_redirects=False).headers["location"] == "/portal/login"


def test_deleting_a_client_removes_its_owner_accounts_and_sessions(env):
    r = env.client.post(f"/admin/client/{A_ID}/delete", params={"key": "master_key_for_tests", "confirm": A_ID})
    assert r.status_code == 200 and r.json()["deleted"]["owner_users"] == 1
    conn = _db(env)
    assert conn.execute("SELECT COUNT(*) FROM owner_users WHERE client_id=?", (A_ID,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM owner_users").fetchone()[0] == 1


# ------------------------------------------------------------ anonymous access
def test_every_portal_route_but_login_sends_anonymous_visitors_to_login(env):
    anon = env.new_browser()
    for path in ("/portal", "/portal/overview", "/portal/calendar", "/portal/calls", "/portal/settings", "/portal/password",
                 "/portal/export/calls.csv", "/portal/export/bookings.csv"):
        r = anon.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] in ("/portal/login", "/portal/overview"), path
        if path != "/portal":
            assert r.headers["location"] == "/portal/login"
    for path in ("/portal/handled", "/portal/logout", "/portal/password"):
        assert anon.post(path, data={"csrf": "x", "call": "c"}, follow_redirects=False).status_code == 303


# ------------------------------------------------------------ isolation (the important part)
def test_user_a_cannot_see_client_b_data_on_any_page_or_export(env):
    _seed_call(env, "a-call", A_ID, summary="AAA summary", frm="+15555550100", transcript=[{"role": "caller", "text": "AAA words"}])
    _seed_call(env, "b-call", B_ID, summary="BBB-SECRET-SUMMARY", frm="+15555550100", outcome_class="CALLBACK_REQUESTED", attention=1,
               transcript=[{"role": "caller", "text": "BBB-SECRET-WORDS"}])
    _seed_booking(env, A_ID, _month_slot(10), name="Alice Aaa", service="AAA service")
    _seed_booking(env, B_ID, _month_slot(11), name="Bob Bbb", service="BBB-SECRET-SERVICE")
    for path in ("/portal/overview", "/portal/calendar", "/portal/calls", "/portal/settings", "/portal/export/calls.csv", "/portal/export/bookings.csv",
                 "/portal/calls?outcome=CALLBACK_REQUESTED", "/portal/calendar?month=" + _month_slot()[:7]):
        body = env.a.get(path).text
        for secret in ("BBB-SECRET", "Bob Bbb", "+15555550100", "555-0222", "Nova"):
            assert secret not in body, (path, secret)
    assert "AAA summary" in env.a.get("/portal/calls").text and "AAA service" in env.a.get("/portal/calendar").text
    assert "BBB-SECRET-SUMMARY" in env.b.get("/portal/calls").text and "AAA summary" not in env.b.get("/portal/calls").text


def test_no_url_or_form_parameter_selects_a_client(env):
    _seed_call(env, "b-call", B_ID, summary="BBB-SECRET-SUMMARY")
    for qs in (f"?client_id={B_ID}", f"?client={B_ID}", f"?key=master_key_for_tests&client_id={B_ID}"):
        for path in ("/portal/overview", "/portal/calls", "/portal/calendar", "/portal/settings", "/portal/export/calls.csv"):
            assert "BBB-SECRET" not in env.a.get(path + qs).text
    assert env.a.get(f"/portal/{B_ID}/overview", follow_redirects=False).status_code == 404


def test_user_a_cannot_mark_client_b_calls_handled(env):
    _seed_call(env, "b-call", B_ID, outcome_class="CALLBACK_REQUESTED", attention=1)
    _seed_call(env, "a-call", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1)
    tok = _csrf(env.a.get("/portal/overview").text)
    env.a.post("/portal/handled", data={"call": "b-call", "csrf": tok, "client_id": B_ID}, follow_redirects=False)
    rows = dict(_db(env).execute("SELECT call_sid, attention_resolved_at FROM calls").fetchall())
    assert rows["b-call"] is None                                                   # untouched
    env.a.post("/portal/handled", data={"call": "a-call", "csrf": tok}, follow_redirects=False)
    assert _db(env).execute("SELECT attention_resolved_at FROM calls WHERE call_sid='a-call'").fetchone()[0] is not None


def test_each_session_sees_its_own_clients_name_only(env):
    from app.config import load_client_config

    na, nb = load_client_config(A_ID).business_name, load_client_config(B_ID).business_name
    sa, sb = env.a.get("/portal/overview").text, env.b.get("/portal/overview").text
    assert _h(na) in sa and _h(nb) not in sa and _h(nb) in sb and _h(na) not in sb


def test_settings_show_only_the_own_clients_config(env):
    from app.config import load_client_config

    a, b = load_client_config(A_ID), load_client_config(B_ID)
    sa = env.a.get("/portal/settings").text
    assert a.business_name.replace("&", "&amp;") in sa or a.business_name in sa
    assert b.business_name not in sa
    from app import brand
    if re.sub(r"\D", "", b.escalation_phone)[-10:] != re.sub(r"\D", "", brand.get().support_phone)[-10:]:      # (the demo clients share the founder's number)
        assert b.escalation_phone not in sa and re.sub(r"\D", "", b.escalation_phone)[-10:] not in re.sub(r"\D", "", sa)


# ------------------------------------------------------------ overview
def test_overview_counts_by_outcome_this_week_and_month(env):
    _seed_call(env, "1", A_ID, outcome_class="BOOKED")
    _seed_call(env, "2", A_ID, outcome_class="BOOKED")
    _seed_call(env, "3", A_ID, outcome_class="FAQ_RESOLVED")
    _seed_call(env, "old", A_ID, outcome_class="BOOKED", started=_now_iso(timedelta(days=40)))
    html = env.a.get("/portal/overview").text
    week = html.split("This week")[1].split("This month")[0]
    assert re.search(r"Booked</td><td>2</td>", week) and re.search(r"Question answered</td><td>1</td>", week) and "<th>3</th>" in week
    assert "Booked</td><td>3<" not in html                                           # the 40-day-old call is outside the month
    assert env.b.get("/portal/overview").text.count("No calls recorded this week.") == 1


def test_overview_empty_state_says_no_data_not_zeros(env):
    html = env.a.get("/portal/overview").text
    assert "No calls recorded this week." in html and "No calls recorded this month." in html and "Nothing waiting on you." in html


def test_needs_attention_and_mark_handled_uses_existing_logic(env):
    _seed_call(env, "x1", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, summary="Wants a callback")
    html = env.a.get("/portal/overview").text
    assert "Needs your attention (1)" in html and "Wants a callback" in html and "Mark handled" in html
    r = env.a.post("/portal/handled", data={"call": "x1", "csrf": _csrf(html)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/portal/overview#attention"
    assert "Needs your attention (0)" in env.a.get("/portal/overview").text


# ------------------------------------------------------------ calendar
def test_calendar_month_grid_and_agenda_show_the_clients_bookings(env):
    slot = _month_slot(15, "10:00")
    _seed_booking(env, A_ID, slot, name="Dana Roe", service="Furnace repair")
    html = env.a.get("/portal/calendar").text
    assert "Furnace repair" in html and "10:00 AM" in html and "Dana Roe" in html and "confirmed" in html and "tel:" in html
    assert 'class="cal"' in html and "<th>Sun</th>" in html


def test_calendar_navigation_and_bad_month_parameter(env):
    _seed_booking(env, A_ID, "2031-03-12T09:00", service="March job")
    assert "March job" in env.a.get("/portal/calendar?month=2031-03").text
    april = env.a.get("/portal/calendar?month=2031-04").text
    assert "March job" in april.split("<h2>April 2031</h2>")[0]                     # upcoming list is month-independent
    assert "March job" not in april.split("<h2>April 2031</h2>")[1]                 # the April grid and agenda do not have it
    assert "No data: no bookings in" in env.a.get("/portal/calendar?month=2031-04").text
    for bad in ("garbage", "2031-13", "1999-01", "2031-3", "<script>"):
        assert env.a.get("/portal/calendar", params={"month": bad}).status_code == 200
    assert "month=2031-02" in env.a.get("/portal/calendar?month=2031-03").text and "month=2031-04" in env.a.get("/portal/calendar?month=2031-03").text


def test_calendar_is_read_only(env):
    html = env.a.get("/portal/calendar").text
    assert "<form" not in html.split("</nav>")[1]                                  # no editing controls (the only form is Sign out, inside nav)
    assert env.a.post("/portal/calendar", data={"csrf": "x"}, follow_redirects=False).status_code == 405


# ------------------------------------------------------------ calls
def test_calls_filter_by_outcome_and_date(env):
    _seed_call(env, "1", A_ID, outcome_class="BOOKED", summary="booked one")
    _seed_call(env, "2", A_ID, outcome_class="FAQ_RESOLVED", summary="faq one")
    _seed_call(env, "3", A_ID, outcome_class="BOOKED", summary="ancient booking", started=_now_iso(timedelta(days=60)))
    allc = env.a.get("/portal/calls").text
    assert all(s in allc for s in ("booked one", "faq one", "ancient booking")) and "3 call(s) match" in allc
    only = env.a.get("/portal/calls?outcome=BOOKED").text
    assert "booked one" in only and "faq one" not in only
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    recent = env.a.get("/portal/calls", params={"date_from": (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")}).text
    assert "booked one" in recent and "ancient booking" not in recent
    old = env.a.get("/portal/calls", params={"date_to": (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")}).text
    assert "ancient booking" in old and "booked one" not in old
    assert env.a.get("/portal/calls", params={"outcome": "' OR 1=1 --", "date_from": "x'; DROP TABLE calls;--", "page": "-5"}).status_code in (200, 400, 422)
    assert _db(env).execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 3
    assert "No data: no calls match." in env.a.get("/portal/calls?outcome=TRANSFERRED").text and today


def test_calls_show_summary_and_transcript_when_retained(env):
    _seed_call(env, "t1", A_ID, summary="Short summary", transcript=[{"role": "caller", "text": "my furnace is out"}, {"role": "assistant", "text": "I can book you in"}])
    html = env.a.get("/portal/calls").text
    assert "Short summary" in html and "Read the full conversation" in html and "my furnace is out" in html and "<b>AI:</b> I can book you in" in html


def test_no_record_clients_never_show_summaries_or_transcripts(env, monkeypatch):
    from app.config import load_client_config

    cfg = load_client_config(A_ID).model_copy(update={"record_transcripts": False})
    monkeypatch.setattr(env.portal, "load_client_config", lambda cid: cfg)
    _seed_call(env, "p1", A_ID, summary="PRIVATE-SUMMARY", transcript=[{"role": "caller", "text": "PRIVATE-WORDS"}], outcome_class="CALLBACK_REQUESTED", attention=1)
    for path in ("/portal/calls", "/portal/overview", "/portal/export/calls.csv"):
        body = env.a.get(path).text
        assert "PRIVATE-SUMMARY" not in body and "PRIVATE-WORDS" not in body, path
    assert "Details not recorded for privacy." in env.a.get("/portal/calls").text


def test_everything_dynamic_is_html_escaped(env):
    evil = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
    _seed_call(env, "e1", A_ID, summary=evil, frm=evil, transcript=[{"role": "caller", "text": evil}], outcome_class="CALLBACK_REQUESTED", attention=1)
    _seed_booking(env, A_ID, _month_slot(20), name=evil, service=evil, phone=evil)
    for path in ("/portal/overview", "/portal/calls", "/portal/calendar"):
        body = env.a.get(path).text
        assert "<script>alert" not in body and "<img src=x" not in body and "&lt;script&gt;" in body, path
    assert "<script" not in env.a.get("/portal/settings").text.lower()


# ------------------------------------------------------------ settings, CSV, hygiene
def test_settings_page_content(env):
    html = env.a.get("/portal/settings").text
    for expect in ("Business hours", "Monday", "Services", "Calls that need a person", "Turn call forwarding off", "*73", "#21#", "##21#", "##002#", "Support", "Change password", A_EMAIL):
        assert expect in html, expect
    assert "webhook" not in html.lower() and "secret" not in html.lower()


def test_csv_exports_have_headers_and_neutralise_formulas(env):
    _seed_call(env, "c1", A_ID, summary="=HYPERLINK(\"http://evil\")", outcome_class="BOOKED")
    _seed_booking(env, A_ID, _month_slot(9), name="=cmd|' /C calc'!A0", service="@SUM(1)", phone="+15555550100")
    calls = env.a.get("/portal/export/calls.csv")
    assert calls.headers["content-type"].startswith("text/csv") and "attachment" in calls.headers["content-disposition"]
    assert calls.text.splitlines()[0].startswith("Started (local),Caller,Outcome") and "'=HYPERLINK" in calls.text and "+15555550100" in calls.text
    bk = env.a.get("/portal/export/bookings.csv")
    assert "'=cmd" in bk.text and "'@SUM" in bk.text and "+15555550100" in bk.text and bk.text.splitlines()[0].startswith("Start (local),End (local),Service")
    assert env.b.get("/portal/export/bookings.csv").text.strip().count("\n") == 0                 # B: header only


def test_pages_are_plain_no_external_requests_no_scripts_no_price(env):
    price = str(json.loads((Path(__file__).resolve().parents[2] / "marketing" / "facts.json").read_text(encoding="utf-8"))["price_monthly"])
    _seed_call(env, "c1", A_ID)
    for path in ("/portal/login", "/portal/password", "/portal/overview", "/portal/calendar", "/portal/calls", "/portal/settings"):
        r = env.a.get(path)
        body = r.text
        assert "<script" not in body.lower() and "http://" not in body and "https://" not in body and "src=" not in body and "<link" not in body.lower(), path
        assert f"${price}" not in body and "per month" not in body.lower() and "/month" not in body.lower(), path
        assert r.headers["cache-control"] == "no-store" and "default-src 'none'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
        assert 'name="viewport"' in body


def test_legacy_keyed_report_link_still_works(env):
    key = env.main.report_key_for(A_ID)
    assert env.new_browser().get(f"/report/{A_ID}", params={"key": key}).status_code == 200
    assert env.new_browser().get(f"/report/{A_ID}", params={"key": env.main.report_key_for(B_ID)}).status_code == 403


def test_schema_migration_is_additive_and_idempotent(tmp_path, monkeypatch):
    import importlib

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE calls (call_sid TEXT PRIMARY KEY, client_id TEXT NOT NULL, from_number TEXT, started_at TEXT NOT NULL, ended_at TEXT, turn_count INTEGER NOT NULL DEFAULT 0, outcome TEXT, transcript_json TEXT NOT NULL DEFAULT '[]');"
                       "INSERT INTO calls (call_sid, client_id, started_at) VALUES ('keep', 'x', 'now');")
    conn.commit()
    conn.close()
    monkeypatch.setenv("CALLKETTLE_DB_PATH", str(db))
    from app import storage

    importlib.reload(storage)
    storage.init_db()
    storage.init_db()
    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"owner_users", "owner_sessions", "owner_login_failures"} <= names
    assert conn.execute("SELECT call_sid FROM calls").fetchone()[0] == "keep"


# ------------------------------------------------------------ UX polish (first paying customer)
def test_new_owner_sees_a_plain_language_what_happens_next_instead_of_empty_tables(env):
    html = env.a.get("/portal/overview").text
    assert "No calls yet. Here is what happens next" in html and "Call back" in html
    assert "No upcoming bookings yet" in html and "Nothing waiting on you." in html
    _seed_call(env, "first", A_ID)
    assert "No calls yet. Here is what happens next" not in env.a.get("/portal/overview").text
    assert "No calls yet" in env.b.get("/portal/calls").text and "No data: no calls match" not in env.b.get("/portal/calls").text
    assert "No data: no calls match" in env.b.get("/portal/calls?outcome=BOOKED").text         # a filter that matches nothing says so


def test_attention_queue_has_one_tap_call_back_and_a_plain_reason(env):
    _seed_call(env, "cb", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, summary="Furnace is making noise", frm="+15555550100")
    html = env.a.get("/portal/overview").text
    assert 'href="tel:+15555550100"' in html and "Call back+15555550100" in html and "Asked for a call back." in html
    calls = env.a.get("/portal/calls").text
    assert 'href="tel:+15555550100"' in calls and "Needs a call back" in calls
    tok = _csrf(html)
    env.a.post("/portal/handled", data={"call": "cb", "csrf": tok}, follow_redirects=False)
    after = env.a.get("/portal/calls").text
    assert "Handled" in after and "Needs a call back" not in after and "Call back (571)" not in after


def test_non_numeric_caller_ids_never_become_tel_links(env):
    _seed_call(env, "weird", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, frm="anonymous")
    _seed_call(env, "js", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, frm='javascript:alert(1)"')
    for path in ("/portal/overview", "/portal/calls"):
        html = env.a.get(path).text
        assert 'href="tel:"' not in html and "javascript:" not in html.replace("javascript:alert(1)&quot;", "").replace("javascript:alert(1)&#x27;", "")
        assert 'href="javascript' not in html


def test_upcoming_bookings_come_first_skip_past_and_cancelled_and_stay_private(env):
    future1 = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%dT10:00")
    future2 = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT09:00")
    past = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%dT10:00")
    _seed_booking(env, A_ID, future1, name="Later Lee", service="Later job")
    _seed_booking(env, A_ID, future2, name="Sooner Sue", service="Sooner job")
    _seed_booking(env, A_ID, past, name="Past Pete", service="Past job")
    _seed_booking(env, B_ID, future2, name="Bob Bbb", service="BBB-SECRET-SERVICE")
    conn = _db(env)
    conn.execute("UPDATE bookings SET status='cancelled' WHERE client_id=? AND service='Past job'", (A_ID,))
    conn.commit()
    for path in ("/portal/overview", "/portal/calendar"):
        html = env.a.get(path).text
        up = html.split("Upcoming bookings")[1]
        assert up.index("Sooner Sue") < up.index("Later Lee") and "BBB-SECRET" not in html, path
        assert 'href="tel:+15555550100"' in html
    up_only = env.a.get("/portal/overview").text.split("Upcoming bookings")[1].split("This week")[0]
    assert "Past Pete" not in up_only


def test_setup_page_explains_the_service_in_plain_language_and_leaks_no_internals(env):
    html = env.a.get("/portal/settings").text
    for expect in ("How Call Kettle is set up for you", "Your receptionist", "Receptionist phone number", "read-only", "Services it can book", "ring"):
        assert expect in html, expect
    from app import provisioning
    provisioning.record_number(A_ID, "+15555550100", None)
    assert "+15555550100" in env.a.get("/portal/settings").text and "555-0999" not in env.b.get("/portal/settings").text
    low = env.a.get("/portal/settings").text.lower()
    for internal in ("webhook", "secret", "ntfy", "api key", "model", "claude", "twilio"):
        assert internal not in low, internal


def test_no_record_clients_see_the_privacy_note_on_setup(env, monkeypatch):
    from app.config import load_client_config

    cfg = load_client_config(A_ID).model_copy(update={"record_transcripts": False})
    monkeypatch.setattr(env.portal, "load_client_config", lambda cid: cfg)
    assert "what callers say is not saved" in env.a.get("/portal/settings").text


def test_help_page_and_footer_give_the_founders_contact_line(env):
    from app import brand

    b = brand.get()
    from email.utils import parseaddr
    addr = parseaddr(b.support_email)[1]
    assert "<" not in f"mailto:{addr}"                                                    # a "Name <addr>" sender must not leak into the link
    for path in ("/portal/overview", "/portal/calendar", "/portal/calls", "/portal/settings", "/portal/help"):
        html = env.a.get(path).text
        assert f"mailto:{addr}" in html and "tel:+" in html and b.support_phone in html and "/portal/help" in html, path
    assert "Common questions" in env.a.get("/portal/help").text
    anon = env.new_browser()
    assert anon.get("/portal/help", follow_redirects=False).headers["location"] == "/portal/login"


def test_login_and_password_pages_guide_a_first_time_owner(env):
    anon = env.new_browser()
    login = anon.get("/portal/login").text
    assert "temporary password" in login and "Forgot your password" in login and "tel:+" in login
    fresh = env.new_browser()
    t = env.owner_auth.reset_user(A_EMAIL)
    env.login(fresh, A_EMAIL, t)
    page = fresh.get("/portal/password").text
    assert "Temporary password" in page and "Welcome" in page
    assert "Current password" in env.b.get("/portal/password").text


def test_every_portal_page_is_mobile_ready_and_self_contained(env):
    for path in ("/portal/login", "/portal/password", "/portal/overview", "/portal/calendar", "/portal/calls", "/portal/settings", "/portal/help"):
        r = env.a.get(path)
        assert r.status_code == 200 and 'name="viewport"' in r.text and "max-width:600px" in r.text, path
        assert "<script" not in r.text.lower() and "https://" not in r.text and "default-src 'none'" in r.headers["content-security-policy"], path


# ------------------------------------------------------------ multi-week trend + malformed page
def _week_start_iso(weeks_ago, hour_utc=15):
    from zoneinfo import ZoneInfo
    cfg_tz = ZoneInfo("America/New_York")
    now = datetime.now(cfg_tz)
    monday = (now - timedelta(days=now.weekday())).replace(hour=hour_utc - 5, minute=0, second=0, microsecond=0)
    return (monday - timedelta(weeks=weeks_ago) + timedelta(hours=1)).astimezone(timezone.utc).isoformat()


def test_overview_shows_recent_weeks_trend_scoped_to_the_tenant(env):
    _seed_call(env, "w0a", A_ID, outcome_class="BOOKED", started=_week_start_iso(0))
    _seed_call(env, "w1a", A_ID, outcome_class="BOOKED", started=_week_start_iso(1))
    _seed_call(env, "w1b", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, started=_week_start_iso(1))
    _seed_call(env, "w3a", A_ID, outcome_class="FAQ_RESOLVED", started=_week_start_iso(3))
    _seed_call(env, "old", A_ID, outcome_class="BOOKED", started=_week_start_iso(12))
    _seed_call(env, "other", B_ID, outcome_class="BOOKED", started=_week_start_iso(1))
    html = env.a.get("/portal/overview").text
    assert "Recent weeks" in html
    section = html.split("Recent weeks")[1]
    rows = re.findall(r'<tr data-week="(\d+)"><td>[^<]*</td><td>(\d+)</td><td>(\d+)</td><td>(\d+)</td></tr>', section)
    by_ago = {int(a): (int(c), int(b), int(n)) for a, c, b, n in rows}
    assert by_ago[0] == (1, 1, 0)       # (calls, booked, needed a person)
    assert by_ago[1] == (2, 1, 1)
    assert by_ago[3] == (1, 0, 0)
    assert 12 not in by_ago and len(rows) == 8
    other = env.b.get("/portal/overview").text.split("Recent weeks")[1]
    assert re.search(r'data-week="1"><td>[^<]*</td><td>1</td><td>1</td>', other)


def test_overview_recent_weeks_empty_state_says_no_data(env):
    html = env.a.get("/portal/overview").text
    assert "No calls recorded in the last 8 weeks." in html


@pytest.mark.parametrize("path", ["/portal/calls?page=abc", "/portal/calls?page=", "/portal/calls?page=1.5"])
def test_calls_malformed_page_is_readable_400_and_anonymous_redirects(env, path):
    response = env.a.get(path)
    assert response.status_code == 400 and "text/html" in response.headers["content-type"]
    assert env.new_browser().get(path, follow_redirects=False).status_code == 303
