"""Admin login (cookie), one-click onboarding pages, number purchase, and the client's activation page."""
import time

import pytest

MASTER = "master_key_for_tests"

INTAKE = {
    "business_name": "Acme Plumbing", "owner_name": "Dana Ruiz", "owner_phone": "+15555550100", "owner_email": "owner@example.com",
    "trade": "plumbing", "phone_provider": "Verizon",
    "hours": {"mon": "08:00-17:00", "tue": "08:00-17:00", "wed": "08:00-17:00", "thu": "08:00-17:00", "fri": "08:00-17:00", "sat": "closed", "sun": "closed"},
    "services": [{"name": "Leak repair", "minutes": 60}, {"name": "Free estimate", "minutes": 30}],
    "faqs": [{"q": "Do you charge for estimates?", "a": "No, estimates are free."}],
    "never_say": "", "google_calendar_email": "", "notes": "Prefers morning calls.",
}


@pytest.fixture
def admin(app_client, tmp_path, monkeypatch):
    client, main = app_client
    from app import config as config_module

    monkeypatch.setattr(config_module, "LIVE_CLIENTS_DIR", tmp_path / "live")
    config_module.load_client_config.cache_clear()
    yield client, main, config_module
    config_module.load_client_config.cache_clear()


def _login(client):
    r = client.post("/admin/login", data={"key": MASTER}, follow_redirects=False)
    assert r.status_code == 303
    client.headers["Origin"] = "http://testserver"
    return r


# ------------------------------------------------------------------ login

def test_wrong_key_does_not_sign_in_and_sets_no_cookie(admin):
    client, _, _ = admin
    r = client.post("/admin/login", data={"key": "nope"}, follow_redirects=False)
    assert r.status_code == 303 and "failed=1" in r.headers["location"] and "dl_admin" not in r.headers.get("set-cookie", "")
    assert client.get("/admin").status_code == 403


def test_the_session_cookie_is_httponly_samesite_strict_and_expires(admin):
    client, _, _ = admin
    cookie = _login(client).headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=43200" in cookie


