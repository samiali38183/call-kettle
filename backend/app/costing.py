"""Estimated cost of a client's calls this month, and the spending ceiling that protects the margin.

The estimate is built from what each call actually recorded (model tokens, characters spoken, minutes, caller utterances) and
published provider rates (marketing/economics.py documents the sources and the date they were checked). It is an ESTIMATE:
it ignores Twilio's per-number fees and the cost of a transferred leg (docs/WORST_CASE_CAP.md bounds that gap). Treat it as a smoke alarm, not an invoice.

Levels: at 75% and 90% of the ceiling the operator is warned; at 100% the client is warned too and new calls go to the
degraded mode (`ceiling_mode`): "message" takes a message at no AI cost, "transfer" rings the owner's phone.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone

from app import storage
from app.config import ClientConfig

RATES = {
    "twilio_inbound_per_min": 0.0085,
    "twilio_speech_per_use": 0.02,
    "polly_neural_per_100_chars": 0.0032,
    "claude_haiku_in_per_mtok": 1.00,
    "claude_haiku_out_per_mtok": 5.00,
}
SUMMARY_TOKENS_IN, SUMMARY_TOKENS_OUT = 1200, 120           # the post-call summary is one more model call
DEFAULT_COST_CEILING_USD = 110.0                             # worst-case loss cap (docs/WORST_CASE_CAP.md): was 150, which cannot meet the $100 cap
DEFAULT_CALL_CEILING = 900                                   # second brake: a flood of calls that cost little each
WARN_LEVELS = (75, 90, 100)
# Worst-case loss cap (owner directive 2026-10-04; model in marketing/cash_margin.py, pinned by tests/test_worst_case_cap.py).
DEFAULT_POST_CEILING_CALL_CAP = 60                           # degraded calls per month after a ceiling; later calls are <Reject>ed (unbilled)
MAX_CONCURRENT_CALLS = 8                                     # simultaneous calls per client; the next is <Reject>ed (unbilled)
OPEN_CALL_RESERVE_USD = 0.25                                 # the guard only sees a call in flight as ~1 minute; reserve this much per open call


def call_cost(row: dict) -> float:
    """Estimated dollars for one call row (keys as in the calls table)."""
    started = _parse(row.get("started_at"))
    ended = _parse(row.get("ended_at"))
    seconds = (ended - started).total_seconds() if started and ended and ended >= started else 60
    minutes = max(1, math.ceil(seconds / 60))
    utterances = (int(row.get("turn_count") or 0) + 1) // 2 + 1        # caller turns (plus one silent retry on average)
    model = ((int(row.get("input_tokens") or 0) + SUMMARY_TOKENS_IN) * RATES["claude_haiku_in_per_mtok"]
             + (int(row.get("output_tokens") or 0) + SUMMARY_TOKENS_OUT) * RATES["claude_haiku_out_per_mtok"]) / 1_000_000
    voice = int(row.get("tts_chars") or 0) / 100 * RATES["polly_neural_per_100_chars"]
    return round(minutes * RATES["twilio_inbound_per_min"] + utterances * RATES["twilio_speech_per_use"] + voice + model, 4)


def _parse(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def month_usage(client_id: str, now: datetime | None = None) -> tuple[float, int]:
    """(estimated dollars, calls) since the first of this month (UTC)."""
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    with storage._conn() as conn:
        conn.row_factory = None
        rows = conn.execute(
            "SELECT started_at, ended_at, turn_count, input_tokens, output_tokens, tts_chars FROM calls "
            "WHERE client_id = ? AND started_at >= ?", (client_id, start)).fetchall()
    keys = ("started_at", "ended_at", "turn_count", "input_tokens", "output_tokens", "tts_chars")
    return round(sum(call_cost(dict(zip(keys, r))) for r in rows), 2), len(rows)


def cost_ceiling(config: ClientConfig) -> float:
    return float(config.monthly_cost_ceiling_usd or os.environ.get("MONTHLY_COST_CEILING_USD", DEFAULT_COST_CEILING_USD))


def call_ceiling(config: ClientConfig) -> int:
    return int(config.monthly_call_ceiling or os.environ.get("MONTHLY_CALL_CEILING", DEFAULT_CALL_CEILING))


def post_ceiling_cap(config: ClientConfig) -> int:
    if config.post_ceiling_call_cap is not None:
        return int(config.post_ceiling_call_cap)
    return int(os.environ.get("POST_CEILING_CALL_CAP", DEFAULT_POST_CEILING_CALL_CAP))


def post_ceiling_calls(client_id: str, now: datetime | None = None) -> int:
    """Degraded (over-ceiling) calls handled since the first of this month (UTC)."""
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    with storage._conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM metrics WHERE name = 'over_ceiling_call' AND client_id = ? AND at >= ?",
                           (client_id, start)).fetchone()
    return int(row[0])


def open_calls(config: ClientConfig) -> int:
    try:
        return storage.open_call_count(config.client_id, within_seconds=int(config.max_call_seconds) + 120)
    except Exception:
        return 0


def status(config: ClientConfig, now: datetime | None = None) -> dict:
    """Where this client stands. `level` is the highest warning level reached (0, 75, 90 or 100); `over` means degraded mode."""
    cost, calls = month_usage(config.client_id, now)
    cap, call_cap = cost_ceiling(config), call_ceiling(config)
    pct = int(cost / cap * 100) if cap > 0 else 0
    level = max([lv for lv in WARN_LEVELS if pct >= lv] or [0])
    over_calls = calls >= call_cap
    # Safety margin: a call in flight is only estimated at about a minute, so each one reserves a little more before the
    # ceiling is judged. The warning levels above still read the plain estimate.
    open_now = open_calls(config)
    reserve = round(open_now * OPEN_CALL_RESERVE_USD, 2)
    over_cost = pct >= 100 or (cap > 0 and cost + reserve >= cap)
    reason = "cost" if over_cost else ("calls" if over_calls else None)
    return {"cost": cost, "cost_ceiling": cap, "percent": pct, "level": level, "calls": calls, "call_ceiling": call_cap,
            "over": reason is not None, "reason": reason, "open_calls": open_now, "reserve": reserve}
