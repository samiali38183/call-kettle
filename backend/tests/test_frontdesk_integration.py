"""Production application wiring, distinct from isolated feature-router tests."""
from tests.test_owner_portal import env


def test_application_wires_frontdesk_router_and_startup_migration(app_client):
    client, main = app_client
    response = client.get('/portal/frontdesk', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/portal/login'
    with main.storage._conn() as conn:
        columns = {r[1] for r in conn.execute('PRAGMA table_info(calls)')}
    assert {'owner_note', 'followup_state', 'followup_due_date', 'followup_updated_at'} <= columns


def test_frontdesk_is_discoverable_in_signed_in_navigation(env):
    response = env.a.get('/portal/overview')
    assert '<a href="/portal/frontdesk"' in response.text


def test_application_wires_customer_activation_and_migration(app_client):
    client, main = app_client
    assert client.get('/portal/activate').status_code == 400
    with main.storage._conn() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='owner_invitations'").fetchone() is not None
