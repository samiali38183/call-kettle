"""Invitation activation; isolated SQLite and offline HTTP only."""
import hashlib
import importlib
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PW = "correct horse battery 42"
EMAIL = "owner@example.com"


@pytest.fixture
def activation(tmp_path, monkeypatch):
    from app import storage, owner_auth
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "activation.db"))
    storage.init_db()
    temp = owner_auth.create_user("demo_hvac", EMAIL)
    module = importlib.import_module("app.owner_activation")
    module.init_db()
    app = FastAPI()
    app.include_router(module.router)
    with TestClient(app, base_url="https://testserver") as client:
        yield module, client, storage, owner_auth, temp


def user(storage):
    with storage._conn() as conn:
        return conn.execute("SELECT id, client_id, email, pw_hash, must_change, failed_count, locked_until, disabled_at FROM owner_users WHERE email=?", (EMAIL,)).fetchone()


def test_invitation_is_hashed_short_lived_and_schema_is_idempotent(activation):
    module, _, storage, _, _ = activation
    assert module is not None, "Invitation implementation is missing"
    module.init_db()
    token = module.create_invitation(user(storage)[0])
    with storage._conn() as conn:
        row = conn.execute("SELECT token_hash, user_id, created_at, expires_at, consumed_at FROM owner_invitations").fetchone()
    assert len(token) >= 43
    assert row[0] == hashlib.sha256(token.encode()).hexdigest()
    assert row[1] == user(storage)[0]
    assert 0 < row[3] - row[2] <= 3600
    assert row[4] is None
    assert token.encode() not in __import__("pathlib").Path(storage.DB_PATH).read_bytes()


def begin(module, client, storage):
    token = module.create_invitation(user(storage)[0])
    page = client.get("/portal/activate", params={"token": token})
    assert page.status_code == 200
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    return token, csrf, page


