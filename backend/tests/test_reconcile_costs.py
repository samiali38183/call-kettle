"""reconcile_costs: app-measured usage vs Twilio's own Usage Records (Twilio is authoritative for billing)."""
import json
import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reconcile_costs as rc  # noqa: E402
from app import cost_observability as co  # noqa: E402


def rec(category, count=0, usage=0, unit=""):
    return SimpleNamespace(category=category, count=str(count), usage=str(usage), price="0", usage_unit=unit)


class FakeRecords:
    def __init__(self, records, fail=False):
        self.records, self.fail, self.calls = records, fail, []

    def list(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("twilio down AC+15555550100secret")
        return self.records


class FakeClient:
    def __init__(self, records, fail=False):
        self.usage = SimpleNamespace(records=FakeRecords(records, fail))


def make_db(tmp_path, calls):
    """calls: list of (client_id, sid, started_at, {metric: value})"""
    path = str(tmp_path / "t.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE calls (call_sid TEXT PRIMARY KEY, client_id TEXT, from_number TEXT, started_at TEXT, ended_at TEXT, "
                 "input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, tts_chars INTEGER DEFAULT 0)")
    for cid, sid, started, _ in calls:
        conn.execute("INSERT INTO calls (call_sid, client_id, started_at) VALUES (?,?,?)", (cid and sid, cid, started))
    conn.commit()
    conn.close()
    co.init_ledger(path)
    for cid, sid, started, metrics in calls:
        ev = {m: {"value": v, "status": "measured", "source": "twilio_status_callback"} for m, v in metrics.items()}
        if ev:
            co.record_snapshot(path, cid, sid, ev, revision=5)
    return path


OCT = "2026-10-05T12:00:00+00:00"


def test_app_side_sums_month_and_rounds_carrier_up_per_call(tmp_path):
    db = make_db(tmp_path, [
        ("a", "C1", OCT, {"carrier_seconds": 61, "gather_count": 3, "tts_chars": 100}),
        ("b", "C2", OCT, {"carrier_seconds": 30, "gather_count": 2, "tts_chars": 50}),
        ("a", "C3", "2026-09-30T23:59:00+00:00", {"carrier_seconds": 999, "gather_count": 9, "tts_chars": 9}),   # other month
    ])
    app = rc.collect_app(db, "2026-10")
    assert app["calls"] == 2
    assert app["carrier_minutes"]["value"] == Decimal(3)                     # ceil(61/60)=2 + ceil(30/60)=1
    assert app["carrier_minutes"]["calls_measured"] == 2
    assert app["gather_count"]["value"] == 5 and app["tts_chars"]["value"] == 150
    assert app["sms_count"]["value"] is None                                  # the app does not count SMS
    only_a = rc.collect_app(db, "2026-10", tenant="a")
    assert only_a["calls"] == 1 and only_a["gather_count"]["value"] == 3


def test_app_side_unknown_when_no_call_has_the_measurement(tmp_path):
    db = make_db(tmp_path, [("a", "C1", OCT, {"gather_count": 3})])
    app = rc.collect_app(db, "2026-10")
    assert app["carrier_minutes"]["value"] is None and app["tts_chars"]["value"] is None
    assert app["gather_count"]["value"] == 3


def test_no_calls_in_month_is_measured_zero_except_sms(tmp_path):
    db = make_db(tmp_path, [("a", "C1", "2026-09-01T00:00:00+00:00", {})])
    app = rc.collect_app(db, "2026-10")
    assert app["calls"] == 0 and app["carrier_minutes"]["value"] == 0 and app["gather_count"]["value"] == 0
    assert app["sms_count"]["value"] is None


def test_partial_coverage_is_reported(tmp_path):
    db = make_db(tmp_path, [("a", "C1", OCT, {"gather_count": 3}), ("a", "C2", OCT, {})])
    app = rc.collect_app(db, "2026-10")
    assert app["gather_count"]["calls_measured"] == 1 and app["calls"] == 2


def test_twilio_side_maps_categories_and_asks_for_the_month():
    client = FakeClient([rec("calls-inbound", 4, "7", "minutes"), rec("speech-recognition", 12, 20, "15 sec interval"),
                         rec("amazon-polly", 5, 41, "use"), rec("sms", 3, 3, "")])
    tw = rc.collect_twilio(client, "2026-10")
    assert tw["calls-inbound"]["usage"] == Decimal(7) and tw["speech-recognition"]["count"] == Decimal(12)
    kw = client.usage.records.calls[0]
    assert str(kw["start_date"]) == "2026-10-01" and str(kw["end_date"]) == "2026-10-31"


def row(rows, name):
    return next(r for r in rows if r["category"] == name)


def full_app(carrier=3, gather=5, tts=150, sms=None, calls=2):
    def m(v):
        return {"value": None if v is None else Decimal(v), "calls_measured": calls if v is not None else 0}
    return {"scope": "account", "polly_only": True, "calls": calls, "carrier_minutes": m(carrier), "gather_count": m(gather), "tts_chars": m(tts), "sms_count": m(sms)}


def tw(**kw):
    base = {"calls-inbound": rec("calls-inbound", 2, 3, "minutes"), "speech-recognition": rec("speech-recognition", 5, 5, ""),
            "amazon-polly": rec("amazon-polly", 5, 150, "characters"), "sms": rec("sms", 0, 0, "")}
    base.update(kw)
    return rc.collect_twilio(FakeClient(list(base.values())), "2026-10")


def test_delta_percent_and_status_thresholds():
    rows = rc.reconcile(full_app(carrier=3, gather=5, tts=150), tw())
    assert all(r["status"] == "OK" for r in rows if r["category"] != "sms_count")
    under = rc.reconcile(full_app(carrier=3, gather=5, tts=150), tw(**{"calls-inbound": rec("calls-inbound", 2, 4, "minutes")}))
    r = row(under, "carrier_minutes")
    assert r["delta"] == Decimal(-1) and r["pct"] == Decimal("-25.0") and r["status"] == "UNDER"
    over = rc.reconcile(full_app(carrier=12), tw(**{"calls-inbound": rec("calls-inbound", 2, 10, "minutes")}))
    assert row(over, "carrier_minutes")["status"] == "OVER" and row(over, "carrier_minutes")["pct"] == Decimal("20.0")
    edge = rc.reconcile(full_app(carrier=11), tw(**{"calls-inbound": rec("calls-inbound", 2, 10, "minutes")}))
    assert row(edge, "carrier_minutes")["status"] == "OK"                    # exactly 10% is not "more than 10%"


def test_unknown_when_either_side_missing():
    rows = rc.reconcile(full_app(sms=None), tw())
    assert row(rows, "sms_count")["status"] == "UNKNOWN"
    rows = rc.reconcile(full_app(carrier=None), tw())
    assert row(rows, "carrier_minutes")["status"] == "UNKNOWN" and row(rows, "carrier_minutes")["delta"] is None
    rows = rc.reconcile(full_app(), rc.collect_twilio(FakeClient([rec("calls-inbound", 2, 3, "minutes")]), "2026-10"))
    assert row(rows, "gather_count")["status"] == "UNKNOWN"                  # Twilio returned no speech-recognition record
    assert row(rows, "carrier_minutes")["status"] == "OK"


def test_twilio_zero_and_app_nonzero_is_over_not_a_crash():
    rows = rc.reconcile(full_app(gather=4), tw(**{"speech-recognition": rec("speech-recognition", 0, 0, "")}))
    r = row(rows, "gather_count")
    assert r["status"] == "OVER" and r["pct"] is None
    rows = rc.reconcile(full_app(gather=0), tw(**{"speech-recognition": rec("speech-recognition", 0, 0, "")}))
    assert row(rows, "gather_count")["status"] == "OK"


def test_partial_app_coverage_is_flagged_in_the_note():
    app = full_app()
    app["gather_count"]["calls_measured"] = 1
    r = row(rc.reconcile(app, tw()), "gather_count")
    assert "1 of 2" in r["note"]
    assert r["status"] == "UNKNOWN" and r["delta"] is None and r["pct"] is None


def test_render_labels_authority_and_never_prints_secrets(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "supersecrettoken")
    out = rc.render(rc.reconcile(full_app(carrier=3), tw(**{"calls-inbound": rec("calls-inbound", 2, 4, "minutes")})), "2026-10")
    assert "Twilio is AUTHORITATIVE" in out and "UNDER" in out and "supersecrettoken" not in out
    assert "10%" in out


def test_main_end_to_end_with_fake_client_is_read_only(tmp_path, capsys):
    db = make_db(tmp_path, [("a", "C1", OCT, {"carrier_seconds": 61, "gather_count": 3, "tts_chars": 100})])
    before = open(db, "rb").read()
    client = FakeClient([rec("calls-inbound", 1, 2, "minutes"), rec("speech-recognition", 3, 4, ""), rec("amazon-polly", 2, 100, "use"), rec("sms", 0, 0, "")])
    code = rc.main(["--month", "2026-10", "--db", db], client_factory=lambda: client)
    out = capsys.readouterr().out
    assert code == 0 and "carrier_minutes" in out and "UNKNOWN" in out        # sms
    assert open(db, "rb").read() == before
    assert not hasattr(client.usage, "triggers")


def test_main_app_json_input_and_bad_month(tmp_path, capsys):
    f = tmp_path / "app.json"
    f.write_text(json.dumps({"calls": 1, "carrier_minutes": {"value": "2", "calls_measured": 1}, "gather_count": {"value": "3", "calls_measured": 1},
                             "tts_chars": {"value": None, "calls_measured": 0}, "sms_count": {"value": None, "calls_measured": 0}}))
    client = FakeClient([rec("calls-inbound", 1, 2, "minutes"), rec("speech-recognition", 3, 4, "")])
    assert rc.main(["--month", "2026-10", "--app-json", str(f)], client_factory=lambda: client) == 0
    assert "carrier_minutes" in capsys.readouterr().out
    assert rc.main(["--month", "2026-13", "--db", "x"], client_factory=lambda: client) == 2


def test_twilio_failure_is_a_clean_error_without_leaking_ids(tmp_path, capsys):
    db = make_db(tmp_path, [("a", "C1", OCT, {"gather_count": 3})])
    code = rc.main(["--month", "2026-10", "--db", db], client_factory=lambda: FakeClient([], fail=True))
    out = capsys.readouterr().out
    assert code == 1 and "AC+15555550100secret" not in out and "could not read" in out.lower()
