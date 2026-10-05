"""Stream STT follow-ups (OFFLINE, flag-off): keyterm derivation + wiring, Endpointer use of speech_final / UtteranceEnd,
and the websockets pin. UNIT TESTED ONLY: no network, no provider, synthetic data. See docs/STREAM_STT_DESIGN.md."""
import base64
import json
import re
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from app import config as cfg

BACKEND = Path(__file__).resolve().parent.parent
ENV = {
    "CALLKETTLE_STREAM_STT_ENABLED": "1",
    "DEEPGRAM_API_KEY": "synthetic-not-a-real-key",
    "CALLKETTLE_STREAM_TOKEN_SECRET": "synthetic-token-secret-for-tests+15555550100",
}


def _s():
    from app import stream_stt
    return stream_stt


def _cfg(**update):
    return cfg.load_client_config("demo_nova_hvac").model_copy(update=update)


# ------------------------------------------------------------------ (1) keyterm derivation
def test_keyterms_come_from_business_name_services_and_service_area():
    terms = _s().derive_keyterms(_cfg())
    assert "Sample Heating & Air" in terms
    assert "AC or heating repair visit" in terms and "New system estimate" in terms
    for place in ("Northern Virginia", "Fairfax", "Arlington", "Alexandria", "Loudoun County"):
        assert place in terms
    assert terms[0] == "Sample Heating & Air"                      # business name first: survives any cap


def test_keyterms_are_deduplicated_case_insensitively_and_capped_at_50():
    s = _s()
    services = [cfg.Service(name=f"Service number {i}", duration_minutes=30) for i in range(80)]
    services += [cfg.Service(name="service number 1", duration_minutes=30)]
    terms = s.derive_keyterms(_cfg(services=services))
    assert len(terms) == 50 and len(terms) <= s.MAX_KEYTERMS
    assert len({t.lower() for t in terms}) == len(terms)


def test_keyterms_are_length_capped_per_term_on_a_word_boundary():
    s = _s()
    long_name = "Extraordinarily " * 20 + "Plumbing"
    terms = s.derive_keyterms(_cfg(business_name=long_name))
    assert terms and all(len(t) <= s.MAX_DERIVED_TERM_CHARS for t in terms)
    assert not terms[0].endswith("Extraordinaril")                 # not cut mid-word
    assert s.MAX_DERIVED_TERM_CHARS <= 60


@pytest.mark.parametrize("bad", [
    "Call+15555550100 now", "+15555550100", "owner@example.com", "AC check $89", "Tune-up 99 dollars", "Save 20% today",
    "see https://example.com", "www.example.com", "Tune-up for 49.99", "$••• plan",
])
def test_sensitive_looking_terms_are_dropped_not_passed_on(bad):
    services = [cfg.Service(name=bad, duration_minutes=30), cfg.Service(name="Drain cleaning", duration_minutes=30)]
    terms = _s().derive_keyterms(_cfg(services=services))
    assert "Drain cleaning" in terms
    joined = " | ".join(terms)
    assert not re.search(r"\d{3,}|@|\$|%|https?|www\.|dollar", joined, re.I)


def test_nothing_from_owner_contact_prices_or_faq_answers_leaks_into_keyterms():
    c = _cfg(escalation_phone="+15555550100", owner_email="owner@example.com", ntfy_topic="secret-topic-xyz",
             webhook_url="https://hooks.example.com/x", webhook_secret="s" * 20,
             calendar_ical_url="https://calendar.example.com/private-abc.ics",
             faqs=[cfg.Faq(q="What area do you serve?", a="We serve Fairfax and Reston. A visit costs $79, call+15555550100 or mail owner@example.com."),
                   cfg.Faq(q="How much is a repair?", a="Repairs start at $129 plus Vienna surcharge.")])
    terms = _s().derive_keyterms(c)
    blob = " | ".join(terms)
    for forbidden in ("9999", "owner@", "secret-topic", "hooks.example", "private-abc", "$", "129", "79", "0100", "a@b", "Vienna", "costs", "visit costs"):
        assert forbidden not in blob, forbidden
    assert "Fairfax" in terms and "Reston" in terms


