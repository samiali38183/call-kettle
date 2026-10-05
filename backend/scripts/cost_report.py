"""PRIVATE operator-only cost observability report for one tenant/month.

Read-only against the existing `calls` table (and the optional additive
`private_cost_usage` ledger, if `app.cost_observability.init_ledger` has been
run against this database). Never writes, never gates calls, never enforces
the existing cost ceiling (see app/cost_guard.py / app/costing.py for that).

This script never reads or prints the live commercial selling price: revenue
is purely an optional, operator-supplied number for THIS invocation
(--revenue-usd), never read from any pricing config. Every dollar figure is
explicitly tagged MEASURED / PROJECTED / UNKNOWN; unknown components are
listed, never silently folded into a total as zero.

    python scripts/cost_report.py acme_hvac
    python scripts/cost_report.py acme_hvac --as-of 2026-10-31 --revenue-usd 497
    python scripts/cost_report.py acme_hvac --db C:/path/to/a/copy/of/callkettle.db

Default is read-only/dry-run: this script has no write/commit mode at all.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import cost_observability as co  # noqa: E402


def _fmt_usd(value):
    if value is None:
        return "UNKNOWN"
    return f"${value:,.4f}"


def _fmt_decimal(value):
    if value is None:
        return "UNKNOWN"
    return str(value)


def worst_case_block(tenant, config=None):
    """Worst-case exposure of THIS tenant's configured limits against the $100 cap (owner directive 2026-10-04).

    Reporting only; MODELED over documented assumptions (marketing/cash_margin.py); reads no price and no revenue.
    """
    try:
        import importlib.util
        path = ROOT.parent / "marketing" / "cash_margin.py"
        spec = importlib.util.spec_from_file_location("cash_margin_wc", path)
        cm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cm)
        from app import costing
        from app.config import load_client_config
        cfg = config or load_client_config(tenant)
    except Exception as exc:  # noqa: BLE001 - a reporting block must never break the report
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    expected = Decimal(cm.forensic_original()["cash_contribution"]["cost"])
    ai = cm.ai_call_max_cost(cfg.max_turns, cfg.max_call_seconds)
    ceiling = Decimal(str(costing.cost_ceiling(cfg)))
    cap_calls = costing.post_ceiling_cap(cfg)
    modes = {}
    for mode in cm.WORST_MODES:
        post = cm.post_ceiling_call_cost(mode, cfg.ceiling_transfer_seconds)
        modes[mode] = cm.worst_case_exposure(
            expected_cost=expected, ceiling=ceiling, ceiling_mode=mode, ai_call_max_cost=ai["usd"],
            concurrency=costing.MAX_CONCURRENT_CALLS, under_read_usd=cm.WORST_UNDER_READ_USD, post_ceiling_call_cap=cap_calls,
            post_call_cost=post["usd"], reserve_per_open_call=Decimal(str(costing.OPEN_CALL_RESERVE_USD)))
    configured = modes[cfg.ceiling_mode]
    return {"available": True, "tenant": tenant, "configured_mode": cfg.ceiling_mode, "ceiling": ceiling, "post_ceiling_call_cap": cap_calls,
            "modes": modes, "status": configured["status"], "allowed_excess": cm.WORST_ALLOWED_EXCESS}


def render_worst_case(block) -> str:
    if not block or not block.get("available"):
        reason = (block or {}).get("reason", "not computed")
        return f"worst-case exposure vs the $100 cap: UNKNOWN ({reason}); nothing is assumed"
    lines = [f"worst-case exposure vs the ${block['allowed_excess']:.0f} cap (MODELED upper bound, not measured; reporting only):",
             f"  configured: ceiling=${block['ceiling']:.2f} mode={block['configured_mode']} post_ceiling_call_cap={block['post_ceiling_call_cap']}"
             f"  overall={str(block['status']).upper()}"]
    for mode, m in block["modes"].items():
        if m["worst_case_loss_vs_expected"] is None:
            lines.append(f"  {mode:9s} worst-case loss vs expected: UNKNOWN  status={m['status']}")
            continue
        lines.append(f"  {mode:9s} worst-case usage ${m['worst_case_usage']:.2f}  loss vs expected profit ${m['worst_case_loss_vs_expected']:.2f}  "
                     f"{m['status'].upper()}  largest safe ceiling ${m['largest_safe_ceiling']:.2f}")
    return "\n".join(lines)


def render(report: dict) -> str:
    lines = []
    lines.append("=" * 72)
    lines.append("PRIVATE cost-observability report — operator eyes only, do not share")
    lines.append("All dollar figures are estimates derived from configured rates, not")
    lines.append("provider invoices. UNKNOWN components are listed, never zeroed out.")
    lines.append("=" * 72)
    lines.append(f"tenant:        {report['tenant']}")
    lines.append(f"period:        {report['period_start']}  ..  {report['period_end']}")
    lines.append(f"call_count:    {report['call_count']}")
    lines.append("")
    m = report["minutes"]
    lines.append("minutes (measured/projected from started_at/ended_at elapsed time):")
    lines.append(f"  total={_fmt_decimal(m['total'])}  avg={_fmt_decimal(m['avg'])}  "
                 f"p50={_fmt_decimal(m['p50'])}  p90={_fmt_decimal(m['p90'])}  p95={_fmt_decimal(m['p95'])}")
    lines.append(f"  calls_missing_duration={m['calls_missing_duration']} (UNKNOWN, excluded from the stats above, not zeroed)")
    lines.append("")
    lines.append("cost breakdown by component:")
    for metric, c in report["cost_breakdown"].items():
        flag = "" if c["complete"] else "  <-- UNKNOWN for some calls"
        lines.append(f"  {metric:20s} {_fmt_usd(c['usd']):>14s}  ({c['calls_known']}/{c['calls_total']} calls known){flag}")
    lines.append("")
    if report["unknown_components"]:
        lines.append(f"UNKNOWN components (never silently treated as $0): {', '.join(report['unknown_components'])}")
    else:
        lines.append("No UNKNOWN components this period.")
    lines.append(f"known_cost_usd (partial sum, always shown): {_fmt_usd(report['known_cost_usd'])}")
    lines.append(f"total_cost_usd (only set when nothing is UNKNOWN): {_fmt_usd(report['total_cost_usd'])}")
    lines.append("")
    lines.append(f"revenue_usd (operator-supplied this run; never read from pricing config): {_fmt_usd(report['revenue_usd'])}")
    lines.append(f"cash_contribution_usd: {_fmt_usd(report['cash_contribution_usd'])}")
    lines.append(f"cash_contribution_status: {report['cash_contribution_status']}")
    a = report["alerts"]
    lines.append(f"runaway calls (> {a['runaway_minutes_threshold']} min; reporting only): "
                 + (", ".join(f"{c['call_sid']}={c['minutes']}min" for c in a["runaway_calls"]) or "none"))
    mg = a["margin"]
    bound = "UNKNOWN" if mg["margin_upper_bound"] is None else f"{mg['margin_upper_bound']:.4f}"
    suffix = " (UPPER bound: some cost components are UNKNOWN)" if mg["incomplete_unknown_components"] else ""
    lines.append(f"margin alert: {mg['status']}  margin={bound}{suffix}")
    h = report.get("policy_headroom")
    if h is not None:
        lines.append("policy headroom (internal usage-cost budget vs this month; reporting only, never gates a call):")
        if h["status"] == "unknown" and all(t["budget_usd"] is None for t in h["targets"]):
            lines.append("policy headroom: unknown (needs --revenue-usd and --fixed-cash-usd; nothing is assumed)")
        else:
            lines.append(f"policy headroom: {h['status']}" + ("  (cost so far is a LOWER bound: some components UNKNOWN)" if h["cost_is_lower_bound"] else ""))
        for t in h["targets"]:
            lines.append(f"  target {int(Decimal(t['target_margin']) * 100)}%: budget={_fmt_usd(t['budget_usd'])}  so_far={_fmt_usd(t['cost_so_far_usd'])}  "
                         f"projected_month_end={_fmt_usd(t['projected_month_end_usd'])}  status={t['status']}")
        lines.append("  " + h["projection_label"])
    if report.get("worst_case") is not None:
        lines.append(render_worst_case(report["worst_case"]))
    lines.append("=" * 72)
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tenant", help="client_id to report on")
    parser.add_argument("--db", default=None, help="path to the sqlite database (defaults to CALLKETTLE_DB_PATH or ./callkettle.db)")
    parser.add_argument("--as-of", default=None, help="YYYY-MM-DD; reports the calendar month containing this date (default: today, UTC)")
    parser.add_argument("--revenue-usd", default=None, help="operator-supplied revenue for this tenant/month; omit to leave revenue UNKNOWN")
    parser.add_argument("--rates", default=None, choices=["example"], help="use the bundled EXAMPLE_RATES placeholder config (default: no rates, everything stays UNKNOWN)")
    parser.add_argument("--fixed-cash-usd", default=None, help="operator-supplied fixed monthly cash cost allocated to this tenant (enables the margin alert)")
    parser.add_argument("--runaway-minutes", default=None, help="flag calls longer than this many minutes (default 15; reporting only)")
    args = parser.parse_args(argv)

    import os

    db_path = args.db or os.environ.get("CALLKETTLE_DB_PATH", "./callkettle.db")
    if not Path(db_path).exists():
        print(f"No database found at {db_path!r}.")
        return 1

    now = None
    if args.as_of:
        try:
            now = datetime.strptime(args.as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            print("--as-of needs a date like 2026-10-31")
            return 2

    rates = co.EXAMPLE_RATES if args.rates == "example" else None
    revenue = Decimal(args.revenue_usd) if args.revenue_usd is not None else None

    fixed = Decimal(args.fixed_cash_usd) if args.fixed_cash_usd is not None else None
    runaway = Decimal(args.runaway_minutes) if args.runaway_minutes is not None else None
    report = co.aggregate_month(db_path, args.tenant, rates, now=now, revenue_usd=revenue,
                                runaway_minutes=runaway, fixed_cash_usd=fixed)
    report["policy_headroom"] = co.policy_headroom(report, fixed, now)
    report["worst_case"] = worst_case_block(args.tenant)
    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
