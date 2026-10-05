"""The fictional sales-demo portal is read-only for spam tagging too, and a real owner cannot tag a sample call."""
import sqlite3

from tests.test_demo_portal import REAL_EMAIL, _csrf, _real_browser, _sample_browser, env  # noqa: F401  (env is a fixture)


def _class(e, sid):
    conn = sqlite3.connect(e.storage.DB_PATH)
    try:
        return conn.execute("SELECT outcome_class FROM calls WHERE call_sid = ?", (sid,)).fetchone()[0]
    finally:
        conn.close()


def test_sample_portal_cannot_mark_or_undo_spam_and_buttons_are_disabled(env):
    b, _ = _sample_browser(env)
    page = b.get("/portal/overview").text
    assert "disabled>Not a customer (spam)" in page
    before = _class(env, "SAMPLE-PORTAL-05")
    for path in ("/portal/spam", "/portal/spam/undo"):
        r = b.post(path, data={"csrf": _csrf(page), "call": "SAMPLE-PORTAL-05"}, follow_redirects=False)
        assert r.status_code == 403
    assert _class(env, "SAMPLE-PORTAL-05") == before


def test_a_real_owner_can_mark_their_own_call_but_not_a_sample_call(env):
    _sample_browser(env)
    real = _real_browser(env)
    page = real.get("/portal/overview").text
    assert real.post("/portal/spam", data={"csrf": _csrf(page), "call": "SAMPLE-PORTAL-05"}, follow_redirects=False).status_code == 303
    assert _class(env, "SAMPLE-PORTAL-05") != "SPAM"
    assert real.post("/portal/spam", data={"csrf": _csrf(page), "call": "CA_REAL_1"}, follow_redirects=False).status_code == 303
    assert _class(env, "CA_REAL_1") == "SPAM"


import pytest


@pytest.fixture(autouse=True)
def enable_spam_tagging(monkeypatch):
    monkeypatch.setenv("CALLKETTLE_SPAM_TAGGING_ENABLED", "true")
