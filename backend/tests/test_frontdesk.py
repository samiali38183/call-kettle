"""Offline front-desk workspace tests; all persistence uses temporary SQLite."""
import importlib
import importlib.util
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import owner_auth, portal, storage

A, B = "demo_hvac", "demo_nova_plumbing"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "frontdesk.db"))
    storage.init_db()
    storage.log_call_start("call-a", A, "+15555550100")
    storage.log_call_start("call-b", B, "+15555550100")
    with storage._conn() as conn:
        conn.execute("UPDATE calls SET needs_attention = 1, summary = 'Please call back', outcome_class = 'CALLBACK_REQUESTED'")
    return storage


def feature():
    assert importlib.util.find_spec("app.frontdesk_storage"), "Front-desk storage is not implemented"
    return importlib.import_module("app.frontdesk_storage")


def test_owner_note_persists_with_idempotent_additive_migration(db):
    f = feature()
    f.init_db()
    f.save_note(A, "call-a", "Called customer; waiting on their model number.")
    f.init_db()
    assert f.get_call(A, "call-a")["owner_note"] == "Called customer; waiting on their model number."
    assert f.get_call(A, "call-a")["summary"] == "Please call back"
    assert f.get_call(B, "call-b")["owner_note"] == ""
    assert f.get_call(A, "call-b") is None
    with pytest.raises(LookupError):
        f.save_note(A, "call-b", "Intrusion")
    with pytest.raises(LookupError):
        f.save_note(A, "missing", "Missing")
    assert f.get_call(B, "call-b")["owner_note"] == ""


def test_explicit_state_due_date_handles_reopens_and_preserves_legacy_compatibility(db):
    f = feature()
    f.init_db()
    assert hasattr(f, "save_followup"), "Follow-up state is not implemented"
    assert f.get_call(A, "call-a")["state"] == "open"
    f.save_followup(A, "call-a", note="Contacted; awaiting approval", state="waiting", due_date="2026-10-06")
    row = f.get_call(A, "call-a")
    assert (row["state"], row["followup_due_date"], row["owner_note"]) == ("waiting", "2026-10-06", "Contacted; awaiting approval")
    assert row["followup_updated_at"]
    storage.resolve_attention(A, "call-a")
    assert f.get_call(A, "call-a")["state"] == "handled"
    f.save_followup(A, "call-a", note="Try again", state="open", due_date="2026-10-07")
    assert f.get_call(A, "call-a")["attention_resolved_at"] is None
    f.save_followup(A, "call-a", note="Done", state="handled", due_date="")
    row = f.get_call(A, "call-a")
    assert row["state"] == "handled" and row["attention_resolved_at"]
    f.save_followup(A, "call-a", note="No further action", state="none", due_date="")
    assert f.get_call(A, "call-a")["needs_attention"] == 0
    assert f.get_call(A, "call-a")["state"] == "none"
    with pytest.raises(LookupError):
        f.save_followup(A, "call-b", note="Intrusion", state="handled", due_date="")
    assert f.get_call(B, "call-b")["state"] == "open"


@pytest.mark.parametrize("changes", [
    {"note": "x" * 4001}, {"state": "garbage"}, {"state": ""},
    {"due_date": "2026-02-30"}, {"due_date": "2026-2-03"}, {"due_date": "20261006"},
    {"due_date": "2026-10-06T10:00"}, {"state": "handled", "due_date": "2026-10-06"},
    {"state": "none", "due_date": "2026-10-06"}, {"call_sid": "x" * 129}, {"call_sid": ""},
])
def test_invalid_annotations_rejected_without_partial_writes(db, changes):
    f = feature()
    f.init_db()
    args = dict(call_sid="call-a", note="Valid note", state="open", due_date="")
    args.update(changes)
    before = f.get_call(A, "call-a")
    with pytest.raises(ValueError):
        f.save_followup(A, **args)
    assert f.get_call(A, "call-a") == before