def test_keyterms_never_contain_the_exact_price_private_or_not():
    # whatever the configs hold, no derived term is a number-with-currency or a long digit run (the monthly price is private)
    for cid in ("demo_nova_hvac", "demo_dental", "demo_nova_garage", "callkettle_demo", "callkettle_sales"):
        terms = _s().derive_keyterms(cfg.load_client_config(cid))
        assert terms, cid
        assert not any(re.search(r"\$|\d{3,}|@|https?:", t) for t in terms), (cid, terms)
        assert all(" ".join(t.split()) == t for t in terms)          # no stray whitespace/newlines/control characters


def test_control_characters_and_markup_are_stripped_from_terms():
    services = [cfg.Service(name="Furnace\n\tinstall\x00 <b>now</b>", duration_minutes=30)]
    terms = _s().derive_keyterms(_cfg(services=services))
    assert not any(re.search(r"[\x00-\x1f<>]", t) for t in terms)
    assert any("Furnace install" in t for t in terms)


def test_keyterms_with_no_faq_area_still_yield_name_and_services():
    terms = _s().derive_keyterms(_cfg(faqs=[]))
    assert terms[0] == "Sample Heating & Air" and "Seasonal maintenance tune-up" in terms


def test_keyterms_honor_an_explicit_service_areas_list_when_a_config_has_one():
    class WithAreas:
        business_name = "Acme Plumbing"
        services = []
        faqs = []
        service_areas = ["Reston", "Herndon", "reston", "Call+15555550100"]
    terms = _s().derive_keyterms(WithAreas())
    assert terms == ["Acme Plumbing", "Reston", "Herndon"]


def test_derive_keyterms_survives_a_malformed_config_object():
    class Weird:
        business_name = None
        services = [None, object()]
        faqs = "not a list"
    assert _s().derive_keyterms(Weird()) == []


def test_deepgram_url_uses_derived_keyterms_and_stays_inside_the_bound():
    s = _s()
    url = s.build_listen_url(keyterms=s.derive_keyterms(_cfg()))
    assert url.count("keyterm=") >= 5 and url.count("keyterm=") <= 50
    assert "DEEPGRAM" not in url and "synthetic" not in url


# ---------------------------------------------------------------- (1b) wiring in main.py
@pytest.fixture
def wired(app_client, monkeypatch):
    client, main = app_client
    s = _s()
    for k in list(ENV) + ["CALLKETTLE_STREAM_STT_KILL"]:
        monkeypatch.delenv(k, raising=False)

    class Updater:
        def update_call(self, call_sid, twiml):
            pass

    monkeypatch.setattr(s, "_UPDATER", Updater())
    seen = {"calls": []}

    class Fake(s.FakeSTT):
        transport_implemented = True

        def __init__(self, *args, **kwargs):
            seen["calls"].append((args, kwargs))
            super().__init__()

    monkeypatch.setattr(s, "PROVIDER_FACTORY", Fake)
    real = cfg.load_client_config
    monkeypatch.setattr(main, "load_client_config",
                        lambda cid: real(cid).model_copy(update={"stt_mode": "stream"}) if cid == "demo_nova_hvac" else real(cid))
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    return client, main, s, seen


def _drive(client):
    form = {"CallSid": "CA_K1", "From": "+155****4567", "To": "+155****0000"}
    text = client.post("/voice/incoming?client_id=demo_nova_hvac", data=form).text
    token = re.search(r'<Parameter name="token" value="([^"]+)"', text).group(1)
    start = {"event": "start", "start": {"callSid": "CA_K1", "streamSid": "MZ1", "customParameters": {"token": token, "client_id": "demo_nova_hvac"}}}
    media = {"event": "media", "media": {"track": "inbound", "payload": base64.b64encode(b"\xff" * 160).decode()}}
    with client.websocket_connect("/voice/stream") as ws:
        for e in (start, media, {"event": "stop"}):
            ws.send_text(json.dumps(e))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_main_passes_derived_keyterms_to_the_provider_factory(wired):
    client, main, s, seen = wired
    _drive(client)
    assert len(seen["calls"]) == 1
    args, kwargs = seen["calls"][0]
    assert args == () and list(kwargs) == ["keyterms"]
    assert kwargs["keyterms"] == s.derive_keyterms(cfg.load_client_config("demo_nova_hvac"))
    assert "Fairfax" in kwargs["keyterms"]


