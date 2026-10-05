"""Offline checks for scripts/stream_e2e_local.py (the developer-run real-Deepgram / fake-Twilio harness). Never connects anywhere."""
import importlib.util
import os
import pathlib
import sys


def _mod():
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "stream_e2e_local.py"
    spec = importlib.util.spec_from_file_location("stream_e2e_local", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["stream_e2e_local"] = mod            # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


KEY = "synthetic-not-a-real-key"


def test_e2e_script_refuses_without_flag_key_or_fixture(capsys):
    m = _mod()
    assert m.main(["x.wav"], {"DEEPGRAM_API_KEY": KEY}) == 2
    assert m.main([m.CONFIRM_FLAG, "x.wav"], {}) == 2
    assert m.main([m.CONFIRM_FLAG], {"DEEPGRAM_API_KEY": KEY}) == 2
    assert m.main([m.CONFIRM_FLAG, "--bad-key"], {"DEEPGRAM_API_KEY": KEY}) == 2
    assert KEY not in capsys.readouterr().err


def test_e2e_script_allows_when_everything_is_supplied():
    m = _mod()
    args, reason = m.check_preconditions([m.CONFIRM_FLAG, "--bad-key", "f.wav"], {"DEEPGRAM_API_KEY": KEY})
    assert reason is None and args.bad_key and args.wav == "f.wav"


def test_e2e_script_import_does_not_touch_the_environment():
    before = dict(os.environ)
    _mod()
    assert dict(os.environ) == before


def test_fake_model_snapshots_the_messages_it_was_asked():
    m = _mod()
    fake = m.FakeAnthropic("ok")
    history = [{"role": "user", "content": "first"}]
    fake.messages.create(messages=history)
    history.append({"role": "assistant", "content": "later"})
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "first"}]


def test_fake_twilio_rest_records_updates():
    m = _mod()
    rest = m.FakeTwilioRest()
    rest.calls("CA1").update(twiml="<Response/>")
    assert [(sid, tw) for _, sid, tw in rest.updates] == [("CA1", "<Response/>")] and rest.event.is_set()
