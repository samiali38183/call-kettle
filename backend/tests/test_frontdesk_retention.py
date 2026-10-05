"""New owner annotations must follow call-content privacy cleanup."""
from datetime import datetime, timedelta, timezone
from app import storage, frontdesk_storage, ops


def test_expired_private_demo_scrubs_owner_annotations(app_client):
    frontdesk_storage.init_db()
    storage.log_call_start('private-note', 'prep_privacy', '+15555550101')
    frontdesk_storage.save_followup('prep_privacy', 'private-note', note='Private caller details', state='open', due_date='2026-10-04')
    storage.clean_private_demo(999, 'prep_privacy')
    row = frontdesk_storage.get_call('prep_privacy', 'private-note')
    assert row['owner_note'] == ''
    assert row['followup_due_date'] is None
    assert row['state'] == 'none'


def test_retention_scrubs_old_owner_notes_but_keeps_recent_notes(app_client):
    frontdesk_storage.init_db()
    for sid in ('old-note', 'recent-note'):
        storage.log_call_start(sid, 'demo_hvac', '+15555550101')
        frontdesk_storage.save_note('demo_hvac', sid, 'Sensitive owner annotation')
    with storage._conn() as conn:
        conn.execute('UPDATE calls SET started_at = ? WHERE call_sid = ?',
                     ((datetime.now(timezone.utc) - timedelta(days=100)).isoformat(), 'old-note'))
    ops.purge_old_transcripts(days=90)
    assert frontdesk_storage.get_call('demo_hvac', 'old-note')['owner_note'] == ''
    assert frontdesk_storage.get_call('demo_hvac', 'recent-note')['owner_note'] == 'Sensitive owner annotation'
