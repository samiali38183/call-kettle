"""Launch review regressions: duplicate browser submits must not lose callback history."""
from tests.test_owner_portal import A_ID, _db, _seed_call, env  # noqa: F401
from tests.test_spamtag import SPAM_NUMBER, _row, _tap


def test_duplicate_spam_mark_does_not_replace_original_outcome(env):
    _seed_call(env, "repeat", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, frm=SPAM_NUMBER)
    assert _tap(env, env.a, "repeat").status_code == 303
    assert _tap(env, env.a, "repeat").status_code == 303
    assert _tap(env, env.a, "repeat", path="/portal/spam/undo").status_code == 303
    cls, attention, resolved = _row(env, "repeat")
    assert (cls, attention, resolved) == ("CALLBACK_REQUESTED", 1, None)


def test_spam_undo_preserves_owner_followup_state_and_due_date(env):
    from app import frontdesk_storage
    _seed_call(env, "workspace", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, frm=SPAM_NUMBER)
    frontdesk_storage.init_db()
    frontdesk_storage.save_followup(A_ID, "workspace", note="Waiting on parts", state="waiting", due_date="2026-10-07")
    before = frontdesk_storage.get_call(A_ID, "workspace")
    _tap(env, env.a, "workspace")
    assert frontdesk_storage.get_call(A_ID, "workspace")["state"] == "handled"
    _tap(env, env.a, "workspace", path="/portal/spam/undo")
    after = frontdesk_storage.get_call(A_ID, "workspace")
    for field in ("state", "owner_note", "followup_state", "followup_due_date", "followup_updated_at"):
        assert after[field] == before[field]


def test_spam_undo_without_caller_id_recomputes_callback_from_recorded_facts(env):
    from app import storage
    _seed_call(env, "anonymous", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1, frm="")
    storage.log_escalation(call_sid="anonymous", client_id=A_ID, reason="callback_requested", caller_phone=None, summary="Repair question")
    _tap(env, env.a, "anonymous")
    _tap(env, env.a, "anonymous", path="/portal/spam/undo")
    cls, attention, resolved = _row(env, "anonymous")
    assert (cls, attention, resolved) == ("CALLBACK_REQUESTED", 1, None)


import pytest


@pytest.fixture(autouse=True)
def enable_spam_tagging(monkeypatch):
    monkeypatch.setenv("CALLKETTLE_SPAM_TAGGING_ENABLED", "true")
