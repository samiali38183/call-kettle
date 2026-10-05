"""Stream STT wiring (OFFLINE, flag-off): HTTP + websocket routes against fakes. No Twilio, no provider, no network.

Status: UNIT TESTED ONLY. Never run with real audio (docs/STREAM_STT_DESIGN.md).
"""
import base64
import json
import logging
import sqlite3

import pytest
from starlette.websockets import WebSocketDisconnect

from app import agent, config as cfg, storage

ENV = {
    "CALLKETTLE_STREAM_STT_ENABLED": "1",
    "DEEPGRAM_API_KEY": "synthetic-not-a-real-key",
    "CALLKETTLE_STREAM_TOKEN_SECRET": "synthetic-token-secret-for-tests+15555550100",
}
FORM = {"CallSid": "CA_G1", "From": "+15555554567", "To": "+15555550000"}

# Captured from the production code BEFORE this feature existed (demo_dental, TestClient host).
GOLDEN_INCOMING = (
    '<?xml version="1.0" encoding="UTF-8"?><Response><Gather input="speech" action="http://testserver/voice/gather?client_id=demo_dental&amp;retry=0" '
    'method="POST" language="en-US" speechTimeout="auto" timeout="5" hints="Bright Smile Dental,Routine cleaning,Filling,Emergency exam">'
    '<Say voice="Polly.Joanna-Neural">This call may be recorded and monitored for quality. Thanks for calling Bright Smile Dental, this is the AI receptionist \u2014 how can I help?</Say></Gather>'
    "<Say voice=\"Polly.Joanna-Neural\">Sorry, I didn't catch that.</Say>"
    '<Redirect method="POST">http://testserver/voice/gather?client_id=demo_dental&amp;retry=0</Redirect></Response>'
)
GOLDEN_RETRY = (
    '<?xml version="1.0" encoding="UTF-8"?><Response><Gather input="speech" action="http://testserver/voice/gather?client_id=demo_dental&amp;retry=1" '
    'method="POST" language="en-US" speechTimeout="auto" timeout="5" hints="Bright Smile Dental,Routine cleaning,Filling,Emergency exam">'
    '<Say voice="Polly.Joanna-Neural">Sorry, could you say that again?</Say></Gather>'
    "<Say voice=\"Polly.Joanna-Neural\">Sorry, I didn't catch that.</Say>"
    '<Redirect method="POST">http://testserver/voice/gather?client_id=demo_dental&amp;retry=1</Redirect></Response>'
)


class FakeUpdater:
    def __init__(self):
        self.calls = []

    def update_call(self, call_sid, twiml):
        self.calls.append((call_sid, twiml))


@pytest.fixture
def env(app_client, monkeypatch):
    client, main = app_client
    from app import stream_stt
    for k in list(ENV) + ["CALLKETTLE_STREAM_STT_KILL"]:
        monkeypatch.delenv(k, raising=False)
    updater = FakeUpdater()
    monkeypatch.setattr(stream_stt, "_UPDATER", updater)
    state = {"script": {}, "fail": False, "instances": []}

    class Fake(stream_stt.FakeSTT):
        transport_implemented = True

        def __init__(self, keyterms=()):
            state["keyterms"] = keyterms
            super().__init__(script=state["script"], fail_on_feed=state["fail"])
            state["instances"].append(self)

    monkeypatch.setattr(stream_stt, "PROVIDER_FACTORY", Fake)
    ticks = iter(x * 0.5 for x in range(100000))
    monkeypatch.setattr(stream_stt, "monotonic", lambda: next(ticks))

    def stream_clients(*ids):
        real = cfg.load_client_config
        monkeypatch.setattr(main, "load_client_config",
                            lambda cid: real(cid).model_copy(update={"stt_mode": "stream"}) if cid in ids else real(cid))

    def enable():
        for k, v in ENV.items():
            monkeypatch.setenv(k, v)

    return client, main, stream_stt, updater, state, stream_clients, enable


def _incoming(client, sid="CA_G1", client_id="demo_dental"):
    return client.post(f"/voice/incoming?client_id={client_id}", data={**FORM, "CallSid": sid})


