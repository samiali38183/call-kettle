import importlib.util
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hours_parser_handles_ranges_single_days_and_zero_padding():
    onboard = _load("onboard_client")
    hours = onboard.parse_hours("mon-fri 8:00-17:00, sat 9:00-13:00")
    assert hours["mon"] == ["08:00", "17:00"] and hours["fri"] == ["08:00", "17:00"]
    assert hours["sat"] == ["09:00", "13:00"] and hours["sun"] == "closed"


def test_hours_parser_rejects_garbage_with_a_useful_message():
    onboard = _load("onboard_client")
    with pytest.raises(ValueError, match="Use e.g."):
        onboard.parse_hours("whenever")


@pytest.mark.parametrize("raw", ["+15555550100", "+15555550100", "+15555550100", "+15555550100"])
def test_phone_normalizer(raw):
    assert _load("onboard_client").normalize_phone(raw) == "+15555550100"


def test_phone_normalizer_rejects_short_numbers():
    with pytest.raises(ValueError):
        _load("onboard_client").normalize_phone("555-0123")


def _cfg(vertical, **kw):
    onboard = _load("onboard_client")
    return onboard.build_config(
        client_id="acme", business_name="Acme", vertical=vertical, hours=onboard.parse_hours("mon-fri 8:00-17:00"),
        services=[("Repair", 60)], faqs=[("Do you do estimates?", "Yes, free.")], escalation_phone="+15555550100", **kw
    )


def test_generated_config_is_valid_yaml_that_the_server_accepts():
    from app.config import ClientConfig

    config = _cfg("plumbing")
    ClientConfig.model_validate(yaml.safe_load(yaml.safe_dump(config)))
    assert "AI receptionist" in config["opening_line"]  # AI disclosure is built in
    assert "record_transcripts" not in config  # normal verticals keep recording on


@pytest.mark.parametrize("vertical", ["dental", "home health care", "law firm", "med spa", "Medical clinic"])
def test_sensitive_verticals_default_to_not_storing_transcripts(vertical):
    assert _cfg(vertical)["record_transcripts"] is False


def test_provisioning_points_voice_status_and_fallback(monkeypatch):
    monkeypatch.setenv("FALLBACK_BASE_URL", "https://fallback.example")
    provision = _load("provision_number")
    provision.FALLBACK_BASE_URL = "https://fallback.example"
    s = provision.webhook_settings("acme", "+15555550100")
    assert s["voice_url"].endswith("/voice/incoming?client_id=acme")
    assert s["status_callback"].endswith("/voice/status")
    assert s["voice_fallback_url"] == "https://fallback.example/api/fallback?to=%2B+15555550100"


def test_email_setup_cleans_the_app_password_and_formats_secrets_for_fly():
    setup = _load("setup_email")
    assert setup.clean("abcd efgh ijkl mnop") == "abcdefghijklmnop"
    payload = setup.secrets_payload("owner@example.com", "abcdefghijklmnop")
    assert "SMTP_HOST=smtp.gmail.com" in payload and "SMTP_PASSWORD=abcdefghijklmnop" in payload
    assert "SMTP_USER=owner@example.com" in payload and payload.endswith("\n")


# ---------------------------------------------------------------- private prospect demo builder
def test_prep_demo_builds_a_valid_private_demo_from_public_data():
    prep = _load("prep_demo")
    from app.config import ClientConfig

    row = {"business_name": "Example Heating & Cooling", "vertical": "HVAC", "city": "Vienna", "business_hours": "Mon-Fri 7:30am-4pm; Sat-Sun closed",
           "source_urls": "https://example.test/", "last_verified_date": "2026-10-01"}
    cfg, hours_ok = prep.build_config(row)
    parsed = ClientConfig.model_validate(cfg)
    assert hours_ok and parsed.business_hours["mon"] == ["07:30", "16:00"] and parsed.business_hours["sat"] == "closed"
    assert parsed.demo_mode and parsed.monthly_cost_ceiling_usd <= 10 and parsed.client_id.startswith("prep_")
    assert "demonstration of an AI receptionist" in parsed.opening_line
    assert "UNVERIFIED PUBLIC DATA" in parsed.extra_instructions and "NOT Example Heating" in parsed.extra_instructions
    assert not any("$" in f.a for f in parsed.faqs)
    sheet = prep.prep_sheet(row, cfg, hours_ok)
    assert "UNVERIFIED PUBLIC DATA" in sheet and "NEVER PUBLISH" in sheet and "dedicated phone number" in sheet


def test_prep_demo_says_so_loudly_when_it_has_to_guess_the_hours():
    prep = _load("prep_demo")
    hours, ok = prep.parse_public_hours("")
    assert not ok and hours["mon"] == ["08:00", "17:00"] and hours["sat"] == "closed"
    row = {"business_name": "No Hours Plumbing", "vertical": "Plumbing", "city": "Leesburg", "business_hours": ""}
    cfg, hours_ok = prep.build_config(row)
    assert "CONFIRM ON THE CALL" in prep.prep_sheet(row, cfg, hours_ok)


@pytest.mark.parametrize("text,mon,sat", [
    ("Sunday-Saturday 7am-5pm", ["07:00", "17:00"], ["07:00", "17:00"]),
    ("Mon-Sat 9am-5pm", ["09:00", "17:00"], ["09:00", "17:00"]),
    ("Mon-Fri 8am-5pm; Sat 9am-1pm", ["08:00", "17:00"], ["09:00", "13:00"]),
])
def test_prep_demo_hours_parser(text, mon, sat):
    prep = _load("prep_demo")
    hours, ok = prep.parse_public_hours(text)
    assert ok and hours["mon"] == mon and hours["sat"] == sat


def test_prep_demo_refuses_an_unknown_or_ambiguous_prospect():
    prep = _load("prep_demo")
    with pytest.raises(SystemExit):
        prep.find_prospect("Definitely Not A Real Company")
    with pytest.raises(SystemExit):
        prep.find_prospect("Heating")        # matches several