def test_after_login_admin_pages_open_without_any_key_and_never_contain_the_master_key(admin):
    client, _, _ = admin
    _login(client)
    for path in ("/admin", "/admin/status", "/admin/intakes"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert MASTER not in r.text, path


def test_a_tampered_or_expired_cookie_is_rejected(admin):
    client, main, _ = admin
    good = main._admin_token(int(time.time()) + 3600)
    for bad in ("garbage", good[:-3] + "abc", main._admin_token(int(time.time()) - 5), "+15555550100." + "0" * 64, ""):
        client.cookies.clear()
        client.cookies.set("dl_admin", bad)
        assert client.get("/admin").status_code == 403, bad
    client.cookies.clear()
    client.cookies.set("dl_admin", good)
    assert client.get("/admin").status_code == 200


def test_logout_clears_access(admin):
    client, _, _ = admin
    _login(client)
    assert client.get("/admin").status_code == 200
    client.get("/admin/logout", follow_redirects=False)
    client.cookies.clear()
    assert client.get("/admin").status_code == 403


def test_brute_forcing_the_login_is_rate_limited(admin):
    client, _, _ = admin
    statuses = [client.post("/admin/login", data={"key": f"guess{i}"}, follow_redirects=False).status_code for i in range(12)]
    assert statuses[:10] == [303] * 10 and statuses[10:] == [429, 429]
    assert client.post("/admin/login", data={"key": MASTER}, follow_redirects=False).status_code == 429   # even the right key is blocked while locked out


def test_the_old_key_parameter_still_works_for_scripts(admin):
    client, _, _ = admin
    assert client.get("/admin/status", params={"key": MASTER}).status_code == 200


# ------------------------------------------------------------------ review and go live

def _intake(main):
    from app import storage

    return storage.create_intake(dict(INTAKE))


def test_the_review_page_shows_the_generated_config_for_a_signed_in_operator(admin):
    client, main, _ = admin
    iid = _intake(main)
    assert client.get(f"/admin/intake/{iid}/review").status_code == 403           # not signed in
    _login(client)
    page = client.get(f"/admin/intake/{iid}/review")
    assert page.status_code == 200 and "Acme Plumbing" in page.text and "client_id: acme_plumbing" in page.text
    assert "Leak repair" in page.text and "Prefers morning calls" in page.text and MASTER not in page.text


def test_go_live_publishes_the_config_marks_the_intake_and_the_receptionist_answers(admin):
    client, main, cfg = admin
    from app import storage

    iid = _intake(main)
    _login(client)
    yaml_text = client.get(f"/admin/intake/{iid}/review").text.split("<textarea")[1].split(">", 1)[1].split("</textarea>")[0]
    import html

    r = client.post(f"/admin/intake/{iid}/publish", data={"yaml": html.unescape(yaml_text)})
    assert r.status_code == 200 and "is live" in r.text and "Get a phone number" in r.text and MASTER not in r.text
    assert "Owner portal login" in r.text and "/portal/login" in r.text and "owner@example.com" in r.text and "Temporary password" in r.text
    temp = r.text.split("Temporary password</th><td><code>", 1)[1].split("</code>", 1)[0]
    assert len(temp) >= 12
    assert "acme_plumbing" in cfg.list_client_ids()
    assert storage.get_intake(iid)["status"] == "live"
    raw_db = __import__("pathlib").Path(storage.DB_PATH).read_bytes()
    assert temp.encode() not in raw_db
    row = __import__("sqlite3").connect(storage.DB_PATH).execute("SELECT client_id, email, pw_hash, must_change FROM owner_users").fetchone()
    assert row[0] == "acme_plumbing" and row[1] == "owner@example.com" and row[2].startswith("scrypt$") and row[3] == 1
    call = client.post("/voice/incoming?client_id=acme_plumbing", data={"CallSid": "CA_ACME", "From": "+15555550100"})
    assert "Acme Plumbing" in call.text or "AI" in call.text


def _publish_yaml(client, iid):
    import html
    return html.unescape(client.get(f"/admin/intake/{iid}/review").text.split("<textarea")[1].split(">", 1)[1].split("</textarea>")[0])


@pytest.mark.parametrize("email", ["", "invalid", "owner@example.com"])
def test_publish_requires_a_new_valid_owner_account_before_going_live(admin, email):
    from app import owner_auth, storage
    client, _, cfg = admin
    if email == "owner@example.com":
        owner_auth.create_user("demo_hvac", email)
    iid = storage.create_intake(dict(INTAKE, owner_email=email))
    _login(client)
    response = client.post(f"/admin/intake/{iid}/publish", data={"yaml": _publish_yaml(client, iid)})
    assert response.status_code == 422
    assert "acme_plumbing" not in cfg.list_client_ids()
    assert storage.get_intake(iid)["status"] == "new"
    with storage._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM owner_users WHERE client_id='acme_plumbing'").fetchone()[0] == 0


def test_duplicate_publish_cannot_create_another_tenant_or_change_live_config(admin):
    from app import storage
    client, main, cfg = admin
    iid = _intake(main)
    _login(client)
    text = _publish_yaml(client, iid)
    assert client.post(f"/admin/intake/{iid}/publish", data={"yaml": text}).status_code == 200
    original = (cfg.LIVE_CLIENTS_DIR / "acme_plumbing.yaml").read_bytes()
    response = client.post(f"/admin/intake/{iid}/publish", data={"yaml": text.replace("acme_plumbing", "different_tenant")})
    assert response.status_code == 409
    assert "Temporary password" not in response.text
    assert (cfg.LIVE_CLIENTS_DIR / "acme_plumbing.yaml").read_bytes() == original
    assert "different_tenant" not in cfg.list_client_ids()
    assert client.get(f"/admin/intake/{iid}/review").status_code == 409
    with storage._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM owner_users").fetchone()[0] == 1


def test_owner_account_failure_rolls_back_publish(admin, monkeypatch):
    from app import owner_auth, storage
    client, main, cfg = admin
    iid = _intake(main)
    _login(client)
    text = _publish_yaml(client, iid)
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic account failure")
    monkeypatch.setattr(owner_auth, "create_user", fail)
    response = client.post(f"/admin/intake/{iid}/publish", data={"yaml": text})
    assert response.status_code == 503
    assert "synthetic account failure" not in response.text
    assert "acme_plumbing" not in cfg.list_client_ids()
    assert storage.get_intake(iid)["status"] == "new"


def test_new_intake_cannot_replace_an_existing_tenant(admin):
    from app import storage
    client, main, cfg = admin
    iid = _intake(main)
    _login(client)
    text = _publish_yaml(client, iid)
    cfg.save_live_config(text)
    original = (cfg.LIVE_CLIENTS_DIR / "acme_plumbing.yaml").read_bytes()
    response = client.post(f"/admin/intake/{iid}/publish", data={"yaml": text.replace("Acme Plumbing", "Other Plumbing")})
    assert response.status_code == 422
    assert (cfg.LIVE_CLIENTS_DIR / "acme_plumbing.yaml").read_bytes() == original
    assert storage.get_intake(iid)["status"] == "new"


def test_cookie_authenticated_publish_rejects_cross_site_or_missing_origin(admin):
    from app import storage
    client, main, cfg = admin
    iid = _intake(main)
    _login(client)
    text = _publish_yaml(client, iid)
    for origin in ("https://attacker.example", ""):
        response = client.post(f"/admin/intake/{iid}/publish", data={"yaml": text}, headers={"Origin": origin})
        assert response.status_code == 403
        assert storage.get_intake(iid)["status"] == "new"
        assert "acme_plumbing" not in cfg.list_client_ids()


@pytest.mark.parametrize("stage", ["before_file", "after_file", "database_commit"])
def test_storage_failure_rolls_back_account_intake_and_new_config(admin, monkeypatch, stage):
    from contextlib import contextmanager
    from app import admin_onboarding, storage
    client, main, cfg = admin
    iid = _intake(main)
    _login(client)
    text = _publish_yaml(client, iid)
    save = admin_onboarding.save_live_config
    original_conn = storage._conn
    def fail_save(yaml):
        if stage == "after_file":
            save(yaml)
        raise OSError("synthetic storage failure")
    @contextmanager
    def fail_commit():
        with original_conn() as conn:
            yield conn
            if conn.in_transaction:
                raise OSError("synthetic commit failure")
    with monkeypatch.context() as patch:
        if stage == "database_commit":
            patch.setattr(storage, "_conn", fail_commit)
        else:
            patch.setattr(admin_onboarding, "save_live_config", fail_save)
        response = client.post(f"/admin/intake/{iid}/publish", data={"yaml": text})
    assert response.status_code == 503
    assert "acme_plumbing" not in cfg.list_client_ids()
    assert storage.get_intake(iid)["status"] == "new"
    with storage._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM owner_users").fetchone()[0] == 0


def test_simultaneous_publish_creates_exactly_one_owner_and_tenant(admin):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    from app import storage
    client, main, cfg = admin
    iid = _intake(main)
    _login(client)
    text = _publish_yaml(client, iid)
    def publish(_):
        with TestClient(main.app) as browser:
            return browser.post(f"/admin/intake/{iid}/publish", params={"key": MASTER}, data={"yaml": text}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(publish, range(2))) == [200, 409]
    with storage._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM owner_users").fetchone()[0] == 1
    assert (cfg.LIVE_CLIENTS_DIR / "acme_plumbing.yaml").is_file()


def test_published_owner_first_login_and_recovery_preserve_tenant_binding(admin, caplog):
    import re
    from app import owner_auth, storage
    client, main, _ = admin
    iid = _intake(main)
    _login(client)
    response = client.post(f"/admin/intake/{iid}/publish", data={"yaml": _publish_yaml(client, iid)})
    temp = response.text.split("Temporary password</th><td><code>", 1)[1].split("</code>", 1)[0]
    def csrf(page):
        return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    def signin(password):
        return client.post("/portal/login", data={"email": INTAKE["owner_email"], "password": password, "csrf": csrf(client.get("/portal/login"))}, follow_redirects=False)
    assert signin(temp).headers["location"] == "/portal/password"
    assert client.get("/portal/export/bookings.csv", follow_redirects=False).headers["location"] == "/portal/password"
    new_pw = "new customer passphrase 42"
    changed = client.post("/portal/password", data={"current": temp, "new": new_pw, "new2": new_pw, "csrf": csrf(client.get("/portal/password"))}, follow_redirects=False)
    assert changed.headers["location"] == "/portal/overview"
    token = client.cookies.get("ck_portal")
    for path in ("overview", "calls", "calendar", "settings", "export/calls.csv", "export/bookings.csv"):
        assert client.get("/portal/" + path).status_code == 200
    recovery = owner_auth.reset_user(INTAKE["owner_email"])
    assert owner_auth.get_session(token) is None
    assert client.get("/portal/overview", follow_redirects=False).headers["location"] == "/portal/login"
    assert signin(new_pw).status_code == 200
    assert signin(recovery).headers["location"] == "/portal/password"
    assert client.post("/portal/logout", data={"csrf": csrf(client.get("/portal/password"))}, follow_redirects=False).status_code == 303
    with storage._conn() as conn:
        assert conn.execute("SELECT client_id, must_change FROM owner_users").fetchone() == ("acme_plumbing", 1)
    assert temp not in caplog.text and recovery not in caplog.text and new_pw not in caplog.text


def test_a_broken_config_is_refused_with_the_reason_and_the_text_is_kept(admin):
    client, main, cfg = admin
    iid = _intake(main)
    _login(client)
    r = client.post(f"/admin/intake/{iid}/publish", data={"yaml": "client_id: acme_plumbing\nbusiness_name: x\n"})
    assert r.status_code == 422 and "textarea" in r.text and "acme_plumbing" not in cfg.list_client_ids()


def test_a_second_intake_with_the_same_business_name_gets_its_own_id(admin):
    client, main, cfg = admin
    iid = _intake(main)
    _login(client)
    import html

    def publish(i):
        y = client.get(f"/admin/intake/{i}/review").text.split("<textarea")[1].split(">", 1)[1].split("</textarea>")[0]
        return client.post(f"/admin/intake/{i}/publish", data={"yaml": html.unescape(y)})

    assert publish(iid).status_code == 200
    from app import storage
    iid2 = storage.create_intake(dict(INTAKE, owner_email="owner@example.com"))
    page = client.get(f"/admin/intake/{iid2}/review").text
    assert "client_id: acme_plumbing_2" in page
    assert publish(iid2).status_code == 200 and {"acme_plumbing", "acme_plumbing_2"} <= set(cfg.list_client_ids())


def test_an_intake_that_cannot_become_a_config_shows_why(admin):
    client, main, _ = admin
    from app import storage

    bad = dict(INTAKE, owner_phone="123")
    iid = storage.create_intake(bad)
    _login(client)
    r = client.get(f"/admin/intake/{iid}/review")
    assert r.status_code == 422 and "10-digit" in r.text


def test_the_admin_index_links_to_review_and_number_without_a_key(admin):
    client, main, _ = admin
    _intake(main)
    _login(client)
    page = client.get("/admin").text
    assert "/admin/intake/1/review" in page and "/number" in page and MASTER not in page


# ------------------------------------------------------------------ buying a number

class _FakeNumber:
    def __init__(self, phone_number, sid="PN123"):
        self.phone_number, self.sid, self.voice_url = phone_number, sid, ""


class _FakeTwilio:
    def __init__(self, available=("+15555550100",), fail=False):
        self.created = []
        outer = self
        self.fail = fail

        class Local:
            def list(self, area_code, limit):
                return [_FakeNumber(n) for n in available if n[2:5] == str(area_code)][:limit]

        class Avail:
            local = Local()

        class Incoming:
            def create(self, phone_number, **kw):
                if outer.fail:
                    raise RuntimeError("Twilio says: number no longer available")
                outer.created.append((phone_number, kw))
                return _FakeNumber(phone_number, "PN_NEW")

            def list(self, **kw):
                return []

        self.available_phone_numbers = lambda country: Avail()
        self.incoming_phone_numbers = Incoming()


def _live_client(admin):
    client, main, cfg = admin
    from app import storage

    iid = storage.create_intake(dict(INTAKE))
    _login(client)
    import html

    y = client.get(f"/admin/intake/{iid}/review").text.split("<textarea")[1].split(">", 1)[1].split("</textarea>")[0]
    client.post(f"/admin/intake/{iid}/publish", data={"yaml": html.unescape(y)})
    return client, main


def test_searching_shows_a_number_and_buying_connects_it_with_the_safety_fallback(admin, monkeypatch):
    client, main = _live_client(admin)
    from app import provisioning, twilio_utils

    fake = _FakeTwilio()
    monkeypatch.setattr(twilio_utils, "_client", lambda: fake)
    found = client.post("/admin/client/acme_plumbing/number", data={"action": "search", "area_code": "571"})
    assert "+15555550100" in found.text and "Buy this number" in found.text and fake.created == []      # searching buys nothing
    bought = client.post("/admin/client/acme_plumbing/number", data={"action": "buy", "number": "+15555550100"})
    assert bought.status_code == 200 and "now answers as Acme Plumbing" in bought.text
    number, settings = fake.created[0]
    assert number == "+15555550100"
    assert settings["voice_url"].endswith("/voice/incoming?client_id=acme_plumbing")
    assert "api/fallback?to=%2B+15555550100" in settings["voice_fallback_url"]       # rings the owner if our server is down
    assert provisioning.numbers_for("acme_plumbing") == ["+15555550100"]


def test_no_numbers_in_an_area_code_is_explained_not_an_error(admin, monkeypatch):
    client, _ = _live_client(admin)
    from app import twilio_utils

    monkeypatch.setattr(twilio_utils, "_client", lambda: _FakeTwilio(available=()))
    r = client.post("/admin/client/acme_plumbing/number", data={"action": "search", "area_code": "202"})
    assert r.status_code == 200 and "No numbers are available" in r.text


@pytest.mark.parametrize("area", ["12", "abcd", "", "+15555550100"])
def test_a_bad_area_code_is_rejected(admin, monkeypatch, area):
    client, _ = _live_client(admin)
    from app import twilio_utils

    monkeypatch.setattr(twilio_utils, "_client", lambda: _FakeTwilio())
    assert client.post("/admin/client/acme_plumbing/number", data={"action": "search", "area_code": area}).status_code == 422


def test_a_twilio_failure_changes_nothing_and_shows_the_reason(admin, monkeypatch):
    client, _ = _live_client(admin)
    from app import provisioning, twilio_utils

    monkeypatch.setattr(twilio_utils, "_client", lambda: _FakeTwilio(fail=True))
    r = client.post("/admin/client/acme_plumbing/number", data={"action": "buy", "number": "+15555550100"})
    assert r.status_code == 502 and "no longer available" in r.text and provisioning.numbers_for("acme_plumbing") == []


@pytest.mark.parametrize("number", ["+15555550100", "+1555555010050", "+1571555", "javascript:alert(1)"])
def test_only_us_numbers_can_be_bought(admin, monkeypatch, number):
    client, _ = _live_client(admin)
    from app import twilio_utils

    fake = _FakeTwilio()
    monkeypatch.setattr(twilio_utils, "_client", lambda: fake)
    assert client.post("/admin/client/acme_plumbing/number", data={"action": "buy", "number": number}).status_code == 422
    assert fake.created == []


def test_number_pages_need_the_operator_and_an_existing_client(admin):
    client, _, _ = admin
    assert client.get("/admin/client/acme_plumbing/number").status_code == 403
    _login(client)
    assert client.get("/admin/client/no_such_client/number").status_code == 404


# ------------------------------------------------------------------ the client's activation page

def test_the_activation_page_fills_in_the_clients_number_in_every_code(admin):
    client, main = _live_client(admin)
    from app import provisioning

    provisioning.record_number("acme_plumbing", "+15555550100", "PN1")
    page = client.get(f"/activate/acme_plumbing?key={main.report_key_for('acme_plumbing')}")
    assert page.status_code == 200 and "+15555550100" in page.text and MASTER not in page.text
    for code in ("*+1555555010023", "*+1555555010023", "*73", "*21*+15555550100#", "#21#", "**21*+15555550100#", "**61*+15555550100#", "##21#"):
        assert code in page.text, code
    assert "ask AT&amp;T" in page.text            # no invented AT&T no-answer code
    assert "/portal/login" in page.text and "owner portal" in page.text
    assert "*61*+15555550100" not in page.text


def test_only_that_client_or_the_operator_can_open_the_activation_page(admin):
    client, main = _live_client(admin)
    from app import provisioning

    provisioning.record_number("acme_plumbing", "+15555550100", None)
    assert client.get("/activate/acme_plumbing").status_code == 403
    assert client.get(f"/activate/acme_plumbing?key={main.report_key_for('callkettle_demo')}").status_code == 403   # another client's key
    assert client.get(f"/activate/acme_plumbing?key={main.report_key_for('acme_plumbing')}").status_code == 200
    assert client.get(f"/activate/acme_plumbing?key={MASTER}").status_code == 200


def test_without_a_number_the_client_sees_a_friendly_wait_page(admin):
    client, main = _live_client(admin)
    page = client.get(f"/activate/acme_plumbing?key={main.report_key_for('acme_plumbing')}")
    assert page.status_code == 200 and "Almost ready" in page.text


def test_forwarding_codes_use_only_verified_formats():
    from app.admin_onboarding import forwarding_codes

    c = forwarding_codes("+15555550100")
    assert c["Verizon"] == {"all": "*+1555555010023", "no_answer": "*+1555555010023", "off": "*73"}
    assert c["AT&T"]["no_answer"] is None and c["AT&T"]["all"] == "*21*+15555550100#" and c["AT&T"]["off"] == "#21#"
    assert c["T-Mobile"]["all"] == "**21*+15555550100#" and c["T-Mobile"]["no_answer"] == "**61*+15555550100#"


def test_the_activation_page_is_phone_friendly_with_a_tap_to_call_help_line_and_portal_button(admin):
    client, main = _live_client(admin)
    from app import brand, provisioning

    provisioning.record_number("acme_plumbing", "+15555550100", None)
    page = client.get(f"/activate/acme_plumbing?key={main.report_key_for('acme_plumbing')}").text
    assert 'name="viewport"' in page and "max-width:600px" in page and '<div class="wrap"><table>' in page    # wide code table scrolls instead of breaking the page
    assert "Open my owner portal" in page and 'href="/portal/login"' in page and 'href="tel:+1' in page and brand.get().support_phone in page
    assert "<script" not in page.lower()
    provisioning_numbers = client.get(f"/activate/acme_plumbing?key={MASTER}").text
    assert "different phone" in provisioning_numbers