def test_note_length_boundary_is_enforced_by_note_helper(db):
    f = feature()
    f.init_db()
    f.save_note(A, "call-a", "x" * 4000)
    with pytest.raises(ValueError):
        f.save_note(A, "call-a", "x" * 4001)
    assert len(f.get_call(A, "call-a")["owner_note"]) == 4000


def test_queue_is_scoped_prioritized_paginated_and_includes_legacy_calls(db):
    f = feature()
    f.init_db()
    assert hasattr(f, "list_calls"), "Actionable queue is not implemented"
    for sid in ("due", "future", "done", "no-action"):
        storage.log_call_start(sid, A, "+15555550100")
    f.save_followup(A, "due", note="Overdue", state="open", due_date="2026-10-01")
    f.save_followup(A, "future", note="Waiting", state="waiting", due_date="2026-10-09")
    f.save_followup(A, "done", note="Done", state="handled", due_date="")
    rows, total = f.list_calls(A, view="queue", page=1, per_page=2)
    assert total == 3
    assert [r["call_sid"] for r in rows] == ["due", "future"]
    rows, total = f.list_calls(A, view="queue", page=2, per_page=2)
    assert total == 3 and [r["call_sid"] for r in rows] == ["call-a"]
    assert [r["call_sid"] for r in f.list_calls(A, view="handled")[0]] == ["done"]
    assert f.list_calls(A, view="all")[1] == 5
    storage.resolve_attention(A, "due")
    assert f.list_calls(A)[1] == 2
    for kw in ({"view": "bad"}, {"page": 0}, {"page": 10001}, {"per_page": 0}, {"per_page": 101}):
        with pytest.raises(ValueError):
            f.list_calls(A, **kw)


def web(monkeypatch):
    assert importlib.util.find_spec("app.frontdesk"), "Front-desk router is not implemented"
    frontdesk = importlib.import_module("app.frontdesk")
    monkeypatch.setattr(frontdesk, "datetime", SimpleNamespace(now=lambda tz: datetime(2026, 10, 3, 12, tzinfo=tz)))
    feature().init_db()
    app = FastAPI()
    app.include_router(portal.router)
    app.include_router(frontdesk.router)
    owner_auth.create_user(A, "owner@example.com")
    owner_auth.create_user(B, "owner@example.com")
    with storage._conn() as conn:
        ids = dict(conn.execute("SELECT client_id, id FROM owner_users"))
    owner_auth.set_password(ids[A], "offline testing password")
    token, csrf = owner_auth.create_session(ids[A])
    browser = TestClient(app)
    browser.cookies.set(portal.SESSION_COOKIE, token, path="/portal")
    config = portal.load_client_config(A).model_copy()
    monkeypatch.setattr(portal, "_config_for", lambda user: config)
    return SimpleNamespace(browser=browser, app=app, csrf=csrf, config=config, ids=ids)


