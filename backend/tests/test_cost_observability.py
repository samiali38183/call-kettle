"""PRIVATE operator cost accounting; synthetic inputs, never provider calls."""
import sqlite3
import pytest


def rate(usd="0.01", unit="1", increment="1"):
    return {"usd_per_unit": usd, "unit_quantity": unit, "billing_increment": increment,
            "rate_id": "provider:product:region:effective-date", "source_url": "https://www.twilio.com/en-us/voice/pricing/us", "checked_at": "2026-10-03"}


def measured(value):
    return {"value": value, "status": "measured", "source": "provider_final_usage"}


def test_explicit_usage_tariffs_and_per_leg_rounding_remain_estimates():
    evidence = {m: measured(0) for m in module().METRICS}
    evidence.update(carrier_seconds=measured(61), input_tokens=measured(100), output_tokens=measured(10),
                    cache_read_tokens=measured(20), cache_write_tokens=measured(30), tts_chars=measured(111),
                    gather_count=measured(2), transfer_count=measured(2), transfer_seconds=measured([1, 61]))
    rates = {m: rate() for m in module().COST_METRICS}
    rates["carrier_seconds"] = rate("0.0085", "60", "60")
    rates["transfer_seconds"] = rate("0.02", "60", "60")
    obs = module().observe_call({}, rates, evidence)
    assert obs["components"]["carrier_seconds"]["usd"] == Decimal("0.017")
    assert obs["components"]["transfer_seconds"]["usd"] == Decimal("0.06")
    assert obs["components"]["input_tokens"]["status"] == "estimated_from_measured"
    assert obs["quantities"]["cache_read_tokens"]["value"] == Decimal(20)
    assert obs["complete_cost_usd"] == Decimal("2.807")
    assert obs["components"]["input_tokens"]["rate_id"] == rates["input_tokens"]["rate_id"]


@pytest.mark.parametrize("value", [-1, "NaN", "Infinity", True, "invalid", 1.5])
def test_invalid_measured_count_is_rejected(value):
    with pytest.raises(ValueError):
        module().observe_call({}, evidence={"input_tokens": measured(value)})


def test_invalid_rate_and_missing_rate_do_not_create_actual_cost():
    obs = module().observe_call({}, evidence={"input_tokens": measured(0)})
    assert obs["quantities"]["input_tokens"]["status"] == "measured"
    assert obs["components"]["input_tokens"]["usd"] is None
    with pytest.raises(ValueError):
        module().observe_call({}, {"input_tokens": rate(unit="0")})
    with pytest.raises(ValueError):
        module().observe_call({}, evidence={"not_a_metric": measured(1)})
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "calls.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE calls (call_sid TEXT PRIMARY KEY, client_id TEXT, started_at TEXT, ended_at TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, tts_chars INTEGER DEFAULT 0)")
        conn.executemany("INSERT INTO calls(call_sid, client_id, started_at, ended_at) VALUES (?,?,?,?)", [
            ("a", "one", "2026-10-01T00:00:00+00:00", "2026-10-01T00:01:01+00:00"),
            ("b", "two", "2026-10-01T00:00:00+00:00", None)])
    return path


def test_ledger_scope_idempotency_and_revision_conflicts(db):
    m = module()
    m.init_ledger(db)
    m.init_ledger(db)
    assert m.record_snapshot(db, "one", "a", {"input_tokens": measured(123)}, revision=1) == 1
    assert m.record_snapshot(db, "one", "a", {"input_tokens": measured(123)}, revision=1) == 0
    with pytest.raises(ValueError, match="revision conflict"):
        m.record_snapshot(db, "one", "a", {"input_tokens": measured(124)}, revision=1)
    with pytest.raises(ValueError, match="tenant"):
        m.record_snapshot(db, "two", "a", {"input_tokens": measured(900)}, revision=2)
    with pytest.raises(ValueError, match="tenant"):
        m.record_snapshot(db, "one", "missing", {"input_tokens": measured(900)}, revision=2)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT input_tokens FROM calls WHERE call_sid='a'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM private_cost_usage").fetchone()[0] == 1


def test_concurrent_snapshots_keep_highest_revision(db):
    m = module()
    m.init_ledger(db)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: m.record_snapshot(db, "one", "a", {"input_tokens": measured(n)}, revision=n), range(1, 25)))
    assert m.read_evidence(db, "one", "a")["input_tokens"]["value"] == Decimal(24)
    assert m.read_evidence(db, "two", "a") == {}
    assert m.record_snapshot(db, "one", "a", {"input_tokens": measured(99)}, revision=0) == 0


