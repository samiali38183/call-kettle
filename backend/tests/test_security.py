"""Regression guards from docs/SECURITY_REVIEW.md: no route can be added without authentication, headers are present, nothing leaks."""
import re

import pytest

PUBLIC_ON_PURPOSE = {
    "/health", "/favicon.ico", "/book", "/book/availability", "/book/next-open", "/book/confirm", "/lead", "/terms", "/start", "/intake",
    "/admin/login", "/admin/logout", "/portal/login", "/portal/activate",  # activation requires a valid single-use invitation, not an existing session
}


def _routes(main):
    from fastapi.routing import APIRoute

    def walk(routes):
        for r in routes:
            if isinstance(r, APIRoute):
                yield r
                continue
            # FastAPI 0.12x may keep included routers lazy in app.routes; expand
            # them so security tests still see every effective endpoint.
            inner = getattr(getattr(r, "original_router", None), "routes", None)
            if inner:
                yield from walk(inner)

    yield from walk(main.app.routes)


def _fill(path):
    path = re.sub(r"\{[^}]+\}", "1", path)
    return path + ("?client_id=demo_hvac" if path.startswith("/voice/") and path != "/voice/status" else "")


def test_every_route_is_either_deliberately_public_or_refuses_anonymous_requests(app_client, monkeypatch):
    """The point of this test is the FUTURE: a new route that forgets its guard fails here."""
    client, main = app_client
    monkeypatch.setattr("app.twilio_utils._SKIP_SIGNATURE_CHECK", False)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t" * 32)
    unguarded = []
    for r in _routes(main):
        if r.path in PUBLIC_ON_PURPOSE:
            continue
        for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
            resp = client.request(method, _fill(r.path), follow_redirects=False)
            if resp.status_code not in (401, 403, 303, 307):
                unguarded.append((method, r.path, resp.status_code))
    assert not unguarded, f"routes that answered an anonymous request: {unguarded}"


def test_the_public_list_is_exactly_what_we_intend(app_client):
    client, main = app_client
    paths = {r.path for r in _routes(main)}
    assert PUBLIC_ON_PURPOSE <= paths, PUBLIC_ON_PURPOSE - paths


def test_api_docs_and_schema_are_not_published(app_client):
    client, main = app_client
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(p).status_code == 404


@pytest.mark.parametrize("path", ["/health", "/terms", "/admin/login", "/book"])
def test_every_response_carries_the_security_headers(app_client, path):
    client, main = app_client
    h = client.get(path).headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer"
    assert "max-age" in h["strict-transport-security"]
    assert "camera=()" in h["permissions-policy"]


def test_framing_is_refused_everywhere_including_the_booking_page(app_client):
    """The website links to /book, it does not embed it; an embeddable booking form can be clickjacked (QA 2026-10-04 M5)."""
    client, main = app_client
    assert client.get("/admin/login").headers["x-frame-options"] == "DENY"
    assert client.get("/terms").headers["x-frame-options"] == "DENY"
    assert client.get("/book").headers["x-frame-options"] == "DENY"


def test_private_pages_are_never_cached(app_client):
    client, main = app_client
    assert client.get("/admin/status", params={"key": "master_key_for_tests"}).headers["cache-control"] == "no-store"
    assert client.get("/report/demo_hvac", params={"key": "wrong"}).headers["cache-control"] == "no-store"


def test_a_header_already_set_by_a_page_is_not_overwritten_or_duplicated(app_client):
    client, main = app_client
    resp = client.get("/report/demo_hvac", params={"key": "wrong"})
    raw = [v for k, v in resp.headers.multi_items() if k.lower() == "referrer-policy"]
    assert raw == ["no-referrer"]


def test_a_forged_or_expired_login_cookie_is_rejected(app_client):
    import time

    client, main = app_client
    for bad in ("1", "forged.value", f"{int(time.time()) + 999}.deadbeef", f"{int(time.time()) - 5}." + "0" * 64, ""):
        client.cookies.clear()
        client.cookies.set("dl_admin", bad)
        assert client.get("/admin/status").status_code == 403
    client.cookies.clear()
    valid = main._admin_token(int(time.time()) + 60)
    client.cookies.set("dl_admin", valid)
    assert client.get("/admin/status").status_code == 200
    client.cookies.clear()
    client.cookies.set("dl_admin", main._admin_token(int(time.time()) - 1))
    assert client.get("/admin/status").status_code == 403


def test_the_master_key_never_appears_in_any_admin_page(app_client):
    client, main = app_client
    client.post("/admin/login", data={"key": "master_key_for_tests"})
    for path in ("/admin", "/admin/intakes", "/admin/status"):
        assert "master_key_for_tests" not in client.get(path).text, path


def test_secrets_in_the_environment_are_not_tracked_or_published():
    """Fails if a secret value from backend/.env appears in a git-tracked file."""
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    env_file = root / "backend" / ".env"
    if not env_file.exists():
        pytest.skip("no local .env to compare against")
    secrets = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            v = v.strip().strip('"')
            if len(v) >= 16 and any(t in k for t in ("KEY", "TOKEN", "SECRET", "PASSWORD", "SID")):
                secrets[k] = v
    tracked = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True).stdout.splitlines()
    leaks = []
    for f in tracked:
        try:
            text = (root / f).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        leaks += [(k, f) for k, v in secrets.items() if v in text]
    assert not leaks, leaks
