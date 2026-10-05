"""Conservative release: spam tagging is opt-in, including persisted marks."""
import pytest
from tests.test_owner_portal import A_ID, _db, _seed_call, env  # noqa: F401
from tests.test_spamtag import _row, _tap

NUMBER = "+15555550100"


@pytest.mark.parametrize("setting", [None, "0", "false", "unexpected"])
def test_disabled_spam_has_no_ui_mutations_classification_or_suppression(env, monkeypatch, setting):
    from app import spamtag, notify, tools
    from app.config import load_client_config
    if setting is None:
        monkeypatch.delenv("CALLKETTLE_SPAM_TAGGING_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CALLKETTLE_SPAM_TAGGING_ENABLED", setting)
    _seed_call(env, "old", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, frm=NUMBER)
    with _db(env) as conn:
        spamtag.ensure_table(conn)
        conn.execute("INSERT INTO spam_numbers (client_id, number, marked_at, call_sid) VALUES (?,?,?,?)",
                     (A_ID, spamtag.key(NUMBER), "2026-10-04", "old"))
        assert spamtag.marked_in(conn, A_ID, NUMBER) is False
    assert spamtag.is_spam(A_ID, NUMBER) is False
    assert spamtag.call_is_spam(A_ID, "old") is False
    assert spamtag.marked_list(A_ID) == []
    html = env.a.get("/portal/overview").text
    help_html = env.a.get("/portal/help").text
    assert "spam" not in help_html.lower() and "Not a customer" not in help_html
    assert "/portal/spam" not in html and "Not a customer" not in html and "Marked as not a customer" not in html
    for route in ("/portal/spam", "/portal/spam/undo"):
        assert env.new_browser().post(route, data={"call": "old"}, follow_redirects=False).status_code == 303
        assert _tap(env, env.a, "old", path=route).status_code in (403, 404)
    assert spamtag.mark(A_ID, "old") is False
    assert spamtag.undo(A_ID, "old") is False
    assert _row(env, "old") == ("CALLBACK_REQUESTED", 1, None)
    env.storage.log_call_start("new", A_ID, NUMBER)
    sent = []
    monkeypatch.setattr(notify, "notify_owner", lambda *a, **kw: sent.append(kw["title"]))
    tools.escalate_to_human(call_sid="new", config=load_client_config(A_ID), reason="callback_requested", caller_name="Caller", caller_phone=NUMBER, summary="Repair callback")
    assert len(sent) == 1
    assert env.storage.classify_and_store("new") == ("CALLBACK_REQUESTED", True)
    assert _row(env, "new")[0] != "SPAM"