def module():
    from app import cost_observability
    return cost_observability


def test_legacy_missing_usage_is_unknown_and_elapsed_is_only_projected():
    obs = module().observe_call({"call_sid": "a", "client_id": "one", "started_at": "2026-10-01T00:00:00+00:00", "ended_at": "2026-10-01T00:01:01+00:00", "input_tokens": 0, "output_tokens": 0, "tts_chars": 0})
    assert obs["quantities"]["carrier_seconds"]["status"] == "projected"
    assert obs["quantities"]["carrier_seconds"]["value"] == Decimal(61)
    for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "tts_chars", "gather_count", "transfer_count", "transfer_seconds"):
        assert obs["quantities"][name]["status"] == "unknown"
        assert obs["quantities"][name]["value"] is None
    assert obs["complete_cost_usd"] is None
    assert obs["components"]["input_tokens"]["usd"] is None
    assert module().observe_call({})["quantities"]["carrier_seconds"]["value"] is None


# ---------------------------------------------------------------------------
# Tenant-scoped monthly aggregation (written first; see them fail, then
# implemented in cost_observability.aggregate_month).
# ---------------------------------------------------------------------------

@pytest.fixture
def month_db(tmp_path):
    path = tmp_path / "month.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE calls (call_sid TEXT PRIMARY KEY, client_id TEXT, started_at TEXT, ended_at TEXT, "
            "input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, tts_chars INTEGER DEFAULT 0)"
        )
        conn.executemany(
            "INSERT INTO calls(call_sid, client_id, started_at, ended_at, input_tokens, output_tokens, tts_chars) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                ("a", "acme", "2026-10-01T00:00:00+00:00", "2026-10-01T00:01:00+00:00", 100, 10, 50),
                ("b", "acme", "2026-10-02T00:00:00+00:00", "2026-10-02T00:02:00+00:00", 200, 20, 60),
                ("c", "acme", "2026-10-03T00:00:00+00:00", "2026-10-03T00:03:00+00:00", 300, 30, 70),
                # Call with no ended_at: carrier_seconds becomes UNKNOWN, must not be silently zero.
                ("d", "acme", "2026-10-04T00:00:00+00:00", None, 0, 0, 0),
                # Different tenant must never leak into acme's aggregate.
                ("z", "other", "2026-10-01T00:00:00+00:00", "2026-10-01T00:05:00+00:00", 999, 999, 999),
                # Outside the queried month: must be excluded.
                ("e", "acme", "2026-09-15T00:00:00+00:00", "2026-09-15T00:01:00+00:00", 100, 10, 50),
            ],
        )
    return path


def month_rates():
    rates = {m: rate() for m in module().COST_METRICS}
    rates["carrier_seconds"] = rate("0.0085", "60", "60")
    return rates


def test_aggregate_month_scopes_tenant_and_calendar_month(month_db):
    now = module().datetime(2026, 10, 15, tzinfo=module().timezone.utc)
    report = module().aggregate_month(month_db, "acme", month_rates(), now=now)
    assert report["tenant"] == "acme"
    # a, b, c, d: all four October "acme" calls count, even though d has no ended_at
    # (its duration is UNKNOWN, not silently dropped from the call count). z is another
    # tenant and must never leak in; e is September and is out of the queried month.
    assert report["call_count"] == 4
    assert report["minutes"]["calls_missing_duration"] == 1


def test_aggregate_month_minutes_statistics_are_measured_from_elapsed(month_db):
    now = module().datetime(2026, 10, 15, tzinfo=module().timezone.utc)
    report = module().aggregate_month(month_db, "acme", month_rates(), now=now)
    minutes = report["minutes"]
    assert minutes["total"] == Decimal("6")
    assert minutes["avg"] == Decimal("2")
    assert minutes["p50"] == Decimal("2")
    assert minutes["p90"] == Decimal("3")
    assert minutes["p95"] == Decimal("3")


def test_aggregate_month_flags_unknown_components_without_silently_dropping(month_db):
    now = module().datetime(2026, 10, 15, tzinfo=module().timezone.utc)
    report = module().aggregate_month(month_db, "acme", month_rates(), now=now)
    # transfer_seconds/transfer_count/gather_count etc. have no recorded evidence anywhere for these legacy rows.
    assert "transfer_seconds" in report["unknown_components"]
    assert report["cost_breakdown"]["carrier_seconds"]["usd"] is not None
    assert report["total_cost_usd"] is None  # unknown components exist -> never a silently-complete total
    assert report["known_cost_usd"] is not None  # but the known partial sum is still surfaced, not hidden


