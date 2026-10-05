"""Stream STT (OFFLINE, flag-off): pure units. No network, no provider, no Twilio. Synthetic data only.

Status of everything here: UNIT TESTED ONLY. Never run with real audio (docs/STREAM_STT_DESIGN.md).
"""
import base64
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from app import config as cfg
from app import cost_observability as co

BACKEND = Path(__file__).resolve().parent.parent
ALL_ENV = {
    "CALLKETTLE_STREAM_STT_ENABLED": "1",
    "DEEPGRAM_API_KEY": "synthetic-not-a-real-key",
    "CALLKETTLE_STREAM_TOKEN_SECRET": "synthetic-token-secret-for-tests+15555550100",
}


def _stt():
    from app import stream_stt
    return stream_stt


def _config(mode="gather"):
    return cfg.load_client_config("demo_dental").model_copy(update={"stt_mode": mode})


class _ReadyFake:
    """A provider class that claims a working transport (the real Deepgram skeleton does not)."""
    transport_implemented = True


# ---------------------------------------------------------------- config flag
def test_stt_mode_defaults_to_gather_and_rejects_unknown_values():
    base = cfg.load_client_config("demo_dental").model_dump()
    assert cfg.ClientConfig.model_validate(base).stt_mode == "gather"
    assert cfg.ClientConfig.model_validate({**base, "stt_mode": "stream"}).stt_mode == "stream"
    with pytest.raises(Exception):
        cfg.ClientConfig.model_validate({**base, "stt_mode": "deepgram"})


def test_config_version_of_a_default_client_is_unchanged_by_the_new_field():
    """config_version fingerprints model_dump; a default (gather) client must keep its pre-feature fingerprint."""
    from app import agent, outcomes
    config = cfg.load_client_config("demo_dental")
    legacy = config.model_dump(mode="json")
    legacy.pop("stt_mode")
    assert agent.config_version(config) == outcomes.fingerprint(legacy)
    assert agent.config_version(_config("stream")) != agent.config_version(config)


# ---------------------------------------------------------------- gating
def test_stream_mode_requires_flag_env_and_ready_provider_and_no_kill_switch(monkeypatch):
    s = _stt()
    monkeypatch.setattr(s, "PROVIDER_FACTORY", _ReadyFake)
    assert s.stream_mode_active(_config("stream"), dict(ALL_ENV)) is True
    assert s.stream_mode_active(_config("gather"), dict(ALL_ENV)) is False          # default client never streams
    for missing in ALL_ENV:                                                          # every env var is required
        env = {k: v for k, v in ALL_ENV.items() if k != missing}
        assert s.stream_mode_active(_config("stream"), env) is False, missing
    for blank in ("", "0", "false"):
        assert s.stream_mode_active(_config("stream"), {**ALL_ENV, "CALLKETTLE_STREAM_STT_ENABLED": blank}) is False
    assert s.stream_mode_active(_config("stream"), {**ALL_ENV, "CALLKETTLE_STREAM_STT_KILL": "1"}) is False


def test_real_deepgram_adapter_is_ready_only_with_every_gate_condition():
    s = _stt()
    assert s.PROVIDER_FACTORY is s.DeepgramSTT
    assert s.DeepgramSTT.transport_implemented is True
    assert s.StreamingSTT.transport_implemented is False
    assert s.stream_mode_active(_config("stream"), dict(ALL_ENV)) is True
    assert s.stream_mode_active(_config("gather"), dict(ALL_ENV)) is False              # client flag still required
    assert s.stream_mode_active(_config("stream"), {}) is False                         # default production environment: off
    for missing in ALL_ENV:
        assert s.stream_mode_active(_config("stream"), {k: v for k, v in ALL_ENV.items() if k != missing}) is False, missing
    assert s.stream_mode_active(_config("stream"), {**ALL_ENV, "CALLKETTLE_STREAM_STT_KILL": "1"}) is False


def test_stream_mode_reads_the_process_environment_by_default(monkeypatch):
    s = _stt()
    monkeypatch.setattr(s, "PROVIDER_FACTORY", _ReadyFake)
    for k in list(ALL_ENV) + ["CALLKETTLE_STREAM_STT_KILL"]:
        monkeypatch.delenv(k, raising=False)
    assert s.stream_mode_active(_config("stream")) is False
    for k, v in ALL_ENV.items():
        monkeypatch.setenv(k, v)
    assert s.stream_mode_active(_config("stream")) is True
    monkeypatch.setenv("CALLKETTLE_STREAM_STT_KILL", "1")
    assert s.stream_mode_active(_config("stream")) is False


