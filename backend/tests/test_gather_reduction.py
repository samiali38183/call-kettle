"""Gather-use reduction on the default (Twilio Gather) path. Twilio bills every Gather use (~$0.02), so a Gather that can
only time out is pure cost. Each scenario replays a scripted call through the real HTTP handlers and counts the Gathers the
server emitted (TwiML) and recorded (private ledger gather_count). Safety paths must stay untouched."""
import pytest

from app import agent, config as cfg, cost_observability as co, storage

FROM = "+170****0199"
SIDS = ("CAR1", "CAR2", "CAR3", "CAR4")

# Measured Gathers per scenario BEFORE this change (HEAD behaviour, recorded by running these scripts) and AFTER.
BEFORE = {"booking_end_call": 3, "booking_text_closing": 3, "silent_after_closing": 4, "emergency": 1,
          "silent_caller": 2, "spanish_text_closing": 4, "transfer_unanswered": 2}
AFTER = {"booking_end_call": 3, "booking_text_closing": 2, "silent_after_closing": 2, "emergency": 1,
         "silent_caller": 2, "spanish_text_closing": 3, "transfer_unanswered": 2}


@pytest.fixture
def env(app_client):
    client, main = app_client
    yield client, main
    for sid in SIDS:
        agent.end_session(sid)


def _script(monkeypatch, main, replies):
    it = iter(replies)
    monkeypatch.setattr(main.agent, "run_turn", lambda s, t: next(it))


class Call:
    def __init__(self, client, client_id="demo_dental", sid="CAR1"):
        self.client, self.client_id, self.sid, self.gathers, self.last = client, client_id, sid, 0, ""

    def _take(self, r):
        assert r.status_code == 200
        self.last = r.text
        self.gathers += r.text.count("<Gather ")
        return r.text

    def incoming(self):
        return self._take(self.client.post(f"/voice/incoming?client_id={self.client_id}", data={"CallSid": self.sid, "From": FROM}))

    def say(self, speech="", retry=0, digits=""):
        return self._take(self.client.post(f"/voice/gather?client_id={self.client_id}&retry={retry}",
                                           data={"CallSid": self.sid, "SpeechResult": speech, "Digits": digits, "From": FROM}))

    def ledger(self):
        ev = co.read_evidence(storage.DB_PATH, self.client_id, self.sid)
        return ev.get("gather_count", {}).get("value", 0)


def _ended(twiml):
    return "<Hangup" in twiml and "<Gather " not in twiml


def test_booking_with_end_call_unchanged(env, monkeypatch):
    client, main = env
    _script(monkeypatch, main, [("What day works?", False, None), ("And your name?", False, None), ("Booked, goodbye.", True, None)])
    c = Call(client)
    c.incoming(); c.say("I need to book"); c.say("Tuesday"); end = c.say("Dana")
    assert _ended(end)
    assert c.gathers == c.ledger() == AFTER["booking_end_call"] == BEFORE["booking_end_call"]


@pytest.mark.parametrize("closing", [
    "You're all set, Dana. Thanks for calling. Goodbye!",
    "Thanks for calling. Have a great day!",
    "Perfect, someone will call you back. Take care, bye.",
])
def test_closing_statement_without_end_call_hangs_up(env, monkeypatch, closing):
    client, main = env
    _script(monkeypatch, main, [("What day works?", False, None), (closing, False, None)])
    c = Call(client)
    c.incoming(); c.say("book me in")
    end = c.say("Tuesday")
    assert _ended(end), end
    assert closing.split(".")[0].split("!")[0] in end
    assert storage.get_call(c.sid)["outcome"] == "completed"
    assert agent.get_session(c.sid) is None


def test_booking_text_closing_gather_counts(env, monkeypatch):
    client, main = env
    _script(monkeypatch, main, [("What day works?", False, None), ("All set. Goodbye!", False, None)])
    c = Call(client)
    c.incoming(); c.say("book me in"); c.say("Tuesday")
    assert c.gathers == AFTER["booking_text_closing"]


def test_silent_after_closing_no_retry_and_no_false_escalation(env, monkeypatch):
    client, main = env
    _script(monkeypatch, main, [("What day works?", False, None), ("All set. Goodbye!", False, None)])
    c = Call(client)
    c.incoming(); c.say("book me in"); end = c.say("Tuesday")
    if not _ended(end):   # pre-change: the caller is silent and gets re-asked, then a false no-speech escalation
        c.say("", retry=0)
        c.say("", retry=1)
    assert c.gathers == AFTER["silent_after_closing"]
    assert (storage.get_call(c.sid) or {}).get("outcome") != "no_input"


@pytest.mark.parametrize("question", [
    "Is there anything else I can help with?",
    "Thanks, Dana. What number should we call? Goodbye for now is not needed.",
    "Do you want me to say goodbye or keep going?",
    "Okay, I have Tuesday. What time?",
    "Goodbye? Are you still there?",
])
def test_questions_and_open_prompts_still_gather(env, monkeypatch, question):
    client, main = env
    _script(monkeypatch, main, [(question, False, None)])
    c = Call(client)
    c.incoming()
    out = c.say("hello")
    assert "<Gather " in out and "<Hangup" not in out


def test_silent_caller_capped_at_one_retry(env):
    client, main = env
    c = Call(client)
    c.incoming(); c.say("", retry=0); end = c.say("", retry=1)
    assert _ended(end)
    assert c.gathers == c.ledger() == AFTER["silent_caller"] == BEFORE["silent_caller"]


def test_emergency_still_transfers_with_no_extra_gather(env, monkeypatch):
    client, main = env
    _script(monkeypatch, main, [("Call 911 now. Connecting you.", True, "+15555550111")])
    c = Call(client)
    c.incoming(); out = c.say("I smell gas in my house")
    assert "<Dial" in out and "<Gather " not in out
    assert c.gathers == AFTER["emergency"] == BEFORE["emergency"]


def test_spanish_switch_and_text_closing(env, monkeypatch):
    client, main = env
    _script(monkeypatch, main, [("\u00bfQu\u00e9 d\u00eda le conviene?", False, None), ("Listo. Gracias por llamar. Adi\u00f3s.", False, None)])
    c = Call(client, "demo_riverside")
    c.incoming()
    sp = c.say(digits="2")          # key press 2: the Spanish greeting is a real question, so it keeps its Gather
    assert "es-US" in sp and "<Gather " in sp
    c.say("necesito una cita")
    end = c.say("el martes")
    assert _ended(end)
    assert c.gathers == c.ledger() == AFTER["spanish_text_closing"]


def test_spanish_closing_question_not_cut(env, monkeypatch):
    client, main = env
    _script(monkeypatch, main, [("\u00bfAlgo m\u00e1s en lo que le pueda ayudar? Adi\u00f3s.", False, None)])
    c = Call(client, "demo_riverside")
    c.incoming(); out = c.say("hola")
    assert "<Gather " in out


def test_transfer_unanswered_still_collects_callback(env):
    client, main = env
    r = client.post("/voice/transfer-result?client_id=demo_dental",
                    data={"CallSid": "CAR4", "DialCallStatus": "no-answer", "From": FROM})
    assert r.status_code == 200 and r.text.count("<Gather ") == 1  # greeting 1 + transfer turn 0 + this callback prompt 1 = AFTER["transfer_unanswered"]