def test_a_failing_derivation_falls_back_to_gather_instead_of_crashing(wired, monkeypatch):
    client, main, s, seen = wired
    monkeypatch.setattr(s, "derive_keyterms", lambda config: (_ for _ in ()).throw(RuntimeError("boom")))
    _drive(client)                                                 # the route closes cleanly; no provider was created
    assert seen["calls"] == []


# ---------------------------------------------------------------- (2) Endpointer: speech_final / UtteranceEnd
def _ep(**kw):
    s = _s()
    return s.Endpointer(**kw), s


def test_speech_final_ends_the_utterance_without_waiting_for_our_silence_gap():
    ep, s = _ep(grace_s=0.0)      # legacy mode: complete at once on provider evidence (new default adds a grace, see test_stream_stt_endpoint_policy.py)
    ep.feed(s.Transcript("I need an appointment", True, speech_final=True), now=0.0)
    utt = ep.poll(0.0)
    assert utt is not None and utt.text == "I need an appointment"


def test_a_plain_final_without_speech_final_still_waits_for_the_silence_gap():
    ep, s = _ep()
    ep.feed(s.Transcript("I need an appointment", True), now=0.0)
    assert ep.poll(0.5) is None and ep.poll(1.0).text == "I need an appointment"


def test_speech_final_does_not_cut_off_a_later_final_that_arrives_first():
    ep, s = _ep()
    ep.feed(s.Transcript("my name is", True, speech_final=True), now=0.0)
    ep.feed(s.Transcript("sam ali", True), now=0.1)                # more speech before the turn was taken: evidence is stale
    assert ep.poll(0.5) is None
    assert ep.poll(1.1).text == "my name is sam ali"


def test_utterance_end_after_interim_only_stream_ends_the_utterance_early():
    ep, s = _ep(grace_s=0.0)      # legacy mode: complete at once on provider evidence (new default adds a grace, see test_stream_stt_endpoint_policy.py)
    ep.feed(s.Transcript("tuesday morning", False), now=0.0)
    assert ep.poll(1.0) is None                                    # interim only: normally waits 2x patience
    ep.utterance_end(now=1.0)
    assert ep.poll(1.0).text == "tuesday morning"


def test_utterance_end_with_nothing_pending_is_ignored_and_not_remembered():
    ep, s = _ep()
    ep.utterance_end(now=0.0)
    ep.feed(s.Transcript("hello", True), now=5.0)
    assert ep.poll(5.5) is None                                    # stale UtteranceEnd must not end a LATER utterance
    assert ep.poll(6.0).text == "hello"


def test_utterance_end_is_cleared_by_new_speech():
    ep, s = _ep()
    ep.feed(s.Transcript("tuesday", False), now=0.0)
    ep.utterance_end(now=0.5)
    ep.feed(s.Transcript("tuesday or wednesday", False), now=0.6)
    assert ep.poll(0.7) is None


def test_phone_number_patience_is_kept_even_when_the_provider_says_speech_final():
    from app.main import _speech_timeout_for
    ep, s = _ep()
    ep.expect(_speech_timeout_for("What's the best phone number to reach you?"))
    ep.feed(s.Transcript("five seven one", True, speech_final=True), now=0.0)   # provider endpoints on the digit-group pause
    assert ep.poll(0.0) is None and ep.poll(2.9) is None
    ep.feed(s.Transcript("two nine zero", True, speech_final=True), now=3.0)
    assert ep.poll(5.9) is None
    assert ep.poll(6.0).text == "five seven one two nine zero"


def test_phone_number_patience_is_kept_with_utterance_end_too():
    from app.main import _speech_timeout_for
    ep, s = _ep()
    ep.expect(_speech_timeout_for("What's your phone number?"))
    ep.feed(s.Transcript("five seven one", False), now=0.0)
    ep.utterance_end(now=1.0)
    assert ep.poll(2.9) is None
    assert ep.poll(3.0).text == "five seven one"                  # interim-only + evidence: 1x patience instead of 2x, never less


