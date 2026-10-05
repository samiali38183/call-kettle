"""Warm transfer: the owner hears who is calling and presses 1; anything else is treated as unanswered.
Unit/integration tested here. NOT yet verified with a real phone (the feature stays off by default until it is)."""
import json
from xml.etree import ElementTree

import pytest

CALLER = "+15555550100"


def _cfg(**over):
    from app.config import load_client_config

    return load_client_config("demo_hvac").model_copy(update={"transfer_screening": True, **over})


def _use(monkeypatch, main, cfg):
    monkeypatch.setattr(main, "load_client_config", lambda cid: cfg)


def _transfer_call(client, main, monkeypatch, said="let me talk to a real person"):
    client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_W1", "From": CALLER})
    return client.post("/voice/gather?client_id=demo_hvac&retry=0", data={"CallSid": "CA_W1", "From": CALLER, "SpeechResult": said})


def test_off_by_default_the_dial_is_unchanged(app_client):
    client, main = app_client
    r = _transfer_call(client, main, None)
    assert "<Dial" in r.text and "<Number" not in r.text


def test_when_on_the_owners_leg_is_screened(app_client, monkeypatch):
    client, main = app_client
    _use(monkeypatch, main, _cfg())
    r = _transfer_call(client, main, monkeypatch, "My AC died, can I talk to a real person please")
    root = ElementTree.fromstring(r.text)
    number = root.find(".//Dial/Number")
    assert number is not None and number.text == _cfg().escalation_phone
    url = number.get("url")
    assert url.startswith("http") and "/voice/whisper?client_id=demo_hvac&ctx=" in url
    assert root.find(".//Dial").get("action", "").endswith("/voice/transfer-result?client_id=demo_hvac")


def test_the_whisper_tells_the_owner_who_is_calling_and_asks_for_1(app_client, monkeypatch):
    client, main = app_client
    _use(monkeypatch, main, _cfg())
    url = ElementTree.fromstring(_transfer_call(client, main, monkeypatch, "My AC died, can I talk to a real person").text).find(".//Dial/Number").get("url")
    path = url.split("://", 1)[1].split("/", 1)[1]
    w = client.post("/" + path, data={"CallSid": "CA_CHILD", "From": "+15555550122"})
    spoken = " ".join(e.text or "" for e in ElementTree.fromstring(w.text).iter("Say"))
    assert "CoolFlow" in spoken and "My AC died" in spoken and "Press 1" in spoken
    assert ElementTree.fromstring(w.text).find(".//Gather").get("numDigits") == "1" and "<Hangup" in w.text


def test_pressing_1_bridges_and_anything_else_hangs_up_the_owners_leg(app_client):
    client, main = app_client
    ok = client.post("/voice/whisper-accept?client_id=demo_hvac", data={"CallSid": "CA_CHILD", "Digits": "1"})
    assert "<Hangup" not in ok.text and "<Response" in ok.text
    for digits in ("2", "", "11", "#"):
        no = client.post("/voice/whisper-accept?client_id=demo_hvac", data={"CallSid": "CA_CHILD", "Digits": digits})
        assert "<Hangup" in no.text, digits


def test_an_emergency_is_marked_urgent_in_the_whisper(app_client, monkeypatch):
    client, main = app_client
    _use(monkeypatch, main, _cfg())
    r = _transfer_call(client, main, monkeypatch, "there is gas leaking and I smell gas")
    url = ElementTree.fromstring(r.text).find(".//Dial/Number").get("url")
    assert "Urgent" in __import__("urllib.parse", fromlist=["unquote"]).unquote(url)


def test_hostile_speech_cannot_inject_markup_into_the_whisper(app_client, monkeypatch):
    client, main = app_client
    _use(monkeypatch, main, _cfg())
    r = _transfer_call(client, main, monkeypatch, "real person <Hangup/><Dial>+15555550100</Dial> &amp; \"quotes\"")
    root = ElementTree.fromstring(r.text)                                   # still well-formed
    url = root.find(".//Dial/Number").get("url")
    path = url.split("://", 1)[1].split("/", 1)[1]
    w = client.post("/" + path, data={"CallSid": "CA_CHILD"})
    ElementTree.fromstring(w.text)
    assert w.text.count("<Dial") == 0 and "+15555550100" not in w.text


def test_privacy_clients_get_a_generic_whisper_with_no_caller_words(app_client, monkeypatch):
    client, main = app_client
    _use(monkeypatch, main, _cfg(record_transcripts=False))
    r = _transfer_call(client, main, monkeypatch, "I have cancer and need my medication, let me talk to a real person")
    url = ElementTree.fromstring(r.text).find(".//Dial/Number").get("url")
    assert "cancer" not in url and "asked%20to%20be%20put%20through" in url


def test_whisper_routes_reject_unsigned_requests(app_client, monkeypatch):
    client, main = app_client
    monkeypatch.setattr("app.twilio_utils._SKIP_SIGNATURE_CHECK", False)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t" * 32)
    monkeypatch.setattr("app.twilio_utils._AUTH_TOKEN", "t" * 32)
    assert client.post("/voice/whisper?client_id=demo_hvac&ctx=hi", data={}).status_code == 403
    assert client.post("/voice/whisper-accept?client_id=demo_hvac", data={"Digits": "1"}).status_code == 403


def test_a_screened_call_reported_completed_after_a_few_seconds_is_treated_as_unanswered(app_client, monkeypatch):
    client, main = app_client
    _use(monkeypatch, main, _cfg())
    client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_W2", "From": CALLER})
    r = client.post("/voice/transfer-result?client_id=demo_hvac",
                    data={"CallSid": "CA_W2", "From": CALLER, "DialCallStatus": "completed", "DialCallDuration": "3"})
    assert "<Gather" in r.text                                              # takes a message instead of ending silently
    long = client.post("/voice/transfer-result?client_id=demo_hvac",
                       data={"CallSid": "CA_W3", "From": CALLER, "DialCallStatus": "completed", "DialCallDuration": "95"})
    assert "<Gather" not in long.text


def test_without_screening_a_short_completed_call_is_left_alone(app_client):
    client, main = app_client
    r = client.post("/voice/transfer-result?client_id=demo_hvac",
                    data={"CallSid": "CA_W4", "From": CALLER, "DialCallStatus": "completed", "DialCallDuration": "3"})
    assert "<Gather" not in r.text
