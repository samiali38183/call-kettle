"""Spanish (beta): press 2, es-US recognition, native Spanish voice, Spanish safety nets,
and a clean bilingual hand-off for clients that have not turned it on."""
import os

import pytest

os.environ["CALLKETTLE_SKIP_SIGNATURE_CHECK"] = "1"

from tests.test_agent import FakeAnthropicClient, FakeResponse, FakeTextBlock  # noqa: E402


# ------------------------------------------------------------------ TwiML

def test_spanish_gather_uses_the_spanish_recognizer_and_a_native_voice():
    from app import twilio_utils

    xml = twilio_utils.gather_twiml(say_text="¿En qué le puedo ayudar?", action_url="https://x/g", lang="es")
    assert 'language="es-US"' in xml.split("<Say")[0]                      # the Gather itself
    assert 'voice="Polly.Lupe-Neural" language="es-US"' in xml
    assert "Polly.Joanna" not in xml                                        # never the English voice for Spanish words


def test_english_gather_is_unchanged_and_does_not_accept_key_presses():
    from app import twilio_utils

    xml = twilio_utils.gather_twiml(say_text="Hi", action_url="https://x/g")
    assert 'input="speech"' in xml and "numDigits" not in xml and 'language="en-US"' in xml


def test_press_two_prompt_is_spoken_in_spanish_inside_the_gather():
    from app import twilio_utils

    xml = twilio_utils.gather_twiml(say_text="Thanks for calling.", action_url="https://x/g", dtmf_prompt="Para español, oprima dos.")
    gather = xml.split("<Gather")[1].split("</Gather>")[0]
    assert 'input="speech dtmf"' in gather and 'numDigits="1"' in gather
    assert "Para español, oprima dos." in gather and "Lupe" in gather and "Joanna" in gather


def test_bilingual_text_is_read_by_the_right_voice_for_each_language():
    from app import twilio_utils

    xml = twilio_utils._say("Sorry. [es]Lo siento.[/es] Goodbye.")
    voices = [v for v in xml.split('voice="')[1:]]
    assert [v.split('"')[0] for v in voices] == ["Polly.Joanna-Neural", "Polly.Lupe-Neural", "Polly.Joanna-Neural"]


def test_phone_numbers_are_spelled_in_spanish_for_the_spanish_voice():
    from app import twilio_utils

    assert twilio_utils.speakable("+15555550100", "es") == "siete cero tres, cinco cinco cinco, cero uno cuatro dos"
    assert twilio_utils.speakable("+15555550100") == "seven zero three, five five five, zero one four two"


# ------------------------------------------------------------------ detection

@pytest.mark.parametrize("text", [
    "Hola, necesito ayuda con mi casa", "¿Habla español?", "do you speak spanish", "hablan espanol",
    "tengo un problema con el aire acondicionado por favor", "Buenas tardes, quiero una cita",
])
def test_spanish_speakers_are_recognized(text):
    from app.agent import looks_spanish

    assert looks_spanish(text)


@pytest.mark.parametrize("text", [
    "My AC stopped cooling", "I need a plumber tomorrow", "Hello, is this a good time?", "thanks, gracias",
    "my mom's house on Casa Grande Drive", "Can I get a quote for the estimate",
])
def test_english_speakers_are_never_mistaken_for_spanish(text):
    from app.agent import looks_spanish

    assert not looks_spanish(text)


# ------------------------------------------------------------------ agent behaviour

def _session(spanish_client=False, lang="en"):
    from app import agent
    from app.config import load_client_config

    cfg = load_client_config("demo_hvac").model_copy(update={"spanish": spanish_client})
    s = agent.start_session("CA_ES_" + str(id(object())), cfg, "+15555550100")
    s.lang = lang
    return s, cfg


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("CALLKETTLE_DB_PATH", str(tmp_path / "es.db"))
    from app import storage

    importlib.reload(storage)
    storage.init_db()


def test_english_only_client_gives_a_clean_bilingual_handoff_without_calling_the_model(monkeypatch):
    from app import agent

    fake = FakeAnthropicClient([])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)
    s, cfg = _session()
    reply, ended, transfer = agent.run_turn(s, "Hola, necesito ayuda con mi casa")
    assert ended is True and transfer is None
    assert "only help in English" in reply and "[es]" in reply and "solo puedo ayudar en ingl" in reply
    assert len(fake.messages.calls) == 0
    import sqlite3

    from app import storage

    conn = sqlite3.connect(storage.DB_PATH)
    assert conn.execute("SELECT reason FROM escalations WHERE call_sid=?", (s.call_sid,)).fetchone()[0] == "non_english_caller"
    conn.close()


def test_spanish_enabled_client_offers_to_continue_in_spanish(monkeypatch):
    from app import agent

    monkeypatch.setattr(agent, "_anthropic_client", lambda: FakeAnthropicClient([]))
    s, cfg = _session(spanish_client=True)
    reply, ended, transfer = agent.run_turn(s, "Hola, necesito un plomero")
    assert ended is False and transfer is None and s.offer_spanish is True
    assert "press 2" in reply and "[es]" in reply


def test_a_spanish_gas_leak_gets_911_advice_in_every_mode(monkeypatch):
    from app import agent

    monkeypatch.setattr(agent, "_anthropic_client", lambda: FakeAnthropicClient([]))
    s, cfg = _session()                                       # English mode, Spanish words
    reply, ended, transfer = agent.run_turn(s, "hay una fuga de gas en mi casa")
    assert "911" in reply and "[es]" in reply and "Cuelgue y llame al 911" in reply
    assert ended is True and transfer == cfg.escalation_phone
    s2, _ = _session(lang="es")                              # Spanish mode: Spanish only
    reply2, _, t2 = agent.run_turn(s2, "huele a gas")
    assert reply2.startswith("Esto parece una emergencia") and "911" in reply2 and t2 == cfg.escalation_phone