def test_silence_gap_fallback_is_unchanged_when_the_provider_sends_neither():
    ep, s = _ep()
    ep.feed(s.Transcript("tuesday morning", False), now=0.0)
    assert ep.poll(1.0) is None and ep.poll(2.0).text == "tuesday morning"
    ep.feed(s.Transcript("done", True), now=10.0)
    assert ep.poll(10.9) is None and ep.poll(11.0).text == "done"


def test_barge_in_is_unchanged_by_provider_evidence():
    ep, s = _ep(grace_s=0.0)      # legacy mode: complete at once on provider evidence (new default adds a grace, see test_stream_stt_endpoint_policy.py)
    ep.ai_speaking_until(10.0)
    assert ep.feed(s.Transcript("actually wait", True, speech_final=True), now=4.0) is True
    assert ep.feed(s.Transcript("I need tomorrow", True, speech_final=True), now=4.2) is False
    utt = ep.poll(4.2)
    assert utt.barge_in is True and utt.text == "actually wait I need tomorrow"


def test_runaway_cap_still_applies():
    ep, s = _ep(max_utterance_s=10.0)
    for i in range(12):
        ep.feed(s.Transcript(f"word{i}", False if i % 2 else True), now=float(i) * 0.9)
    assert ep.poll(10.5) is not None


# --- StreamCall plumbs the provider's UtteranceEnd counter into the Endpointer
class _Updater:
    def __init__(self):
        self.calls = []

    def update_call(self, call_sid, twiml):
        self.calls.append(twiml)


def _media_event(n=160):
    return {"event": "media", "media": {"track": "inbound", "payload": base64.b64encode(b"\xff" * n).decode()}}


class _CountingSTT(_s().FakeSTT):
    def feed(self, audio):
        super().feed(audio)
        if self.feeds == 2:
            self.utterance_ends += 1


def test_stream_call_turns_a_provider_utterance_end_into_an_early_turn():
    s = _s()
    stt = _CountingSTT(script={1: [s.Transcript("tuesday morning", False)]})
    clock = {"t": 0.0}
    call = s.StreamCall(call_sid="CA1", client_id="demo_dental", stt=stt, updater=_Updater(),
                        turn_handler=lambda text: s.TurnResult("<Response/>", "ok", False), fallback_twiml=lambda: "<Response/>",
                        clock=lambda: clock["t"], endpointer=s.Endpointer(grace_s=0.0))
    assert call.handle_event(_media_event()) is None
    clock["t"] = 0.2
    utt = call.handle_event(_media_event())                        # second frame: provider reports UtteranceEnd
    assert utt is not None and utt.text == "tuesday morning"


def test_stream_call_without_utterance_end_support_behaves_as_before():
    s = _s()
    stt = s.FakeSTT(script={1: [s.Transcript("tuesday morning", False)]})
    assert stt.utterance_ends == 0                                 # interface default
    clock = {"t": 0.0}
    call = s.StreamCall(call_sid="CA1", client_id="demo_dental", stt=stt, updater=_Updater(),
                        turn_handler=lambda text: s.TurnResult("<Response/>", "ok", False), fallback_twiml=lambda: "<Response/>",
                        clock=lambda: clock["t"])
    call.handle_event(_media_event())
    clock["t"] = 1.0
    assert call.handle_event(_media_event()) is None
    clock["t"] = 2.0
    assert call.handle_event(_media_event()).text == "tuesday morning"


# ---------------------------------------------------------------- (3) websockets pin
def test_websockets_is_pinned_to_the_installed_version_in_requirements():
    from importlib.metadata import version
    lines = [l.strip() for l in (BACKEND / "requirements.txt").read_text().splitlines()]
    pins = [l for l in lines if re.match(r"websockets\s*==", l, re.I)]
    assert pins == [f"websockets=={version('websockets')}"]


def test_every_requirements_line_still_uses_the_exact_pin_style():
    for line in (BACKEND / "requirements.txt").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            assert re.fullmatch(r"[A-Za-z0-9_.\-]+(\[[a-z,]+\])?==[0-9][A-Za-z0-9.]*", line.strip()), line
