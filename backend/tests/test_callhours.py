"""When calls came in: business hours vs after hours (the client's OWN configured hours), what came of each, what is still waiting,
and how long a callback has been waiting. Recorded facts only: no dollar figure, no estimate, no price."""
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tests.test_owner_portal import A_ID, B_ID, _csrf, _db, _seed_call, env  # noqa: F401  (env is a fixture)

NY = ZoneInfo("America/New_York")


def _weekday_local(hour: int) -> datetime:
    """A recent weekday (not today) at the given local hour, as aware UTC: demo_hvac is open 07:00-19:00 Monday-Friday."""
    day = datetime.now(NY).replace(hour=hour, minute=0, second=0, microsecond=0) - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.astimezone(timezone.utc)


def _seed(e, sid, client, when, cls, attention=0, resolved=None):
    _seed_call(e, sid, client, outcome_class=cls, attention=attention, started=when.isoformat())
    if resolved:
        conn = _db(e)
        conn.execute("UPDATE calls SET attention_resolved_at = ? WHERE call_sid = ?", (resolved, sid))
        conn.commit()
        conn.close()


def test_split_uses_the_clients_own_hours_and_counts_outcomes():
    from app import callhours
    from app.config import load_client_config

    cfg = load_client_config("demo_hvac")
    rows = [
        (_weekday_local(10).isoformat(), "BOOKED", 0, None),
        (_weekday_local(11).isoformat(), "FAQ_RESOLVED", 0, None),
        (_weekday_local(21).isoformat(), "CALLBACK_REQUESTED", 1, None),
        (_weekday_local(22).isoformat(), "AFTER_HOURS_MESSAGE", 1, "2026-01-01T00:00:00+00:00"),
        (_weekday_local(23).isoformat(), "ABANDONED", 0, None),
        (_weekday_local(5).isoformat(), "BOOKED", 0, None),
        ("not-a-date", "BOOKED", 0, None),
    ]
    split = callhours.split(cfg, rows)
    assert split["business"] == {"calls": 2, "booked": 1, "left_details": 0, "hung_up": 0, "waiting": 0}
    assert split["after"] == {"calls": 4, "booked": 1, "left_details": 2, "hung_up": 1, "waiting": 1}


def test_empty_split_is_all_zero():
    from app import callhours
    from app.config import load_client_config

    split = callhours.split(load_client_config("demo_hvac"), [])
    assert split["business"]["calls"] == 0 and split["after"]["calls"] == 0


def test_overview_shows_hours_breakdown_for_last_seven_days_only_this_tenant(env):
    _seed(env, "a1", A_ID, _weekday_local(10), "BOOKED")
    _seed(env, "a2", A_ID, _weekday_local(21), "CALLBACK_REQUESTED", attention=1)
    _seed(env, "a3", A_ID, _weekday_local(22), "ABANDONED")
    _seed(env, "old", A_ID, _weekday_local(21) - timedelta(days=30), "CALLBACK_REQUESTED", attention=1)
    _seed(env, "b1", B_ID, _weekday_local(21), "CALLBACK_REQUESTED", attention=1)
    html = env.a.get("/portal/overview").text
    assert "When your calls came in" in html
    table = html.split("When your calls came in")[1].split("<h2")[0]
    assert "Business hours" in table and "After hours" in table
    assert "<td>Business hours</td><td>1</td>" in table
    assert "<td>After hours</td><td>2</td>" in table
    assert "$" not in table
    assert "last 7 days" in table.lower()


def test_overview_hours_breakdown_has_an_honest_empty_state(env):
    html = env.a.get("/portal/overview").text
    assert "When your calls came in" in html
    assert "No calls recorded in the last 7 days." in html