def test_workspace_end_to_end_secure_edit_queue_and_reopen(db, monkeypatch):
    e = web(monkeypatch)
    f = feature()
    page = e.browser.get("/portal/frontdesk?client_id=" + B)
    assert page.status_code == 200
    assert 'href="/portal/frontdesk/call/call-a"' in page.text
    assert 'href="/portal/frontdesk/call/call-b"' not in page.text
    assert "tel:+15555550100" in page.text
    assert "1 call(s)" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    detail = e.browser.get("/portal/frontdesk/call/call-a")
    assert 'name="note"' in detail.text and 'name="due_date"' in detail.text
    assert e.csrf in detail.text
    unsafe = '</textarea><script>alert(1)</script>'
    r = e.browser.post("/portal/frontdesk/call/call-a", data={"csrf": e.csrf, "client_id": B, "revision": f.revision(f.get_call(A, "call-a")),
        "note": unsafe, "state": "waiting", "due_date": "2026-10-01"}, follow_redirects=False)
    assert r.status_code == 303
    row = f.get_call(A, "call-a")
    assert row["owner_note"] == unsafe and row["state"] == "waiting"
    page = e.browser.get("/portal/frontdesk")
    assert "Overdue" in page.text and "<script>" not in page.text
    assert "&lt;script&gt;" in page.text
    detail = e.browser.get("/portal/frontdesk/call/call-a")
    assert "&lt;/textarea&gt;" in detail.text and "<script>" not in detail.text
    before = f.get_call(A, "call-a")
    assert e.browser.post("/portal/frontdesk/call/call-a", data={"note": "CSRF", "state": "handled"}).status_code == 403
    assert f.get_call(A, "call-a") == before
    foreign = e.browser.get("/portal/frontdesk/call/call-b")
    missing = e.browser.get("/portal/frontdesk/call/missing")
    assert foreign.status_code == missing.status_code == 404 and foreign.text == missing.text
    assert e.browser.post("/portal/frontdesk/call/call-b", data={"csrf": e.csrf, "note": "Attack", "state": "handled"}).status_code == 404
    assert f.get_call(B, "call-b")["state"] == "open"
    assert e.browser.post("/portal/frontdesk/call/call-a", data={"csrf": e.csrf, "revision": f.revision(f.get_call(A, "call-a")), "note": "Done", "state": "handled"}, follow_redirects=False).status_code == 303
    assert "Nothing waiting" in e.browser.get("/portal/frontdesk").text
    handled = e.browser.get("/portal/frontdesk?view=handled")
    assert 'href="/portal/frontdesk/call/call-a"' in handled.text
    assert "Reopen" in e.browser.get("/portal/frontdesk/call/call-a").text
    assert e.browser.post("/portal/frontdesk/call/call-a", data={"csrf": e.csrf, "revision": f.revision(f.get_call(A, "call-a")), "note": "Try again", "state": "open"}, follow_redirects=False).status_code == 303
    assert f.get_call(A, "call-a")["attention_resolved_at"] is None
    e.config.portal_sample = True
    before = f.get_call(A, "call-a")
    assert "Read-only" in e.browser.get("/portal/frontdesk").text
    assert 'name="note"' not in e.browser.get("/portal/frontdesk/call/call-a").text
    assert e.browser.post("/portal/frontdesk/call/call-a", data={"csrf": e.csrf, "note": "Sample attack", "state": "handled"}).status_code == 403
    assert f.get_call(A, "call-a") == before
    anon = TestClient(e.app)
    for path in ("/portal/frontdesk", "/portal/frontdesk/call/call-a"):
        assert anon.get(path, follow_redirects=False).headers["location"] == "/portal/login"
    assert anon.post("/portal/frontdesk/call/call-a", data={}, follow_redirects=False).headers["location"] == "/portal/login"


@pytest.mark.parametrize("length", [129, 2000])
def test_route_rejects_oversized_call_identifiers_before_query(db, monkeypatch, length):
    e = web(monkeypatch)
    path = "/portal/frontdesk/call/" + "x" * length
    assert e.browser.get(path).status_code == 400
    assert e.browser.post(path, data={"csrf": e.csrf, "note": "", "state": "open"}).status_code == 400


@pytest.mark.parametrize("changes", [{"note": "x" * 4001}, {"state": "bad"}, {"state": ""},
    {"due_date": "2026-02-30"}, {"due_date": "20261006"}, {"state": "handled", "due_date": "2026-10-06"}])
def test_http_invalid_inputs_leave_call_unchanged(db, monkeypatch, changes):
    e = web(monkeypatch)
    before = feature().get_call(A, "call-a")
    fields = dict(csrf=e.csrf, revision=feature().revision(before), note="Valid", state="open", due_date="")
    fields.update(changes)
    response = e.browser.post("/portal/frontdesk/call/call-a", data=fields)
    assert response.status_code == 400
    assert feature().get_call(A, "call-a") == before


