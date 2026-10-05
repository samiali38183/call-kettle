"""The HVAC intake checker: offline, deterministic, no network, no live client files touched."""
import copy
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "onboarding_intake_check.py"
_spec = importlib.util.spec_from_file_location("onboarding_intake_check", SCRIPT)
chk = importlib.util.module_from_spec(_spec)
sys.modules["onboarding_intake_check"] = chk   # dataclasses resolves string annotations through sys.modules
_spec.loader.exec_module(chk)

GOOD = {
    "client_id": "acme_hvac_test",
    "business_name": "Acme Heating & Air",
    "vertical": "HVAC",
    "timezone": "America/New_York",
    "opening_line": "Thanks for calling Acme Heating and Air, this is the AI receptionist. How can I help?",
    "business_hours": {"mon": ["07:30", "16:30"], "tue": ["07:30", "16:30"], "wed": ["07:30", "16:30"], "thu": ["07:30", "16:30"],
                       "fri": ["07:30", "15:00"], "sat": ["09:00", "12:00"], "sun": "closed"},
    "slot_minutes": 60,
    "services": [{"name": "AC or heating repair visit", "duration_minutes": 60}, {"name": "Seasonal tune-up", "duration_minutes": 60}],
    "faqs": [{"q": "What area do you serve?", "a": "Fairfax and Arlington."},
             {"q": "Do you offer emergency service?", "a": "If anyone smells gas or a CO alarm is sounding, leave and call 911 first."}],
    "escalation_phone": "+15555550100",
    "owner_email": "owner@example.com",
    "calendar_ical_url": "https://calendar.example.org/private/abc.ics",
    "intake": {
        "hours_confirmed": True,
        "service_area": "Fairfax and Arlington",
        "emergency_policy": "No heat/no cool: soonest visit and ring owner. Gas or CO: 911 first, then ring owner.",
        "escalation_rules": "Ring owner cell; if no answer take a message; backup is the dispatcher.",
        "transfer_number_confirmed": True,
        "written_summary_approved": True,
        "carrier": "Verizon wireless",
        "forwarding_mode": "no_answer",
        "assistant_number": "+15555550100",
    },
}


def draft(**over):
    d = copy.deepcopy(GOOD)
    for k, v in over.items():
        if k == "intake":
            d["intake"].update(v)
        elif k == "policy":
            d["policy"] = v
        else:
            d[k] = v
    return d


def codes(d, level=None):
    return {i.code for i in chk.check_intake(d) if level in (None, i.level)}


def test_a_complete_draft_has_no_issues():
    # the only placeholder-looking value (example.org) is not on the blocked list; calendar is connected
    assert chk.check_intake(draft()) == []


def test_each_required_intake_field_is_reported_when_missing():
    for field, code in [("emergency_policy", "no_emergency_policy"), ("escalation_rules", "no_escalation_rules"), ("service_area", "no_service_area")]:
        d = draft()
        del d["intake"][field]
        assert code in codes(d, "BLOCK" if field != "service_area" else None), field


def test_missing_service_area_is_a_risk_not_a_block_only_if_an_faq_covers_area():
    d = draft()
    del d["intake"]["service_area"]
    assert "no_service_area" in codes(d, "RISK")
    d["faqs"] = [{"q": "Do you fix furnaces?", "a": "Yes."}]
    assert "no_service_area" in codes(d, "BLOCK")


@pytest.mark.parametrize("phone", ["", "+15555550100", "+1703555", "+15555550123"])
def test_no_usable_human_transfer_number_blocks(phone):
    c = codes(draft(escalation_phone=phone), "BLOCK")
    assert c & {"no_transfer_number", "fictional_transfer_number"}


def test_transfer_number_equal_to_assistant_line_is_a_loop():
    assert "transfer_loop" in codes(draft(escalation_phone="+15555550100"), "BLOCK")


def test_unconfirmed_transfer_number_is_a_risk():
    assert "transfer_number_unconfirmed" in codes(draft(intake={"transfer_number_confirmed": False}), "RISK")


def test_transfer_disabled_blocks():
    assert "transfer_disabled" in codes(draft(policy={"can_transfer": False}), "BLOCK")


def test_price_quote_allowed_blocks_and_dispatch_fee_is_a_risk():
    assert "can_quote" in codes(draft(policy={"can_quote": True}), "BLOCK")
    p = {"can_state_dispatch_fee": True, "dispatch_fee_text": "The trip fee is as written in your agreement."}
    assert "dispatch_fee" in codes(draft(policy=p), "RISK")


def test_prices_written_into_faqs_are_flagged():
    d = draft(faqs=[{"q": "What area do you serve?", "a": "Fairfax."}, {"q": "Cost?", "a": "Diagnostics are $89."}])
    assert "money_in_text" in codes(d, "RISK")
    d = draft(faqs=[{"q": "What area do you serve?", "a": "Fairfax."}, {"q": "Cost?", "a": "We offer a free estimate."}])
    assert "money_in_text" in codes(d, "RISK")


