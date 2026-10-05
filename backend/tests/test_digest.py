"""Weekly recap email: counts, timing, once-per-week, and only-if-actually-sent."""
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

NY = ZoneInfo("America/New_York")


@pytest.fixture
def db(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("CALLKETTLE_DB_PATH", str(tmp_path / "d.db"))
    monkeypatch.setenv("REPORT_KEY", "master_key_for_tests")
    from app import storage

    importlib.reload(storage)
    storage.init_db()
    from app import digest

    importlib.reload(digest)
    return storage, digest


def _config(monkeypatch, digest, **over):
    from app.config import load_client_config

    base = load_client_config("demo_hvac")
    cfg = base.model_copy(update={"client_id": "acme", "business_name": "Acme Plumbing",
                                  "owner_email": "owner@example.com", "timezone": "America/New_York", **over})
    monkeypatch.setattr(digest, "load_client_config", lambda cid: cfg)
    return cfg


def _call(storage, sid, local_dt, outcome="completed"):
    storage.log_call_start(sid, "acme", "+15555550100")
    conn = sqlite3.connect(storage.DB_PATH)
    conn.execute("UPDATE calls SET started_at = ?, outcome = ? WHERE call_sid = ?",
                 (local_dt.astimezone(timezone.utc).isoformat(), outcome, sid))
    conn.commit()
    conn.close()


# Monday 2026-01-12 09:00 New York; the recap covers Mon 1/5 - Sun 1/11.
NOW = datetime(2026, 1, 12, 9, 0, tzinfo=NY).astimezone(timezone.utc)


def test_counts_calls_after_hours_bookings_and_callbacks(db, monkeypatch):
    storage, digest = db
    cfg = _config(monkeypatch, digest)
    start, end = digest.week_bounds(NOW, NY)
    assert start == datetime(2026, 1, 5, tzinfo=NY) and end == datetime(2026, 1, 12, tzinfo=NY)
    hours = cfg.business_hours["tue"]
    in_hours = datetime(2026, 1, 6, 10, 0, tzinfo=NY)
    at_night = datetime(2026, 1, 6, 23, 30, tzinfo=NY)
    _call(storage, "C1", in_hours)
    _call(storage, "C2", at_night)
    _call(storage, "C3", datetime(2026, 1, 11, 10, 0, tzinfo=NY), outcome="transferred")  # Sunday
    _call(storage, "OLD", datetime(2025, 12, 20, 10, 0, tzinfo=NY))  # outside the week
    assert hours != "closed"
    storage.log_escalation(call_sid="C2", client_id="acme", reason="callback", caller_phone="+1570", summary="x")
    storage.create_booking(call_sid="C1", client_id="acme", caller_name="Pat", caller_phone="+1570",
                           service="Repair", slot_start="2026-01-14T10:00", slot_end="2026-01-14T11:00")
    conn = sqlite3.connect(storage.DB_PATH)
    stamp = datetime(2026, 1, 7, 9, 0, tzinfo=NY).astimezone(timezone.utc).isoformat()
    conn.execute("UPDATE escalations SET created_at = ?", (stamp,))
    conn.execute("UPDATE bookings SET created_at = ?", (stamp,))
    conn.commit()
    conn.close()
    stats = digest.week_stats(cfg, start, end)
    assert stats["booked"] == 1
    assert stats["calls"] == 3 and stats["transferred"] == 1 and stats["callbacks"] == 1
    assert stats["after_hours"] >= 2  # 11:30pm and a Sunday


def test_sent_once_per_week_and_only_when_delivered(db, monkeypatch):
    storage, digest = db
    _config(monkeypatch, digest)
    _call(storage, "C1", datetime(2026, 1, 6, 10, 0, tzinfo=NY))
    mail = []
    monkeypatch.setattr(digest.notify, "_send_email", lambda to, s, b, ics=None, **kw: mail.append((to, s, b)) or False)
    assert digest.send_due_digests(NOW, ["acme"]) == []  # email not configured: nothing marked sent
    monkeypatch.setattr(digest.notify, "_send_email", lambda to, s, b, ics=None, **kw: mail.append((to, s, b)) or True)
    assert digest.send_due_digests(NOW, ["acme"]) == ["acme"]
    assert digest.send_due_digests(NOW + timedelta(hours=6), ["acme"]) == []  # already sent this week
    to, subject, body = mail[-1]
    assert to == "owner@example.com" and "1 call answered" in subject
    assert "Acme Plumbing" in body and "/report/acme?key=" in body and "Jan 5 to Jan 11" in body


def test_not_before_monday_8am_and_skips_quiet_weeks_and_opt_outs(db, monkeypatch):
    storage, digest = db
    _call(storage, "C1", datetime(2026, 1, 6, 10, 0, tzinfo=NY))
    sent = []
    monkeypatch.setattr(digest.notify, "_send_email", lambda to, s, b, ics=None, **kw: sent.append(to) or True)
    _config(monkeypatch, digest)
    early = datetime(2026, 1, 12, 7, 0, tzinfo=NY).astimezone(timezone.utc)
    assert digest.send_due_digests(early, ["acme"]) == []
    _config(monkeypatch, digest, weekly_recap=False)
    assert digest.send_due_digests(NOW, ["acme"]) == []
    _config(monkeypatch, digest, owner_email=None)
    assert digest.send_due_digests(NOW, ["acme"]) == []
    _config(monkeypatch, digest)
    quiet = NOW + timedelta(days=7)  # the following Monday: last week had no calls
    assert digest.send_due_digests(quiet, ["acme"]) == [] and sent == []


def test_demo_clients_never_get_a_recap(db, monkeypatch):
    _storage, digest = db
    monkeypatch.setattr(digest.notify, "_send_email", lambda *a, **k: pytest.fail("demo must not email"))
    assert digest.send_due_digests(NOW, ["callkettle_demo", "demo_dental"]) == []


# ---- outcome breakdown, aging follow-ups, preview ------------------------------------------------------------------
def _classed(storage, sid, local_dt, cls, attention, summary="Wants a callback", resolved=False):
    _call(storage, sid, local_dt)
    conn = sqlite3.connect(storage.DB_PATH)
    conn.execute("UPDATE calls SET outcome_class = ?, needs_attention = ?, summary = ?, attention_resolved_at = ? WHERE call_sid = ?",
                 (cls, 1 if attention else 0, summary, "2026-01-08T00:00:00+00:00" if resolved else None, sid))
    conn.commit()
    conn.close()


def test_outcome_counts_come_only_from_recorded_classes(db, monkeypatch):
    storage, digest = db
    cfg = _config(monkeypatch, digest)
    start, end = digest.week_bounds(NOW, NY)
    _classed(storage, "A", datetime(2026, 1, 6, 10, 0, tzinfo=NY), "BOOKED", False)
    _classed(storage, "B", datetime(2026, 1, 7, 10, 0, tzinfo=NY), "CALLBACK_REQUESTED", True)
    _classed(storage, "C", datetime(2026, 1, 8, 10, 0, tzinfo=NY), "CALLBACK_REQUESTED", True)
    _call(storage, "D", datetime(2026, 1, 9, 10, 0, tzinfo=NY))                       # never classified
    _classed(storage, "OLD", datetime(2025, 12, 20, 10, 0, tzinfo=NY), "BOOKED", False)  # outside the week
    assert digest.week_outcomes(cfg, start, end) == {"BOOKED": 1, "CALLBACK_REQUESTED": 2, None: 1}


def test_stale_attention_only_unresolved_older_than_24h_oldest_first(db, monkeypatch):
    storage, digest = db
    cfg = _config(monkeypatch, digest)
    _classed(storage, "NEW", NOW.astimezone(NY) - timedelta(hours=5), "CALLBACK_REQUESTED", True)        # too recent
    _classed(storage, "DONE", datetime(2026, 1, 6, 10, 0, tzinfo=NY), "CALLBACK_REQUESTED", True, resolved=True)
    _classed(storage, "FINE", datetime(2026, 1, 6, 11, 0, tzinfo=NY), "BOOKED", False)                  # not flagged
    _classed(storage, "OPEN2", datetime(2026, 1, 8, 10, 0, tzinfo=NY), "AFTER_HOURS_MESSAGE", True)
    _classed(storage, "OPEN1", datetime(2026, 1, 2, 10, 0, tzinfo=NY), "EMERGENCY_ESCALATED", True)     # last week: still counts
    items = digest.stale_attention(cfg, NOW)
    assert [i["class"] for i in items] == ["EMERGENCY_ESCALATED", "AFTER_HOURS_MESSAGE"]
    assert items[0]["age_days"] == 9 and items[1]["age_days"] == 3   # whole days elapsed


def test_compose_states_no_data_and_none_recorded_instead_of_inventing(db, monkeypatch):
    _storage, digest = db
    cfg = _config(monkeypatch, digest)
    start, end = digest.week_bounds(NOW, NY)
    stats = {"calls": 0, "after_hours": 0, "booked": 0, "callbacks": 0, "transferred": 0}
    _subject, body = digest.compose(cfg, stats, start, end, [], {}, [])
    assert "no data recorded" in body and "none recorded" in body
    _subject, legacy = digest.compose(cfg, stats, start, end, [])        # old call shape: sections omitted
    assert "no data recorded" not in legacy and "Follow-ups" not in legacy


def test_compose_lists_waiting_followups_without_money_or_price(db, monkeypatch):
    storage, digest = db
    cfg = _config(monkeypatch, digest)
    for i in range(7):
        _classed(storage, f"S{i}", datetime(2026, 1, 2 + (i % 3), 10, i, tzinfo=NY), "CALLBACK_REQUESTED", True, summary="Line one\nLine two")
    start, end = digest.week_bounds(NOW, NY)
    stale = digest.stale_attention(cfg, NOW)
    subject, body = digest.compose(cfg, {"calls": 3, "after_hours": 0, "booked": 1, "callbacks": 0, "transferred": 0}, start, end, [],
                                   {"CALLBACK_REQUESTED": 7}, stale)
    assert "7 follow-ups waiting" in subject
    assert "Callback requested" in body and "+15555550100" in body and "Line one Line two" in body
    assert "...and 2 more" in body and "Mark handled" in body
    assert "$" not in body and "revenue" not in body.lower() and "ROI" not in body


def test_stale_followups_alone_trigger_a_recap_but_quiet_clean_weeks_do_not(db, monkeypatch):
    storage, digest = db
    _config(monkeypatch, digest)
    mail = []
    monkeypatch.setattr(digest.notify, "_send_email", lambda to, s, b, ics=None, **kw: mail.append((s, b)) or True)
    quiet = NOW + timedelta(days=7)
    _classed(storage, "OLD", datetime(2026, 1, 6, 10, 0, tzinfo=NY), "CALLBACK_REQUESTED", True)   # unresolved, 2 weeks old by `quiet`
    assert digest.send_due_digests(quiet, ["acme"]) == ["acme"]
    assert "Follow-ups still open" in mail[0][1]


def test_preview_reads_without_sending_or_marking_sent(db, monkeypatch):
    storage, digest = db
    _config(monkeypatch, digest)
    monkeypatch.setattr(digest.notify, "_send_email", lambda *a, **k: pytest.fail("preview must not send"))
    _classed(storage, "A", datetime(2026, 1, 6, 10, 0, tzinfo=NY), "BOOKED", False)
    subject, body = digest.preview("acme", NOW)
    assert "1 call answered" in subject and "Booked" in body
    digest.ensure_table()
    assert not digest._already_sent("acme", "2026-W02")


def test_preview_cli_unknown_client_and_usage(db, capsys):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "digest_preview.py"
    spec = importlib.util.spec_from_file_location("digest_preview", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main([]) == 2
    assert mod.main(["no_such_client_zz"]) == 1
    assert "No client config" in capsys.readouterr().out
