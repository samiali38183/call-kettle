"""State consistency after legacy actions and late automatic classification."""
from app import storage
from tests.test_frontdesk import db, feature, A


def test_legacy_handled_clears_due_date_and_synchronizes_explicit_state(db):
    workspace = feature()
    workspace.init_db()
    workspace.save_followup(A, 'call-a', note='Waiting', state='waiting', due_date='2026-10-01')
    assert storage.resolve_attention(A, 'call-a')
    row = workspace.get_call(A, 'call-a')
    assert row is not None
    assert row['state'] == row['followup_state'] == 'handled'
    assert row['followup_due_date'] is None
    assert row['owner_note'] == 'Waiting'


def test_late_classification_preserves_owner_no_followup_decision(db):
    workspace = feature()
    workspace.init_db()
    storage.log_escalation(call_sid='call-a', client_id=A, reason='callback_requested', caller_phone='+15555550101', summary='Callback')
    workspace.save_followup(A, 'call-a', note='No further action needed', state='none', due_date='')
    storage.classify_and_store('call-a')
    row = workspace.get_call(A, 'call-a')
    assert row is not None
    assert row['outcome_class'] == 'CALLBACK_REQUESTED'
    assert row['state'] == 'none' and row['needs_attention'] == 0


def test_late_classification_preserves_owner_open_decision(db):
    workspace = feature()
    workspace.init_db()
    workspace.save_followup(A, 'call-a', note='Owner follow-up', state='open', due_date='')
    storage.classify_and_store('call-a')
    row = workspace.get_call(A, 'call-a')
    assert row is not None
    assert row['state'] == 'open' and row['needs_attention'] == 1
