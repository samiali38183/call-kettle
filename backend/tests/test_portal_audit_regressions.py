"""Regression coverage for concrete defects found in the read-only portal audit."""
from datetime import timedelta

from tests.test_owner_portal import env, _seed_call, _seed_booking, _now_iso, _month_slot, A_ID, B_ID


def test_attention_total_is_not_capped_and_oldest_is_visible(env):
    for index in range(51):
        _seed_call(env, f'attention-{index}', A_ID, attention=1,
                   summary=f'Callback case {index}', started=_now_iso(timedelta(minutes=index)),
                   outcome_class='CALLBACK_REQUESTED')
    _seed_call(env, 'other-tenant', B_ID, attention=1, summary='Other tenant secret')
    page = env.a.get('/portal/overview')
    assert 'Needs your attention (51)' in page.text
    assert 'Callback case 50' in page.text
    assert 'Showing the oldest 50 of 51' in page.text
    assert 'Other tenant secret' not in page.text


def test_calendar_does_not_make_invalid_phone_dialable(env):
    _seed_booking(env, A_ID, _month_slot(), phone='not-a-phone')
    page = env.a.get('/portal/calendar')
    assert 'not-a-phone' in page.text
    assert 'href="tel:not-a-phone"' not in page.text
