"""The public demo line: a menu of FICTIONAL companies, each isolated and capped, a simulated hand-off, and no path to a real customer."""
from xml.etree import ElementTree

import pytest

CALLER = "+15555550100"


def test_the_demo_line_plays_a_menu_not_an_ai_conversation(app_client):
    client, main = app_client
    r = client.post("/voice/incoming?client_id=callkettle_demo", data={"CallSid": "CA_MENU1", "From": CALLER})
    assert r.status_code == 200 and "<Gather" in r.text and "/voice/demo-select?client_id=callkettle_demo" in r.text
    assert "press 1" in r.text and "garage door" in r.text and "recorded and monitored" in r.text
    root = ElementTree.fromstring(r.text)
    assert "dtmf" in root.find(".//Gather").get("input") and root.find(".//Gather").get("numDigits") == "1"
    from app import agent

    assert agent.get_session("CA_MENU1") is None                       # no model, no cost


@pytest.mark.parametrize("digit,target", [("1", "demo_nova_hvac"), ("2", "demo_nova_garage"), ("3", "demo_nova_plumbing"), ("4", "demo_riverside")])
def test_a_key_press_moves_the_call_to_that_demo_and_its_spend_cap(app_client, digit, target):
    client, main = app_client
    from app import storage

    client.post("/voice/incoming?client_id=callkettle_demo", data={"CallSid": f"CA_SEL{digit}", "From": CALLER})
    r = client.post("/voice/demo-select?client_id=callkettle_demo", data={"CallSid": f"CA_SEL{digit}", "Digits": digit})
    assert f"/voice/incoming?client_id={target}" in r.text and "<Redirect" in r.text
    assert storage.get_call(f"CA_SEL{digit}")["client_id"] == target
    follow = client.post(f"/voice/incoming?client_id={target}", data={"CallSid": f"CA_SEL{digit}", "From": CALLER})
    assert "<Gather" in follow.text and "demo" in follow.text.lower() and "AI receptionist" in follow.text


@pytest.mark.parametrize("digits", ["", "7", "x", "#"])
def test_silence_or_a_wrong_key_starts_the_first_demo(app_client, digits):
    client, main = app_client
    r = client.post("/voice/demo-select?client_id=callkettle_demo", data={"CallSid": "CA_SELX", "Digits": digits})
    assert "client_id=demo_nova_hvac" in r.text


def test_the_menu_can_only_ever_lead_to_a_demo_client(app_client, monkeypatch):
    client, main = app_client
    from app.config import load_client_config

    real = load_client_config("callkettle_demo")
    hostile = real.model_copy(update={"demo_menu": {"1": "sample_homecare"}})          # a customer's live line
    monkeypatch.setattr(main, "load_client_config", lambda cid: hostile if cid == "callkettle_demo" else load_client_config(cid))
    r = client.post("/voice/demo-select?client_id=callkettle_demo", data={"CallSid": "CA_BAD", "Digits": "1"})
    assert "sample_homecare" not in r.text and "<Hangup" in r.text


def test_demo_select_is_refused_for_a_client_without_a_menu_and_when_unsigned(app_client, monkeypatch):
    client, main = app_client
    assert client.post("/voice/demo-select?client_id=demo_hvac", data={"CallSid": "CA_N", "Digits": "1"}).status_code == 404
    monkeypatch.setattr("app.twilio_utils._SKIP_SIGNATURE_CHECK", False)
    monkeypatch.setattr("app.twilio_utils._AUTH_TOKEN", "t" * 32)
    assert client.post("/voice/demo-select?client_id=callkettle_demo", data={"Digits": "1"}).status_code == 403


def test_a_demo_hand_off_is_simulated_and_never_rings_a_real_phone(app_client):
    client, main = app_client
    client.post("/voice/incoming?client_id=demo_nova_hvac", data={"CallSid": "CA_HO", "From": CALLER})
    r = client.post("/voice/gather?client_id=demo_nova_hvac&retry=0", data={"CallSid": "CA_HO", "From": CALLER, "SpeechResult": "let me talk to a real person"})
    assert "<Dial" not in r.text and "<Hangup" in r.text and "your own phone would ring" in r.text


def test_an_emergency_phrase_on_a_demo_is_still_handled_but_never_dials(app_client):
    client, main = app_client
    client.post("/voice/incoming?client_id=demo_nova_garage", data={"CallSid": "CA_EM", "From": CALLER})
    r = client.post("/voice/gather?client_id=demo_nova_garage&retry=0", data={"CallSid": "CA_EM", "From": CALLER, "SpeechResult": "I smell gas in my garage"})
    assert "911" in r.text and "<Dial" not in r.text


def test_every_demo_company_is_fictional_capped_disclosed_and_price_free():
    from app.config import list_client_ids, load_client_config

    menu = load_client_config("callkettle_demo").demo_menu
    assert set(menu.values()) >= {"demo_nova_hvac", "demo_nova_garage", "demo_nova_plumbing", "demo_riverside"}
    for target in menu.values():
        cfg = load_client_config(target)
        assert cfg.demo_mode and cfg.monthly_cost_ceiling_usd and cfg.monthly_cost_ceiling_usd <= 40 and cfg.ceiling_mode == "message"
        assert "AI" in cfg.opening_line and "demo" in cfg.opening_line.lower() or target == "demo_riverside"
        if target.startswith("demo_nova_"):
            assert cfg.business_name.startswith("Sample ")
            assert not any("$" in f.a for f in cfg.faqs), "a demo company must not quote prices"
            assert cfg.escalation_phone.startswith("+1")


def test_a_menu_without_demo_mode_is_rejected():
    from app.config import ClientConfig, load_client_config

    base = load_client_config("demo_hvac").model_dump()
    with pytest.raises(Exception):
        ClientConfig.model_validate({**base, "demo_menu": {"1": "demo_hvac"}})
    with pytest.raises(Exception):
        ClientConfig.model_validate({**base, "demo_mode": True, "demo_menu": {"12": "demo_hvac"}})
