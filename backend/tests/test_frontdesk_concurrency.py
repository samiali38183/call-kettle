"""Stale owner forms must not overwrite a newer note or reopen handled calls."""
import re
from tests.test_frontdesk import db, web, feature, A
from app import storage


def revision(browser):
    page = browser.get('/portal/frontdesk/call/call-a')
    match = re.search(r'name="revision" value="([^"]+)"', page.text)
    assert match, 'No stale-write protection on the follow-up form'
    return match.group(1)


def test_stale_form_is_rejected_without_overwriting_newer_note(db, monkeypatch):
    e = web(monkeypatch)
    stale = revision(e.browser)
    workspace = feature()
    workspace.save_followup(A, 'call-a', note='Newer note from second tab', state='waiting', due_date='')
    result = e.browser.post('/portal/frontdesk/call/call-a', data={
        'csrf': e.csrf, 'revision': stale, 'note': 'Stale note', 'state': 'open', 'due_date': ''}, follow_redirects=False)
    assert result.status_code == 409
    assert workspace.get_call(A, 'call-a')['owner_note'] == 'Newer note from second tab'
    current = revision(e.browser)
    result = e.browser.post('/portal/frontdesk/call/call-a', data={
        'csrf': e.csrf, 'revision': current, 'note': 'Updated safely', 'state': 'handled', 'due_date': ''}, follow_redirects=False)
    assert result.status_code == 303


def test_stale_form_cannot_reopen_legacy_handled_call(db, monkeypatch):
    e = web(monkeypatch)
    stale = revision(e.browser)
    assert storage.resolve_attention(A, 'call-a')
    result = e.browser.post('/portal/frontdesk/call/call-a', data={
        'csrf': e.csrf, 'revision': stale, 'note': 'Stale reopen', 'state': 'open', 'due_date': ''}, follow_redirects=False)
    assert result.status_code == 409
    assert feature().get_call(A, 'call-a')['state'] == 'handled'


def test_browser_crlf_notes_are_normalized_before_length_validation(db):
    workspace = feature()
    workspace.init_db()
    note = 'a\r\n' * 1500
    workspace.save_followup(A, 'call-a', note=note, state='open', due_date='')
    assert workspace.get_call(A, 'call-a')['owner_note'] == 'a\n' * 1500
