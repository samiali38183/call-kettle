"""Offline tests for the pure helpers of scripts/measure_call_costs.py (no network, no provider calls)."""
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parent.parent / "scripts" / "measure_call_costs.py"


@pytest.fixture(scope="module")
def m():
    spec = importlib.util.spec_from_file_location("measure_call_costs", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_percentile_nearest_rank_and_empty(m):
    assert m.percentile([], 90) is None
    assert m.percentile([5], 90) == 5
    vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert m.percentile(list(reversed(vals)), 100) == 10      # sorts internally
    assert m.percentile(vals, 0) == 1
    assert m.percentile(vals, 90) == 10                       # ceil(0.9 * 9) = 9 -> index 9
    assert m.percentile(vals, 50) == 6                        # ceil(4.5) = 5 -> index 5


def test_aggregate_avg_p90_total(m):
    rows = [{"x": Decimal(v)} for v in (1, 2, 3, 4, 10)]
    a = m.aggregate(rows, "x")
    assert a["n"] == 5 and a["total"] == Decimal(20) and a["avg"] == Decimal(4)
    assert a["p90"] == Decimal(10) and a["max"] == Decimal(10) and a["min"] == Decimal(1)
    assert m.aggregate([], "x")["avg"] is None


def test_spend_tracker_cap(m):
    t = m.SpendTracker(cap_usd=Decimal("0.01"))
    t.add(1000, 100)                                          # 1000*1/1M + 100*5/1M = 0.0015
    assert t.estimate_usd == Decimal("0.0015") and not t.exceeded
    t.add(0, 1800)                                            # +0.009 -> 0.0105 > cap
    assert t.exceeded
    with pytest.raises(m.SpendCapExceeded):
        t.check()


def test_spend_tracker_cap_is_inclusive_safe(m):
    t = m.SpendTracker(cap_usd=Decimal("0.005"))
    t.add(0, 1000)                                            # exactly 0.005: not over the cap
    assert not t.exceeded
    t.check()


def test_spend_tracker_ignores_missing_usage(m):
    t = m.SpendTracker(cap_usd=Decimal("1"))
    t.add(None, None)
    assert t.estimate_usd == 0


def test_carrier_seconds_assumption(m):
    assert m.assumed_carrier_seconds(0) == m.CALL_SETUP_SECONDS
    assert m.assumed_carrier_seconds(4) == m.CALL_SETUP_SECONDS + 4 * m.SECONDS_PER_TURN
    assert m.assumed_carrier_seconds(-3) == m.CALL_SETUP_SECONDS


def test_project_monthly_scales_linearly(m):
    assert m.project_monthly(Decimal("0.05"), 300) == Decimal("15.00")
    assert m.project_monthly(Decimal("0.05"), 450) == Decimal("22.50")


def test_max_token_cost_for_target_margin(m):
    # revenue 100, margin 80% -> total cost budget 20; other cost 12 over 100 calls -> 8 left -> $0.08 per call
    assert m.max_token_cost_per_call(revenue=Decimal(100), margin=Decimal("0.8"), other_cost=Decimal(12), calls=100) == Decimal("0.08")
    # already over budget without any tokens -> negative (no token cost can reach the target)
    assert m.max_token_cost_per_call(revenue=Decimal(100), margin=Decimal("0.8"), other_cost=Decimal(25), calls=100) < 0


def test_requires_explicit_flag(m, capsys):
    assert m.main([]) == 2
    assert "i-understand-this-spends-api-credits" in capsys.readouterr().err


def test_personas_cover_required_scenarios(m):
    names = {p["name"] for p in m.PERSONAS}
    for need in ("ac_repair_booking", "no_heat_emergency", "gas_smell_emergency", "price_shopper", "rambling_changes_mind", "reschedule",
                 "cancel", "hours_question", "spanish_caller", "wants_a_person", "wrong_number", "long_chatty"):
        assert need in names
    assert len({p["from_number"] for p in m.PERSONAS}) == len(m.PERSONAS)
    assert all(p["from_number"].startswith("+1") and p["from_number"][5:8] == "555" for p in m.PERSONAS)   # fictional 555 numbers only
