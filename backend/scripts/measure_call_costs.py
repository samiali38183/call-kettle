"""PRIVATE developer tool: measure the real per-call usage of the AI receptionist with real Claude Haiku calls.

    cd backend
    python scripts/measure_call_costs.py --i-understand-this-spends-api-credits

SPENDS REAL ANTHROPIC CREDITS (expected well under $1; hard stop at $2.00 total, enforced between turns).
The app runs IN-PROCESS (FastAPI TestClient) against a TEMPORARY database, so production data is untouched; the Twilio
signature check is skipped only inside this process. Only ANTHROPIC_API_KEY is read from backend/.env (Twilio and mail
credentials are never loaded; push/SMS/email stay off). It never places a call or contacts anyone.

Per call: a scripted AI "caller" persona (separate Haiku calls, counted separately) talks to the app's normal agent path
through the real /voice/incoming, /voice/gather and /voice/status webhooks against the shipped HVAC demo config
(clients/demo_hvac.yaml). Tokens, <Say> characters and Gathers come from the ledger the app already writes
(app/cost_observability.py). There is NO phone audio, so carrier seconds are ASSUMED (see CALL_SETUP_SECONDS and
SECONDS_PER_TURN), and the cost report treats them as the Twilio status callback value we supply.

Output: a per-call and aggregate table, the cost report (scripts/cost_report.py --rates example) on the temp DB, and the raw
results in docs/measured_call_costs_2026-10-04.json (no secrets, no transcripts, no real phone numbers).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
OUT_JSON = REPO / "docs" / "measured_call_costs_2026-10-04.json"
CLIENT_ID = "demo_hvac"
FLAG = "--i-understand-this-spends-api-credits"
SIM_MODEL = "claude-haiku-4-5-20251001"
HARD_CAP_USD = Decimal("2.00")

# ASSUMPTION (no real phone audio exists here): carrier seconds = CALL_SETUP_SECONDS (ring/connect + greeting) +
# caller turns x SECONDS_PER_TURN (about 7 s of AI speech + 5 s of caller speech + 3 s of recognition/model latency).
CALL_SETUP_SECONDS = 8
SECONDS_PER_TURN = 15

# Haiku 4.5 list price per million tokens (same figures as app/cost_observability.EXAMPLE_RATES).
IN_PER_MTOK, OUT_PER_MTOK = Decimal("1.00"), Decimal("5.00")
CACHE_READ_PER_MTOK, CACHE_WRITE_PER_MTOK = Decimal("0.10"), Decimal("1.25")

PERSONAS = [
    {"name": "ac_repair_booking", "from_number": "+15555550100", "turns": 12,
     "behaviour": "Your AC stopped cooling this morning. Cooperative: book a repair visit at the first time offered, give the name 'Dana Ortiz', confirm the number you are calling from, then say thanks and goodbye."},
    {"name": "no_heat_emergency", "from_number": "+15555550100", "turns": 12,
     "behaviour": "Your furnace is not heating the house, it is freezing and you have a baby at home. You are stressed and want someone right away. Answer what is asked, give the name 'Marcus Lee', accept whatever urgent help is offered, then say thanks and goodbye."},
    {"name": "gas_smell_emergency", "from_number": "+15555550100", "turns": 10,
     "behaviour": "You smell gas near your furnace in the basement. You are scared. Say so right away, answer questions briefly, follow instructions, then say goodbye."},
    {"name": "price_shopper", "from_number": "+15555550100", "turns": 10,
     "behaviour": "You only want to know how much a new AC unit and a repair visit cost and you are comparing three companies. Press for a price at least twice. If told a technician must quote it, ask about a free estimate, then say you will think about it and hang up."},
    {"name": "rambling_changes_mind", "from_number": "+15555550100", "turns": 12,
     "behaviour": "You ramble: mention your neighbour's unit, the heat wave and your old thermostat before getting to the point. You want a maintenance tune-up, then change your mind and ask for a repair visit instead, then change the day once. Give the name 'Pat Nguyen'. Finish by confirming whatever is booked and saying goodbye."},
    {"name": "reschedule", "from_number": "+15555550100", "turns": 14,
     "behaviour": "First book a tune-up at the first time offered (name 'Jordan Kim', confirm the number you are calling from). After it is confirmed say 'actually, can we move it to a different day?' and accept the first different time offered. Then say thanks and goodbye."},
    {"name": "cancel", "from_number": "+15555550100", "turns": 14,
     "behaviour": "First book a repair visit at the first time offered (name 'Sam Patel', confirm the number you are calling from). After it is confirmed say 'sorry, please cancel that appointment', confirm yes if asked, then say thanks and goodbye."},
    {"name": "hours_question", "from_number": "+15555550100", "turns": 6,
     "behaviour": "You only want to know what hours the company is open, and whether it is open on Sundays. After the answer, say thanks and hang up. Do not book anything."},
    {"name": "spanish_caller", "from_number": "+15555550100", "turns": 8,
     "behaviour": "Speak ONLY Spanish. Your aire acondicionado no enfría and you want a technician to come. Keep speaking Spanish whatever the receptionist says, then say gracias y adios."},
    {"name": "wants_a_person", "from_number": "+15555550100", "turns": 8,
     "behaviour": "You do not want to talk to an AI. Say you want a real person or the owner, repeat it if refused, and say it is about an unpaid invoice dispute. Accept a callback if offered, then say goodbye."},
    {"name": "wrong_number", "from_number": "+15555550100", "turns": 6,
     "behaviour": "You meant to call a pizza restaurant. You are confused. Once you realise it is the wrong number, apologise and hang up."},
    {"name": "long_chatty", "from_number": "+15555550100", "turns": 16,
     "behaviour": "You are very chatty and slow to the point. Ask several separate questions one at a time (service area, emergency service, how soon someone can come, whether the tech wears shoe covers, what brands they service) before finally booking a routine maintenance visit at the first time offered (name 'Chris Walker', confirm the number you are calling from). Do NOT agree to book until you have asked at least five questions, so the call lasts 12 or more exchanges before you finally book, then say goodbye."},
]


# ---------------------------------------------------------------- pure helpers (unit-tested offline)
class SpendCapExceeded(RuntimeError):
    pass


class SpendTracker:
    """Running Anthropic spend ESTIMATE (list price x reported usage). exceeded only when strictly over the cap."""

    def __init__(self, cap_usd=HARD_CAP_USD):
        self.cap_usd = min(Decimal(str(cap_usd)), HARD_CAP_USD)
        self.input_tokens = self.output_tokens = self.cache_read = self.cache_write = 0
        self._lock = threading.Lock()

    def add(self, input_tokens, output_tokens, cache_read=0, cache_write=0):
        with self._lock:
            self.input_tokens += input_tokens or 0
            self.output_tokens += output_tokens or 0
            self.cache_read += cache_read or 0
            self.cache_write += cache_write or 0

    @property
    def estimate_usd(self):
        return (Decimal(self.input_tokens) * IN_PER_MTOK + Decimal(self.output_tokens) * OUT_PER_MTOK
                + Decimal(self.cache_read) * CACHE_READ_PER_MTOK + Decimal(self.cache_write) * CACHE_WRITE_PER_MTOK) / Decimal(1_000_000)

    @property
    def exceeded(self):
        return self.estimate_usd > self.cap_usd

    def check(self):
        if self.exceeded:
            raise SpendCapExceeded(f"estimated Anthropic spend ${self.estimate_usd:.4f} is over the ${self.cap_usd} cap")


def percentile(values, pct):
    """Nearest-rank percentile (same rule as app.cost_observability._percentile); None for no data."""
    data = sorted(values)
    if not data:
        return None
    idx = int(math.ceil(float(pct) / 100.0 * (len(data) - 1)))
    return data[min(max(idx, 0), len(data) - 1)]


def aggregate(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return {"n": 0, "total": None, "avg": None, "p90": None, "min": None, "max": None}
    total = sum(vals)
    return {"n": len(vals), "total": total, "avg": total / len(vals), "p90": percentile(vals, 90), "min": min(vals), "max": max(vals)}


def assumed_carrier_seconds(turns):
    return CALL_SETUP_SECONDS + max(int(turns), 0) * SECONDS_PER_TURN


def project_monthly(per_call_usd, calls):
    return Decimal(str(per_call_usd)) * Decimal(calls)


def max_token_cost_per_call(*, revenue, margin, other_cost, calls):
    """Largest token cost per call that still reaches `margin`, given all other monthly cash cost. Negative = unreachable."""
    budget = Decimal(str(revenue)) * (Decimal(1) - Decimal(str(margin)))
    return (budget - Decimal(str(other_cost))) / Decimal(calls)


def token_cost(input_tokens, output_tokens, cache_read=0, cache_write=0):
    t = SpendTracker()
    t.add(input_tokens, output_tokens, cache_read, cache_write)
    return t.estimate_usd


# ---------------------------------------------------------------- run (needs network and the key)
def _spoken(twiml):
    root = ElementTree.fromstring(twiml)
    text = " ".join((e.text or "") for e in root.iter("Say")).strip()
    kind = "dial" if root.find(".//Dial") is not None else "hangup" if root.find(".//Hangup") is not None else "gather"
    return text, kind


def _load_cash_margin():
    import importlib.util
    spec = importlib.util.spec_from_file_location("cash_margin_measure", REPO / "marketing" / "cash_margin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def modeled_per_call(calls=300):
    cm = _load_cash_margin()
    u = cm.usage_cost(cm.current_stack_architecture(), cm.matrix_workload(), calls, "3")
    d = Decimal
    n = d(calls)
    r_in, r_out = d(u["input_tokens"]) / n, d(u["output_tokens"]) / n
    tts, rec, mins = d(u["tts_chars"]) / n, d(u["recognitions"]) / n, d(u["billed_ai_minutes"]) / n
    tok = token_cost(r_in, r_out)
    total = d(u["usage_cost"]) / n
    return {"input_tokens": r_in, "output_tokens": r_out, "tts_chars": tts, "gathers": rec, "carrier_minutes": mins,
            "token_cost": tok, "tts_cost": tts / 100 * d("0.0032"), "gather_cost": rec * d("0.02"), "usage_cost_total": total,
            "revenue": cm.PRIVATE_REVENUE, "fixed_cash_ex_usage": cm.FIXED_RECURRING_CASH_EX_USAGE, "basis": "marketing/cash_margin.py matrix, 3-minute call"}


def run(cap_usd, only=None):
    from dotenv import dotenv_values

    key = dotenv_values(ROOT / ".env").get("ANTHROPIC_API_KEY")
    if not key:
        print("ANTHROPIC_API_KEY missing from backend/.env", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="ck_measure_"))
    for var in ("REPORT_KEY", "SMS_ENABLED", "CALLKETTLE_CLIENTS_DIR", "TWILIO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID", "SMTP_HOST", "RESEND_API_KEY"):
        os.environ.pop(var, None)
    os.environ.update(CALLKETTLE_DB_PATH=str(tmp / "measure.db"), CALLKETTLE_SKIP_SIGNATURE_CHECK="1", CALLKETTLE_DISABLE_PUSH="1",
                      ANTHROPIC_API_KEY=key)
    sys.path.insert(0, str(ROOT))
    import anthropic
    from fastapi.testclient import TestClient

    import anthropic.resources.messages  # noqa: F401
    from app import cost_observability as co, main, storage

    import logging
    logging.disable(logging.WARNING)                              # the app and httpx log every request; keep the console readable
    total, sim = SpendTracker(cap_usd), SpendTracker(cap_usd)
    orig_create = anthropic.resources.messages.Messages.create

    def counting_create(self, *a, **kw):                       # tallies EVERY model call (app, summary, simulator) for the cap
        resp = orig_create(self, *a, **kw)
        u = getattr(resp, "usage", None)
        if u is not None:
            total.add(getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0),
                      getattr(u, "cache_read_input_tokens", 0) or 0, getattr(u, "cache_creation_input_tokens", 0) or 0)
        return resp

    anthropic.resources.messages.Messages.create = counting_create
    sim_client = anthropic.Anthropic(api_key=key, timeout=30)
    storage.init_db()
    results, stopped = [], None
    try:
        with TestClient(main.app) as http:
            for p in [x for x in PERSONAS if not only or x['name'] in only.split(',')]:
                try:
                    total.check()
                except SpendCapExceeded as exc:
                    stopped = f"stopped before {p['name']}: {exc}"
                    break
                results.append(_hold_call(http, sim_client, sim, total, p, co, storage))
                print(f"  {p['name']:24s} turns={results[-1]['turns']:2d} in={results[-1]['input_tokens']:6d} out={results[-1]['output_tokens']:4d} "
                      f"outcome={results[-1]['outcome']}  spend so far ${total.estimate_usd:.4f}", flush=True)
                if total.exceeded:
                    stopped = f"stopped after {p['name']}: estimated spend ${total.estimate_usd:.4f} is over the ${total.cap_usd} cap"
                    break
            time.sleep(1)
            for w in threading.enumerate():                      # let notification workers finish before the DB is read
                if getattr(getattr(w, "_target", None), "__module__", "") == "app.notify":
                    w.join(timeout=10)
    finally:
        anthropic.resources.messages.Messages.create = orig_create
    report = subprocess.run([sys.executable, str(ROOT / "scripts" / "cost_report.py"), CLIENT_ID, "--db", os.environ["CALLKETTLE_DB_PATH"], "--rates", "example"],
                            capture_output=True, text=True, cwd=str(ROOT), env={k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"})
    cost_report_text = report.stdout
    summary = summarize(results, total, sim, stopped, cost_report_text)
    if not only:
        OUT_JSON.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print_summary(summary)
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if not stopped else 3


def _caller_says(client, sim, persona, transcript):
    system = ("You are role-playing a customer phoning a local heating and air conditioning company. You are a real person, never mention being an AI. "
              "Reply with ONLY what you would say out loud: one or two short natural sentences (a few more if you ramble). "
              f"How you behave: {persona['behaviour']}")
    msgs = [{"role": "user", "content": f"(The call connected.) Receptionist: {transcript[0][1]}"}]
    for who, text in transcript[1:]:
        msgs.append({"role": "assistant" if who == "caller" else "user", "content": text})
    if msgs[-1]["role"] == "assistant":
        msgs.append({"role": "user", "content": "(silence)"})
    out = client.messages.create(model=SIM_MODEL, max_tokens=120, system=system, messages=msgs)
    sim.add(out.usage.input_tokens, out.usage.output_tokens)
    return "".join(b.text for b in out.content if b.type == "text").strip().strip('"')


def _hold_call(http, sim_client, sim, total, persona, co, storage):
    sid = f"CA_MEASURE_{uuid.uuid4().hex[:10]}"
    frm = persona["from_number"]
    r = http.post(f"/voice/incoming?client_id={CLIENT_ID}", data={"CallSid": sid, "From": frm})
    r.raise_for_status()
    text, kind = _spoken(r.text)
    transcript, caller_turns, last_kind = [("ai", text)], 0, kind
    while kind == "gather" and caller_turns < persona["turns"] and not total.exceeded:
        said = _caller_says(sim_client, sim, persona, transcript)
        transcript.append(("caller", said))
        caller_turns += 1
        r = http.post(f"/voice/gather?client_id={CLIENT_ID}&retry=0", data={"CallSid": sid, "From": frm, "SpeechResult": said})
        r.raise_for_status()
        text, kind = _spoken(r.text)
        last_kind = kind
        transcript.append(("ai", text))
    carrier = assumed_carrier_seconds(caller_turns)
    http.post("/voice/status", data={"CallSid": sid, "CallStatus": "completed", "CallDuration": str(carrier)})
    row = storage.get_call(sid) or {}
    evidence = co.read_evidence(storage.DB_PATH, CLIENT_ID, sid)
    q = lambda m: evidence.get(m, {}).get("value")   # noqa: E731
    ledger_in, ledger_out = int(q("input_tokens") or 0), int(q("output_tokens") or 0)
    import sqlite3                                  # storage.get_call() does not select the usage columns
    with sqlite3.connect(storage.DB_PATH) as conn:
        u = conn.execute("SELECT input_tokens, output_tokens, model_calls FROM calls WHERE call_sid = ?", (sid,)).fetchone() or (0, 0, 0)
    row_in, row_out, model_calls = int(u[0] or 0), int(u[1] or 0), int(u[2] or 0)
    return {
        "persona": persona["name"], "call_sid": sid, "caller_turns": caller_turns, "turns": caller_turns, "row_turn_count": int(row.get("turn_count") or 0),
        "ended_with": last_kind, "outcome": row.get("outcome"), "summary_written": bool(row.get("summary")), "model_calls": model_calls, "row_input_tokens": row_in, "ledger_input_tokens": ledger_in,
        "input_tokens_turns": ledger_in, "output_tokens_turns": ledger_out,                    # the live-call agent path (ledger)
        "input_tokens": max(row_in, ledger_in), "output_tokens": max(row_out, ledger_out),     # plus the post-call summary (calls table)
        "summary_input_tokens": max(row_in - ledger_in, 0), "summary_output_tokens": max(row_out - ledger_out, 0),
        "cache_read_tokens": int(q("cache_read_tokens") or 0), "cache_write_tokens": int(q("cache_write_tokens") or 0),
        "tts_chars": int(q("tts_chars") or 0), "gather_count": int(q("gather_count") or 0),
        "carrier_seconds_ASSUMED": carrier,
    }


def _components(r, rates):
    """Per-call dollar components at the example rates. carrier is ASSUMED-duration based; everything else measured."""
    d = Decimal
    token = token_cost(r["input_tokens"], r["output_tokens"], r["cache_read_tokens"], r["cache_write_tokens"])
    tts = d(r["tts_chars"]) / d(rates["tts_chars"]["unit_quantity"]) * d(rates["tts_chars"]["usd_per_unit"])
    gather = d(r["gather_count"]) * d(rates["gather_count"]["usd_per_unit"])
    inc = d(rates["carrier_seconds"]["billing_increment"])
    billed = (d(r["carrier_seconds_ASSUMED"]) / inc).to_integral_value(rounding=ROUND_CEILING) * inc
    carrier = billed * d(rates["carrier_seconds"]["usd_per_unit"]) / d(rates["carrier_seconds"]["unit_quantity"])
    return {"token_usd": token, "tts_usd": tts, "gather_usd": gather, "carrier_usd_ASSUMED": carrier,
            "usage_usd": token + tts + gather + carrier, "usage_ex_carrier_usd": token + tts + gather}


def summarize(results, total, sim, stopped, cost_report_text):
    from app import cost_observability as co

    rates = {k: {kk: vv for kk, vv in v.items()} for k, v in co.EXAMPLE_RATES.items()}
    for r in results:
        r["cost"] = _components(r, rates)
        r["cost_total_for_agg"] = r["cost"]["usage_usd"]
    flat = []
    for r in results:
        f = {k: v for k, v in r.items() if isinstance(v, (int, Decimal))}
        f.update({k: v for k, v in r["cost"].items()})
        flat.append(f)
    keys = ["input_tokens", "output_tokens", "tts_chars", "gather_count", "turns", "carrier_seconds_ASSUMED", "summary_input_tokens", "token_usd",
            "tts_usd", "gather_usd", "carrier_usd_ASSUMED", "usage_usd", "usage_ex_carrier_usd"]
    agg = {k: aggregate(flat, k) for k in keys}
    modeled = modeled_per_call(300)
    mr = modeled["revenue"]
    fixed = modeled["fixed_cash_ex_usage"]
    avg = {k: agg[k]["avg"] for k in keys if agg[k]["avg"] is not None}
    scen = {}
    for calls in (300, 450):
        n = Decimal(calls)
        modeled_usage = modeled["usage_cost_total"] * n
        non_token_modeled = modeled_usage - modeled["token_cost"] * n
        meas_tokens = avg["token_usd"] * n
        views = {
            "A_modeled_usage": modeled_usage,
            "B_measured_tokens_only": non_token_modeled + meas_tokens,
            "C_measured_tokens_tts_gathers": modeled_usage - (modeled["token_cost"] + modeled["tts_cost"] + modeled["gather_cost"]) * n
            + (avg["token_usd"] + avg["tts_usd"] + avg["gather_usd"]) * n,
            "D_all_measured_plus_ASSUMED_carrier_no_transfer_legs": avg["usage_usd"] * n,
        }
        scen[str(calls)] = {
            "calls_per_month": calls,
            "measured_token_cost_month": meas_tokens, "measured_token_cost_p90_month": agg["token_usd"]["p90"] * n,
            "measured_tts_cost_month": avg["tts_usd"] * n, "measured_gather_cost_month": avg["gather_usd"] * n,
            "ASSUMED_carrier_cost_month": avg["carrier_usd_ASSUMED"] * n,
            "usage_cost_views": views,
            "margin_views": {k: (mr - (fixed + v)) / mr for k, v in views.items()},
            "max_token_cost_per_call_for_80pct_if_other_costs_as_modeled": max_token_cost_per_call(
                revenue=mr, margin="0.8", other_cost=fixed + non_token_modeled, calls=calls),
            "margin_points_per_extra_cent_per_call": Decimal("0.01") * n / mr * 100,
        }
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "client_config": "clients/demo_hvac.yaml (shipped)",
        "method": "in-process TestClient, temp DB, real Anthropic via the app's agent path, AI caller persona counted separately",
        "assumptions": {"CALL_SETUP_SECONDS": CALL_SETUP_SECONDS, "SECONDS_PER_TURN": SECONDS_PER_TURN,
                        "carrier": "ASSUMED: no real phone audio. Everything else (tokens, tts_chars, gather_count, turns) is measured.",
                        "rates": "app/cost_observability.EXAMPLE_RATES (Haiku 4.5 $1/$5 per MTok, Polly $0.0032/100 chars, Gather $0.02, carrier $0.0085/min billed per whole minute)"},
        "spend": {"anthropic_total_estimate_usd": total.estimate_usd, "caller_simulator_estimate_usd": sim.estimate_usd,
                  "app_and_summary_estimate_usd": total.estimate_usd - sim.estimate_usd, "cap_usd": total.cap_usd,
                  "total_tokens": {"input": total.input_tokens, "output": total.output_tokens},
                  "simulator_tokens": {"input": sim.input_tokens, "output": sim.output_tokens}, "stopped_early": stopped},
        "calls": results, "aggregate": agg, "modeled_per_call": modeled, "scenarios": scen, "cost_report_text": cost_report_text,
    }


def _f(v, places=4):
    return "n/a" if v is None else f"{Decimal(v):.{places}f}"


def print_summary(s):
    print("\n=== per call (carrier seconds ASSUMED; everything else measured) ===")
    print(f"{'persona':24s} {'turns':>5s} {'in_tok':>7s} {'out_tok':>7s} {'tts':>5s} {'gath':>4s} {'tokens$':>8s} {'tts$':>7s} {'gather$':>8s} {'carrier$*':>9s} {'usage$':>8s}")
    for r in s["calls"]:
        c = r["cost"]
        print(f"{r['persona']:24s} {r['turns']:5d} {r['input_tokens']:7d} {r['output_tokens']:7d} {r['tts_chars']:5d} {r['gather_count']:4d} "
              f"{_f(c['token_usd'], 5):>8s} {_f(c['tts_usd'], 5):>7s} {_f(c['gather_usd']):>8s} {_f(c['carrier_usd_ASSUMED'], 5):>9s} {_f(c['usage_usd']):>8s}")
    print("\n=== aggregate (avg / p90 / max per call) ===")
    for k, a in s["aggregate"].items():
        print(f"  {k:26s} avg={_f(a['avg'], 5):>10s}  p90={_f(a['p90'], 5):>10s}  max={_f(a['max'], 5):>10s}")
    m = s["modeled_per_call"]
    print(f"\nmodeled per call ({m['basis']}): in={m['input_tokens']:.0f} out={m['output_tokens']:.0f} tts={m['tts_chars']:.0f} gathers={m['gathers']:.1f} "
          f"token$={m['token_cost']:.5f} usage$={m['usage_cost_total']:.4f}")
    for n, sc in s["scenarios"].items():
        print(f"\n{n} calls/month: measured token cost ${_f(sc['measured_token_cost_month'], 2)} (p90 ${_f(sc['measured_token_cost_p90_month'], 2)}), "
              f"tts ${_f(sc['measured_tts_cost_month'], 2)}, gathers ${_f(sc['measured_gather_cost_month'], 2)}, carrier ${_f(sc['ASSUMED_carrier_cost_month'], 2)} (ASSUMED)")
        for k, v in sc["usage_cost_views"].items():
            print(f"   {k:56s} usage ${_f(v, 2):>8s}  margin {sc['margin_views'][k] * 100:6.2f}%")
        print(f"   max token $/call for 80% (other costs as modeled): {_f(sc['max_token_cost_per_call_for_80pct_if_other_costs_as_modeled'], 5)}")
    sp = s["spend"]
    print(f"\nAnthropic spend ESTIMATE: total ${_f(sp['anthropic_total_estimate_usd'])} (caller simulator ${_f(sp['caller_simulator_estimate_usd'])}, app+summary ${_f(sp['app_and_summary_estimate_usd'])}), cap ${sp['cap_usd']}"
          + (f"  STOPPED EARLY: {sp['stopped_early']}" if sp["stopped_early"] else ""))
    print("\n" + s["cost_report_text"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(FLAG, dest="ok", action="store_true", help="required: this script spends real Anthropic credits")
    ap.add_argument("--only", default=None, help="comma-separated persona names (a quick, cheaper subset; does not write the results JSON)")
    ap.add_argument("--cap-usd", default="2.00", help="stop when the spend estimate exceeds this (never more than 2.00)")
    args = ap.parse_args(argv)
    if not args.ok:
        print(f"Refusing to run: this spends real Anthropic API credits. Re-run with {FLAG}.", file=sys.stderr)
        return 2
    return run(Decimal(args.cap_usd), args.only)


if __name__ == "__main__":
    sys.exit(main())
