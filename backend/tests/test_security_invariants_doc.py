"""docs/SECURITY_INVARIANTS.md must name only evaluation cases and test files that exist, and every invariant must name at least one."""
import re
from pathlib import Path

from qa import security_eval as ev

DOC = Path(__file__).resolve().parents[2] / "docs" / "SECURITY_INVARIANTS.md"
BACKEND = Path(__file__).resolve().parents[1]


def _rows():
    return [line for line in DOC.read_text(encoding="utf-8").splitlines() if re.match(r"\| I-\d+ \|", line)]


def test_every_invariant_row_names_evidence():
    rows = _rows()
    assert len(rows) >= 11
    for row in rows:
        assert re.search(r"\b[A-Z]{2,3}-\d{2}\b|tests/test_\w+\.py", row), f"no case or test named in: {row[:60]}"


def test_every_case_id_in_the_doc_exists():
    known = {c.id for c in ev.CASES}
    named = set(re.findall(r"\b(?:PI|IN|TA|XT|CF|PX|SAN|LE|BN|RT)-\d{2}\b", DOC.read_text(encoding="utf-8")))
    assert named and named <= known, f"unknown case ids: {sorted(named - known)}"


def test_every_test_file_in_the_doc_exists():
    for rel in set(re.findall(r"tests/test_\w+\.py", DOC.read_text(encoding="utf-8"))):
        assert (BACKEND / rel).exists(), rel


def test_saved_recorded_example_matches_a_fresh_run():
    from qa import record_example

    fresh = record_example.to_text([record_example.record(cid, label) for cid, label in record_example.PICKS])
    assert fresh == (DOC.parent / "security_eval" / "recorded_example.txt").read_text(encoding="utf-8"), "re-run python -m qa.record_example --out ../docs/security_eval"


def test_saved_results_match_a_fresh_run():
    import json

    saved = json.loads((DOC.parent / "security_eval" / "results.json").read_text(encoding="utf-8"))
    fresh = ev.summarize(ev.run_all(), 0)
    for key in ("cases", "passed", "attack_cases", "benign_cases", "tool_calls_attempted", "tool_calls_refused_by_gate",
                "tool_calls_rejected_by_tool_rules", "network_attempts", "audit_leaks"):
        assert saved["summary"][key] == fresh[key], f"{key}: re-run python -m qa.security_eval --out ../docs/security_eval"
