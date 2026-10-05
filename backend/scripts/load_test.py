"""Measure how many simultaneous calls the server really handles. In-process, no network, no real model.

    python scripts/load_test.py                       # on this computer
    (on Fly)  PYTHONPATH=/app python /tmp/load_test.py --fly

What it does: runs the real FastAPI app against a throwaway database with the real call flow (incoming -> N speech turns ->
status), N simultaneous calls at a time, with a FAKE model that waits `--model-ms` (default 1300, our measured median) before
answering. So the numbers show OUR server's overhead and its queueing under load (thread pool, SQLite, session store), not
Anthropic's or Twilio's behaviour. It cannot show the real model's rate limits, Twilio's own concurrency limits, or memory
under a real model's longer prompts: say so when quoting results.
Per turn a caller's wait is: server time + the model. "Overhead" below is server time minus the model time.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

os.environ.setdefault("CALLKETTLE_SKIP_SIGNATURE_CHECK", "1")
os.environ.setdefault("CALLKETTLE_DISABLE_PUSH", "1")
os.environ.setdefault("ANTHROPIC_API_KEY", "load-test")
os.environ.setdefault("REPORT_KEY", "load_test_key")
_fd, _DB = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["CALLKETTLE_DB_PATH"] = _DB
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging  # noqa: E402

import httpx  # noqa: E402

logging.disable(logging.INFO)


@dataclass
class _Block:
    text: str = "Okay, what day works best for you?"
    type: str = "text"


@dataclass
class _Usage:
    input_tokens: int = 20000
    output_tokens: int = 60


@dataclass
class _Response:
    content: list = field(default_factory=lambda: [_Block()])
    stop_reason: str = "end_turn"
    usage: _Usage = field(default_factory=_Usage)


class _FakeModel:
    def __init__(self, ms: float):
        self.ms = ms
        self.messages = self

    def create(self, **kwargs):
        time.sleep(self.ms / 1000)           # blocks a worker thread exactly like the real HTTP call does
        return _Response()


def _rss_mb() -> float | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    try:
        import psutil

        return round(psutil.Process().memory_info().rss / 1048576, 1)
    except Exception:
        return None


async def one_call(client: httpx.AsyncClient, n: int, turns: int, stats: dict) -> None:
    sid = f"CA_LOAD_{n}_{time.time_ns()}"
    frm = f"+1703555{n % 10000:04d}"

    async def post(path: str, data: dict, kind: str) -> None:
        t0 = time.perf_counter()
        try:
            r = await client.post(path, data=data, timeout=60)
            ok = r.status_code == 200 and "Application error" not in r.text
        except Exception:
            ok = False
        stats[kind].append((time.perf_counter() - t0) * 1000)
        if not ok:
            stats["errors"] += 1

    await post("/voice/incoming?client_id=demo_hvac", {"CallSid": sid, "From": frm}, "incoming")
    for _ in range(turns):
        await post("/voice/gather?client_id=demo_hvac&retry=0", {"CallSid": sid, "From": frm, "SpeechResult": "I need a repair visit please"}, "gather")
    await post("/voice/status", {"CallSid": sid, "CallStatus": "completed"}, "status")


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0.0


async def run_level(app, concurrency: int, turns: int, model_ms: float, threads: int) -> dict:
    import anyio.to_thread

    anyio.to_thread.current_default_thread_limiter().total_tokens = threads      # the app's lifespan does this in production
    stats = {"incoming": [], "gather": [], "status": [], "errors": 0}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started = time.perf_counter()
        await asyncio.gather(*(one_call(client, i, turns, stats) for i in range(concurrency)))
        wall = time.perf_counter() - started
    g = stats["gather"]
    return {
        "concurrency": concurrency, "turns": len(g), "errors": stats["errors"], "wall_s": round(wall, 1),
        "gather_p50_ms": round(pct(g, 0.5)), "gather_p95_ms": round(pct(g, 0.95)), "gather_max_ms": round(max(g)) if g else 0,
        "overhead_p95_ms": round(pct(g, 0.95) - model_ms), "incoming_p95_ms": round(pct(stats["incoming"], 0.95)),
        "rss_mb": _rss_mb(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="1,5,10,20,40,80")
    ap.add_argument("--turns", type=int, default=6)
    ap.add_argument("--model-ms", type=float, default=1300)
    ap.add_argument("--threads", type=int, default=120, help="worker thread limit (production default 120; anyio's own default is 40)")
    ap.add_argument("--fly", action="store_true", help="label the output as measured on the Fly machine")
    args = ap.parse_args()

    from app import agent, main as main_module, storage

    storage.init_db()
    agent._anthropic_client = lambda: _FakeModel(args.model_ms)
    import app.config as cfg

    cfg.load_client_config("demo_hvac")
    print(f"{'Fly machine' if args.fly else 'Local computer'}; fake model {args.model_ms:.0f} ms; {args.turns} turns per call; {args.threads} worker threads; RSS before: {_rss_mb()} MB")
    print(f"{'calls':>6} {'turns':>6} {'errors':>6} {'wall s':>7} {'turn p50':>9} {'turn p95':>9} {'turn max':>9} {'overhead p95':>13} {'RSS MB':>7}")
    rows = []
    for level in (int(x) for x in args.levels.split(",")):
        r = asyncio.run(run_level(main_module.app, level, args.turns, args.model_ms, args.threads))
        rows.append(r)
        print(f"{r['concurrency']:>6} {r['turns']:>6} {r['errors']:>6} {r['wall_s']:>7} {r['gather_p50_ms']:>9} {r['gather_p95_ms']:>9} {r['gather_max_ms']:>9} {r['overhead_p95_ms']:>13} {str(r['rss_mb']):>7}")
    try:
        os.remove(_DB)
    except OSError:
        pass
    bad = [r for r in rows if r["errors"]]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
