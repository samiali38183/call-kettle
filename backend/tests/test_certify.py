"""The readiness lint: each way a client can be mis-configured must produce the right level, and the verdict rules must hold."""
import pytest

from app import certify
from app.config import ClientConfig, load_client_config


def _cfg(**over) -> ClientConfig:
    base = load_client_config("callkettle_demo").model_dump()
    base.update(over)
    return ClientConfig.model_validate(base)


def _levels(cfg, **kw):
    return {f.name: f.level for f in certify.lint(cfg, **kw)}


GOOD = dict(escalation_phone="+15555550100", owner_email="owner@example.com")


def test_a_complete_client_is_ready_with_only_unconfirmable_warnings():
    f = certify.lint(_cfg(**GOOD), hours_confirmed=True)
    assert certify.verdict(f) in ("READY", "READY WITH WARNINGS")
    assert not [x for x in f if x.level == "FAIL"]


def test_verdict_rules():
    P, W, F = certify.Finding("PASS", "a", ""), certify.Finding("WARN", "b", ""), certify.Finding("FAIL", "c", "")
    assert certify.verdict([P]) == "READY" and certify.verdict([P, W]) == "READY WITH WARNINGS"
    assert certify.verdict([P, W, F]) == "BLOCKED" and certify.verdict([]) == "READY"


@pytest.mark.parametrize("phone", ["", "+15555550100", "+1703555", "+15555550100", "+15555550100x", "+15555550122"])
def test_a_bad_or_fictional_transfer_number_blocks(phone):
    assert _levels(_cfg(**{**GOOD, "escalation_phone": phone}))["Transfer number"] == "FAIL"


def test_transferring_to_the_assistants_own_line_is_a_loop_and_blocks():
    assert _levels(_cfg(**GOOD), own_numbers=["+15555550100"])["Transfer number"] == "FAIL"
    assert _levels(_cfg(**GOOD), own_numbers=["+15555550100"])["Transfer number"] == "PASS"


def test_no_open_days_blocks():
    closed = {d: "closed" for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    assert _levels(_cfg(**GOOD, business_hours=closed))["Booking hours"] == "FAIL"


def test_closing_before_opening_blocks():
    hours = {"mon": ["17:00", "09:00"], "tue": "closed", "wed": "closed", "thu": "closed", "fri": "closed", "sat": "closed", "sun": "closed"}
    with pytest.raises(ValueError):                    # refused when the config is loaded, so it can never reach a live call
        _cfg(**GOOD, business_hours=hours, booking_hours=hours)
    unvalidated = _cfg(**GOOD).model_copy(update={"business_hours": hours, "booking_hours": hours})
    assert _levels(unvalidated)["Booking hours"] == "FAIL"                 # the readiness lint still catches it on its own


def test_a_service_longer_than_any_window_blocks():
    hours = {"mon": ["09:00", "10:00"], "tue": "closed", "wed": "closed", "thu": "closed", "fri": "closed", "sat": "closed", "sun": "closed"}
    cfg = _cfg(**GOOD, business_hours=hours, booking_hours=hours, services=[{"name": "Big job", "duration_minutes": 180}])
    assert _levels(cfg)["Services fit the hours"] == "FAIL"


def test_duplicate_or_absurd_services_block():
    assert _levels(_cfg(**GOOD, services=[{"name": "Visit", "duration_minutes": 30}, {"name": "visit", "duration_minutes": 60}]))["Services"] == "FAIL"
    assert _levels(_cfg(**GOOD, services=[{"name": "Visit", "duration_minutes": 0}]))["Services"] == "FAIL"
    assert _levels(_cfg(**GOOD, services=[]))["Services"] == "FAIL"


def test_nobody_to_tell_blocks_but_only_an_email_missing_warns():
    none = _cfg(escalation_phone="+15555550100", owner_email=None, ntfy_topic=None)
    assert _levels(none)["Owner notification"] == "FAIL" and _levels(none)["Owner email"] == "WARN"
    ntfy = _cfg(escalation_phone="+15555550100", owner_email=None, ntfy_topic="some-secret-topic")
    assert "Owner notification" not in _levels(ntfy) and _levels(ntfy)["Owner email"] == "WARN"


def test_placeholder_text_blocks():
    cfg = _cfg(**GOOD, extra_instructions="TODO fill this in")
    assert _levels(cfg)["Placeholder text"] == "FAIL"
    cfg = _cfg(**GOOD, faqs=[{"q": "How?", "a": "Email info@example.com"}])
    assert _levels(cfg)["Placeholder text"] == "FAIL"


def test_missing_calendar_and_unconfirmed_hours_warn():
    lv = _levels(_cfg(**GOOD))
    assert lv["Calendar"] == "WARN" and lv["Hours confirmed with the owner"] == "WARN"
    assert "Hours confirmed with the owner" not in _levels(_cfg(**GOOD), hours_confirmed=True) or _levels(_cfg(**GOOD), hours_confirmed=True)["Hours confirmed with the owner"] != "WARN"
    assert _levels(_cfg(**GOOD, calendar_ical_url="https://calendar.example.org/x.ics"))["Calendar"] == "PASS"


def test_transfer_mode_ceiling_warns():
    assert _levels(_cfg(**GOOD, ceiling_mode="transfer"))["Over-limit behavior"] == "WARN"
    assert _levels(_cfg(**GOOD))["Over-limit behavior"] == "PASS"


def test_report_renders_a_verdict_and_escapes_table_pipes():
    f = [certify.Finding("PASS", "ok", "fine"), certify.Finding("WARN", "w", "a | b")]
    text = certify.render("acme", f)
    assert "**Verdict: READY WITH WARNINGS**" in text and "a / b" in text and "| WARN | w |" in text


def test_owner_first_routing_warns_about_loops_and_voicemail():
    lv = _levels(_cfg(**GOOD, routing_mode="owner_first"))
    assert lv["Routing"] == "WARN"
    text = next(f.detail for f in certify.lint(_cfg(**GOOD, routing_mode="owner_first")) if f.name == "Routing")
    assert "forward ALL calls" in text and "voicemail" in text
    screened = next(f.detail for f in certify.lint(_cfg(**GOOD, routing_mode="owner_first", transfer_screening=True)) if f.name == "Routing")
    assert "voicemail" not in screened
    assert _levels(_cfg(**GOOD))["Routing"] == "PASS"
    assert _levels(_cfg(**GOOD, always_ring_owner=["+15555550100"]))["Routing"] == "WARN"


def test_readiness_summary_uses_owner_words_and_never_reads_not_tested_as_pass():
    from app.certify import Finding, lint, readiness_summary
    from app.config import load_client_config

    cfg = load_client_config("demo_hvac")
    findings = lint(cfg) + [
        Finding("PASS", "Books exactly one in-hours appointment for a configured service", "1 booking(s)"),
        Finding("FAIL", "Cancelling frees the slot and keeps a record", "0 live, 0 cancelled"),
        Finding("WARN", "Asks for a human: put through to the owner", "dial"),
    ]
    text = readiness_summary(cfg, findings)
    rows = {line.split("|")[1].strip(): line.split("|")[2].strip() for line in text.splitlines() if line.startswith("| ") and "---" not in line and "Area" not in line}
    assert rows["BOOKING"] == "PASS" and rows["CANCEL"] == "BLOCKED" and rows["HUMAN HANDOFF"] == "NEEDS ATTENTION"
    assert rows["RESCHEDULE"] == "NOT TESTED" and rows["SPANISH"] == "NOT ENABLED"
    assert "Overall: **BLOCKED**" in text and "do not read it as passing" in text
    assert "%" not in text                                                   # no fake score
