"""Owner-driven spam / not-a-customer tagging: one tap on a callback, tenant-scoped and CSRF-safe. The number is remembered for this business only,
so the owner is no longer paged for it. Nothing is blocked and no caller is contacted."""
import sqlite3

from tests.test_owner_portal import A_ID, B_ID, NEW_PW, _csrf, _db, _seed_call, env  # noqa: F401  (env is a fixture)

SPAM_NUMBER = "+15555550100"


def _tap(e, browser, sid, path="/portal/spam", token=None):
    token = token or _csrf(browser.get("/portal/overview").text)
    return browser.post(path, data={"call": sid, "csrf": token}, follow_redirects=False)


def _row(e, sid):
    conn = _db(e)
    try:
        return conn.execute("SELECT outcome_class, needs_attention, attention_resolved_at FROM calls WHERE call_sid = ?", (sid,)).fetchone()
    finally:
        conn.close()


def test_overview_offers_spam_button_on_each_callback(env):
    _seed_call(env, "s1", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, frm=SPAM_NUMBER)
    html = env.a.get("/portal/overview").text
    assert "Mark handled" in html and "Not a customer" in html
    assert 'action="/portal/spam"' in html


def test_marking_spam_tags_the_call_clears_the_queue_and_remembers_the_number(env):
    _seed_call(env, "s1", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, frm=SPAM_NUMBER)
    r = _tap(env, env.a, "s1")
    assert r.status_code == 303
    cls, attention, resolved = _row(env, "s1")
    assert cls == "SPAM" and resolved is not None
    html = env.a.get("/portal/overview").text
    assert "Needs your attention (0)" in html
    assert "Marked as not a customer" in html and "Mark handled" not in html


def test_future_calls_from_a_marked_number_do_not_page_the_owner(env, monkeypatch):
    from app import notify, tools

    _seed_call(env, "s1", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, frm=SPAM_NUMBER)
    _tap(env, env.a, "s1")
    # a later call from the same caller ID
    env.storage.log_call_start("s2", A_ID, SPAM_NUMBER)
    sent = []
    monkeypatch.setattr(notify, "notify_owner", lambda config, *, title, body, ics=None: sent.append(title))
    from app.config import load_client_config

    result = tools.escalate_to_human(call_sid="s2", config=load_client_config(A_ID), reason="callback", caller_name="x", caller_phone="+1",
                                     summary="Extended warranty")
    assert result["escalated"] is True and result["recorded"] is True      # still recorded for the owner's history
    assert sent == []                                                       # but nobody was paged
    env.storage.classify_and_store("s2")
    cls, attention, _ = _row(env, "s2")
    assert cls == "SPAM" and attention == 0


def test_other_businesses_are_still_paged_for_the_same_number(env, monkeypatch):
    from app import notify, tools
    from app.config import load_client_config

    _seed_call(env, "s1", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, frm=SPAM_NUMBER)
    _tap(env, env.a, "s1")
    env.storage.log_call_start("b1", B_ID, SPAM_NUMBER)
    sent = []
    monkeypatch.setattr(notify, "notify_owner", lambda config, *, title, body, ics=None: sent.append(title))
    tools.escalate_to_human(call_sid="b1", config=load_client_config(B_ID), reason="callback", caller_name="x", caller_phone="+1", summary="Real job")
    assert len(sent) == 1
    env.storage.classify_and_store("b1")
    assert _row(env, "b1")[0] != "SPAM"


def test_cannot_mark_another_tenants_call(env):
    _seed_call(env, "bcall", B_ID, outcome_class="CALLBACK_REQUESTED", attention=1, frm=SPAM_NUMBER)
    r = _tap(env, env.a, "bcall")
    assert r.status_code in (303, 404)
    cls, attention, resolved = _row(env, "bcall")
    assert cls == "CALLBACK_REQUESTED" and attention == 1 and resolved is None
    conn = _db(env)
    assert conn.execute("SELECT COUNT(*) FROM spam_numbers").fetchone()[0] == 0


def test_spam_post_needs_csrf_and_a_session(env):
    _seed_call(env, "s1", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, frm=SPAM_NUMBER)
    for path in ("/portal/spam", "/portal/spam/undo"):
        assert env.a.post(path, data={"call": "s1"}, follow_redirects=False).status_code == 403
        assert env.a.post(path, data={"call": "s1", "csrf": "forged"}, follow_redirects=False).status_code == 403
        assert env.a.post(path, data={"call": "s1", "csrf": _csrf(env.b.get("/portal/overview").text)}, follow_redirects=False).status_code == 403
    assert _row(env, "s1")[0] == "CALLBACK_REQUESTED"
    anon = env.new_browser()
    assert anon.post("/portal/spam", data={"call": "s1", "csrf": "x"}, follow_redirects=False).status_code in (303, 401, 403)
    assert _row(env, "s1")[0] == "CALLBACK_REQUESTED"


def test_undo_restores_the_call_and_stops_suppressing(env):
    _seed_call(env, "s1", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, frm=SPAM_NUMBER)
    _tap(env, env.a, "s1")
    assert 'action="/portal/spam/undo"' in env.a.get("/portal/overview").text
    r = _tap(env, env.a, "s1", path="/portal/spam/undo")
    assert r.status_code == 303
    cls, attention, resolved = _row(env, "s1")
    assert cls != "SPAM" and resolved is None and attention == 1
    from app import spamtag

    assert spamtag.is_spam(A_ID, SPAM_NUMBER) is False


def test_hostile_number_is_escaped_in_the_marked_list(env):
    evil = '<img src=x onerror=alert(1)>+15555550100'
    _seed_call(env, "s1", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, frm=evil)
    _tap(env, env.a, "s1")
    html = env.a.get("/portal/overview").text
    assert evil not in html


def test_samples_are_read_only_and_empty_state_is_honest(env):
    html = env.a.get("/portal/overview").text
    assert "Marked as not a customer" not in html           # nothing marked: no empty table clutter
    from app import spamtag

    assert spamtag.is_spam(A_ID, "") is False and spamtag.is_spam(A_ID, None) is False


def test_unknown_or_missing_number_is_never_marked(env):
    _seed_call(env, "s1", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, frm="")
    _tap(env, env.a, "s1")
    conn = _db(env)
    assert conn.execute("SELECT COUNT(*) FROM spam_numbers").fetchone()[0] == 0
    assert _row(env, "s1")[0] == "SPAM"                     # this call is tagged, but there is no caller ID to remember


def test_a_possible_emergency_from_a_marked_number_still_pages_the_owner(env, monkeypatch):
    from app import notify, tools
    from app.config import load_client_config

    _seed_call(env, "s1", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, frm=SPAM_NUMBER)
    _tap(env, env.a, "s1")
    env.storage.log_call_start("s3", A_ID, SPAM_NUMBER)
    sent = []
    monkeypatch.setattr(notify, "notify_owner", lambda config, *, title, body, ics=None: sent.append(title))
    tools.escalate_to_human(call_sid="s3", config=load_client_config(A_ID), reason="possible_emergency", caller_name="x", caller_phone=SPAM_NUMBER,
                            summary="Gas smell at the furnace")
    assert len(sent) == 1 and sent[0].startswith("URGENT")
    env.storage.classify_and_store("s3")
    assert _row(env, "s3")[0] == "EMERGENCY_ESCALATED"            # never hidden as spam



import pytest


@pytest.fixture(autouse=True)
def enable_spam_tagging(monkeypatch):
    monkeypatch.setenv("CALLKETTLE_SPAM_TAGGING_ENABLED", "true")
