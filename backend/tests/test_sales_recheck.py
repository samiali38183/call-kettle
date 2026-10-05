from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SALES_PATH = ROOT / "marketing" / "sales.py"
spec = importlib.util.spec_from_file_location("callkettle_sales", SALES_PATH)
assert spec is not None
sales = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(sales)


def row(**overrides):
    base = {
        "id": "specialties",
        "business_name": "Service Specialties Inc.",
        "published_business_phone": "+15555550100",
        "fit_tier": "A",
        "sales_status": "READY_TO_CALL",
        "do_not_contact": "no",
        "call_priority": "45",
        "trigger_source": "the company's own careers page https://www.ssihvac.com/contact-us/join-our-team/ (read 2026-10-01) plus search listings",
        "source_urls": "https://www.ssihvac.com/",
        "website": "https://www.ssihvac.com/",
        "trigger_type": "JOB_OPENING",
        "trigger_date": "2026-10-01",
        "research_confidence": "high",
        "reason_to_call_now": "Advertising a customer service / dispatcher role (fresh; re-verify it is still open).",
        "first_question": "Are you mainly hiring that role for inbound calls and scheduling, dispatch coordination, or both?",
        "reason_this_might_be_a_bad_fit": "",
        "decision_maker_hypothesis": "Rusty Murphy",
        "decision_maker_confidence": "medium",
        "vertical": "HVAC",
        "city": "Chantilly",
        "demo_scenario": "Heating season: no-heat call.",
        "known_fsm_crm": "not found",
    }
    base.update(overrides)
    return base


def test_recheck_flags_warns_for_job_listing_without_claiming_a_problem():
    flags = sales.recheck_flags(row(), today=date(2026, 10, 3))
    text = " ".join(flags).lower()

    assert "job-opening trigger" in text
    assert "company's own site" in text
    assert "missed call" not in text
    assert "understaffed" not in text


def test_recheck_flags_warns_when_trigger_is_stale():
    flags = sales.recheck_flags(row(trigger_type="HOURS_GAP", trigger_source="https://example.com", trigger_date="2026-09-20"), today=date(2026, 10, 3))

    assert any("13 days old" in flag for flag in flags)


def test_recheck_text_includes_safe_fallback_question_and_source():
    text = sales.recheck_text([row()], 1, today=date(2026, 10, 3))

    assert "PRECISION RECHECK CHECKLIST" in text
    assert "open now: the company's own careers page" in text
    assert "safe fallback question" in text
    assert "When calls come in after hours or while everyone is tied up" in text
    assert "Never say they miss calls" in text


def test_call_block_text_combines_briefs_recheck_and_worksheet_without_outreach():
    text = sales.call_block_text([row()], 1)

    assert "# Call Kettle founder call block" in text
    assert "## 1. Briefs" in text
    assert "## 2. Precision recheck" in text
    assert "PRECISION RECHECK CHECKLIST" in text
    assert "## 3. Worksheet" in text
    assert "Log command:" in text
    assert "does not contact anyone" in text
    assert "send email" in text
