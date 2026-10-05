"""Stream-STT endpointing POLICY (OFFLINE, flag-off). Pins how the Endpointer treats a bare speech_final / UtteranceEnd so a caller
who pauses mid-thought is not answered early, while a caller who finishes still gets a quick reply. Never contacts a provider.

Replays hand-built event sequences and RECORDED provider timelines (tests/fixtures/stream_stt/timelines/*.json, SYNTHETIC SAPI
audio sent once to real Deepgram by scripts/stream_policy_eval.py) into the Endpointer on an injected virtual clock."""
import base64
import importlib.util
import json
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
TIMELINES = HERE / "fixtures" / "stream_stt" / "timelines"


def _s():
    from app import stream_stt
    return stream_stt


def _ep(**kw):
    s = _s()
    return s.Endpointer(**kw), s


def _eval_mod():
    path = HERE.parent / "scripts" / "stream_policy_eval.py"
    spec = importlib.util.spec_from_file_location("stream_policy_eval", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["stream_policy_eval"] = mod
    spec.loader.exec_module(mod)
    return mod


def _timeline(name):
    return json.loads((TIMELINES / f"{name}.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ mechanism: grace after a bare speech_final / UtteranceEnd
def test_default_grace_is_zero_so_an_explicit_endpointer_keeps_the_old_immediate_behaviour():
    ep, s = _ep(grace_s=0.0)
    ep.feed(s.Transcript("I need an appointment.", True, speech_final=True), now=0.0)
    assert ep.poll(0.0).text == "I need an appointment."


def test_a_speech_final_sentence_waits_for_the_grace_and_is_joined_with_what_follows():
    ep, s = _ep(grace_s=1.1)
    ep.feed(s.Transcript("My air conditioner stopped cooling.", True, speech_final=True), now=0.0)
    assert ep.poll(1.0) is None                                     # caller may be mid-thought
    ep.feed(s.Transcript("It is also loud", False), now=1.05)       # more speech inside the grace
    assert ep.poll(1.2) is None
    ep.feed(s.Transcript("It is also loud.", True, speech_final=True), now=2.0)
    assert ep.poll(2.9) is None
    utt = ep.poll(3.1)
    assert utt.text == "My air conditioner stopped cooling. It is also loud."


def test_a_finished_caller_is_answered_as_soon_as_the_grace_has_passed():
    ep, s = _ep(grace_s=1.1)
    ep.feed(s.Transcript("Hi, my water heater is leaking.", True, speech_final=True), now=0.0)
    assert ep.poll(1.09) is None
    assert ep.poll(1.1).text == "Hi, my water heater is leaking."


def test_a_question_gets_a_shorter_grace_than_a_plain_statement():
    ep, s = _ep(grace_s=1.1, question_grace_s=0.5)
    ep.feed(s.Transcript("Do you service Arlington?", True, speech_final=True), now=0.0)
    assert ep.poll(0.49) is None
    assert ep.poll(0.5).text == "Do you service Arlington?"


def test_text_that_ends_open_gets_extra_grace_comma_no_terminal_punctuation_or_dangling_words():
    for text in ("My name is John Smith,", "My name is John Smith", "my number is seven one three", "I live in Fairfax and"):
        ep, s = _ep(grace_s=1.1, open_grace_s=2.5)
        ep.feed(s.Transcript(text, True, speech_final=True), now=0.0)
        assert ep.poll(2.4) is None, text
        assert ep.poll(2.5).text == text


def test_a_digit_run_still_in_progress_gets_extra_grace_but_a_complete_number_does_not():
    ep, s = _ep(grace_s=1.1, open_grace_s=2.5)
    ep.feed(s.Transcript("My number is (713) 555.", True, speech_final=True), now=0.0)    # 6 digits: more are coming
    assert ep.poll(2.4) is None and ep.poll(2.5) is not None
    ep, s = _ep(grace_s=1.1, open_grace_s=2.5)
    ep.feed(s.Transcript("My number is+15555550100.", True, speech_final=True), now=0.0)  # 10 digits: complete
    assert ep.poll(1.1).text == "My number is+15555550100."


def test_digits_spoken_as_words_across_several_finals_count_toward_a_complete_number():
    ep, s = _ep(grace_s=1.1, open_grace_s=2.5)
    for i, chunk in enumerate(("seven one three.", "five five five.", "zero one four two.")):
        ep.feed(s.Transcript(chunk, True, speech_final=True), now=float(i))
    assert ep.poll(2.5) is None                                      # 10 digits reached at t=2.0, normal grace runs from there
    assert ep.poll(3.1).text == "seven one three. five five five. zero one four two."


def test_utterance_end_follows_the_same_grace_as_speech_final():
    ep, s = _ep(grace_s=1.1, open_grace_s=2.5)
    ep.feed(s.Transcript("My name is John,", True), now=0.0)
    ep.utterance_end(now=1.0)
    assert ep.poll(1.0) is None and ep.poll(2.4) is None
    assert ep.poll(2.5).text == "My name is John,"


def test_phone_number_patience_still_wins_over_the_grace():
    ep, s = _ep(grace_s=1.1)
    ep.expect("3")
    ep.feed(s.Transcript("seven one three.", True, speech_final=True), now=0.0)
    assert ep.poll(2.9) is None and ep.poll(3.0) is not None


# ------------------------------------------------------------------ SpeechStarted (provider voice-activity onset)
def test_speech_started_inside_the_grace_holds_the_turn_until_the_next_words_arrive():
    ep, s = _ep(grace_s=1.1, speech_hold_s=2.0)
    ep.feed(s.Transcript("My air conditioner stopped cooling.", True, speech_final=True), now=0.0)
    ep.speech_started(now=0.7)                                      # caller resumed; the first words are still being recognised
    assert ep.poll(1.5) is None and ep.poll(1.9) is None
    ep.feed(s.Transcript("It is also loud.", True, speech_final=True), now=2.0)
    assert ep.poll(3.2).text == "My air conditioner stopped cooling. It is also loud."


def test_speech_started_with_nothing_pending_is_ignored_and_does_not_delay_a_later_turn():
    ep, s = _ep(grace_s=1.1, speech_hold_s=2.0)
    ep.speech_started(now=0.0)
    ep.feed(s.Transcript("Hello there.", True, speech_final=True), now=5.0)
    assert ep.poll(6.2).text == "Hello there."


def test_speech_hold_is_bounded_noise_cannot_hold_a_turn_forever():
    ep, s = _ep(grace_s=1.1, speech_hold_s=2.0)
    ep.feed(s.Transcript("Hello there.", True, speech_final=True), now=0.0)
    ep.speech_started(now=0.5)
    assert ep.poll(2.4) is None
    assert ep.poll(2.5).text == "Hello there."                      # no words ever followed the noise


# ------------------------------------------------------------------ barge-in is unchanged: only real caller speech over the AI cuts it
def test_barge_in_still_fires_once_when_the_caller_speaks_over_the_ai():
    ep, s = _ep(grace_s=1.1)
    ep.ai_speaking_until(10.0)
    assert ep.feed(s.Transcript("wait", False), now=4.0) is True
    assert ep.feed(s.Transcript("wait actually", True, speech_final=True), now=4.2) is False
    utt = ep.poll(6.0)
    assert utt.barge_in is True and utt.text == "wait actually"


def test_speech_started_alone_while_the_ai_talks_is_not_a_barge_in():
    ep, s = _ep(grace_s=1.1, speech_hold_s=2.0)
    ep.ai_speaking_until(10.0)
    ep.speech_started(now=4.0)                                      # a cough / line noise: no words, no cut
    assert ep.poll(9.0) is None


# ------------------------------------------------------------------ adapter + StreamCall plumbing
def test_vad_events_param_is_only_in_the_url_when_asked_for():
    from urllib.parse import parse_qs, urlsplit
    s = _s()
    assert "vad_events" not in parse_qs(urlsplit(s.build_listen_url()).query)
    assert parse_qs(urlsplit(s.build_listen_url(vad_events=True)).query)["vad_events"] == ["true"]


def test_deepgram_adapter_counts_speech_started_messages():
    s = _s()

    class Conn:
        def __init__(self):
            self.inbox = [json.dumps({"type": "SpeechStarted", "channel": [0, 1], "timestamp": 1.2})]
        def send_bytes(self, d): pass
        def send_text(self, t): pass
        def recv(self, timeout=0.0): return self.inbox.pop(0) if self.inbox else None
        def close(self): pass

    stt = s.DeepgramSTT({"DEEPGRAM_API_KEY": "synthetic", s.ENV_ENABLE: "1"}, connect=lambda url, headers: Conn(), vad_events=True)
    stt.feed(b"\xff" * 160)
    assert stt.poll() == [] and stt.speech_starts == 1
    assert s.StreamingSTT.speech_starts == 0                         # providers without the signal leave it at 0


class _Updater:
    def update_call(self, call_sid, twiml):
        pass


def _media(n=160):
    return {"event": "media", "media": {"track": "inbound", "payload": base64.b64encode(b"\xff" * n).decode()}}


def test_stream_call_turns_a_provider_speech_started_into_a_hold():
    s = _s()

    class STT(s.FakeSTT):
        def feed(self, audio):
            super().feed(audio)
            if self.feeds == 2:
                self.speech_starts += 1

    stt = STT(script={1: [s.Transcript("My air conditioner stopped cooling.", True, speech_final=True)]})
    clock = {"t": 0.0}
    call = s.StreamCall(call_sid="CA1", client_id="demo_dental", stt=stt, updater=_Updater(),
                        turn_handler=lambda text: s.TurnResult("<Response/>", "ok", False), fallback_twiml=lambda: "<Response/>",
                        clock=lambda: clock["t"], endpointer=s.Endpointer(grace_s=1.1, speech_hold_s=2.0))
    assert call.handle_event(_media()) is None
    clock["t"] = 0.4
    assert call.handle_event(_media()) is None                       # speech resumed
    clock["t"] = 1.6
    assert call.handle_event(_media()) is None                       # grace would have passed, hold keeps the turn open
    clock["t"] = 2.5
    assert call.handle_event(_media()).text == "My air conditioner stopped cooling."


# ------------------------------------------------------------------ the chosen DEFAULTS, on RECORDED real-Deepgram timelines (synthetic audio)
RECORDINGS = {
    "sentence_pause": ("sentence_pause_e300_vad", 1, None),   # 0.6 s gap between two sentences: ONE utterance
    "phone_digits": ("phone_digits_e300_vad", 1, None),       # name, 1.0 s, number in groups 0.7 s / 0.5 s: ONE utterance
    "clean_single": ("clean_single_e300", 1, 1.5),            # finished caller: one utterance, quick
    "question_stop": ("question_stop_e300", 1, 1.5),
}


@pytest.mark.parametrize("clip", sorted(RECORDINGS))
def test_default_policy_on_recorded_timelines_has_no_cut_offs_and_low_latency(clip):
    mod, s = _eval_mod(), _s()
    name, expected_utterances, max_latency = RECORDINGS[clip]
    result = mod.score(_timeline(name), s.Endpointer())
    assert result["utterances"] == expected_utterances, result
    if max_latency is not None:
        assert result["latency_s"] <= max_latency, result


def test_default_policy_latency_on_pause_clips_is_bounded_too():
    mod, s = _eval_mod(), _s()
    for name in ("sentence_pause_e300_vad", "phone_digits_e300_vad"):
        assert mod.score(_timeline(name), s.Endpointer())["latency_s"] <= 2.0, name


def test_the_old_immediate_policy_cut_the_caller_off_on_the_same_recordings():
    """Regression anchor: grace_s=0 is the old behaviour and splits both pause clips (this is the bug being fixed)."""
    mod, s = _eval_mod(), _s()
    for name in ("sentence_pause_e300", "phone_digits_e300"):
        assert mod.score(_timeline(name), s.Endpointer(grace_s=0.0, speech_hold_s=0.0))["cut_offs"] >= 1, name


def test_production_defaults_are_unchanged_for_gather_clients():
    """Stream mode is flag-off; the gate and the Gather TwiML are not touched by this tuning."""
    s = _s()
    assert s.stream_mode_active(type("C", (), {"stt_mode": "gather"})(), {}) is False


def test_chosen_default_constants_are_pinned():
    s = _s()
    assert (s.DEFAULT_ENDPOINTING_MS, s.DEFAULT_GRACE_S, s.DEFAULT_QUESTION_GRACE_S, s.DEFAULT_OPEN_GRACE_S, s.DEFAULT_SPEECH_HOLD_S) == (300, 0.8, 0.5, 1.5, 2.0)
    assert s.DEFAULT_VAD_EVENTS is True
    from urllib.parse import parse_qs, urlsplit
    stt = s.DeepgramSTT({"DEEPGRAM_API_KEY": "synthetic", s.ENV_ENABLE: "1"}, connect=lambda u, h: None)
    assert parse_qs(urlsplit(stt._url).query)["vad_events"] == ["true"]


def test_phone_number_prompt_keeps_its_three_second_patience_with_the_new_defaults():
    ep, s = _ep()
    ep.expect("3")
    ep.feed(s.Transcript("seven one three.", True, speech_final=True), now=0.0)
    assert ep.poll(2.9) is None and ep.poll(3.0) is not None


def test_utterance_shape_cases():
    s = _s()
    shape = s.utterance_shape
    assert shape("Do you service Arlington?") == "question"
    assert shape("My water heater is leaking.") == "complete"
    assert shape("Yes.") == "complete"
    assert shape("My name is John,") == "open"
    assert shape("Tuesday morning") == "open"
    assert shape("I live in Fairfax and.") == "open"
    assert shape("seven one three.") == "open"
    assert shape("My number is+15555550100.", "My number is+15555550100.") == "complete"
    assert shape("") == "open"


def test_policy_eval_script_refuses_without_flag_key_or_paths(capsys):
    mod = _eval_mod()
    key = "synthetic-not-a-real-key"
    assert mod.main(["record", "a.wav", "o.json"], {"DEEPGRAM_API_KEY": key}) == 2
    assert mod.main(["record", mod.CONFIRM_FLAG, "a.wav", "o.json"], {}) == 2
    assert mod.main(["record", mod.CONFIRM_FLAG, "a.wav"], {"DEEPGRAM_API_KEY": key}) == 2
    assert mod.main([], {}) == 2
    assert key not in capsys.readouterr().err


def test_recorded_timelines_hold_no_secret_and_are_small():
    for path in TIMELINES.glob("*.json"):
        raw = path.read_text(encoding="utf-8")
        assert len(raw) < 20000 and "Token " not in raw and "Authorization" not in raw
