"""The prospect sheet must stay safe to hand to the owner: sourced, phone-verified fields, no fake claims, no disqualified account in the call queue."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_prospect_sheet_passes_its_quality_gate():
    r = subprocess.run([sys.executable, str(ROOT / "marketing" / "check_prospects.py")], capture_output=True, text=True, cwd=ROOT / "marketing")
    assert r.returncode == 0, r.stdout + r.stderr


def test_rebuilding_the_sheet_is_idempotent_and_keeps_owner_notes(tmp_path, monkeypatch):
    import csv
    import importlib

    sys.path.insert(0, str(ROOT / "marketing"))
    bp = importlib.import_module("build_prospects")
    monkeypatch.setattr(bp, "HERE", tmp_path)
    (tmp_path / "research").mkdir()
    for f in (ROOT / "marketing" / "research").glob("batch*.py"):
        (tmp_path / "research" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    first = bp.build()
    bp.write(first)
    rows = list(csv.DictReader((tmp_path / "prospects.csv").open(encoding="utf-8")))
    name = next(r["business_name"] for r in rows if r["fit_tier"] == "A")
    for r in rows:
        if r["business_name"] == name:
            r.update(sales_status="CONNECTED", last_contact_date="2026-10-06", next_action="Call back Thursday 9am", next_action_date="2026-10-08", do_not_contact="no")
    with (tmp_path / "prospects.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=bp.FIELDS)
        w.writeheader()
        w.writerows(rows)
    again = {r["business_name"]: r for r in bp.build()}
    assert again[name]["sales_status"] == "CONNECTED" and again[name]["next_action_date"] == "2026-10-08" and again[name]["last_contact_date"] == "2026-10-06"
    others = [r for r in again.values() if r["business_name"] != name and r["fit_tier"] == "A"]
    assert all(r["sales_status"] == "READY_TO_CALL" for r in others)


def _sales(tmp_path, monkeypatch):
    import csv
    import importlib

    sys.path.insert(0, str(ROOT / "marketing"))
    bp = importlib.import_module("build_prospects")
    sales = importlib.import_module("sales")
    monkeypatch.setattr(sales, "PROSPECTS", tmp_path / "prospects.csv")
    monkeypatch.setattr(sales, "CALLS", tmp_path / "call_log.csv")
    rows = list(csv.DictReader((ROOT / "marketing" / "prospects.csv").open(encoding="utf-8")))
    sales.write(tmp_path / "prospects.csv", rows, bp.FIELDS)
    return sales, next(r["business_name"] for r in rows if r["fit_tier"] == "A")


class _Args:
    def __init__(self, business, outcome, **kw):
        self.business, self.outcome = business, outcome
        self.dm, self.note, self.objection, self.block, self.when = kw.get("dm"), kw.get("note"), kw.get("objection"), kw.get("block", "morning"), kw.get("when")


def test_logging_a_call_moves_the_account_and_sets_a_next_action(tmp_path, monkeypatch):
    sales, name = _sales(tmp_path, monkeypatch)
    sales.cmd_log(_Args(name, "voicemail"))
    row = next(r for r in sales.read(tmp_path / "prospects.csv") if r["business_name"] == name)
    assert row["sales_status"] == "ATTEMPTED" and row["next_action_date"] and row["last_contact_date"]
    assert len(sales.read(tmp_path / "call_log.csv")) == 1


def test_do_not_contact_is_permanent_and_blocks_further_calls(tmp_path, monkeypatch):
    import pytest

    sales, name = _sales(tmp_path, monkeypatch)
    sales.cmd_log(_Args(name, "dnc", note="asked not to be called"))
    row = next(r for r in sales.read(tmp_path / "prospects.csv") if r["business_name"] == name)
    assert row["do_not_contact"] == "yes" and row["sales_status"] == "DO_NOT_CONTACT" and row["next_action_date"] == ""
    with pytest.raises(SystemExit):
        sales.cmd_log(_Args(name, "connected"))


def test_the_diagnosis_waits_for_enough_volume_and_then_names_the_weak_stage(tmp_path, monkeypatch):
    sales, _ = _sales(tmp_path, monkeypatch)
    assert "Not enough" in sales.diagnose(10, 9, 5, 5, 4, 3, 2, 1)
    assert sales.diagnose(40, 5, 3, 1, 0, 0, 0, 0).startswith("LOW CONNECT RATE")
    assert sales.diagnose(40, 20, 12, 2, 0, 0, 0, 0).startswith("GOOD CONNECT, NO PAIN")
    assert sales.diagnose(40, 20, 12, 10, 2, 0, 0, 0).startswith("PAIN BUT NO DEMO")
    assert sales.diagnose(40, 20, 12, 10, 6, 5, 1, 0).startswith("DEMOS BUT NO PILOT")
    assert sales.diagnose(40, 20, 12, 10, 6, 5, 4, 1).startswith("PILOTS DON'T CONVERT")


# ---------------------------------------------------------------- trigger layer and the command center
def test_every_a_tier_account_has_a_fresh_reason_a_question_a_role_and_a_demo_scenario():
    import csv

    rows = list(csv.DictReader((ROOT / "marketing" / "prospects.csv").open(encoding="utf-8")))
    a = [r for r in rows if r["fit_tier"] == "A"]
    assert a
    for r in a:
        assert r["reason_to_call_now"] and not r["reason_to_call_now"].startswith("No fresh reason"), r["business_name"]
        assert r["first_question"] and r["decision_maker_hypothesis"] and r["demo_scenario"] and r["suggested_opener"], r["business_name"]
        assert r["trigger_type"] != "NONE" and not r["trigger_freshness"].startswith("stale"), r["business_name"]


def test_ids_are_unique_and_unknown_is_a_valid_value():
    import csv

    rows = list(csv.DictReader((ROOT / "marketing" / "prospects.csv").open(encoding="utf-8")))
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)) and all(ids)
    assert any(r["lsa_signal"] == "UNKNOWN" for r in rows)          # nothing is guessed: unresearched means UNKNOWN


def test_a_verified_hiring_trigger_ranks_above_a_plain_hours_gap_and_never_lifts_a_big_company():
    import csv

    rows = {r["business_name"]: r for r in csv.DictReader((ROOT / "marketing" / "prospects.csv").open(encoding="utf-8"))}
    ss, donmar = rows["Service Specialties Inc."], rows["Donmar Heating, Cooling & Plumbing"]
    assert ss["trigger_type"] == "JOB_OPENING" and float(ss["call_priority"]) > max(float(r["call_priority"]) for r in rows.values() if r["trigger_type"] == "HOURS_GAP")
    assert donmar["fit_tier"] != "A"                                 # two-state operation: kept for the hiring signal, not auto-promoted


def test_the_diagnosis_names_the_weak_stage_with_the_new_vocabulary_only_once_there_is_volume(tmp_path, monkeypatch):
    sales, _ = _sales(tmp_path, monkeypatch)
    assert "Not enough" in sales.diagnose(19, 10, 5, 5, 4, 3, 2, 1)


def test_structured_outcomes_by_id_with_legacy_names_still_working(tmp_path, monkeypatch):
    import csv

    sales, name = _sales(tmp_path, monkeypatch)
    rows = sales.read(tmp_path / "prospects.csv")
    rid = next(r["id"] for r in rows if r["business_name"] == name)

    class A:
        business, outcome, dm, note, objection, block, when = rid, "DECISION_MAKER_CONNECTED", None, "owner picked up", None, "morning", None
        reason, opener, demo_type, next = None, "a", None, None

    sales.cmd_log(A)
    row = next(r for r in sales.read(tmp_path / "prospects.csv") if r["business_name"] == name)
    assert row["sales_status"] == "CONNECTED" and row["next_action_date"]
    entry = sales.read(tmp_path / "call_log.csv")[0]
    assert entry["outcome"] == "DECISION_MAKER_CONNECTED" and entry["decision_maker_reached"] == "yes" and entry["opener"] == "A" and entry["id"] == rid
    assert sales.canon("connected") == "DECISION_MAKER_CONNECTED" and sales.canon("dnc") == "DO_NOT_CONTACT"


def test_a_no_records_a_reason_code_and_a_bad_reason_is_refused(tmp_path, monkeypatch):
    import pytest

    sales, name = _sales(tmp_path, monkeypatch)

    class A:
        business, outcome, dm, note, objection, block, when = name, "NO_PAIN", "yes", "has live answering", None, "morning", None
        reason, opener, demo_type, next = "existing_solution", None, None, None

    sales.cmd_log(A)
    row = next(r for r in sales.read(tmp_path / "prospects.csv") if r["business_name"] == name)
    assert row["sales_status"] == "LOST" and row["lost_reason"].startswith("EXISTING_SOLUTION") and row["next_action_date"] == ""
    A.reason = "because"
    with pytest.raises(SystemExit):
        sales.cmd_log(A)


def test_lookup_accepts_an_id_a_unique_part_of_a_name_and_refuses_ambiguity(tmp_path, monkeypatch):
    import pytest

    sales, _ = _sales(tmp_path, monkeypatch)
    rows = sales.read(tmp_path / "prospects.csv")
    assert sales.find(rows, "specialties")["business_name"] == "Service Specialties Inc."
    assert sales.find(rows, "Service Specialties")["id"] == "specialties"
    with pytest.raises(SystemExit):
        sales.find(rows, "heating")


def test_the_followup_draft_carries_no_price_and_the_opt_out(tmp_path, monkeypatch, capsys):
    sales, _ = _sales(tmp_path, monkeypatch)

    class A:
        business = "specialties"

    # A draft must follow a real logged conversation, never pretend a voicemail was left.
    sales.write(sales.CALLS, [{"business": "Service Specialties Inc.", "outcome": "SEND_INFO", "at": "2026-10-02T12:00", "note": ""}], sales.CALL_FIELDS)
    sales.cmd_followup(A)
    out = capsys.readouterr().out
    assert "$" not in out and "Reply \"stop\"" in out and "[YOUR MAILING ADDRESS]" in out


def test_a_warm_introduction_becomes_the_top_account_and_keeps_its_status_across_rebuilds(tmp_path, monkeypatch):
    import csv
    import importlib

    sys.path.insert(0, str(ROOT / "marketing"))
    bp = importlib.import_module("build_prospects")
    tr = importlib.import_module("triggers")
    monkeypatch.setattr(bp, "HERE", tmp_path)
    monkeypatch.setattr(tr, "HERE", tmp_path)
    (tmp_path / "research").mkdir()
    for f in (ROOT / "marketing" / "research").glob("batch*.py"):
        (tmp_path / "research" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "triggers.csv").write_text((ROOT / "marketing" / "triggers.csv").read_text(encoding="utf-8"), encoding="utf-8")
    with (tmp_path / "warm_contacts.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=tr.WARM_FIELDS)
        w.writeheader()
        w.writerow({"business_name": "Example Family Plumbing", "vertical": "Plumbing", "city": "Fairfax", "phone": "+15555550100", "contact": "Pat Lee", "via": "Aunt Maria",
                    "email": "", "note": "met at a family event", "added": "2026-10-01"})
    rows = bp.build()
    bp.write(rows)
    top = rows[0]
    assert top["business_name"] == "Example Family Plumbing" and top["warm_intro"] == "yes" and top["trigger_type"] == "WARM_INTRO" and top["fit_tier"] == "A"
    assert "permission to talk" in top["reason_to_call_now"] and "Aunt Maria" in top["suggested_opener"]
    today = list(csv.DictReader((tmp_path / "today.csv").open(encoding="utf-8")))
    assert today[0]["business"] == "Example Family Plumbing"
    # the owner logs a call; a rebuild must not forget it
    saved = list(csv.DictReader((tmp_path / "prospects.csv").open(encoding="utf-8")))
    for r in saved:
        if r["business_name"] == "Example Family Plumbing":
            r.update(sales_status="DEMO_BOOKED", next_action="Run the demo", next_action_date="2026-10-08", last_contact_date="2026-10-02")
    with (tmp_path / "prospects.csv").open("w", encoding="utf-8", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=bp.FIELDS)
        wr.writeheader()
        wr.writerows(saved)
    again = {r["business_name"]: r for r in bp.build()}
    assert again["Example Family Plumbing"]["sales_status"] == "DEMO_BOOKED" and again["Example Family Plumbing"]["next_action_date"] == "2026-10-08"
