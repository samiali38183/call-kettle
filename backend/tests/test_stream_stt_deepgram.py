"""DeepgramSTT transport, tested against an in-process FAKE connection. No sockets, no network, no real key."""
import json
import logging
import socket
from urllib.parse import parse_qs, urlsplit

import pytest

from app import stream_stt as s

KEY = "synthetic-dg-key-DO-NOT-LEAK-12345"
ENV = {"DEEPGRAM_API_KEY": KEY, "CALLKETTLE_STREAM_STT_ENABLED": "1"}
SECRET_TEXT = "my number is+15555550100 SENTINELTEXT"


class FakeConn:
    def __init__(self):
        self.bytes_sent, self.text_sent, self.inbox, self.closed = [], [], [], False
        self.close_error = None

    def send_bytes(self, data):
        self.bytes_sent.append(data)

    def send_text(self, text):
        self.text_sent.append(json.loads(text))

    def recv(self, timeout=0.0):
        if not self.inbox:
            return None
        item = self.inbox.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(**kw):
    conn, calls, clock = FakeConn(), [], Clock()

    def connect(url, headers):
        calls.append((url, dict(headers)))
        return conn

    return s.DeepgramSTT(dict(ENV), connect=connect, clock=clock, **kw), conn, calls, clock


def results(text, *, final=False, speech_final=False):
    return json.dumps({"type": "Results", "is_final": final, "speech_final": speech_final,
                       "channel": {"alternatives": [{"transcript": text, "confidence": 0.9}]}})


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


# ------------------------------------------------------------------ flag + construction
def test_transport_flag_true_only_for_the_adapter():
    assert s.DeepgramSTT.transport_implemented is True
    assert s.StreamingSTT.transport_implemented is False


def test_construction_does_not_connect():
    stt, conn, calls, _ = make()
    assert calls == []
    stt.close()
    assert calls == [] and conn.text_sent == []          # closing an unused adapter sends nothing and never connects


# ------------------------------------------------------------------ URL / headers
def test_url_has_required_query_params():
    url = s.build_listen_url()
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("wss", "api.deepgram.com", "/v1/listen")
    q = parse_qs(parts.query)
    assert q["encoding"] == ["mulaw"] and q["sample_rate"] == ["8000"] and q["channels"] == ["1"]
    assert q["model"] == ["nova-3"] and q["interim_results"] == ["true"]
    assert q["punctuate"] == ["true"] and q["smart_format"] == ["true"]
    assert int(q["endpointing"][0]) > 0 and int(q["utterance_end_ms"][0]) >= 1000   # provider minimum is 1000


def test_url_is_configurable_and_keyterms_repeat_for_nova3():
    q = parse_qs(urlsplit(s.build_listen_url(model="nova-3", endpointing_ms=500, utterance_end_ms=1500,
                                             keyterms=["Acme Heating", "furnace tune-up", "  ", "Acme Heating"])).query)
    assert q["endpointing"] == ["500"] and q["utterance_end_ms"] == ["1500"]
    assert q["keyterm"] == ["Acme Heating", "furnace tune-up"]                       # repeated param, deduped, blanks dropped
    assert "keywords" not in q


def test_older_models_use_keywords_not_keyterm():
    q = parse_qs(urlsplit(s.build_listen_url(model="nova-2-phonecall", keyterms=["Acme"])).query)
    assert q["keywords"] == ["Acme"] and "keyterm" not in q


def test_keyterms_are_bounded_and_sanitised():
    terms = [f"term{i}" for i in range(500)] + ["x" * 500, "bad&term=1\n"]
    q = parse_qs(urlsplit(s.build_listen_url(keyterms=terms)).query)
    assert len(q["keyterm"]) <= s.MAX_KEYTERMS
    assert all(len(t) <= s.MAX_KEYTERM_CHARS and "\n" not in t for t in q["keyterm"])
    assert "term" not in q                                                            # '&term=1' could not inject a parameter


def test_key_goes_in_authorization_header_only_at_first_use():
    stt, conn, calls, _ = make(keyterms=["Acme"])
    stt.feed(b"\xff" * 160)
    (url, headers), = calls
    assert headers["Authorization"] == f"Token {KEY}"
    assert KEY not in url
    assert conn.bytes_sent == [b"\xff" * 160]