# ---------------------------------------------------------------- Deepgram skeleton / no import at startup
def test_deepgram_adapter_raises_clear_error_unless_configured(monkeypatch):
    s = _stt()
    for k in ALL_ENV:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(s.NotConfigured) as e:
        s.DeepgramSTT()
    assert "DEEPGRAM_API_KEY" in str(e.value)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "synthetic-not-a-real-key")
    with pytest.raises(s.NotConfigured) as e:                                        # key alone is not enough: explicit enable flag
        s.DeepgramSTT()
    assert "CALLKETTLE_STREAM_STT_ENABLED" in str(e.value)
    assert "synthetic-not-a-real-key" not in str(e.value)                            # the key value is never echoed


def test_deepgram_adapter_makes_no_network_calls_when_constructed_or_closed_unused(monkeypatch):
    import socket
    s = _stt()
    monkeypatch.setenv("DEEPGRAM_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.setenv("CALLKETTLE_STREAM_STT_ENABLED", "1")

    def no_network(*a, **k):
        raise AssertionError("network attempted")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    adapter = s.DeepgramSTT()
    assert "synthetic-not-a-real-key" not in repr(adapter)
    assert adapter.poll() == []                                                      # nothing connected yet
    adapter.close()                                                                  # closing is always safe
    assert "deepgram" not in {m.split(".")[0] for m in sys.modules}                  # no provider SDK is ever imported


def test_stream_module_is_not_imported_at_app_startup():
    code = "import sys, app.main; assert 'app.stream_stt' not in sys.modules, 'imported at startup'; print('ok')"
    env = {k: v for k, v in __import__("os").environ.items() if k not in ("REPORT_KEY", "CALLKETTLE_DB_PATH", "PYTHONPATH")}
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, env={**env, "PYTHONPATH": str(BACKEND)}, timeout=120)
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr[-800:]


# ---------------------------------------------------------------- signed stream token
def test_token_binds_call_sid_and_client_id_and_expires():
    s = _stt()
    secret = ALL_ENV["CALLKETTLE_STREAM_TOKEN_SECRET"]
    tok = s.make_token(secret, "CA1", "demo_dental", now=1000.0, ttl=60)
    assert s.verify_token(secret, tok, "CA1", "demo_dental", now=1010.0) is True
    assert s.verify_token(secret, tok, "CA2", "demo_dental", now=1010.0) is False    # another call
    assert s.verify_token(secret, tok, "CA1", "demo_hvac", now=1010.0) is False      # another tenant
    assert s.verify_token(secret, tok, "CA1", "demo_dental", now=1061.0) is False    # expired
    assert s.verify_token("other-secret-other-secret-xxxxxxxx", tok, "CA1", "demo_dental", now=1010.0) is False
    exp, mac = tok.split(".")
    assert s.verify_token(secret, f"{int(exp) + 999}.{mac}", "CA1", "demo_dental", now=1010.0) is False   # extended expiry
    for junk in ("", "x", "a.b", ".", None, 5):
        assert s.verify_token(secret, junk, "CA1", "demo_dental", now=1010.0) is False
    assert s.verify_token("", tok, "CA1", "demo_dental", now=1010.0) is False
    assert secret not in tok
    with pytest.raises(ValueError):
        s.make_token("", "CA1", "demo_dental", now=1.0)


# ---------------------------------------------------------------- endpointing (pure)
def _ep(**kw):
    s = _stt()
    return s.Endpointer(**kw), s


def test_endpointer_waits_for_silence_after_a_final_transcript():
    ep, s = _ep()
    ep.feed(s.Transcript("I need an appointment", True), now=0.0)
    assert ep.poll(0.5) is None
    utt = ep.poll(1.0)
    assert utt is not None and utt.text == "I need an appointment" and utt.barge_in is False
    assert ep.poll(5.0) is None                                                      # consumed: never emitted twice


def test_new_speech_resets_the_silence_gap_and_finals_are_joined():
    ep, s = _ep()
    ep.feed(s.Transcript("my name is", True), now=0.0)
    ep.feed(s.Transcript("sam", False), now=0.8)                                     # still talking
    assert ep.poll(1.5) is None
    ep.feed(s.Transcript("sam ali", True), now=1.6)
    assert ep.poll(2.0) is None
    assert ep.poll(2.6).text == "my name is sam ali"


