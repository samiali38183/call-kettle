"""Record three cases from the real evaluation harness as a readable example: a legitimate request and two adversarial ones.

    python -m qa.record_example --out ../docs/security_eval

Writes recorded_example.json (structured) and recorded_example.txt (what the terminal shows) into --out. The cases are the same
ones `python -m qa.security_eval` runs: same scripted model, same agent loop, gate, tools and a fresh SQLite database with
synthetic tenants. Nothing here calls a model or a network.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from qa import security_eval as ev

# (case id, label). BN-01 is a legitimate booking; PI-01 is refused by the gate; PI-02 passes the gate and is refused by the tool's own rule.
PICKS = [("BN-01", "A. legitimate request"), ("PI-01", "B. adversarial request, stopped by the gate"), ("PI-02", "C. adversarial request, stopped by the tool's ownership rule")]


def _short(value: object, limit: int = 110) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(", ", ": ")) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[: limit - 3] + "..."


def record(case_id: str, label: str) -> dict:
    case = next(c for c in ev.CASES if c.id == case_id)
    trace: dict = {}
    result = ev.run_case(case, trace)
    name, tool_input, tool_result = trace["dispatches"][-1]
    row = trace["audit"][-1]
    return {
        "label": label, "case": case_id, "policy_overrides": case.policy, "caller_says": case.speech, "model_proposes": {"tool": name, "arguments": tool_input},
        "gate": {"decision": row["decision"], "control": row["control"]}, "tool_result": tool_result, "audit_row": row,
        "model_reply": trace["replies"][-1], "bookings_before": trace["bookings_before"], "bookings_after": trace["bookings_after"],
        "expected": [result["expected_decision"], result["expected_control"]], "actual": [result["actual_decision"], result["actual_control"]],
        "state_check": result["invariant"], "state_check_held": result["invariant_ok"], "passed": result["passed"],
    }


def _changed(before: list[dict], after: list[dict]) -> str:
    b = {r["id"]: r for r in before}
    diffs = [f"id {r['id']} {r['client_id']} {r['slot_start']} {r['status']} (new)" for r in after if r["id"] not in b]
    diffs += [f"id {r['id']} {b[r['id']]['status']} -> {r['status']}" for r in after if r["id"] in b and b[r["id"]]["status"] != r["status"]]
    return "; ".join(diffs) or "no booking row changed"


def to_text(records: list[dict]) -> str:
    out = ["$ python -m qa.record_example", ""]
    for r in records:
        gate = r["gate"]
        res = r["tool_result"]
        if gate["decision"] == "refused":
            tool_outcome = "not run. The model was told: " + _short(res.get("error", res), 60)
        elif isinstance(res, dict) and ("error" in res or res.get("success") is False):
            tool_outcome = "run, rejected by the tool's own rule: " + _short(res.get("error", res), 60)
        else:
            tool_outcome = "run, succeeded"
        out += [
            f"== {r['label']} (case {r['case']}) ==",
            f"caller says     {_short(r['caller_says'], 116)}",
            f"model proposes  {r['model_proposes']['tool']}({_short(r['model_proposes']['arguments'], 88)})",
            f"gate decision   {gate['decision'].upper()}  control={gate['control']}",
            f"tool outcome    {tool_outcome}",
            f"audit row       tool={r['audit_row']['tool']} decision={r['audit_row']['decision']} control={r['audit_row']['control']}",
            f"audit args      {_short(r['audit_row']['args_json'], 100)}",
            f"bookings        {len(r['bookings_before'])} before, {len(r['bookings_after'])} after; changed: {_changed(r['bookings_before'], r['bookings_after'])}",
            f"state check     {r['state_check']}: {'held' if r['state_check_held'] else 'VIOLATED'}",
            "",
        ]
    ok = all(r["passed"] for r in records)
    out.append(f"{sum(r['passed'] for r in records)} of {len(records)} recorded cases match their expected decision and control. Scripted model, synthetic data, no network.")
    assert ok, "a recorded case no longer matches its expected result"
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="directory for recorded_example.json and recorded_example.txt")
    args = ap.parse_args()
    records = [record(cid, label) for cid, label in PICKS]
    text = to_text(records)
    print(text)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "recorded_example.txt").write_text(text, encoding="utf-8")
        (out / "recorded_example.json").write_text(json.dumps(records, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