def test_customer_chooses_password_for_invited_account_only(activation):
    module, client, storage, auth, temp = activation
    auth.create_user("demo_nova_plumbing", "owner@example.com")
    session, _ = auth.create_session(user(storage)[0])
    token, csrf, page = begin(module, client, storage)
    for header, value in [("cache-control", "no-store"), ("referrer-policy", "no-referrer")]:
        assert page.headers[header] == value
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "httponly" in page.headers["set-cookie"].lower()
    assert "secure" in page.headers["set-cookie"].lower()
    response = client.post("/portal/activate", data={"token": token, "csrf": csrf, "new": PW, "new2": PW,
        "email": "owner@example.com", "client_id": "demo_nova_plumbing"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/portal/login"
    row = user(storage)
    assert row[1:3] == ("demo_hvac", EMAIL)
    assert auth.verify_password(PW, row[3]) and not auth.verify_password(temp, row[3])
    assert row[4] == 0 and auth.get_session(session) is None
    with storage._conn() as conn:
        assert conn.execute("SELECT must_change FROM owner_users WHERE email='owner@example.com'").fetchone()[0] == 1
        assert conn.execute("SELECT consumed_at FROM owner_invitations").fetchone()[0] is not None


@pytest.mark.parametrize("kind", ["missing", "wrong", "other_browser", "other_token", "cross_origin"])
def test_csrf_failure_changes_nothing(activation, kind):
    module, client, storage, _, _ = activation
    token, csrf, _ = begin(module, client, storage)
    before = user(storage)
    headers = {}
    if kind == "missing":
        csrf = ""
    if kind == "wrong":
        csrf = "wrong"
    if kind == "other_browser":
        client.cookies.clear()
    if kind == "other_token":
        token = module.create_invitation(before[0])
    if kind == "cross_origin":
        headers = {"Origin": "https://attacker.example"}
    response = client.post("/portal/activate", data={"token": token, "csrf": csrf, "new": PW, "new2": PW}, headers=headers, follow_redirects=False)
    assert response.status_code == 403
    assert user(storage) == before
    with storage._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM owner_invitations WHERE consumed_at IS NOT NULL").fetchone()[0] == 0


@pytest.mark.parametrize("password,confirmation", [("short", "short"), ("x" * 201, "x" * 201),
    ("a" * 12, "a" * 12), ("customer-long-secret", "customer-long-secret"), (PW, "mismatch")])
def test_invalid_password_attempts_do_not_lock_account_or_consume_invitation(activation, password, confirmation):
    module, client, storage, _, _ = activation
    token, csrf, _ = begin(module, client, storage)
    before = user(storage)
    for _ in range(25):
        response = client.post("/portal/activate", data={"token": token, "csrf": csrf, "new": password, "new2": confirmation}, follow_redirects=False)
        assert response.status_code == 422
        assert password not in response.text
    assert user(storage) == before
    with storage._conn() as conn:
        assert conn.execute("SELECT consumed_at FROM owner_invitations").fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM owner_login_failures").fetchone()[0] == 0
    assert client.post("/portal/activate", data={"token": token, "csrf": csrf, "new": PW, "new2": PW}, follow_redirects=False).status_code == 303


@pytest.mark.parametrize("state", ["expired", "replayed", "disabled", "reset", "password_changed", "unknown"])
def test_unavailable_invitation_never_changes_existing_account(activation, state, monkeypatch):
    module, client, storage, auth, _ = activation
    token, csrf, _ = begin(module, client, storage)
    if state == "expired":
        now = auth._now()
        monkeypatch.setattr(auth, "_now", lambda: now + module.INVITATION_SECONDS)
    elif state == "replayed":
        assert client.post("/portal/activate", data={"token": token, "csrf": csrf, "new": PW, "new2": PW}, follow_redirects=False).status_code == 303
    elif state == "disabled":
        with storage._conn() as conn:
            conn.execute("UPDATE owner_users SET disabled_at=1")
    elif state == "reset":
        auth.reset_user(EMAIL)
    elif state == "password_changed":
        auth.set_password(user(storage)[0], PW)
    elif state == "unknown":
        token = "unknown"
        csrf = module._csrf(client.cookies.get(module.CSRF_COOKIE), token)
    before = user(storage)
    response = client.post("/portal/activate", data={"token": token, "csrf": csrf, "new": "another good password", "new2": "another good password"}, follow_redirects=False)
    assert response.status_code == 400
    assert EMAIL not in response.text and token not in response.text
    assert user(storage) == before
    assert client.get("/portal/activate", params={"token": token}).status_code == 400


@pytest.mark.parametrize("state", ["unknown", "disabled", "active"])
def test_issue_refuses_non_pending_accounts(activation, state):
    module, _, storage, auth, _ = activation
    uid = user(storage)[0]
    if state == "unknown":
        uid = 999
    elif state == "disabled":
        with storage._conn() as conn:
            conn.execute("UPDATE owner_users SET disabled_at=1")
    else:
        auth.set_password(uid, PW)
    before = user(storage)
    with pytest.raises(ValueError):
        module.create_invitation(uid)
    assert user(storage) == before
    with storage._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM owner_invitations").fetchone()[0] == 0


@pytest.fixture
def publishing(app_client, tmp_path, monkeypatch):
    from app import config, owner_activation
    client, main = app_client
    monkeypatch.setattr(config, "LIVE_CLIENTS_DIR", tmp_path / "live")
    config.load_client_config.cache_clear()
    main.app.include_router(owner_activation.router)
    yield client, main, config
    config.load_client_config.cache_clear()


def test_publish_hands_off_keyless_invitation_with_legacy_password_fallback(publishing):
    from app import storage, owner_auth
    from tests.test_admin_pages import INTAKE, MASTER, _login, _publish_yaml
    client, _, _ = publishing
    iid = storage.create_intake(dict(INTAKE))
    _login(client)
    response = client.post(f"/admin/intake/{iid}/publish", data={"yaml": _publish_yaml(client, iid)})
    assert response.status_code == 200
    match = re.search(r'href="(/portal/activate\?token=[^\"]+)"', response.text)
    assert match is not None, "Publish must show a customer activation invitation"
    link = match.group(1)
    token = link.split("token=", 1)[1]
    assert "key=" not in link and MASTER not in response.text
    assert "expires in one hour" in response.text.lower()
    assert "Temporary password</th><td><code>" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    client.cookies.clear()
    page = client.get(link)
    assert page.status_code == 200
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    result = client.post("/portal/activate", data={"token": token, "csrf": csrf, "new": PW, "new2": PW}, follow_redirects=False)
    assert result.status_code == 303
    assert owner_auth.authenticate(INTAKE["owner_email"], PW, "fixture")[0] == "ok"


@pytest.mark.parametrize("stage", ["consumption", "commit"])
def test_activation_storage_failure_rolls_back_password_sessions_and_consumption(activation, monkeypatch, stage):
    from contextlib import contextmanager
    module, client, storage, auth, _ = activation
    token, csrf, _ = begin(module, client, storage)
    session, _ = auth.create_session(user(storage)[0])
    before = user(storage)
    original_conn = storage._conn
    if stage == "consumption":
        with storage._conn() as conn:
            conn.execute("CREATE TRIGGER fail_consumption BEFORE UPDATE ON owner_invitations BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    @contextmanager
    def fail_commit():
        with original_conn() as conn:
            yield conn
            if conn.in_transaction:
                raise OSError("synthetic commit failure")
    with monkeypatch.context() as patch:
        if stage == "commit":
            patch.setattr(storage, "_conn", fail_commit)
        response = client.post("/portal/activate", data={"token": token, "csrf": csrf, "new": PW, "new2": PW}, follow_redirects=False)
    assert response.status_code == 503
    assert "synthetic" not in response.text
    assert user(storage) == before
    assert auth.get_session(session) is not None
    with storage._conn() as conn:
        assert conn.execute("SELECT consumed_at FROM owner_invitations").fetchone()[0] is None


def test_simultaneous_activation_consumes_once(activation):
    from concurrent.futures import ThreadPoolExecutor
    module, client, storage, auth, _ = activation
    token, csrf, _ = begin(module, client, storage)
    cookie = client.cookies.get(module.CSRF_COOKIE)
    def attempt(password):
        with TestClient(client.app, base_url="https://testserver") as browser:
            browser.cookies.set(module.CSRF_COOKIE, cookie)
            return password, browser.post("/portal/activate", data={"token": token, "csrf": csrf, "new": password, "new2": password}, follow_redirects=False).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [PW, "a second secure password"]))
    assert sorted(status for _, status in results) == [303, 400]
    winner = next(password for password, status in results if status == 303)
    assert auth.verify_password(winner, user(storage)[3])


@pytest.mark.parametrize("stage", ["invitation", "after_file", "commit"])
def test_failed_publication_commits_no_invitation_account_or_intake(publishing, monkeypatch, stage):
    from contextlib import contextmanager
    from app import admin_onboarding, owner_activation, storage
    from tests.test_admin_pages import INTAKE, _login, _publish_yaml
    client, _, config = publishing
    owner_activation.init_db()
    iid = storage.create_intake(dict(INTAKE))
    _login(client)
    text = _publish_yaml(client, iid)
    save = admin_onboarding.save_live_config
    original_conn = storage._conn
    def fail_invitation(*args, **kwargs):
        raise OSError("synthetic invitation failure")
    def fail_save(yaml):
        save(yaml)
        raise OSError("synthetic post-write failure")
    @contextmanager
    def fail_commit():
        with original_conn() as conn:
            yield conn
            if conn.in_transaction:
                raise OSError("synthetic commit failure")
    with monkeypatch.context() as patch:
        if stage == "invitation":
            patch.setattr(owner_activation, "create_invitation", fail_invitation)
        elif stage == "after_file":
            patch.setattr(admin_onboarding, "save_live_config", fail_save)
        else:
            patch.setattr(storage, "_conn", fail_commit)
        response = client.post(f"/admin/intake/{iid}/publish", data={"yaml": text})
    assert response.status_code == 503
    assert "acme_plumbing" not in config.list_client_ids()
    assert storage.get_intake(iid)["status"] == "new"
    with storage._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM owner_users").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM owner_invitations").fetchone()[0] == 0


