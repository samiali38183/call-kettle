"""Existing login must clearly explain account handoff and setup requests."""


def test_login_explains_invitation_and_links_setup_request(app_client):
    client, _ = app_client
    response = client.get('/portal/login')
    assert response.status_code == 200
    assert 'activation link' in response.text.lower()
    assert 'href="/start"' in response.text
    assert 'does not turn on phone service' in response.text.lower()
    assert response.headers['cache-control'] == 'no-store'


def test_setup_request_does_not_promise_unapproved_turnaround(app_client):
    client, _ = app_client
    response = client.get('/start')
    assert response.status_code == 200
    assert 'within one business day' not in response.text
    assert 'does not turn on phone service' in response.text
    assert 'href="/portal/login"' in response.text
