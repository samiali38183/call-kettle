#!/usr/bin/env python
"""Reconcile what the app measured against Twilio's own Usage Records for one calendar month (PRIVATE, read-only).

  python backend/scripts/reconcile_costs.py --month 2026-10 --db path/to/callkettle.db
  python backend/scripts/reconcile_costs.py --month 2026-10 --app-json app_side.json     # app side collected elsewhere (e.g. production)

TWILIO IS AUTHORITATIVE for billing. The app side is a cross-check: a category the app under- or over-counts by more
than 10 percent is flagged only for compatible, complete evidence. UNKNOWN / NON_COMPARABLE
are printed for missing/partial evidence or scope/unit mismatches. This is not invoice certification. The Twilio call is
a read of usage records only (credentials from backend/.env or the environment, never printed). The database is opened
read-only. Account-wide: Twilio's records cover the whole account, so the app side sums every tenant unless --tenant is given.

Comparisons (caveats are printed with the rows):
  carrier_minutes  app: sum over calls of ceil(carrier_seconds / 60), Twilio-measured seconds  vs  calls-inbound usage (minutes)
  gather_count     app: Gathers the server emitted                                              vs  speech-recognition count
  tts_chars        app: characters sent to <Say>                                                vs  amazon-polly usage
  sms_count        app: no SMS counter exists yet (UNKNOWN)                                     vs  sms count (inbound + outbound)
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

THRESHOLD = Decimal("0.10")
MEASURED = ("carrier_seconds", "gather_count", "tts_chars")

# category -> (twilio record category, twilio field, caveat)
MAPPING = {
    "carrier_minutes": ("calls-inbound", "usage", "app minutes round each call up to a whole minute, as Twilio bills; transfer legs are outside calls-inbound"),
    "gather_count": ("speech-recognition", "count", "Twilio's usage field is 15-second intervals; the count of recognitions is compared"),
    "tts_chars": ("amazon-polly", "usage", "Twilio counts only Polly-voiced <Say>; the app counts all <Say> text, and Twilio's unit for this record is 'use'"),
    "sms_count": ("sms", "count", "inbound plus outbound messages; the app keeps no SMS counter"),
}


def _dec(v):
    try:
        value = Decimal(str(v)) if v not in (None, "") else None
        return value if value is not None and value.is_finite() and value >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def month_range(month: str):
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    last = calendar.monthrange(start.year, start.month)[1]
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end, date(start.year, start.month, last)


def collect_app(db_path, month: str, tenant: str | None = None) -> dict:
    start, end, _ = month_range(month)
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as conn:
        conn.execute("PRAGMA query_only=ON")
        sql = "SELECT client_id, call_sid FROM calls WHERE started_at >= ? AND started_at < ?"
        args = [start.isoformat(), end.isoformat()]
        if tenant:
            sql += " AND client_id = ?"
            args.append(tenant)
        calls = conn.execute(sql, args).fetchall()
        has_ledger = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='private_cost_usage'").fetchone()
        values = {m: [] for m in MEASURED}
        for client_id, call_sid in calls:
            if not has_ledger:
                break
            for metric, payload in conn.execute("SELECT metric, payload FROM private_cost_usage WHERE client_id=? AND call_sid=?", (client_id, call_sid)):
                if metric in values:
                    p = json.loads(payload)
                    if p.get("status") == "measured" and p.get("value") is not None:
                        value = _dec(p["value"])
                        if value is not None:
                            values[metric].append(value)
    n = len(calls)

    def out(vals, fn=lambda v: v):
        if n == 0:
            return {"value": Decimal(0), "calls_measured": 0}          # no calls this month: nothing could have been used
        if not vals:
            return {"value": None, "calls_measured": 0}
        return {"value": sum(fn(v) for v in vals), "calls_measured": len(vals)}

    return {"scope": f"tenant:{tenant}" if tenant else "account", "calls": n, "carrier_minutes": out(values["carrier_seconds"], lambda s: Decimal(math.ceil(s / 60))),
            "gather_count": out(values["gather_count"]), "tts_chars": out(values["tts_chars"]),
            "sms_count": {"value": None, "calls_measured": 0}}


def collect_twilio(client, month: str) -> dict:
    start, _, last = month_range(month)
    out = {}
    for r in client.usage.records.list(start_date=start.date(), end_date=last):
        out[r.category] = {"count": _dec(getattr(r, "count", None)), "usage": _dec(getattr(r, "usage", None)), "unit": getattr(r, "usage_unit", "") or ""}
    return out


def reconcile(app: dict, twilio: dict, threshold: Decimal = THRESHOLD) -> list[dict]:
    rows = []
    for name, (category, field, caveat) in MAPPING.items():
        a = (app.get(name) or {})
        a_val = None if a.get("value") is None else _dec(a["value"])
        record = twilio.get(category)
        t_val = None if record is None else _dec(record.get(field))
        row = {"category": name, "twilio_category": category, "app": a_val, "twilio": t_val, "delta": None, "pct": None,
               "status": "UNKNOWN", "note": caveat}
        if a_val is None and name == "sms_count":
            row["note"] = caveat
        elif a_val is None:
            row["note"] = "no call measured this; " + caveat
        if record is None:
            row["note"] = f"Twilio returned no '{category}' record; " + row["note"]
        measured, total = a.get("calls_measured"), app.get("calls")
        scope = app.get("scope")
        coverage = (type(total) is int and type(measured) is int and total >= 0 and measured == total)
        comparable = a_val is not None and t_val is not None
        if scope is not None and scope != "account":
            row["status"] = "NON_COMPARABLE"
            row["note"] = "tenant/non-account app scope vs account-wide Twilio; " + row["note"]
            comparable = False
        elif scope is None or not coverage:
            row["note"] = (f"app measured on {measured} of {total} calls (missing/partial coverage or scope evidence); " + row["note"])
            comparable = False
        elif comparable and name == "tts_chars" and (record.get("unit", "").strip().lower() not in {"character", "characters"} or app.get("polly_only") is not True):
            row["status"] = "NON_COMPARABLE"
            row["note"] = "Polly character units and Polly-only app coverage not established; " + row["note"]
            comparable = False
        if comparable:
            delta = a_val - t_val
            row["delta"] = delta
            if t_val == 0:
                row["status"] = "OK" if a_val == 0 else "OVER"
            else:
                row["pct"] = (delta / t_val * 100).quantize(Decimal("0.1"))
                row["status"] = "UNDER" if delta / t_val < -threshold else "OVER" if delta / t_val > threshold else "OK"
        rows.append(row)
    return rows


def _f(v):
    return "UNKNOWN" if v is None else format(v.normalize() if v else Decimal(0), "f")


def render(rows: list[dict], month: str) -> str:
    lines = [f"Reconciliation for {month} (GMT): app measurement vs Twilio Usage Records",
             "Twilio is AUTHORITATIVE for billing; this exploratory usage cross-check is not invoice certification. Only compatible complete evidence may flag more than 10% under/over.",
             "delta = app - Twilio; UNKNOWN = missing/partial evidence; NON_COMPARABLE = scope/unit mismatch (nothing assumed).",
             f"{'category':16}{'app':>10}{'Twilio':>10}{'delta':>10}{'pct':>9}  status"]
    for r in rows:
        pct = "n/a" if r["pct"] is None else f"{r['pct']}%"
        lines.append(f"{r['category']:16}{_f(r['app']):>10}{_f(r['twilio']):>10}{_f(r['delta']):>10}{pct:>9}  {r['status']}")
    lines.append("notes:")
    lines += [f"  {r['category']}: {r['note']}" for r in rows]
    flagged = [r["category"] for r in rows if r["status"] in ("UNDER", "OVER")]
    unknown = [r["category"] for r in rows if r["status"] == "UNKNOWN"]
    non_comparable = [r["category"] for r in rows if r["status"] == "NON_COMPARABLE"]
    lines.append("FLAGGED (>10% off): " + (", ".join(flagged) if flagged else "none"))
    lines.append("UNKNOWN (cannot be reconciled): " + (", ".join(unknown) if unknown else "none"))
    lines.append("NON_COMPARABLE (scope/units): " + (", ".join(non_comparable) if non_comparable else "none"))
    return "\n".join(lines)


def _default_client():
    from twilio_usage_guard import default_client

    return default_client()


def main(argv=None, *, client_factory=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--month", required=True, help="calendar month YYYY-MM (GMT, as Twilio reports)")
    p.add_argument("--db", default=None, help="sqlite database (opened read-only)")
    p.add_argument("--tenant", default=None, help="limit the app side to one client_id (Twilio side is always account-wide)")
    p.add_argument("--app-json", default=None, help="app-side numbers collected elsewhere (same shape collect_app returns, values as strings)")
    a = p.parse_args(argv)
    try:
        month_range(a.month)
    except ValueError:
        print("--month needs YYYY-MM, for example 2026-10")
        return 2
    if a.app_json:
        raw = json.loads(Path(a.app_json).read_text())
        app = raw
        if a.tenant:
            app["scope"] = f"tenant:{a.tenant}"
    elif a.db:
        if not Path(a.db).exists():
            print(f"No database found at {a.db!r}.")
            return 1
        app = collect_app(a.db, a.month, a.tenant)
    else:
        print("give --db or --app-json")
        return 2
    try:
        twilio = collect_twilio((client_factory or _default_client)(), a.month)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - never echo the exception text (it can carry account ids)
        print(f"Could not read Twilio usage records ({type(exc).__name__}). Nothing was changed.")
        return 1
    print(render(reconcile(app, twilio), a.month))
    return 0


if __name__ == "__main__":
    sys.exit(main())