def test_aggregate_month_revenue_vs_cash_contribution_flags_unknowns(month_db):
    now = module().datetime(2026, 10, 15, tzinfo=module().timezone.utc)
    report = module().aggregate_month(month_db, "acme", month_rates(), now=now, revenue_usd=Decimal("497"))
    assert report["revenue_usd"] == Decimal("497")
    assert report["cash_contribution_usd"] is None  # cost incomplete -> contribution cannot be a false precise number
    assert report["cash_contribution_status"] == "incomplete_unknown_components"


@pytest.fixture
def complete_month_db(tmp_path):
    """Two calls with EVERY metric covered by measured ledger evidence, so the
    aggregate can legitimately reach `cash_contribution_status == "complete"`."""
    path = tmp_path / "complete.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE calls (call_sid TEXT PRIMARY KEY, client_id TEXT, started_at TEXT, ended_at TEXT, "
            "input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, tts_chars INTEGER DEFAULT 0)"
        )
        conn.executemany(
            "INSERT INTO calls(call_sid, client_id, started_at, ended_at, input_tokens, output_tokens, tts_chars) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                ("p", "acme", "2026-10-01T00:00:00+00:00", "2026-10-01T00:01:00+00:00", 100, 10, 50),
                ("q", "acme", "2026-10-02T00:00:00+00:00", "2026-10-02T00:02:00+00:00", 200, 20, 60),
            ],
        )
    m = module()
    m.init_ledger(path)
    for sid in ("p", "q"):
        m.record_snapshot(path, "acme", sid, {
            "cache_read_tokens": measured(0), "cache_write_tokens": measured(0),
            "gather_count": measured(2), "transfer_count": measured(0), "transfer_seconds": measured([]),
        }, revision=1)
    return path


def test_aggregate_month_complete_data_yields_a_real_cash_contribution(complete_month_db):
    now = module().datetime(2026, 10, 15, tzinfo=module().timezone.utc)
    full_rates = {m: rate() for m in module().COST_METRICS}
    full_rates["carrier_seconds"] = rate("0.0085", "60", "60")
    report = module().aggregate_month(complete_month_db, "acme", full_rates, now=now, revenue_usd=Decimal("497"))
    assert report["unknown_components"] == []
    assert report["total_cost_usd"] == report["known_cost_usd"]
    assert report["cash_contribution_usd"] == report["revenue_usd"] - report["known_cost_usd"]
    assert report["cash_contribution_status"] == "complete"


def test_aggregate_month_without_revenue_never_fabricates_a_number(month_db):
    now = module().datetime(2026, 10, 15, tzinfo=module().timezone.utc)
    report = module().aggregate_month(month_db, "acme", month_rates(), now=now)
    assert report["revenue_usd"] is None
    assert report["cash_contribution_usd"] is None
    assert report["cash_contribution_status"] == "unknown_revenue"


def test_aggregate_month_empty_tenant_is_explicit_not_an_error(month_db):
    now = module().datetime(2026, 10, 15, tzinfo=module().timezone.utc)
    report = module().aggregate_month(month_db, "nobody", month_rates(), now=now)
    assert report["call_count"] == 0
    assert report["minutes"]["total"] == Decimal("0")
    assert report["minutes"]["avg"] is None
    assert report["total_cost_usd"] is None
    assert report["cash_contribution_status"] == "unknown_revenue"


# ---------------------------------------------------------------------------
# Operator CLI (backend/scripts/cost_report.py): read-only, stdout only,
# never prints a public selling price, default dry-run.
# ---------------------------------------------------------------------------

import subprocess
import sys
from pathlib import Path

CLI = Path(__file__).resolve().parent.parent / "scripts" / "cost_report.py"


def run_cli(*args):
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True)


def test_cli_prints_private_report_and_flags_unknowns(month_db):
    result = run_cli("acme", "--db", str(month_db), "--as-of", "2026-10-15")
    assert result.returncode == 0, result.stderr
    assert "PRIVATE" in result.stdout
    assert "UNKNOWN" in result.stdout
    assert "497" not in result.stdout  # no price is baked into this module; nothing to leak by default