def test_key_never_in_repr_str_vars_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    stt, conn, calls, _ = make()
    stt.feed(b"\x00" * 80)
    conn.inbox.append(results(SECRET_TEXT, final=True))
    stt.poll()
    stt.close()
    for blob in (repr(stt), str(stt), repr(vars(stt)), caplog.text):
        assert KEY not in blob


def test_key_read_from_the_process_environment_at_connect_time(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    conn, calls = FakeConn(), []
    stt = s.DeepgramSTT(connect=lambda url, headers: calls.append(dict(headers)) or conn, clock=Clock())
    monkeypatch.setenv("DEEPGRAM_API_KEY", "rotated-key-value")                       # read when connecting, not when constructing
    stt.feed(b"\x00")
    assert calls[0]["Authorization"] == "Token rotated-key-value"


def test_connect_failure_raises_provider_error_without_leaking_key():
    def bad_connect(url, headers):
        raise OSError(f"boom {headers['Authorization']}")
    stt = s.DeepgramSTT(dict(ENV), connect=bad_connect, clock=Clock())
    with pytest.raises(s.ProviderError) as e:
        stt.feed(b"\x00")
    assert KEY not in str(e.value) and KEY not in repr(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__                  # chained original could carry the header


# ------------------------------------------------------------------ results mapping
def test_interim_and_final_transcripts_are_mapped():
    stt, conn, _, clock = make()
    stt.feed(b"\x00" * 160)
    conn.inbox += [results("book a", final=False), results("book a tune up", final=True, speech_final=True)]
    out = stt.poll()
    assert [(t.text, t.is_final) for t in out] == [("book a", False), ("book a tune up", True)]
    assert out[0].at == clock.t
    assert out[1].speech_final is True and out[0].speech_final is False
    assert stt.poll() == []


def test_empty_and_whitespace_transcripts_are_dropped_but_counted_as_speech_final():
    stt, conn, _, _ = make()
    stt.feed(b"\x00")
    conn.inbox += [results("", final=True, speech_final=True), results("   ", final=False)]
    assert stt.poll() == []


def test_utterance_end_and_metadata_are_handled_without_transcripts():
    stt, conn, _, _ = make()
    stt.feed(b"\x00")
    conn.inbox += [json.dumps({"type": "Metadata", "request_id": "abc"}),
                   json.dumps({"type": "UtteranceEnd", "last_word_end": 1.2}),
                   json.dumps({"type": "SpeechStarted"})]
    assert stt.poll() == []
    assert stt.utterance_ends == 1


def test_utterance_end_marks_the_last_final_as_speech_final_for_the_next_poll():
    stt, conn, _, _ = make()
    stt.feed(b"\x00")
    conn.inbox += [results("hello there", final=True), json.dumps({"type": "UtteranceEnd"})]
    out = stt.poll()
    assert [(t.text, t.is_final) for t in out] == [("hello there", True)]
    assert stt.utterance_ends == 1


def test_bytes_message_is_decoded():
    stt, conn, _, _ = make()
    stt.feed(b"\x00")
    conn.inbox.append(results("hi", final=True).encode())
    assert [t.text for t in stt.poll()] == ["hi"]


@pytest.mark.parametrize("bad", [
    "not json", "", "[]", "null", "42", '{"type": "Results"}', '{"type": "Results", "channel": []}',
    '{"type": "Results", "channel": {"alternatives": []}}', '{"type": "Results", "channel": {"alternatives": [5]}}',
    '{"type": "Results", "channel": {"alternatives": [{"transcript": 7}]}}', '{"type": 5}', '{"no_type": 1}',
    '{"type":"Results","channel":{"alternatives":[{"transcript":"x"}]},"is_final":"yes"}',
    b"\xff\xfe\x00bad utf8", '{"type":"Mystery"}',
])
def test_malformed_messages_are_ignored_safely(bad):
    stt, conn, _, _ = make()
    stt.feed(b"\x00")
    conn.inbox += [bad, results("still works", final=True)]
    out = stt.poll()
    assert [t.text for t in out if t.text == "still works"] == ["still works"]
    assert all(isinstance(t.text, str) for t in out)


def test_oversized_message_is_ignored():
    stt, conn, _, _ = make()
    stt.feed(b"\x00")
    conn.inbox += [results("a" * (s.MAX_MESSAGE_BYTES + 1), final=True), results("ok", final=True)]
    assert [t.text for t in stt.poll()] == ["ok"]


def test_poll_drain_is_bounded_per_call():
    stt, conn, _, _ = make()
    stt.feed(b"\x00")
    conn.inbox += [results(f"w{i}", final=True) for i in range(s.MAX_MESSAGES_PER_POLL + 25)]
    assert len(stt.poll()) == s.MAX_MESSAGES_PER_POLL
    assert len(stt.poll()) == 25


def test_poll_before_any_audio_is_empty_and_does_not_connect():
    stt, _, calls, _ = make()
    assert stt.poll() == [] and calls == []


# ------------------------------------------------------------------ keepalive
def test_keepalive_cadence_when_idle_uses_injected_clock():
    stt, conn, _, clock = make()
    stt.feed(b"\x00" * 160)
    clock.t += s.KEEPALIVE_SECONDS - 0.5
    stt.poll()
    assert conn.text_sent == []                                  # not yet
    clock.t += 1.0
    stt.poll()
    assert conn.text_sent == [{"type": "KeepAlive"}]             # sent as a TEXT frame
    stt.poll()
    assert len(conn.text_sent) == 1                              # not repeated within the interval
    clock.t += s.KEEPALIVE_SECONDS
    stt.poll()
    assert len(conn.text_sent) == 2


def test_no_keepalive_while_audio_is_flowing():
    stt, conn, _, clock = make()
    for _ in range(20):
        clock.t += 1.0
        stt.feed(b"\x00" * 160)
        stt.poll()
    assert conn.text_sent == []


def test_keepalive_interval_is_inside_deepgrams_3_to_5_second_window():
    assert 3.0 <= s.KEEPALIVE_SECONDS <= 5.0


# ------------------------------------------------------------------ close
def test_close_sends_close_stream_then_closes_once():
    stt, conn, _, _ = make()
    stt.feed(b"\x00")
    stt.close()
    stt.close()
    assert conn.text_sent == [{"type": "CloseStream"}] and conn.closed is True


def test_close_swallows_transport_errors_and_still_closes():
    stt, conn, _, _ = make()
    stt.feed(b"\x00")
    conn.send_text = lambda t: (_ for _ in ()).throw(OSError("gone"))
    stt.close()
    assert conn.closed is True


def test_use_after_close_raises_instead_of_reconnecting():
    stt, _, calls, _ = make()
    stt.feed(b"\x00")
    stt.close()
    with pytest.raises(s.ProviderError):
        stt.feed(b"\x00")
    assert len(calls) == 1                                       # no reconnect, ever


# ------------------------------------------------------------------ failures -> provider error
def test_remote_close_raises_provider_error_with_code_only():
    stt, conn, _, _ = make()
    stt.feed(b"\x00")
    conn.inbox.append(s.ProviderConnectionClosed(1011, reason=f"NET-0001 {SECRET_TEXT} {KEY}"))
    with pytest.raises(s.ProviderError) as e:
        stt.poll()
    assert "1011" in str(e.value)
    assert KEY not in str(e.value) and "SENTINELTEXT" not in str(e.value)


def test_error_message_from_provider_raises_without_echoing_it():
    stt, conn, _, _ = make()
    stt.feed(b"\x00")
    conn.inbox.append(json.dumps({"type": "Error", "description": f"bad {SECRET_TEXT}"}))
    with pytest.raises(s.ProviderError) as e:
        stt.poll()
    assert "SENTINELTEXT" not in str(e.value)


def test_send_failure_raises_provider_error_and_stays_failed():
    stt, conn, calls, _ = make()
    stt.feed(b"\x00")
    conn.send_bytes = lambda d: (_ for _ in ()).throw(OSError("pipe"))
    with pytest.raises(s.ProviderError):
        stt.feed(b"\x00")
    with pytest.raises(s.ProviderError):
        stt.feed(b"\x00")
    assert len(calls) == 1


def test_provider_error_is_a_failure_for_streamcall_and_falls_back(caplog):
    caplog.set_level(logging.DEBUG)
    stt, conn, _, clock = make()

    class Updater:
        def __init__(self):
            self.sent = []

        def update_call(self, sid, twiml):
            self.sent.append(twiml)

    up = Updater()
    call = s.StreamCall(call_sid="CA1", client_id="c", stt=stt, updater=up, turn_handler=lambda t: None,
                        fallback_twiml=lambda: "<Response>gather</Response>", clock=clock)
    conn.inbox.append(s.ProviderConnectionClosed(1006))
    import base64
    call.handle_event({"event": "media", "media": {"payload": base64.b64encode(b"\xff" * 160).decode()}})
    assert call.fallback_reason == "stt_error"
    assert up.sent == ["<Response>gather</Response>"]
    assert conn.closed is True


def test_transcript_text_is_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    stt, conn, _, _ = make()
    stt.feed(b"\x00")
    conn.inbox += [results(SECRET_TEXT, final=False), results(SECRET_TEXT, final=True, speech_final=True),
                   "garbage " + SECRET_TEXT, s.ProviderConnectionClosed(1011, reason=SECRET_TEXT)]
    with pytest.raises(s.ProviderError):
        stt.poll()
    stt.close()
    assert "SENTINELTEXT" not in caplog.text and KEY not in caplog.text


# ------------------------------------------------------------------ default connection (library mapping, still no sockets)
def test_default_connect_is_lazy_and_maps_library_closes(monkeypatch):
    import websockets.sync.client as wsc
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    seen = {}

    class FakeWS:
        def send(self, data):
            seen["sent"] = data

        def recv(self, timeout=None):
            raise ConnectionClosedError(Close(1011, "secret reason"), None)

        def close(self):
            seen["closed"] = True

    def fake_connect(url, **kw):
        seen["url"], seen["kw"] = url, kw
        return FakeWS()

    monkeypatch.setattr(wsc, "connect", fake_connect)
    conn = s.default_connect("wss://api.deepgram.com/v1/listen?x=1", {"Authorization": "Token k"})
    assert seen["kw"]["additional_headers"] == {"Authorization": "Token k"}
    assert seen["kw"].get("open_timeout", 0) > 0
    conn.send_bytes(b"\x01")
    assert seen["sent"] == b"\x01"
    with pytest.raises(s.ProviderConnectionClosed) as e:
        conn.recv(0)
    assert e.value.code == 1011


def test_default_connect_recv_timeout_returns_none(monkeypatch):
    import websockets.sync.client as wsc

    class FakeWS:
        def recv(self, timeout=None):
            raise TimeoutError

    monkeypatch.setattr(wsc, "connect", lambda url, **kw: FakeWS())
    assert s.default_connect("wss://x", {}).recv(0) is None


# ------------------------------------------------------------------ live smoke script: refusal logic only, never connects
def _smoke():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "stream_stt_smoke.py"
    spec = importlib.util.spec_from_file_location("stream_stt_smoke", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_smoke_script_refuses_without_flag_key_or_file(capsys):
    m = _smoke()
    assert m.main(["x.wav"], {"DEEPGRAM_API_KEY": KEY}) == 2                          # no confirmation flag
    assert m.main([m.CONFIRM_FLAG, "x.wav"], {}) == 2                                 # no key
    assert m.main([m.CONFIRM_FLAG], {"DEEPGRAM_API_KEY": KEY}) == 2                   # no fixture
    assert KEY not in capsys.readouterr().err


def test_smoke_script_wav_parser_accepts_only_8k_mono_mulaw(tmp_path):
    import struct
    m = _smoke()

    def wav(tag=7, ch=1, rate=8000, bits=8, payload=b"\xff" * 400):
        fmt = struct.pack("<HHIIHH", tag, ch, rate, rate * ch * bits // 8, ch * bits // 8, bits)
        body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(payload)) + payload
        return b"RIFF" + struct.pack("<I", len(body)) + body

    good = tmp_path / "g.wav"
    good.write_bytes(wav())
    assert m.read_mulaw_wav(str(good)) == b"\xff" * 400
    for name, blob in {"pcm": wav(tag=1, bits=16), "16k": wav(rate=16000), "stereo": wav(ch=2), "junk": b"nope"}.items():
        bad = tmp_path / f"{name}.wav"
        bad.write_bytes(blob)
        with pytest.raises(ValueError):
            m.read_mulaw_wav(str(bad))