def _token_from(twiml):
    import re
    return re.search(r'<Parameter name="token" value="([^"]+)"', twiml).group(1)


def _start(sid, client_id, token):
    return {"event": "start", "start": {"callSid": sid, "streamSid": "MZ1", "customParameters": {"token": token, "client_id": client_id}}}


def _media(n=160):
    return {"event": "media", "media": {"track": "inbound", "payload": base64.b64encode(b"\xff" * n).decode()}}


def _ws_session(client, events):
    """Send events, then stop; return after the server finished handling the stream."""
    with client.websocket_connect("/voice/stream") as ws:
        ws.send_text(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        for e in events:
            ws.send_text(json.dumps(e))
        ws.send_text(json.dumps({"event": "stop"}))
        with pytest.raises(WebSocketDisconnect):      # the server closes once it has processed everything (TestClient would cancel it otherwise)
            ws.receive_text()


# ------------------------------------------------------- default path is byte-for-byte unchanged
def test_default_config_twiml_is_identical_to_the_pre_feature_output(env):
    client, *_ = env
    assert _incoming(client).text == GOLDEN_INCOMING
    assert client.post("/voice/gather?client_id=demo_dental&retry=0", data={"CallSid": "CA_G1", "From": FORM["From"]}).text == GOLDEN_RETRY


def test_stream_flag_without_env_stays_on_gather_even_with_a_ready_provider(env):
    client, main, s, updater, state, stream_clients, enable = env
    stream_clients("demo_dental")
    assert _incoming(client).text == GOLDEN_INCOMING


@pytest.mark.parametrize("missing", list(ENV))
def test_stream_flag_with_any_missing_env_stays_on_gather(env, monkeypatch, missing):
    client, main, s, updater, state, stream_clients, enable = env
    stream_clients("demo_dental")
    enable()
    monkeypatch.delenv(missing)
    assert _incoming(client).text == GOLDEN_INCOMING


def test_kill_switch_forces_gather(env, monkeypatch):
    client, main, s, updater, state, stream_clients, enable = env
    stream_clients("demo_dental")
    enable()
    monkeypatch.setenv("CALLKETTLE_STREAM_STT_KILL", "1")
    assert _incoming(client).text == GOLDEN_INCOMING


def test_real_deepgram_adapter_still_needs_every_env_var_and_the_client_flag(app_client, monkeypatch):
    client, main = app_client
    real = cfg.load_client_config
    monkeypatch.setattr(main, "load_client_config", lambda cid: real(cid).model_copy(update={"stt_mode": "stream"}))
    for k in list(ENV) + ["CALLKETTLE_STREAM_STT_KILL"]:
        monkeypatch.delenv(k, raising=False)
    assert _incoming(client).text == GOLDEN_INCOMING                                 # default environment: byte-identical Gather TwiML
    for missing in ENV:
        for k, v in ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv(missing)
        assert _incoming(client).text == GOLDEN_INCOMING, missing
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("CALLKETTLE_STREAM_STT_KILL", "1")
    assert _incoming(client).text == GOLDEN_INCOMING
    monkeypatch.delenv("CALLKETTLE_STREAM_STT_KILL")
    assert "<Start><Stream" in _incoming(client).text                                # all conditions met: the real adapter opens the gate


# ------------------------------------------------------- stream-mode greeting TwiML
def test_stream_mode_greeting_starts_a_stream_and_has_no_gather_and_a_gather_redirect_fallback(env):
    client, main, s, updater, state, stream_clients, enable = env
    stream_clients("demo_dental")
    enable()
    text = _incoming(client).text
    assert '<Start><Stream url="wss://testserver/voice/stream" track="inbound_track"' in text
    assert "<Gather" not in text                                                     # no double billing: speech recognition is not running
    assert "This call may be recorded and monitored for quality." in text           # disclosure + Polly <Say> unchanged
    assert '<Say voice="Polly.Joanna-Neural">' in text
    assert '<Redirect method="POST">http://testserver/voice/gather?client_id=demo_dental&amp;retry=0</Redirect>' in text
    assert 'name="client_id" value="demo_dental"' in text
    tok = _token_from(text)
    assert s.verify_token(ENV["CALLKETTLE_STREAM_TOKEN_SECRET"], tok, "CA_G1", "demo_dental", now=__import__("time").time())
    assert ENV["CALLKETTLE_STREAM_TOKEN_SECRET"] not in text and ENV["DEEPGRAM_API_KEY"] not in text
    assert agent.get_session("CA_G1") is not None                                    # same session the Gather path uses


def test_stream_greeting_failure_degrades_to_the_normal_gather_twiml(env, monkeypatch):
    client, main, s, updater, state, stream_clients, enable = env
    stream_clients("demo_dental")
    enable()
    monkeypatch.setattr(main.twilio_utils, "stream_greeting_twiml", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert _incoming(client).text == GOLDEN_INCOMING


def test_fallback_gather_after_silence_still_works_in_stream_mode(env):
    """A silent stream-mode caller is redirected into the ordinary /voice/gather handler (the only path that bills a Gather)."""
    client, main, s, updater, state, stream_clients, enable = env
    stream_clients("demo_dental")
    enable()
    _incoming(client)
    again = client.post("/voice/gather?client_id=demo_dental&retry=0", data={"CallSid": "CA_G1", "From": FORM["From"]})
    assert "<Gather" in again.text and "say that again" in again.text


# ------------------------------------------------------- websocket authentication
def _expect_rejected(client, first_events):
    with client.websocket_connect("/voice/stream") as ws:
        for e in first_events:
            try:
                ws.send_text(json.dumps(e))
            except Exception:
                break
        with pytest.raises(WebSocketDisconnect) as exc:
            for _ in range(5):
                ws.receive_text()
        assert exc.value.code == 1008


def test_stream_route_rejects_everything_when_stream_mode_is_off(env):
    client, main, s, updater, state, stream_clients, enable = env
    _expect_rejected(client, [{"event": "start", "start": {"callSid": "CA_G1", "customParameters": {"token": "1.aa", "client_id": "demo_dental"}}}])
    assert state["instances"] == [] and updater.calls == []


def test_forged_or_missing_tokens_are_rejected_and_no_provider_is_ever_created(env):
    client, main, s, updater, state, stream_clients, enable = env
    stream_clients("demo_dental")
    enable()
    tok = _token_from(_incoming(client).text)
    forged = [
        _start("CA_G1", "demo_dental", ""),                                          # no token
        _start("CA_G1", "demo_dental", "+15555550100.deadbeef"),                       # made-up MAC
        _start("CA_OTHER", "demo_dental", tok),                                      # token replayed for another call
        _start("CA_G1", "demo_hvac", tok),                                           # token replayed for another tenant
        {"event": "start", "start": {"callSid": "CA_G1"}},                           # no parameters at all
        {"event": "media", "media": {"payload": base64.b64encode(b"x").decode()}},   # media before any start
    ]
    for ev in forged:
        _expect_rejected(client, [ev, _media(), _media()])
    assert state["instances"] == [] and updater.calls == []


def test_unknown_call_and_non_stream_client_are_rejected(env):
    client, main, s, updater, state, stream_clients, enable = env
    enable()
    secret = ENV["CALLKETTLE_STREAM_TOKEN_SECRET"]
    import time
    tok = s.make_token(secret, "CA_NOSESSION", "demo_dental", now=time.time())       # validly signed, but no live session
    _expect_rejected(client, [_start("CA_NOSESSION", "demo_dental", tok)])
    _incoming(client)                                                                # default (gather) client: has a session, not a stream client
    tok2 = s.make_token(secret, "CA_G1", "demo_dental", now=time.time())
    _expect_rejected(client, [_start("CA_G1", "demo_dental", tok2)])
    assert state["instances"] == []


def test_tenant_isolation_a_session_of_another_client_cannot_be_streamed_into(env):
    client, main, s, updater, state, stream_clients, enable = env
    stream_clients("demo_dental", "demo_hvac")
    enable()
    import time
    _incoming(client, sid="CA_A", client_id="demo_dental")
    secret = ENV["CALLKETTLE_STREAM_TOKEN_SECRET"]
    tok_b = s.make_token(secret, "CA_A", "demo_hvac", now=time.time())              # attacker signs nothing: but even a valid-looking
    _expect_rejected(client, [_start("CA_A", "demo_hvac", tok_b)])                   # token for the wrong tenant label must not bind to A's call
    assert agent.get_session("CA_A").client_id == "demo_dental"
    assert state["instances"] == [] and updater.calls == []


# ------------------------------------------------------- authenticated stream: same agent path
def _stream_call(env, sid="CA_G1", client_id="demo_dental", reply=("Sure, what day works?", False, None), script=None, frames=8):
    client, main, s, updater, state, stream_clients, enable = env
    stream_clients("demo_dental", "demo_hvac")
    enable()
    state["script"] = script if script is not None else {1: [s.Transcript("I need a cleaning", True)]}
    text = _incoming(client, sid=sid, client_id=client_id).text
    seen = []
    main.agent.run_turn = lambda session, caller_text: (seen.append((session.call_sid, session.client_id, caller_text)) or reply)
    _ws_session(client, [_start(sid, client_id, _token_from(text))] + [_media() for _ in range(frames)])
    return seen


def test_authenticated_stream_runs_the_same_run_turn_and_updates_the_live_call(env, monkeypatch):
    client, main, s, updater, state, *_ = env
    monkeypatch.setattr(main.agent, "run_turn", main.agent.run_turn)                 # restored after the test by monkeypatch
    seen = _stream_call(env)
    assert seen == [("CA_G1", "demo_dental", "I need a cleaning")]
    assert len(updater.calls) == 1
    sid, twiml = updater.calls[0]
    assert sid == "CA_G1"
    assert 'Polly.Joanna-Neural' in twiml and "Sure, what day works?" in twiml       # <Say> (Polly), voice unchanged
    assert "<Gather" not in twiml and "<Pause" in twiml
    assert '/voice/gather?client_id=demo_dental' in twiml                            # silence falls through to the normal Gather handler
    assert state["instances"][0].closed is True


def test_stream_turn_transfer_and_end_produce_the_same_twiml_shapes_as_gather(env, monkeypatch):
    client, main, s, updater, state, *_ = env
    monkeypatch.setattr(main.agent, "run_turn", main.agent.run_turn)
    _stream_call(env, sid="CA_T", reply=("Connecting you now.", True, "+15555550111"))
    twiml = updater.calls[0][1]
    assert "+15555550111</Dial>" in twiml and "voice/transfer-result" in twiml and "Connecting you now." in twiml
    updater.calls.clear()
    state["instances"].clear()
    _stream_call(env, sid="CA_E", reply=("Goodbye.", True, None))
    assert "<Hangup/>" in updater.calls[0][1]


def test_stream_ledger_has_measured_stt_seconds_and_no_gather_count_and_no_text(env, monkeypatch):
    client, main, s, updater, state, *_ = env
    monkeypatch.setattr(main.agent, "run_turn", main.agent.run_turn)
    _stream_call(env, frames=50, script={1: [s.Transcript("my phone is+15555550100 and my secret is purple walrus", True)]})
    from app import cost_observability as co
    ev = co.read_evidence(storage.DB_PATH, "demo_dental", "CA_G1")
    assert "gather_count" not in ev                                                  # no Gather happened, so none is counted
    assert ev["stt_seconds"]["status"] == "measured"
    assert float(ev["stt_seconds"]["value"]) == pytest.approx(50 * 160 / 8000)
    with sqlite3.connect(storage.DB_PATH) as conn:
        dump = json.dumps(conn.execute("SELECT * FROM private_cost_usage").fetchall())
    assert "walrus" not in dump and "703" not in dump


def test_stream_ledger_is_tenant_scoped(env, monkeypatch):
    client, main, s, updater, state, *_ = env
    monkeypatch.setattr(main.agent, "run_turn", main.agent.run_turn)
    _stream_call(env, sid="CA_A", client_id="demo_dental")
    from app import cost_observability as co
    assert "stt_seconds" in co.read_evidence(storage.DB_PATH, "demo_dental", "CA_A")
    assert co.read_evidence(storage.DB_PATH, "demo_hvac", "CA_A") == {}


def test_gather_path_still_counts_gathers(env, monkeypatch):
    client, main, s, updater, state, *_ = env
    monkeypatch.setattr(main.agent, "run_turn", lambda session, text: ("Sure.", False, None))
    _incoming(client)
    client.post("/voice/gather?client_id=demo_dental&retry=0", data={"CallSid": "CA_G1", "SpeechResult": "hello"})
    from app import cost_observability as co
    # greeting Gather + reply Gather: counted where emitted (the old callback-only count of 1 undercounted)
    assert co.read_evidence(storage.DB_PATH, "demo_dental", "CA_G1")["gather_count"]["value"] == 2


def test_stt_failure_over_the_websocket_falls_back_to_gather_twiml(env, monkeypatch):
    client, main, s, updater, state, *_ = env
    monkeypatch.setattr(main.agent, "run_turn", main.agent.run_turn)
    state["fail"] = True
    _stream_call(env, script={})
    assert len(updater.calls) == 1
    sid, twiml = updater.calls[0]
    assert sid == "CA_G1" and "<Gather" in twiml and '<Stop><Stream name="ck_stt"/></Stop>' in twiml
    assert "say that again" in twiml
    assert "action=\"http://testserver/voice/gather?client_id=demo_dental&amp;retry=0\"" in twiml


def test_websocket_drop_mid_call_falls_back_to_gather(env, monkeypatch):
    client, main, s, updater, state, stream_clients, enable = env
    monkeypatch.setattr(main.agent, "run_turn", main.agent.run_turn)
    stream_clients("demo_dental")
    enable()
    text = _incoming(client).text
    with client.websocket_connect("/voice/stream") as ws:
        ws.send_text(json.dumps(_start("CA_G1", "demo_dental", _token_from(text))))
        ws.send_text(json.dumps(_media()))
    assert len(updater.calls) == 1 and "<Gather" in updater.calls[0][1]


def test_route_logs_contain_no_transcript_token_or_key(env, monkeypatch, caplog):
    client, main, s, updater, state, stream_clients, enable = env
    monkeypatch.setattr(main.agent, "run_turn", main.agent.run_turn)
    caplog.set_level(logging.DEBUG)
    stream_clients("demo_dental")
    enable()
    text = _incoming(client).text
    tok = _token_from(text)
    state["script"] = {1: [s.Transcript("purple walrus 078-05-1120", True)]}
    main.agent.run_turn = lambda session, t: ("Okay.", False, None)
    _ws_session(client, [_start("CA_G1", "demo_dental", tok)] + [_media() for _ in range(8)])
    _expect_rejected(client, [_start("CA_G1", "demo_dental", "1.forged")])
    logs = "\n".join(r.getMessage() for r in caplog.records)
    for forbidden in ("purple walrus", "078-05-1120", tok, ENV["DEEPGRAM_API_KEY"], ENV["CALLKETTLE_STREAM_TOKEN_SECRET"]):
        assert forbidden not in logs


@pytest.mark.parametrize("override", [{"spanish": True}, {"record_transcripts": False}])
def test_spanish_and_no_transcript_clients_stay_on_gather_even_when_stream_is_otherwise_ready(env, monkeypatch, override):
    """Spanish needs the press-2 key press (a Gather); record_transcripts=false clients are privacy-sensitive: neither streams audio to a third party."""
    client, main, s, updater, state, stream_clients, enable = env
    enable()
    real = cfg.load_client_config
    monkeypatch.setattr(main, "load_client_config", lambda cid: real(cid).model_copy(update={"stt_mode": "stream", **override}))
    text = _incoming(client).text
    assert "<Stream" not in text and "<Gather" in text