def test_http_filters_pagination_privacy_and_wrong_session_csrf(db, monkeypatch):
    e = web(monkeypatch)
    for query in ("view=bad", "page=0", "page=10001"):
        assert e.browser.get("/portal/frontdesk?" + query).status_code == 400
    for i in range(50):
        storage.log_call_start(f"extra-{i}", A, "+15555550100")
    page = e.browser.get("/portal/frontdesk?view=all")
    assert "51 call(s)" in page.text and "Next" in page.text
    page = e.browser.get("/portal/frontdesk?view=all&page=2")
    assert "51 call(s)" in page.text and "Previous" in page.text and "Next" not in page.text
    token, other_csrf = owner_auth.create_session(e.ids[B])
    before = feature().get_call(A, "call-a")
    assert e.browser.post("/portal/frontdesk/call/call-a", data={"csrf": other_csrf, "note": "Forged", "state": "handled"}).status_code == 403
    assert feature().get_call(A, "call-a") == before
    with storage._conn() as conn:
        conn.execute("UPDATE calls SET summary = '<script>unsafe summary</script>', from_number = 'javascript:alert(1)' WHERE call_sid = 'call-a'")
    page = e.browser.get("/portal/frontdesk")
    assert "<script>" not in page.text and "&lt;script&gt;" in page.text
    assert 'href="javascript:' not in page.text and 'class="btn call-back"' not in page.text
    e.config.record_transcripts = False
    for path in ("/portal/frontdesk", "/portal/frontdesk/call/call-a"):
        page = e.browser.get(path)
        assert "unsafe summary" not in page.text and "Details not recorded for privacy" in page.text


def test_auth_gates_forced_change_disabled_expired_and_offboarded_accounts(db, monkeypatch):
    e = web(monkeypatch)
    with storage._conn() as conn:
        conn.execute("UPDATE owner_users SET must_change = 1 WHERE client_id = ?", (A,))
    for method in ("get", "post"):
        r = getattr(e.browser, method)("/portal/frontdesk/call/call-a", follow_redirects=False)
        assert r.headers["location"] == "/portal/password"
    with storage._conn() as conn:
        conn.execute("UPDATE owner_users SET must_change = 0, disabled_at = 1 WHERE client_id = ?", (A,))
    assert e.browser.get("/portal/frontdesk", follow_redirects=False).headers["location"] == "/portal/login"
    with storage._conn() as conn:
        conn.execute("UPDATE owner_users SET disabled_at = NULL WHERE client_id = ?", (A,))
    token, _ = owner_auth.create_session(e.ids[A])
    e.browser.cookies.set(portal.SESSION_COOKIE, token, path="/portal")
    with storage._conn() as conn:
        conn.execute("UPDATE owner_sessions SET expires_at = 0")
    assert e.browser.get("/portal/frontdesk", follow_redirects=False).headers["location"] == "/portal/login"
    token, _ = owner_auth.create_session(e.ids[A])
    e.browser.cookies.set(portal.SESSION_COOKIE, token, path="/portal")
    monkeypatch.setattr(portal, "_config_for", lambda user: None)
    assert e.browser.get("/portal/frontdesk", follow_redirects=False).headers["location"] == "/portal/login"
    assert owner_auth.get_session(token) is None


def test_http_malformed_page_is_a_readable_400_and_unauthenticated_still_redirects(db, monkeypatch):
    e = web(monkeypatch)
    for query in ("page=abc", "page=", "page=1.5", "page=-"):
        response = e.browser.get("/portal/frontdesk?" + query)
        assert response.status_code == 400, query
        assert "text/html" in response.headers["content-type"], query
        assert "Back to front desk" in response.text, query
    anonymous = TestClient(e.app).get("/portal/frontdesk?page=abc", follow_redirects=False)
    assert anonymous.status_code == 303