def test_cli_revenue_is_operator_supplied_never_baked_in(month_db):
    result = run_cli("acme", "--db", str(month_db), "--as-of", "2026-10-15", "--revenue-usd", "497")
    assert result.returncode == 0, result.stderr
    assert "cash contribution" in result.stdout.lower() or "unknown" in result.stdout.lower()


def test_cli_rejects_unknown_tenant_cleanly(month_db):
    result = run_cli("does-not-exist", "--db", str(month_db), "--as-of", "2026-10-15")
    assert result.returncode == 0
    assert "0" in result.stdout


# ---------------------------------------------------------------------------
# Operator alerts: reporting only, never gate a call or change a commercial cap.
# ---------------------------------------------------------------------------

def test_alerts_flag_runaway_calls_by_duration(month_db):
    now = module().datetime(2026, 10, 15, tzinfo=module().timezone.utc)
    report = module().aggregate_month(month_db, "acme", month_rates(), now=now, runaway_minutes=Decimal("2.5"))
    alerts = report["alerts"]
    assert alerts["runaway_calls"] == [{"call_sid": "c", "minutes": Decimal("3")}]
    assert alerts["runaway_minutes_threshold"] == Decimal("2.5")
    default = module().aggregate_month(month_db, "acme", month_rates(), now=now)
    assert default["alerts"]["runaway_calls"] == []  # nothing near the 15 minute default


def test_alerts_margin_needs_operator_fixed_cost_and_marks_incomplete(month_db):
    now = module().datetime(2026, 10, 15, tzinfo=module().timezone.utc)
    m = module()
    no_fixed = m.aggregate_month(month_db, "acme", month_rates(), now=now, revenue_usd=Decimal("497"))
    assert no_fixed["alerts"]["margin"]["status"] == "unavailable_no_fixed_cash_cost"
    with_fixed = m.aggregate_month(month_db, "acme", month_rates(), now=now, revenue_usd=Decimal("497"),
                                   fixed_cash_usd=Decimal("450"))
    margin = with_fixed["alerts"]["margin"]
    assert margin["status"] == "below_70"
    assert margin["incomplete_unknown_components"] is True  # an upper bound, never presented as final
    assert margin["margin_upper_bound"] < Decimal("0.7")


def test_alerts_margin_ok_when_complete_and_above_target(complete_month_db):
    now = module().datetime(2026, 10, 15, tzinfo=module().timezone.utc)
    full_rates = {m: rate() for m in module().COST_METRICS}
    full_rates["carrier_seconds"] = rate("0.0085", "60", "60")
    report = module().aggregate_month(complete_month_db, "acme", full_rates, now=now,
                                      revenue_usd=Decimal("497"), fixed_cash_usd=Decimal("38.342"))
    margin = report["alerts"]["margin"]
    assert margin["status"] == "ok"
    assert margin["incomplete_unknown_components"] is False


def test_cli_prints_alerts_and_accepts_fixed_cost(month_db):
    import subprocess, sys
    out = subprocess.run(
        [sys.executable, "scripts/cost_report.py", "acme", "--db", str(month_db), "--as-of", "2026-10-15",
         "--rates", "example", "--revenue-usd", "497", "--fixed-cash-usd", "450", "--runaway-minutes", "2.5"],
        capture_output=True, text=True, check=True, cwd=str(Path(__file__).resolve().parent.parent)).stdout
    assert "runaway calls" in out and "c=3" in out
    assert "margin alert: below_70" in out and "UPPER bound" in out


# ---------------------------------------------------------------------------
# Policy headroom block (reporting only; never gates a call). Written first.
# ---------------------------------------------------------------------------

def _hr_report(cost, *, calls=5, unknown=(), revenue="497"):
    return {"period_start": "2026-10-01T00:00:00+00:00", "period_end": "2026-11-01T00:00:00+00:00",
            "call_count": calls, "known_cost_usd": None if cost is None else Decimal(cost),
            "unknown_components": list(unknown), "revenue_usd": None if revenue is None else Decimal(revenue)}


def _utc(day):
    m = module()
    return m.datetime(2026, 10, day, tzinfo=m.timezone.utc)


def test_headroom_budgets_are_revenue_times_one_minus_target_minus_fixed():
    h = module().policy_headroom(_hr_report("10"), Decimal("38.342"), _utc(16))
    by = {t["target_margin"]: t for t in h["targets"]}
    assert by["0.8"]["budget_usd"] == Decimal("61.058")
    assert by["0.7"]["budget_usd"] == Decimal("110.758")
    assert h["targets"][0]["cost_so_far_usd"] == Decimal("10")