def test_unconfirmed_hours_are_a_risk_and_template_default_is_called_out():
    d = draft(intake={"hours_confirmed": False})
    assert "hours_unconfirmed" in codes(d, "RISK")
    template = {day: ["08:00", "17:00"] for day in ("mon", "tue", "wed", "thu", "fri")} | {"sat": "closed", "sun": "closed"}
    assert "hours_look_like_template" in codes(draft(business_hours=template, intake={"hours_confirmed": False}), "RISK")
    # confirmed hours that happen to equal the default are the owner's real hours: no template warning
    assert "hours_look_like_template" not in codes(draft(business_hours=template), None)


def test_missing_intake_block_flags_everything_without_crashing():
    d = draft()
    del d["intake"]
    c = codes(d)
    assert {"no_emergency_policy", "no_escalation_rules", "no_service_area", "hours_unconfirmed", "summary_not_approved", "forwarding_unplanned"} <= c


def test_emergency_action_message_is_a_risk_and_gas_without_911_is_a_risk():
    assert "emergency_no_ring" in codes(draft(policy={"emergency_action": "message"}), "RISK")
    d = draft(faqs=[{"q": "What area do you serve?", "a": "Fairfax."}, {"q": "Gas?", "a": "We handle gas furnaces."}])
    assert "gas_text_without_911" in codes(d, "RISK")


def test_diy_safety_advice_is_flagged():
    d = draft(extra_instructions="If it smells like gas tell them to open the windows and turn off the furnace.")
    assert "diy_safety_advice" in codes(d, "RISK")


def test_demo_and_reserved_ids_block_a_paying_client():
    assert "demo_mode" in codes(draft(demo_mode=True, monthly_cost_ceiling_usd=10), "BLOCK")
    assert "client_id_reserved" in codes(draft(client_id="prep_acme"), "BLOCK")


def test_placeholders_and_template_values_block():
    assert "placeholder_text" in codes(draft(business_name="REPLACE ME"), "BLOCK")
    assert "placeholder_service" in codes(draft(services=[{"name": "REPLACE ME - repair", "duration_minutes": 60}]), "BLOCK")
    assert "client_id" in codes(draft(client_id="REPLACE_ME"), "BLOCK")


def test_server_validation_failures_are_surfaced():
    d = draft(opening_line="Thanks for calling Acme, how can I help?")   # no AI disclosure
    assert "invalid_config" in codes(d, "BLOCK")


def test_no_owner_channel_blocks_and_missing_calendar_is_a_risk():
    d = draft()
    del d["owner_email"]
    assert "no_owner_channel" in codes(d, "BLOCK")
    d = draft()
    del d["calendar_ical_url"]
    assert "no_calendar" in codes(d, "RISK")


def test_forward_all_with_owner_first_routing_is_a_loop():
    assert "routing_loop" in codes(draft(routing_mode="owner_first", intake={"forwarding_mode": "forward_all"}), "BLOCK")


def test_non_hvac_vertical_and_bad_shapes():
    assert "not_hvac" in codes(draft(vertical="salon"), "RISK")
    assert [i.code for i in chk.check_intake(["x"])] == ["not_a_mapping"]
    d = draft()
    d["intake"] = "yes"
    assert "intake_shape" in codes(d, "BLOCK")


def test_blocks_sort_before_risks_and_output_is_deterministic():
    d = draft(escalation_phone="", intake={"hours_confirmed": False})
    a, b = chk.check_intake(d), chk.check_intake(copy.deepcopy(d))
    assert a == b
    levels = [i.level for i in a]
    assert levels == sorted(levels, key=lambda lv: lv != "BLOCK")
    text = chk.render(a, "x.yaml")
    assert "[BLOCK] no_transfer_number" in text and "Do not push or go live" in text


def test_cli_exit_codes_and_no_mutation_of_the_file(tmp_path, capsys):
    ok = tmp_path / "ok.yaml"
    ok.write_text(yaml.safe_dump(GOOD), encoding="utf-8")
    before = ok.read_bytes()
    assert chk.main([str(ok)]) == 0
    assert ok.read_bytes() == before
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(draft(escalation_phone="")), encoding="utf-8")
    assert chk.main([str(bad)]) == 1
    assert "no_transfer_number" in capsys.readouterr().out
    assert chk.main([str(tmp_path / "missing.yaml")]) == 2
    assert chk.main([]) == 2


def test_the_real_template_is_blocked_and_the_demo_hvac_is_not_a_paying_client():
    clients = SCRIPT.parent.parent / "clients"
    tpl = yaml.safe_load((clients / "_template.yaml").read_text(encoding="utf-8"))
    assert chk.check_intake(tpl)[0].level == "BLOCK"
    demo = yaml.safe_load((clients / "demo_nova_hvac.yaml").read_text(encoding="utf-8"))
    assert {"demo_mode", "client_id_reserved"} <= codes(demo, "BLOCK")