def test_interim_only_is_not_an_utterance_until_a_long_silence_then_it_is_used():
    ep, s = _ep()
    ep.feed(s.Transcript("tuesday morning", False), now=0.0)
    assert ep.poll(1.0) is None                                                      # provider has not finalised yet
    assert ep.poll(2.0).text == "tuesday morning"                                    # provider never finalised: do not strand the caller


def test_phone_number_patience_matches_gather_speech_timeout_semantics():
    from app.main import _speech_timeout_for
    ep, s = _ep()
    ep.expect(_speech_timeout_for("What's the best phone number to reach you?"))     # "3": longer patience
    ep.feed(s.Transcript("five seven one", True), now=0.0)
    assert ep.poll(2.9) is None                                                      # a pause between digit groups is not the end
    ep.feed(s.Transcript("two nine zero", True), now=3.0)
    assert ep.poll(5.9) is None
    assert ep.poll(6.0).text == "five seven one two nine zero"
    ep.expect(_speech_timeout_for("What day works?"))                                # back to the default patience
    ep.feed(s.Transcript("monday", True), now=10.0)
    assert ep.poll(11.0).text == "monday"


def test_endpointer_ignores_blank_transcripts():
    ep, s = _ep()
    ep.feed(s.Transcript("   ", True), now=0.0)
    ep.feed(s.Transcript("", False), now=0.1)
    assert ep.poll(9.0) is None


def test_endpointer_flushes_a_runaway_utterance_at_the_cap():
    ep, s = _ep(max_utterance_s=10.0)
    for i in range(12):
        ep.feed(s.Transcript(f"word{i}", False if i % 2 else True), now=float(i) * 0.9)
    utt = ep.poll(10.5)                                                              # never a 1s gap, yet capped
    assert utt is not None and "word0" in utt.text


def test_barge_in_is_detected_once_and_marks_the_utterance():
    ep, s = _ep()
    ep.ai_speaking_until(10.0)
    assert ep.feed(s.Transcript("actually", False), now=4.0) is True                 # first caller speech over the AI: barge-in
    assert ep.feed(s.Transcript("actually wait", False), now=4.3) is False           # reported once, not per transcript
    ep.feed(s.Transcript("actually wait I need tomorrow", True), now=5.0)
    utt = ep.poll(6.0)
    assert utt.barge_in is True and utt.text == "actually wait I need tomorrow"
    ep.feed(s.Transcript("thanks", True), now=20.0)                                  # AI no longer speaking
    assert ep.poll(21.0).barge_in is False


def test_speech_after_the_ai_finished_is_not_a_barge_in():
    ep, s = _ep()
    ep.ai_speaking_until(3.0)
    assert ep.feed(s.Transcript("yes", True), now=3.5) is False


# ---------------------------------------------------------------- FakeSTT
def test_fake_stt_is_scripted_and_can_fail():
    s = _stt()
    f = s.FakeSTT(script={2: [s.Transcript("hi", True)]})
    f.feed(b"a" * 160)
    assert f.poll() == []
    f.feed(b"b" * 160)
    assert [t.text for t in f.poll()] == ["hi"] and f.poll() == []
    assert f.bytes_fed == 320
    bad = s.FakeSTT(fail_on_feed=True)
    with pytest.raises(RuntimeError):
        bad.feed(b"x")
    f.close()
    assert f.closed is True


# ---------------------------------------------------------------- StreamCall (transport-independent processor)
class _Updater:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def update_call(self, call_sid, twiml):
        if self.fail:
            raise RuntimeError("rest down")
        self.calls.append((call_sid, twiml))


def _media(n=160):
    return {"event": "media", "media": {"track": "inbound", "payload": base64.b64encode(b"\xff" * n).decode()}}


def _call(stt, updater=None, turn=None, **kw):
    s = _stt()
    updater = updater or _Updater()
    times = iter(x * 0.5 for x in range(10000))
    seen = []

    def default_turn(text):
        seen.append(text)
        return s.TurnResult(twiml=f"<Response>reply-to-{len(seen)}</Response>", reply_text="Sure.", ended=False, speech_timeout="auto")

    call = s.StreamCall(
        call_sid="CA1", client_id="demo_dental", stt=stt, updater=updater, turn_handler=turn or default_turn,
        fallback_twiml=lambda: "<Response>FALLBACK-GATHER</Response>", clock=lambda: next(times),
        barge_in_twiml="<Response>HOLD</Response>", **kw)
    return call, updater, seen