def test_headroom_projection_is_linear_run_rate_and_labeled_assumption():
    h = module().policy_headroom(_hr_report("10"), Decimal("38.342"), _utc(16))  # 15 of 31 days elapsed
    t = h["targets"][0]
    assert t["projected_month_end_usd"] == Decimal("10") * Decimal(31) / Decimal(15)
    assert h["projection_label"].startswith("ASSUMPTION")
    assert "never gates" in h["note"].lower()


def test_headroom_status_ok_watch_over():
    m = module()
    ok = m.policy_headroom(_hr_report("10"), Decimal("38.342"), _utc(16))        # projected 20.7 < 61.058
    assert ok["status"] == "ok" and ok["targets"][0]["status"] == "ok"
    watch = m.policy_headroom(_hr_report("40"), Decimal("38.342"), _utc(16))     # projected 82.7 > 61.058, < 110.758
    assert watch["targets"][0]["status"] == "watch" and watch["targets"][1]["status"] == "ok"
    assert watch["status"] == "watch"
    over80 = m.policy_headroom(_hr_report("70"), Decimal("38.342"), _utc(16))    # actual above 61.058 only
    assert over80["targets"][0]["status"] == "over" and over80["status"] == "watch"
    over = m.policy_headroom(_hr_report("120"), Decimal("38.342"), _utc(16))
    assert over["targets"][1]["status"] == "over" and over["status"] == "over"


def test_headroom_at_75_percent_of_budget_is_watch_even_if_run_rate_is_low():
    h = module().policy_headroom(_hr_report("50"), Decimal("38.342"), _utc(31))  # last day: projection ~ actual; 50 >= .75*61.058
    assert h["targets"][0]["status"] == "watch"


def test_headroom_unknown_inputs_never_become_zero():
    m = module()
    assert m.policy_headroom(_hr_report("10", revenue=None), Decimal("38.342"), _utc(16))["status"] == "unknown"
    assert m.policy_headroom(_hr_report("10"), None, _utc(16))["status"] == "unknown"
    nocost = m.policy_headroom(_hr_report(None, calls=3), Decimal("38.342"), _utc(16))
    assert nocost["status"] == "unknown" and nocost["targets"][0]["cost_so_far_usd"] is None
    partial = m.policy_headroom(_hr_report("10", unknown=["tts_chars"]), Decimal("38.342"), _utc(16))
    assert partial["cost_is_lower_bound"] is True and partial["status"] == "unknown"  # ok is never claimed on a lower bound
    big = m.policy_headroom(_hr_report("120", unknown=["tts_chars"]), Decimal("38.342"), _utc(16))
    assert big["status"] == "over"  # a lower bound already over budget is still over


def test_headroom_no_calls_is_measured_zero_and_ok():
    h = module().policy_headroom(_hr_report(None, calls=0), Decimal("38.342"), _utc(16))
    assert h["status"] == "ok" and h["targets"][0]["cost_so_far_usd"] == Decimal("0")


def test_headroom_zero_budget_and_closed_month():
    m = module()
    h = m.policy_headroom(_hr_report("1"), Decimal("500"), _utc(16))  # fixed cash alone exceeds every budget
    assert h["targets"][0]["budget_usd"] == Decimal("0") and h["status"] == "over"
    past = m.policy_headroom(_hr_report("10"), Decimal("38.342"), m.datetime(2026, 12, 1, tzinfo=m.timezone.utc))
    assert past["targets"][0]["projected_month_end_usd"] == Decimal("10")  # closed month: actual, not extrapolated


def test_cli_prints_policy_headroom_block(month_db):
    out = subprocess.run(
        [sys.executable, "scripts/cost_report.py", "acme", "--db", str(month_db), "--as-of", "2026-10-15",
         "--rates", "example", "--revenue-usd", "497", "--fixed-cash-usd", "38.342"],
        capture_output=True, text=True, check=True, cwd=str(Path(__file__).resolve().parent.parent)).stdout
    assert "policy headroom" in out
    assert "ASSUMPTION" in out and "target 80%" in out and "target 70%" in out
    assert "497" not in out.split("policy headroom", 1)[1]
    bare = subprocess.run([sys.executable, "scripts/cost_report.py", "acme", "--db", str(month_db), "--as-of", "2026-10-15"],
                          capture_output=True, text=True, check=True, cwd=str(Path(__file__).resolve().parent.parent)).stdout
    assert "policy headroom: unknown" in bare
