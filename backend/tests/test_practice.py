"""Practice mode: a roleplayed skeptical owner and honest feedback, with no network."""
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "marketing"))
practice = importlib.import_module("practice")


@dataclass
class Block:
    text: str
    type: str = "text"


class Resp:
    def __init__(self, text):
        self.content = [Block(text)]


class FakeClient:
    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return Resp("Who is this?" if "roleplaying" in kw["system"] else "WHAT WORKED\nx")


def test_every_persona_is_defined_and_the_prompt_contains_the_product_truths():
    assert {"skeptical-owner", "office-manager", "receptionist", "servicetitan-user", "ai-skeptic", "price", "busy-owner"} <= set(practice.PERSONAS)
    c = FakeClient()
    practice.persona_reply(c, "servicetitan-user", [("FOUNDER", "Hi, it's Sami")])
    system = c.calls[0]["system"]
    assert "does NOT integrate" in system and "no customers" in system.lower() and "Never mention that this is practice" in system
    assert c.calls[0]["messages"][0]["role"] == "user"


def test_a_session_alternates_turns_and_ends_on_the_end_command():
    c = FakeClient()
    inputs = iter(["Hi, Sami with Call Kettle.", "", "What happens after hours?", "/end"])
    out = []
    turns = practice.run_session(c, "price", read=lambda _p: next(inputs), write=out.append)
    assert [w for w, _ in turns] == ["FOUNDER", "PROSPECT", "FOUNDER", "PROSPECT"]
    assert any("Who is this?" in x for x in out)


def test_talk_share_is_plain_arithmetic_with_no_score():
    share = practice.talk_share([("FOUNDER", "one two three four"), ("PROSPECT", "ok"), ("FOUNDER", "five six")])
    assert share["founder_words"] == 6 and share["prospect_words"] == 1 and share["founder_longest_turn_words"] == 4 and share["turns"] == 2
    c = FakeClient()
    practice.feedback(c, "price", [("FOUNDER", "hi there friend"), ("PROSPECT", "what")])
    assert "No scores" in c.calls[0]["system"] and "founder spoke 3 words" in c.calls[0]["messages"][0]["content"]