def test_completed_utterance_runs_the_turn_and_updates_the_live_call():
    s = _stt()
    stt = s.FakeSTT(script={1: [s.Transcript("book me a cleaning", True)]})
    call, updater, seen = _call(stt)
    utt = None
    for _ in range(6):
        utt = call.handle_event(_media()) or utt
    assert utt is not None and utt.text == "book me a cleaning"
    call.run_utterance(utt)
    assert seen == ["book me a cleaning"]
    assert updater.calls == [("CA1", "<Response>reply-to-1</Response>")]
    assert call.fallback_reason is None


def test_audio_seconds_are_measured_from_mulaw_bytes_and_reported_on_close():
    s = _stt()
    reported = []
    call, _, _ = _call(s.FakeSTT(), on_audio_seconds=reported.append)
    for _ in range(100):
        call.handle_event(_media(160))                                               # 100 frames x 20 ms of 8 kHz mu-law
    call.close()
    call.close()                                                                     # idempotent
    assert reported == [pytest.approx(2.0)]
    assert call.audio_seconds == pytest.approx(2.0)


def test_stt_failure_falls_back_to_gather_once_and_stops_listening():
    s = _stt()
    stt = s.FakeSTT(fail_on_feed=True)
    call, updater, _ = _call(stt)
    assert call.handle_event(_media()) is None
    assert call.fallback_reason == "stt_error"
    assert updater.calls == [("CA1", "<Response>FALLBACK-GATHER</Response>")]
    call.handle_event(_media())
    assert len(updater.calls) == 1 and stt.closed is True                            # no second fallback, provider closed


def test_provider_poll_error_also_falls_back():
    s = _stt()
    stt = s.FakeSTT(poll_error=True)
    call, updater, _ = _call(stt)
    call.handle_event(_media())
    assert call.fallback_reason == "stt_error" and len(updater.calls) == 1


def test_missing_audio_for_n_seconds_falls_back():
    s = _stt()
    call, updater, _ = _call(s.FakeSTT(), no_audio_s=4.0)
    call.handle_event(_media())                                                      # t=0.0, last audio at 0.0
    assert call.tick() is None and updater.calls == []                               # t=0.5
    for _ in range(8):
        call.tick()                                                                  # clock walks past 4 s with no audio
    assert call.fallback_reason == "no_audio" and len(updater.calls) == 1


def test_websocket_drop_without_stop_falls_back_but_a_clean_stop_does_not():
    s = _stt()
    call, updater, _ = _call(s.FakeSTT())
    call.handle_event(_media())
    call.on_disconnect()
    assert call.fallback_reason == "ws_drop" and len(updater.calls) == 1
    call2, updater2, _ = _call(s.FakeSTT())
    call2.handle_event(_media())
    call2.handle_event({"event": "stop"})
    call2.on_disconnect()
    assert call2.fallback_reason is None and updater2.calls == []


def test_turn_handler_failure_falls_back_instead_of_dead_ending_the_caller():
    s = _stt()

    def boom(text):
        raise RuntimeError("model down")

    call, updater, _ = _call(s.FakeSTT(), turn=boom)
    call.run_utterance(s.Utterance("hello", False))
    assert call.fallback_reason == "turn_error"
    assert updater.calls == [("CA1", "<Response>FALLBACK-GATHER</Response>")]


def test_rest_update_failure_never_raises_into_the_websocket_loop():
    s = _stt()
    call, updater, _ = _call(s.FakeSTT(), updater=_Updater(fail=True))
    call.run_utterance(s.Utterance("hello", False))                                  # must not raise
    assert call.fallback_reason in ("rest_error", "turn_error")


def test_barge_in_cuts_the_ai_once_and_the_reply_is_still_answered():
    s = _stt()
    stt = s.FakeSTT(script={
        1: [s.Transcript("hello", True)],
        8: [s.Transcript("wait", False)],
        9: [s.Transcript("wait no tomorrow", True)],
    })
    call, updater, seen = _call(stt)
    utts = []
    for i in range(6):
        u = call.handle_event(_media())
        if u:
            utts.append(u)
    assert utts and utts[0].text == "hello"
    call.run_utterance(utts[0])                                                      # AI replies "Sure." -> speaking window opens
    assert updater.calls[-1][1] == "<Response>reply-to-1</Response>"
    for i in range(8):
        u = call.handle_event(_media())
        if u:
            utts.append(u)
    holds = [c for c in updater.calls if "HOLD" in c[1]]
    assert len(holds) == 1                                                           # one cut, not one per transcript
    assert utts[-1].text == "wait no tomorrow" and utts[-1].barge_in is True


