import pytest
from pydantic import ValidationError

from app.config import ClientConfig, ClientNotFoundError, load_client_config


def test_loads_demo_dental():
    config = load_client_config("demo_dental")
    assert config.business_name == "Bright Smile Dental"
    assert config.slot_minutes == 30
    assert any(s.name == "Routine cleaning" for s in config.services)
    assert config.business_hours["sun"] == "closed"


def test_loads_demo_hvac():
    config = load_client_config("demo_hvac")
    assert config.business_name == "CoolFlow Heating & Air"
    assert config.business_hours["sat"] == ["08:00", "14:00"]


def test_unknown_client_raises():
    with pytest.raises(ClientNotFoundError):
        load_client_config("does_not_exist")


def _minimal_config_kwargs(**overrides):
    base = dict(
        client_id="x",
        business_name="X Co",
        vertical="test",
        timezone="America/New_York",
        opening_line="Thanks for calling X Co, how can I help?",
        business_hours={"mon": ["09:00", "17:00"]},
        services=[{"name": "Service", "duration_minutes": 30}],
        escalation_phone="+15555550100",
    )
    base.update(overrides)
    return base


def test_opening_line_without_ai_disclosure_is_rejected():
    with pytest.raises(ValidationError):
        ClientConfig.model_validate(_minimal_config_kwargs())


def test_opening_line_with_ai_word_passes():
    ClientConfig.model_validate(
        _minimal_config_kwargs(opening_line="Thanks for calling X Co, this is the AI receptionist.")
    )


def test_opening_line_with_automated_passes():
    ClientConfig.model_validate(
        _minimal_config_kwargs(opening_line="Thanks for calling X Co, this is an automated assistant.")
    )


def test_opening_line_false_positive_words_still_rejected():
    # "certain", "captain", "maintain" etc. contain the substring "ai" but
    # aren't an AI disclosure — the check must be word-boundary aware.
    with pytest.raises(ValidationError):
        ClientConfig.model_validate(
            _minimal_config_kwargs(opening_line="We're certain to maintain your captain's trust.")
        )


def test_demo_configs_pass_ai_disclosure_validation():
    # The real shipped configs must actually satisfy this, not just the tests.
    load_client_config("demo_dental")
    load_client_config("demo_hvac")
