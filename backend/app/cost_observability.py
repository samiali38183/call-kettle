"""PRIVATE operator observability, independent of costing/cost_guard.

No bundled prices, no commercial cap enforcement, no network calls. Provider
usage is measured; multiplying it by a configured tariff remains an estimate,
not an invoice. Legacy defaults cannot establish a measured zero or completeness.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from urllib.parse import urlparse
from pathlib import Path
from contextlib import closing
import json
import sqlite3


def _connect(path, *, writable=False, timeout=10):
    # mode=rw also refuses accidental creation of a missing operational DB.
    uri = Path(path).resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    if not writable:
        conn.execute("PRAGMA query_only=ON")
    return conn


def init_ledger(path):
    """Explicit, additive opt-in; never called by a report or CLI.

    Separate table is justified because legacy calls have no provenance,
    cache/gather/transfer counts or carrier duration. It contains no transcripts,
    phone numbers, revenue or customer prices. No existing rows are changed.
    """
    with closing(_connect(path, writable=True)) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS private_cost_usage (client_id TEXT NOT NULL, call_sid TEXT NOT NULL, metric TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(client_id, call_sid, metric))")


def record_snapshot(path, tenant, call_sid, evidence, *, revision, timeout=10):
    """Store authoritative cumulative per-metric totals, NEVER deltas.

    Callers own reconciliation/coverage and supply a monotonic revision per
    metric. Missing keys do not overwrite other metrics. Same revision/value
    retries are idempotent; stale revisions ignored; conflicting retries rejected.
    """
    revision = int(_number(revision, integer=True))
    values = _evidence(evidence)
    changed = 0
    with closing(_connect(path, writable=True, timeout=timeout)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM calls WHERE client_id=? AND call_sid=?", (tenant, call_sid)).fetchone():
            raise ValueError("call does not belong to tenant")
        for metric, item in values.items():
            payload = json.dumps(item, default=str, sort_keys=True, separators=(",", ":"))
            old = conn.execute("SELECT revision,payload FROM private_cost_usage WHERE client_id=? AND call_sid=? AND metric=?", (tenant, call_sid, metric)).fetchone()
            if old and old["revision"] >= revision:
                if old["revision"] == revision and old["payload"] != payload:
                    raise ValueError("revision conflict")
                continue
            conn.execute("INSERT INTO private_cost_usage VALUES (?,?,?,?,?) ON CONFLICT(client_id,call_sid,metric) DO UPDATE SET revision=excluded.revision,payload=excluded.payload", (tenant, call_sid, metric, revision, payload))
            changed += 1
    return changed


def add_token_usage(path, tenant, call_sid, deltas, *, source="anthropic_usage", timeout=10):
    """Add measured token counts to a call's cumulative totals in one transaction (used for work done after the live
    session is gone, e.g. the post-call summary). Only counts are stored, never text. Tenant must own the call."""
    deltas = {m: int(_number(v, integer=True)) for m, v in (deltas or {}).items() if v is not None}
    if not deltas:
        return 0
    with closing(_connect(path, writable=True, timeout=timeout)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM calls WHERE client_id=? AND call_sid=?", (tenant, call_sid)).fetchone():
            raise ValueError("call does not belong to tenant")
        for metric, add in deltas.items():
            if metric not in METRICS:
                raise ValueError("invalid metric")
            old = conn.execute("SELECT revision,payload FROM private_cost_usage WHERE client_id=? AND call_sid=? AND metric=?", (tenant, call_sid, metric)).fetchone()
            base, revision = 0, 1
            if old:
                prior = json.loads(old["payload"])
                base = int(prior.get("value") or 0) if prior.get("status") == "measured" else 0
                revision = old["revision"] + 1
            payload = json.dumps({"value": base + add, "status": "measured", "source": source}, sort_keys=True, separators=(",", ":"))
            conn.execute("INSERT INTO private_cost_usage VALUES (?,?,?,?,?) ON CONFLICT(client_id,call_sid,metric) DO UPDATE SET revision=excluded.revision,payload=excluded.payload", (tenant, call_sid, metric, revision, payload))
    return len(deltas)


def _read_evidence(conn, tenant, call_sid):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='private_cost_usage'").fetchone():
        return {}
    rows = conn.execute("SELECT metric,payload FROM private_cost_usage WHERE client_id=? AND call_sid=? AND EXISTS (SELECT 1 FROM calls WHERE calls.client_id=? AND calls.call_sid=?)", (tenant, call_sid, tenant, call_sid))
    return _evidence({r["metric"]: json.loads(r["payload"]) for r in rows})


def read_evidence(path, tenant, call_sid):
    with closing(_connect(path)) as conn:
        return _read_evidence(conn, tenant, call_sid)



def _number(value, *, integer=False, positive=False):
    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not usage")
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or (positive and number == 0):
            raise ValueError("expected finite nonnegative number")
        if integer and number != number.to_integral_value():
            raise ValueError("expected integer count")
        return number
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("invalid number") from exc


def _evidence(evidence):
    out = {}
    for metric, item in (evidence or {}).items():
        if metric not in METRICS or item.get("status") not in ("measured", "projected", "unknown"):
            raise ValueError("invalid metric/status")
        source = item.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("usage requires provenance")
        value = item.get("value")
        if item["status"] == "unknown":
            if value is not None:
                raise ValueError("unknown usage cannot carry a numeric value")
        elif metric == "transfer_seconds" and isinstance(value, list):
            value = [_number(v) for v in value]
        else:
            value = _number(value, integer=not metric.endswith("seconds"))
        out[metric] = {"value": value, "status": item["status"], "source": source}
    return out


def _rates(rates):
    out = {}
    for metric, item in (rates or {}).items():
        if metric not in COST_METRICS:
            raise ValueError("invalid cost metric")
        if not item.get("rate_id") or not item.get("checked_at"):
            raise ValueError("rate requires official product/region/version identifier and checked date")
        datetime.fromisoformat(item["checked_at"])
        url = urlparse(item.get("source_url", ""))
        if url.scheme != "https" or not url.netloc:
            raise ValueError("rate requires official HTTPS source URL")
        out[metric] = {**item, "usd_per_unit": _number(item["usd_per_unit"]),
                       "unit_quantity": _number(item["unit_quantity"], positive=True),
                       "billing_increment": _number(item["billing_increment"], positive=True)}
    return out

METRICS = ("carrier_seconds", "input_tokens", "output_tokens", "cache_read_tokens",
           "cache_write_tokens", "tts_chars", "gather_count", "transfer_count", "transfer_seconds",
           "stt_seconds")   # stt_seconds: measured streaming-STT audio (stream mode); NO example rate on purpose: the unit rate is UNKNOWN
COST_METRICS = tuple(m for m in METRICS if m != "transfer_count")


# External, swappable rate config. These are PLACEHOLDER figures copied from public list
# prices at the `checked_at` date below, not a secret and not the live commercial price of
# this product. The parent should replace `source_url`/`checked_at`/`usd_per_unit` with the
# operator's actual contracted rates before trusting PROJECTED dollar figures for real money
# decisions; `_rates()` already refuses any rate missing an identifier, https source or date.
EXAMPLE_RATES = {
    "carrier_seconds": {"usd_per_unit": "0.0085", "unit_quantity": "60", "billing_increment": "60",
                         "rate_id": "twilio:voice:us-inbound:placeholder", "source_url": "https://www.twilio.com/en-us/voice/pricing/us", "checked_at": "2026-10-03"},
    "transfer_seconds": {"usd_per_unit": "0.013", "unit_quantity": "60", "billing_increment": "60",
                          "rate_id": "twilio:voice:us-outbound-dial:placeholder", "source_url": "https://www.twilio.com/en-us/voice/pricing/us", "checked_at": "2026-10-03"},
    "input_tokens": {"usd_per_unit": "1.00", "unit_quantity": "1000000", "billing_increment": "1",
                      "rate_id": "anthropic:claude-haiku:input:placeholder", "source_url": "https://www.anthropic.com/pricing", "checked_at": "2026-10-03"},
    "output_tokens": {"usd_per_unit": "5.00", "unit_quantity": "1000000", "billing_increment": "1",
                       "rate_id": "anthropic:claude-haiku:output:placeholder", "source_url": "https://www.anthropic.com/pricing", "checked_at": "2026-10-03"},
    "cache_read_tokens": {"usd_per_unit": "0.10", "unit_quantity": "1000000", "billing_increment": "1",
                           "rate_id": "anthropic:claude-haiku:cache-read:placeholder", "source_url": "https://www.anthropic.com/pricing", "checked_at": "2026-10-03"},
    "cache_write_tokens": {"usd_per_unit": "1.25", "unit_quantity": "1000000", "billing_increment": "1",
                            "rate_id": "anthropic:claude-haiku:cache-write:placeholder", "source_url": "https://www.anthropic.com/pricing", "checked_at": "2026-10-03"},
    "tts_chars": {"usd_per_unit": "0.0032", "unit_quantity": "100", "billing_increment": "1",
                  "rate_id": "aws-polly:neural:placeholder", "source_url": "https://aws.amazon.com/polly/pricing/", "checked_at": "2026-10-03"},
    "gather_count": {"usd_per_unit": "0.02", "unit_quantity": "1", "billing_increment": "1",
                      "rate_id": "twilio:speech-recognition:placeholder", "source_url": "https://www.twilio.com/en-us/voice/pricing/us", "checked_at": "2026-10-03"},
}


def _elapsed(row):
    try:
        start = datetime.fromisoformat(row["started_at"])
        end = datetime.fromisoformat(row["ended_at"])
        if start.tzinfo is None or end.tzinfo is None or end < start:
            return None
        return Decimal(str((end - start).total_seconds()))
    except (KeyError, TypeError, ValueError):
        return None


def _percentile(sorted_values, pct):
    """Nearest-rank percentile over a sorted list of Decimals (pct in 0..100)."""
    if not sorted_values:
        return None
    n = len(sorted_values)
    rank = (Decimal(str(pct)) / Decimal(100) * (n - 1))
    idx = int(rank.to_integral_value(rounding=ROUND_CEILING))
    idx = min(max(idx, 0), n - 1)
    return sorted_values[idx]


DEFAULT_RUNAWAY_MINUTES = Decimal("15")
MARGIN_ALERT_TARGETS = ((Decimal("0.8"), "below_80"), (Decimal("0.7"), "below_70"))


def _margin_alert(revenue_usd, known_cost_usd, fixed_cash_usd, incomplete):
    """Margin on supplied revenue and operator-supplied fixed cash cost. With unknown cost components the
    figure is only an UPPER bound (unknown costs would lower it), and is labeled so."""
    if revenue_usd is None or revenue_usd <= 0:
        return {"status": "unavailable_no_revenue", "incomplete_unknown_components": incomplete, "margin_upper_bound": None}
    if fixed_cash_usd is None:
        return {"status": "unavailable_no_fixed_cash_cost", "incomplete_unknown_components": incomplete, "margin_upper_bound": None}
    if known_cost_usd is None:
        return {"status": "unavailable_no_usage_cost", "incomplete_unknown_components": incomplete, "margin_upper_bound": None}
    margin = (revenue_usd - known_cost_usd - _number(fixed_cash_usd)) / revenue_usd
    status = "ok"
    if margin < Decimal("0.7"):
        status = "below_70"
    elif margin < Decimal("0.8"):
        status = "below_80"
    return {"status": status, "incomplete_unknown_components": incomplete, "margin_upper_bound": margin}


HEADROOM_TARGETS = (Decimal("0.80"), Decimal("0.70"))
HEADROOM_WATCH_FRACTION = Decimal("0.75")
HEADROOM_PROJECTION_LABEL = ("ASSUMPTION: simple linear run-rate (cost so far / elapsed share of the month); "
                             "not a forecast, ignores weekday/seasonal patterns")


def _headroom_text(value):
    return format(value.normalize(), "f")


def policy_headroom(report, fixed_cash_usd, now=None, targets=HEADROOM_TARGETS):
    """Operator-only usage-cost headroom against the budget for each target margin. Reporting only.

    budget = revenue x (1 - target) - operator-supplied non-usage cash (floored at 0). Status per target:
    over = cost so far already above budget; watch = projected month-end above budget or >= 75% of it used;
    ok otherwise; unknown when an input is unknown (an `ok` on a lower-bound cost is never claimed). The overall
    status is `over` only when the LAST (lowest) target is over; the other targets can only raise it to `watch`.
    """
    now = now or datetime.now(timezone.utc)
    start, end = datetime.fromisoformat(report["period_start"]), datetime.fromisoformat(report["period_end"])
    revenue = report.get("revenue_usd")
    fixed = _number(fixed_cash_usd) if fixed_cash_usd is not None else None
    lower_bound = bool(report.get("unknown_components"))
    cost = report.get("known_cost_usd")
    if cost is None and report.get("call_count") == 0:
        cost = Decimal(0)  # no calls this month is a measured zero, not unknown
    elapsed = min(max((now - start).total_seconds(), 0), (end - start).total_seconds())
    fraction = Decimal(str(elapsed)) / Decimal(str((end - start).total_seconds()))
    projected = None if cost is None or fraction <= 0 else cost / fraction
    rows = []
    for target in targets:
        budget = None
        if revenue is not None and fixed is not None:
            budget = max(Decimal(0), revenue * (Decimal(1) - target) - fixed)
        if budget is None or cost is None:
            status = "unknown"
        elif cost > budget:
            status = "over"
        elif (projected is not None and projected > budget) or cost >= budget * HEADROOM_WATCH_FRACTION:
            status = "watch"
        else:
            status = "unknown" if lower_bound else "ok"
        rows.append({"target_margin": _headroom_text(target), "budget_usd": budget, "cost_so_far_usd": cost,
                     "projected_month_end_usd": projected, "status": status})
    statuses = [r["status"] for r in rows]
    if statuses and statuses[-1] == "over":
        overall = "over"
    elif any(s in ("over", "watch") for s in statuses):
        overall = "watch"
    elif any(s == "unknown" for s in statuses) or not statuses:
        overall = "unknown"
    else:
        overall = "ok"
    return {"status": overall, "targets": rows, "cost_is_lower_bound": lower_bound,
            "projection_label": HEADROOM_PROJECTION_LABEL,
            "note": "Operator reporting only: never gates a call and never changes a customer cap or price."}



def aggregate_month(path, tenant, rates=None, *, now=None, revenue_usd=None, runaway_minutes=None, fixed_cash_usd=None):
    """Tenant-scoped monthly rollup, additive/read-only over `calls` + the optional ledger.

    Never gates calls, never mutates storage. Costs/contribution are surfaced as
    MEASURED/PROJECTED where possible; any component with no usable evidence anywhere
    in the month is listed in `unknown_components` and the totals that depend on it
    stay None rather than silently treating missing data as zero.
    """
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    with closing(_connect(path)) as conn:
        rows = conn.execute(
            "SELECT call_sid, client_id, started_at, ended_at, input_tokens, output_tokens, tts_chars "
            "FROM calls WHERE client_id = ? AND started_at >= ? AND started_at < ?",
            (tenant, start.isoformat(), end.isoformat()),
        ).fetchall()
        observations = []
        for row in rows:
            row = dict(row)
            evidence = _read_evidence(conn, tenant, row["call_sid"])
            observations.append(observe_call(row, rates, evidence))

    minute_values = []
    runaway_threshold = _number(runaway_minutes) if runaway_minutes is not None else DEFAULT_RUNAWAY_MINUTES
    runaway_calls = []
    for obs in observations:
        seconds = obs["quantities"]["carrier_seconds"]["value"]
        if seconds is not None:
            call_minutes = seconds / Decimal(60)
            minute_values.append(call_minutes)
            if call_minutes > runaway_threshold:
                runaway_calls.append({"call_sid": obs["call_sid"], "minutes": call_minutes})
    minute_values_sorted = sorted(minute_values)
    minutes = {
        "total": sum(minute_values_sorted) if minute_values_sorted else Decimal("0"),
        "avg": (sum(minute_values_sorted) / len(minute_values_sorted)) if minute_values_sorted else None,
        "p50": _percentile(minute_values_sorted, 50),
        "p90": _percentile(minute_values_sorted, 90),
        "p95": _percentile(minute_values_sorted, 95),
        "calls_missing_duration": len(observations) - len(minute_values_sorted),
    }

    cost_breakdown = {}
    unknown_components = []
    for metric in COST_METRICS:
        usd_values = [obs["components"][metric]["usd"] for obs in observations]
        known = [v for v in usd_values if v is not None]
        incomplete = len(known) != len(usd_values)
        if incomplete and not observations:
            incomplete = False  # no calls at all is "no data", not a per-component unknown
        if incomplete:
            unknown_components.append(metric)
        cost_breakdown[metric] = {
            "usd": sum(known) if known else (None if observations else None),
            "calls_known": len(known),
            "calls_total": len(observations),
            "complete": not incomplete,
        }

    known_cost_usd = None
    if observations:
        knowns = [c["usd"] for c in cost_breakdown.values() if c["usd"] is not None]
        known_cost_usd = sum(knowns) if knowns else None
    total_cost_usd = known_cost_usd if (observations and not unknown_components) else None

    revenue_usd = _number(revenue_usd) if revenue_usd is not None else None
    if revenue_usd is None:
        cash_contribution_usd, cash_contribution_status = None, "unknown_revenue"
    elif unknown_components or known_cost_usd is None:
        cash_contribution_usd, cash_contribution_status = None, "incomplete_unknown_components"
    else:
        cash_contribution_usd, cash_contribution_status = revenue_usd - known_cost_usd, "complete"

    alerts = {
        "runaway_calls": runaway_calls,
        "runaway_minutes_threshold": runaway_threshold,
        "margin": _margin_alert(revenue_usd, known_cost_usd, fixed_cash_usd, bool(unknown_components) or not observations),
        "note": "Operator reporting only: never gates a call and never changes a customer cap or price.",
    }
    return {
        "tenant": tenant,
        "alerts": alerts,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "call_count": len(observations),
        "minutes": minutes,
        "cost_breakdown": cost_breakdown,
        "unknown_components": unknown_components,
        "known_cost_usd": known_cost_usd,
        "total_cost_usd": total_cost_usd,
        "revenue_usd": revenue_usd,
        "cash_contribution_usd": cash_contribution_usd,
        "cash_contribution_status": cash_contribution_status,
    }


def observe_call(row, rates=None, evidence=None):
    """Represent a call without inferring carrier billing from app timestamps."""
    quantities = {m: {"value": None, "status": "unknown", "source": "not_recorded"} for m in METRICS}
    elapsed = _elapsed(row)
    if elapsed is not None:
        quantities["carrier_seconds"] = {"value": elapsed, "status": "projected", "source": "app_elapsed_not_carrier"}
    for metric in ("input_tokens", "output_tokens", "tts_chars"):
        value = row.get(metric)
        if value is not None and Decimal(str(value)) > 0:
            quantities[metric] = {"value": Decimal(str(value)), "status": "projected", "source": "legacy_partial_coverage"}
    quantities.update(_evidence(evidence))
    tariffs = _rates(rates)
    components = {}
    for metric in COST_METRICS:
        quantity, tariff = quantities[metric], tariffs.get(metric)
        if metric == "stt_seconds" and quantity["source"] == "not_recorded":
            # No streaming-STT evidence means this call used Gather (counted by gather_count), not that the cost is unknown.
            components[metric] = {"usd": Decimal(0), "status": "not_applicable", "rate_id": None, "source_url": None, "checked_at": None}
            continue
        component = {"usd": None, "status": "unknown", "rate_id": tariff["rate_id"] if tariff else None,
                     "source_url": tariff["source_url"] if tariff else None,
                     "checked_at": tariff["checked_at"] if tariff else None}
        if quantity["value"] is not None and tariff:
            values = quantity["value"] if isinstance(quantity["value"], list) else [quantity["value"]]
            increment = tariff["billing_increment"]
            billed = sum((v / increment).to_integral_value(rounding=ROUND_CEILING) * increment for v in values)
            component.update(usd=billed * tariff["usd_per_unit"] / tariff["unit_quantity"],
                             status="estimated_from_" + quantity["status"])
        components[metric] = component
    costs = [c["usd"] for c in components.values()]
    return {"call_sid": row.get("call_sid"), "tenant": row.get("client_id"), "quantities": quantities,
            "components": components, "complete_cost_usd": sum(costs) if all(v is not None for v in costs) else None}