def test_ended_turn_closes_the_provider_and_stops_listening():
    s = _stt()
    stt = s.FakeSTT()
    call, updater, _ = _call(stt, turn=lambda t: s.TurnResult("<Response>BYE</Response>", "Bye.", True, "auto"))
    call.run_utterance(s.Utterance("that's all", False))
    assert call.finished is True and stt.closed is True
    n = stt.bytes_fed
    call.handle_event(_media())
    assert stt.bytes_fed == n                                                        # no audio forwarded (and billed) after the call ended


def test_exceeding_the_audio_cap_falls_back_so_a_stuck_stream_cannot_bill_forever():
    s = _stt()
    call, updater, _ = _call(s.FakeSTT(), max_audio_s=1.0)
    for _ in range(80):
        call.handle_event(_media(160))
    assert call.fallback_reason == "audio_cap"


def test_logs_never_contain_transcripts_audio_or_secrets(caplog):
    s = _stt()
    secret_words = "my social is 078-05-1120 and my secret phrase is purple walrus"
    stt = s.FakeSTT(script={1: [s.Transcript(secret_words, True)]}, fail_on_feed=False)
    caplog.set_level(logging.DEBUG)
    call, updater, _ = _call(stt)
    utt = None
    for _ in range(6):
        utt = call.handle_event(_media()) or utt
    call.run_utterance(utt)
    call.on_disconnect()                                                             # forces fallback logging too
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "purple walrus" not in text and "078-05-1120" not in text
    assert ALL_ENV["DEEPGRAM_API_KEY"] not in text
    assert base64.b64encode(b"\xff" * 160).decode() not in text


# ---------------------------------------------------------------- cost ledger metric
def test_stt_seconds_is_a_cost_metric_with_no_invented_rate():
    assert "stt_seconds" in co.METRICS and "stt_seconds" in co.COST_METRICS
    assert "stt_seconds" not in co.EXAMPLE_RATES                                     # the unit rate stays UNKNOWN until a real contract exists


def _row():
    return {"call_sid": "CA1", "client_id": "demo_dental", "started_at": "2026-01-05T09:00:00+00:00", "ended_at": "2026-01-05T09:01:00+00:00"}


def test_stt_seconds_without_evidence_is_not_applicable_and_does_not_poison_gather_calls():
    obs = co.observe_call(_row(), rates=co.EXAMPLE_RATES)
    comp = obs["components"]["stt_seconds"]
    assert comp["status"] == "not_applicable" and comp["usd"] == 0
    other_unknown = [m for m, c in obs["components"].items() if c["usd"] is None and m != "stt_seconds"]
    assert "stt_seconds" not in other_unknown


def test_measured_stt_seconds_without_a_rate_is_unknown_never_zero_or_guessed():
    ev = {"stt_seconds": {"value": 37.5, "status": "measured", "source": "twilio_media_stream_bytes"}}
    obs = co.observe_call(_row(), rates=co.EXAMPLE_RATES, evidence=ev)
    comp = obs["components"]["stt_seconds"]
    assert comp["usd"] is None and comp["status"] == "unknown"
    assert obs["quantities"]["stt_seconds"]["value"] == 37.5
    assert obs["complete_cost_usd"] is None                                          # honest: total cost incomplete without a rate


def test_measured_stt_seconds_is_priced_only_when_the_operator_supplies_a_rate():
    ev = {"stt_seconds": {"value": 61, "status": "measured", "source": "twilio_media_stream_bytes"}}
    rate = {"usd_per_unit": "1", "unit_quantity": "60", "billing_increment": "1", "rate_id": "synthetic:test-only",
            "source_url": "https://example.com/synthetic", "checked_at": "2026-10-04"}
    obs = co.observe_call(_row(), rates={"stt_seconds": rate}, evidence=ev)
    comp = obs["components"]["stt_seconds"]
    assert comp["status"] == "estimated_from_measured" and float(comp["usd"]) == pytest.approx(61 / 60)
