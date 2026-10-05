"""Private cost ledger: server-measured gather_count (every emitted Gather) and tts_chars (text sent to <Say>)."""
import sqlite3

import pytest

from app import agent, config as cfg, cost_observability as co, storage

SECRET = "my number is+15555550100 and my secret"
RETRY_EN = "Sorry, could you say that again?"
RETRY_ES = "Perd\u00f3n, \u00bfpuede repetirlo?"
GATHER = "/voice/gather?client_id=demo_dental&retry="


@pytest.fixture
def env(app_client):
    client, main = app_client
    yield client, main
    for sid in ("CAG1", "CAG2", "CAG3"):
        agent.end_session(sid)


def _ev(sid="CAG1", tenant="demo_dental"):
    return co.read_evidence(storage.DB_PATH, tenant, sid)


def _post(client, retry, sid="CAG1", speech=""):
    return client.post(GATHER + str(retry), data={"CallSid": sid, "SpeechResult": speech, "From": "+170****0199"})


def _incoming(client, sid="CAG1"):
    return client.post("/voice/incoming?client_id=demo_dental", data={"CallSid": sid, "From": "+170****0199"})


def _script(monkeypatch, main, replies):
    it = iter(replies)
    monkeypatch.setattr(main.agent, "run_turn", lambda s, t: next(it))


def _greeting_len(main):
    config = cfg.load_client_config("demo_dental")
    n = len(main.CALL_DISCLOSURE + config.opening_line)
    return n + (len(main.SPANISH_PROMPT) if config.spanish else 0)


def test_scripted_call_counts_every_emitted_gather_and_say(env, monkeypatch):
    client, main = env
    _script(monkeypatch, main, [("Okay.", False, None), ("Bye now.", True, None)])
    responses = [_incoming(client)]                          # greeting gather
    responses.append(_post(client, 0))                       # silent -> retry gather
    responses.append(_post(client, 1, speech=SECRET))        # turn -> gather
    responses.append(_post(client, 0, speech="thanks"))      # closing -> say + hangup, no gather
    assert all(r.status_code == 200 for r in responses)
    emitted_gathers = sum(r.text.count("<Gather") for r in responses)
    assert emitted_gathers == 3
    ev = _ev()
    assert ev["gather_count"] == {"value": emitted_gathers, "status": "measured", "source": "server_emitted_gather"}
    expected = _greeting_len(main) + len(RETRY_EN) + len("Okay.") + len("Bye now.")
    assert ev["tts_chars"] == {"value": expected, "status": "measured", "source": "server_say_text"}


def test_no_double_count_callback_with_speech(env, monkeypatch):
    client, main = env
    _script(monkeypatch, main, [("Okay.", False, None)])
    _incoming(client)
    _post(client, 0, speech="hello")
    assert _ev()["gather_count"]["value"] == 2   # greeting + reply; the callback itself is not a new Gather


def test_transfer_announcement_counted_not_gather(env, monkeypatch):
    client, main = env
    _script(monkeypatch, main, [("Connecting you.", True, "+15555550111")])
    _incoming(client)
    _post(client, 0, speech="agent please")
    ev = _ev()
    assert ev["gather_count"]["value"] == 1
    assert ev["tts_chars"]["value"] == _greeting_len(main) + len("Connecting you.")
    assert ev["transfer_count"]["value"] == 1


def test_silent_ceiling_hangup_message_counted(env, monkeypatch):
    client, main = env
    monkeypatch.setattr(main.tools, "escalate_to_human", lambda **k: None)
    _incoming(client)
    r = _post(client, main.MAX_SILENT_RETRIES)
    assert "<Gather" not in r.text
    ev = _ev()
    assert ev["gather_count"]["value"] == 1
    assert ev["tts_chars"]["value"] == _greeting_len(main) + len("I'm not hearing anything \u2014 I'll have someone from the team follow up. Goodbye.")


def test_spanish_call_counts(env, monkeypatch):
    client, main = env
    _script(monkeypatch, main, [("Hola.", False, None)])
    _incoming(client)
    agent.get_session("CAG1").lang = "es"
    _post(client, 0)                                  # Spanish silent retry
    _post(client, 0, speech="hola")                   # Spanish reply
    ev = _ev()
    assert ev["gather_count"]["value"] == 3
    assert ev["tts_chars"]["value"] == _greeting_len(main) + len(RETRY_ES) + len("Hola.")


def test_stream_turn_counts_say_but_not_gather(env, monkeypatch):
    client, main = env
    _script(monkeypatch, main, [("Streamed.", False, None), ("Done.", True, None)])
    config = cfg.load_client_config("demo_dental")
    storage.log_call_start("CAG2", "demo_dental", "+170****0199")
    session = agent.start_session("CAG2", config)
    handler = main._stream_turn_handler("https://x.test", config, session, "CAG2")
    handler("caller words")
    handler("more caller words")
    ev = _ev("CAG2")
    assert "gather_count" not in ev
    assert ev["tts_chars"]["value"] == len("Streamed.") + len("Done.")


def test_stream_fallback_gather_is_counted(env):
    client, main = env
    config = cfg.load_client_config("demo_dental")
    storage.log_call_start("CAG2", "demo_dental", "+170****0199")
    session = agent.start_session("CAG2", config)
    main._gather(config, say_text=RETRY_EN, action_url="https://x.test/g", session=session)
    assert _ev("CAG2")["gather_count"]["value"] == 1


def test_ledger_failure_never_changes_responses(env, monkeypatch):
    client, main = env
    _script(monkeypatch, main, [("Okay.", False, None), ("Bye.", True, None)] * 2)
    storage.log_call_start("CAG3", "demo_dental", "+170****0199")
    plain = [_incoming(client, "CAG1"), _post(client, 0, "CAG1"), _post(client, 0, "CAG1", "hi"), _post(client, 0, "CAG1", "bye")]
    monkeypatch.setattr(co, "record_snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ledger down")))
    broken = [_incoming(client, "CAG3"), _post(client, 0, "CAG3"), _post(client, 0, "CAG3", "hi"), _post(client, 0, "CAG3", "bye")]
    for a, b in zip(plain, broken):
        assert a.status_code == b.status_code == 200
        assert a.text == b.text


def test_no_session_means_no_ledger_write(env):
    client, main = env
    config = cfg.load_client_config("demo_dental")
    storage.log_call_start("CAG2", "demo_dental", "+170****0199")
    main._gather(config, say_text="Hello", action_url="https://x.test/g")   # no session (ceiling-style path)
    assert _ev("CAG2") == {}


def test_tenant_isolation_and_no_caller_text(env, monkeypatch):
    client, main = env
    _script(monkeypatch, main, [("Okay.", False, None)])
    _incoming(client)
    _post(client, 0, speech=SECRET)
    assert _ev("CAG1", "other_tenant") == {}
    with sqlite3.connect(storage.DB_PATH) as conn:
        dump = repr(conn.execute("SELECT * FROM private_cost_usage").fetchall())
    assert "703" not in dump and "secret" not in dump and "5550199" not in dump
