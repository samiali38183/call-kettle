"""A recap must distinguish stale followups from all open followups and unknown outcomes."""
from datetime import datetime, timedelta, timezone
from tests.test_callhours import _cfg


def test_empty_stale_list_does_not_claim_all_callbacks_are_handled():
    from app import digest
    start = datetime(2026, 10, 5, tzinfo=timezone.utc)
    stats = {"calls": 1, "after_hours": 0, "booked": 0, "callbacks": 1, "transferred": 0}
    _, body = digest.compose(_cfg(), stats, start, start + timedelta(days=7), [], {}, [])
    glance = body.split("Calls answered")[0]
    assert "more than 24 hours" in glance
    assert "no callers waiting on you." not in glance


def test_other_afterhours_outcomes_are_not_invented_as_answered_or_transferred():
    from app import digest
    start = datetime(2026, 10, 5, tzinfo=timezone.utc)
    stats = {"calls": 1, "after_hours": 1, "booked": 0, "callbacks": 0, "transferred": 0,
             "after_hours_booked": 0, "after_hours_left_details": 0, "after_hours_hung_up": 0}
    _, body = digest.compose(_cfg(), stats, start, start + timedelta(days=7), [], {None: 1}, [])
    assert "the rest were questions answered or put through to you" not in body
    assert "other or unclassified outcomes" in body