def test_callbacks_show_how_long_they_have_waited_and_overdue_ones_say_so(env):
    _seed_call(env, "fresh", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1,
               started=(datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat())
    _seed_call(env, "stale", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1,
               started=(datetime.now(timezone.utc) - timedelta(days=3, hours=2)).isoformat())
    html = env.a.get("/portal/overview").text
    assert "Waiting 20 min" in html
    assert "Waiting 3 days" in html and "Overdue" in html
    assert html.count("Overdue") == 1                       # only the stale one


def test_age_helper_wording():
    from app import portal

    now = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
    f = lambda **k: portal.waiting_label((now - timedelta(**k)).isoformat(), now)
    assert f(seconds=30) == "Waiting under a minute"
    assert f(minutes=1) == "Waiting 1 min"
    assert f(minutes=125) == "Waiting 2 hours"
    assert f(hours=24) == "Waiting 1 day"
    assert f(days=4) == "Waiting 4 days"
    assert portal.waiting_label("garbage", now) == ""
    assert portal.waiting_label(None, now) == ""


def test_front_desk_queue_also_shows_waiting_time(env):
    _seed_call(env, "stale", A_ID, outcome_class="CALLBACK_REQUESTED", attention=1,
               started=(datetime.now(timezone.utc) - timedelta(days=2, hours=1)).isoformat())
    assert "Waiting 2 days" in env.a.get("/portal/frontdesk").text


# ------------------------------------------------------------------ recap: useful in 20 seconds
def _cfg(**over):
    from app.config import load_client_config

    return load_client_config("demo_hvac").model_copy(update={"business_name": "Acme HVAC", **over})


def test_recap_opens_with_a_one_glance_summary_of_recorded_facts():
    from app import digest

    start = datetime(2026, 1, 5, tzinfo=NY)
    stats = {"calls": 14, "after_hours": 5, "booked": 3, "callbacks": 4, "transferred": 1,
             "after_hours_booked": 1, "after_hours_left_details": 3, "after_hours_hung_up": 1}
    stale = [{"started_at": start, "age_days": 2, "number": "+15555550100", "class": "CALLBACK_REQUESTED", "summary": "No heat"}]
    _subject, body = digest.compose(_cfg(), stats, start, start + timedelta(days=7), [], {}, stale)
    glance = body.split("Calls answered")[0]
    assert "This week at a glance" in glance
    assert "14 calls" in glance and "5 after hours" in glance
    assert "1 caller still waiting on you" in glance
    assert "Of the 5 after-hours calls: 1 booked, 3 left details, 1 hung up" in body
    assert "$" not in body


def test_recap_glance_is_honest_when_nothing_waits_and_old_stats_shape_still_works():
    from app import digest

    start = datetime(2026, 1, 5, tzinfo=NY)
    _subject, body = digest.compose(_cfg(), {"calls": 3, "after_hours": 0, "booked": 1, "callbacks": 0, "transferred": 0},
                                    start, start + timedelta(days=7), [], {}, [])
    assert "This week at a glance" in body
    assert "no callers waiting on you" in body
    assert "after-hours calls:" not in body


def test_week_stats_carries_the_after_hours_outcome_counts(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("CALLKETTLE_DB_PATH", str(tmp_path / "d.db"))
    from app import storage

    importlib.reload(storage)
    storage.init_db()
    from app import digest

    importlib.reload(digest)
    cfg = _cfg(client_id="acme", timezone="America/New_York")
    when = _weekday_local(21)
    storage.log_call_start("n1", "acme", "+15555550100")
    conn = sqlite3.connect(storage.DB_PATH)
    conn.execute("UPDATE calls SET started_at = ?, outcome_class = 'CALLBACK_REQUESTED', needs_attention = 1 WHERE call_sid = 'n1'", (when.isoformat(),))
    conn.commit()
    conn.close()
    stats = digest.week_stats(cfg, when - timedelta(days=1), when + timedelta(days=1))
    assert stats["after_hours"] == 1 and stats["after_hours_left_details"] == 1
    assert stats["after_hours_booked"] == 0 and stats["after_hours_hung_up"] == 0