def test_operator_can_complete_missing_email_on_review_without_reassigning_accounts(publishing):
    from app import storage
    from tests.test_admin_pages import INTAKE, _login, _publish_yaml
    client, _, config = publishing
    iid = storage.create_intake(dict(INTAKE, owner_email=""))
    _login(client)
    page = client.get(f"/admin/intake/{iid}/review")
    assert 'name="owner_email"' in page.text
    assert 'type="email"' in page.text
    response = client.post(f"/admin/intake/{iid}/publish", data={"yaml": _publish_yaml(client, iid), "owner_email": "  owner@example.com "})
    assert response.status_code == 200
    with storage._conn() as conn:
        assert conn.execute("SELECT email FROM owner_users").fetchone()[0] == "owner@example.com"
    assert config.load_client_config("acme_plumbing").owner_email == "owner@example.com"


@pytest.mark.parametrize("email", ["", "invalid", "owner@example.com"])
def test_explicit_operator_email_override_is_validated_before_publication(publishing, email):
    from app import owner_auth, storage
    from tests.test_admin_pages import INTAKE, _login, _publish_yaml
    client, _, config = publishing
    owner_auth.create_user("demo_hvac", "owner@example.com")
    with storage._conn() as conn:
        before = conn.execute("SELECT * FROM owner_users").fetchall()
    iid = storage.create_intake(dict(INTAKE))
    _login(client)
    response = client.post(f"/admin/intake/{iid}/publish", data={"yaml": _publish_yaml(client, iid), "owner_email": email})
    assert response.status_code == 422
    assert 'name="owner_email"' in response.text
    assert "acme_plumbing" not in config.list_client_ids()
    assert storage.get_intake(iid)["status"] == "new"
    with storage._conn() as conn:
        assert conn.execute("SELECT * FROM owner_users").fetchall() == before


def test_expired_invitation_cannot_be_bypassed_with_legacy_temporary_password(activation, monkeypatch):
    module, _, storage, auth, temp = activation
    module.create_invitation(user(storage)[0])
    now = auth._now()
    monkeypatch.setattr(auth, "_now", lambda: now + module.INVITATION_SECONDS + 1)
    assert auth.authenticate(EMAIL, temp, "offline-fixture")[0] == "bad"