def test_spanish_request_for_a_person_transfers_without_the_model(monkeypatch):
    from app import agent

    fake = FakeAnthropicClient([])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)
    s, cfg = _session(lang="es")
    reply, ended, transfer = agent.run_turn(s, "quiero hablar con una persona")
    assert transfer == cfg.escalation_phone and reply == "Claro, le conecto ahora mismo." and len(fake.messages.calls) == 0


def test_the_spanish_prompt_and_english_prompt_differ_only_by_the_language_block():
    from app import agent
    from app.config import load_client_config

    cfg = load_client_config("demo_hvac")
    en = agent.build_system_prompt(cfg, "+15555550100")
    es = agent.build_system_prompt(cfg, "+15555550100", lang="es")
    assert "SPANISH" not in en and "SPANISH" in es and "ONLY Spanish" in es


def test_spanish_replies_come_from_the_model_with_the_spanish_prompt(monkeypatch):
    from app import agent

    fake = FakeAnthropicClient([FakeResponse([FakeTextBlock("Claro, ¿qué día le conviene?")], "end_turn")])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)
    s, cfg = _session(spanish_client=True, lang="es")
    reply, ended, _ = agent.run_turn(s, "necesito una cita para mi aire acondicionado")
    assert reply == "Claro, ¿qué día le conviene?" and not ended
    assert "ONLY Spanish" in fake.messages.calls[0]["system"]


# ------------------------------------------------------------------ routes (press 2)

def _spanish_client(monkeypatch, main, enabled=True):
    from app import config as config_module

    cfg = config_module.load_client_config("demo_hvac").model_copy(update={"spanish": enabled})
    monkeypatch.setattr(main, "load_client_config", lambda cid: cfg)
    return cfg


def test_opening_invites_spanish_speakers_only_when_the_client_turned_it_on(app_client, monkeypatch):
    client, main = app_client
    _spanish_client(monkeypatch, main, enabled=True)
    on = client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_E1", "From": "+15555550100"}).text
    assert 'input="speech dtmf"' in on and "Para español, oprima dos." in on
    _spanish_client(monkeypatch, main, enabled=False)
    off = client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_E2", "From": "+15555550100"}).text
    assert 'input="speech"' in off and "español" not in off


def test_pressing_two_switches_the_rest_of_the_call_to_spanish(app_client, monkeypatch):
    client, main = app_client
    from app import agent

    _spanish_client(monkeypatch, main)
    client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_E3", "From": "+15555550100"})
    r = client.post("/voice/gather?client_id=demo_hvac&retry=0", data={"CallSid": "CA_E3", "From": "+15555550100", "Digits": "2"})
    assert 'language="es-US"' in r.text and "Con gusto le ayudo en español" in r.text and "Lupe" in r.text
    assert agent.get_session("CA_E3").lang == "es"
    # the next turn keeps the Spanish recognizer and voice
    monkeypatch.setattr(agent, "_anthropic_client", lambda: FakeAnthropicClient([FakeResponse([FakeTextBlock("¿Qué día le conviene?")], "end_turn")]))
    r2 = client.post("/voice/gather?client_id=demo_hvac&retry=0", data={"CallSid": "CA_E3", "From": "+15555550100", "SpeechResult": "necesito una cita"})
    assert 'language="es-US"' in r2.text and "¿Qué día le conviene?" in r2.text and "Joanna" not in r2.text


def test_pressing_two_does_nothing_when_spanish_is_off(app_client, monkeypatch):
    client, main = app_client
    from app import agent

    _spanish_client(monkeypatch, main, enabled=False)
    client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_E4", "From": "+15555550100"})
    r = client.post("/voice/gather?client_id=demo_hvac&retry=0", data={"CallSid": "CA_E4", "From": "+15555550100", "Digits": "2"})
    assert 'language="es-US"' not in r.text
    assert agent.get_session("CA_E4").lang == "en"


def test_a_spanish_speaker_who_did_not_press_two_is_offered_the_key(app_client, monkeypatch):
    client, main = app_client
    _spanish_client(monkeypatch, main)
    client.post("/voice/incoming?client_id=demo_hvac", data={"CallSid": "CA_E5", "From": "+15555550100"})
    r = client.post("/voice/gather?client_id=demo_hvac&retry=0", data={"CallSid": "CA_E5", "From": "+15555550100", "SpeechResult": "hola necesito ayuda"})
    assert 'input="speech dtmf"' in r.text and "press 2" in r.text and "Lupe" in r.text
    r2 = client.post("/voice/gather?client_id=demo_hvac&retry=0", data={"CallSid": "CA_E5", "From": "+15555550100", "Digits": "2"})
    assert 'language="es-US"' in r2.text


def test_an_empty_model_reply_is_repeated_in_the_callers_language(monkeypatch):
    from app import agent

    empty = FakeResponse([], "end_turn")
    monkeypatch.setattr(agent, "_anthropic_client", lambda: FakeAnthropicClient([empty, empty]))
    s_es, _ = _session(spanish_client=True, lang="es")
    assert agent.run_turn(s_es, "necesito una cita")[0] == "Perdón, ¿puede repetirlo?"
    s_en, _ = _session()
    assert agent.run_turn(s_en, "I need an appointment")[0] == "Sorry, could you say that again?"
