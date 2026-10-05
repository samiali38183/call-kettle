"""Deterministic interleavings from the independent authentication audit."""
from app import owner_auth, owner_activation, storage


def test_pending_invitation_session_expires_with_its_temporary_credential(app_client, monkeypatch):
    owner_auth.create_user('demo_hvac', 'owner@example.com')
    with storage._conn() as conn:
        uid = conn.execute('SELECT id FROM owner_users').fetchone()[0]
    owner_activation.create_invitation(uid)
    session, _ = owner_auth.create_session(uid)
    assert owner_auth.get_session(session) is not None
    now = owner_auth._now()
    monkeypatch.setattr(owner_auth, '_now', lambda: now + owner_activation.INVITATION_SECONDS + 1)
    assert owner_auth.get_session(session) is None



def test_current_password_failure_does_not_overwrite_concurrent_failures(app_client, monkeypatch):
    owner_auth.create_user('demo_hvac', 'owner@example.com')
    with storage._conn() as conn:
        uid = conn.execute('SELECT id FROM owner_users').fetchone()[0]
    def interleaved_verification(password, pw_hash):
        with storage._conn() as conn:
            conn.execute('UPDATE owner_users SET failed_count = 4 WHERE id = ?', (uid,))
        return False
    monkeypatch.setattr(owner_auth, 'verify_password', interleaved_verification)
    assert not owner_auth.check_current_password(uid, 'wrong-password')
    with storage._conn() as conn:
        locked = conn.execute('SELECT locked_until FROM owner_users WHERE id = ?', (uid,)).fetchone()[0]
    assert locked > owner_auth._now()


def test_login_does_not_accept_password_invalidated_during_verification(app_client, monkeypatch):
    temp = owner_auth.create_user('demo_hvac', 'owner@example.com')
    replacement_hash = owner_auth.hash_password('replacement-password-2026')
    def interleaved_verification(password, pw_hash):
        with storage._conn() as conn:
            conn.execute('UPDATE owner_users SET pw_hash = ? WHERE email = ?', (replacement_hash, 'owner@example.com'))
        return True
    monkeypatch.setattr(owner_auth, 'verify_password', interleaved_verification)
    status, user = owner_auth.authenticate('owner@example.com', temp, 'test-ip')
    assert status == 'bad' and user is None
