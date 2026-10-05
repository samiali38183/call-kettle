import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("CALLKETTLE_DB_PATH", path)
    monkeypatch.setenv("CALLKETTLE_SKIP_SIGNATURE_CHECK", "1")
    import importlib

    from app import storage

    importlib.reload(storage)
    storage.init_db()
    yield storage
    os.remove(path)


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class FakeResponse:
    content: list
    stop_reason: str


class FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("no more fake responses queued")
        return self._responses.pop(0)


class FakeAnthropicClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


class FakeAnthropicClientRaises:
    class _Messages:
        def create(self, **kwargs):
            raise RuntimeError("simulated API outage")

    def __init__(self):
        self.messages = self._Messages()


def _config():
    from app.config import load_client_config

    return load_client_config("demo_dental")


def test_open_status_line_reports_open_during_business_hours():
    from app import agent

    now = datetime(2026, 1, 12, 10, 0, tzinfo=timezone.utc)  # Monday, 10:00 — demo_dental is 09:00-17:00
    line = agent._open_status_line(_config(), now)
    assert "OPEN" in line
    assert "CLOSED" not in line


def test_open_status_line_reports_closed_after_hours_same_day():
    from app import agent

    now = datetime(2026, 1, 12, 21, 0, tzinfo=timezone.utc)  # Monday, 21:00 — after 17:00 close
    line = agent._open_status_line(_config(), now)
    assert "CLOSED" in line
    assert "09:00-17:00" in line


def test_open_status_line_reports_closed_on_a_closed_day():
    from app import agent

    now = datetime(2026, 1, 11, 10, 0, tzinfo=timezone.utc)  # Sunday — demo_dental is closed
    line = agent._open_status_line(_config(), now)
    assert "CLOSED" in line
    assert "Sunday" in line


def test_system_prompt_embeds_the_open_status_line(monkeypatch):
    from app import agent

    # build_system_prompt uses real "now" internally, so just confirm the
    # status line's own marker text is present, not a specific open/closed
    # value that would make this test flaky depending on when it runs.
    prompt = agent.build_system_prompt(_config())
    assert "Right now:" in prompt
    assert "use this line; don't recompute it from the hours table" in prompt


def test_plain_text_reply(monkeypatch):
    from app import agent

    fake = FakeAnthropicClient([FakeResponse(content=[FakeTextBlock(text="We're open until 5pm.")], stop_reason="end_turn")])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)

    session = agent.start_session("CA_TEXT", _config())
    reply, should_end, _transfer = agent.run_turn(session, "what are your hours?")

    assert should_end is False
    assert "5pm" in reply
    assert len(fake.messages.calls) == 1


def test_tool_use_then_text_reply(monkeypatch):
    from app import agent

    responses = [
        FakeResponse(
            content=[FakeToolUseBlock(id="t1", name="check_availability", input={"date": "2026-01-12"})],
            stop_reason="tool_use",
        ),
        FakeResponse(content=[FakeTextBlock(text="I have 9am open Monday, does that work?")], stop_reason="end_turn"),
    ]
    fake = FakeAnthropicClient(responses)
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)

    session = agent.start_session("CA_TOOL", _config())
    reply, should_end, _transfer = agent.run_turn(session, "any openings monday?")

    assert should_end is False
    assert "9am" in reply
    assert len(fake.messages.calls) == 2  # one for the tool call, one for the follow-up


def test_end_call_tool_ends_immediately(monkeypatch):
    from app import agent

    fake = FakeAnthropicClient(
        [FakeResponse(content=[FakeToolUseBlock(id="t1", name="end_call", input={"closing_message": "Bye now!"})], stop_reason="tool_use")]
    )
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)

    session = agent.start_session("CA_END", _config())
    reply, should_end, _transfer = agent.run_turn(session, "that's all, thanks")

    assert should_end is True
    assert reply == "Bye now!"


def test_api_error_fails_soft_and_escalates(monkeypatch):
    from app import agent

    monkeypatch.setattr(agent, "_anthropic_client", lambda: FakeAnthropicClientRaises())

    session = agent.start_session("CA_ERR", _config())
    reply, should_end, _transfer = agent.run_turn(session, "hello?")

    assert should_end is True
    assert "trouble" in reply.lower()


def test_cost_guard_blocks_before_any_api_call(monkeypatch):
    from app import agent

    fake = FakeAnthropicClient([])  # asserts if create() is ever called
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)

    config = _config()
    session = agent.start_session("CA_LIMIT", config)
    session.turn_count = config.max_turns  # already at the cap
    reply, should_end, _transfer = agent.run_turn(session, "one more thing...")

    assert should_end is True
    assert len(fake.messages.calls) == 0  # the whole point: no wasted spend on a call we're ending anyway


def test_tool_failure_degrades_gracefully_instead_of_crashing(monkeypatch):
    from app import agent

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated: SMS send blew up, or any other tool-side failure")

    monkeypatch.setattr(agent, "_dispatch_tool", _boom)

    responses = [
        FakeResponse(
            content=[FakeToolUseBlock(id="t1", name="book_appointment", input={})],
            stop_reason="tool_use",
        ),
        FakeResponse(content=[FakeTextBlock(text="Sorry, something went wrong — I'll have someone call you.")], stop_reason="end_turn"),
    ]
    fake = FakeAnthropicClient(responses)
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)

    session = agent.start_session("CA_TOOLFAIL", _config())
    reply, should_end, _transfer = agent.run_turn(session, "book me in")  # must not raise

    assert should_end is False
    assert "something went wrong" in reply.lower()
    assert len(fake.messages.calls) == 2


@pytest.mark.parametrize(
    "phrase",
    [
        "I need to speak to a real person right now please",
        "can I talk to a human",
        "let me speak with someone",
        "please transfer me",
        "connect me to a representative",
        "I want to talk to somebody, not a robot",
        # Real gaps found testing against the live production line for
        # sample_homecare, caught before a real caller hit them again:
        "put me through to your mom",
        "can I talk to the owner",
        "put me through please",
        "can you connect me with the manager",
    ],
)
def test_explicit_human_request_transfers_deterministically_without_any_api_call(monkeypatch, phrase):
    """This is the actual production bug report: the AI didn't reliably
    transfer when asked for a real person. Root cause was that transfer
    depended entirely on LLM judgment, which isn't reliable several turns
    into a real conversation. This must now work with zero dependency on
    the model — no API call happens at all for these phrases."""
    from app import agent

    fake = FakeAnthropicClient([])  # asserts if create() is ever called
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)

    config = _config()
    session = agent.start_session("CA_HUMAN_REQ", config)
    reply, should_end, transfer_to = agent.run_turn(session, phrase)

    assert should_end is True
    assert transfer_to == config.escalation_phone
    assert len(fake.messages.calls) == 0


@pytest.mark.parametrize(
    "phrase",
    [
        "can you update my personal information",
        "is this a personal line",
        "don't impersonate anyone",
        "what are your service hours",
        "how long has the owner run this business",
        "is the manager position still open",
        "my mom needs care, not me",
    ],
)
def test_similar_but_non_matching_phrases_do_not_false_positive(monkeypatch, phrase):
    """Word-boundary matching must not fire on 'personal', 'impersonate',
    etc. just because they contain 'person' as a substring."""
    from app import agent

    assert agent._HUMAN_REQUEST_RE.search(phrase) is None


@pytest.mark.parametrize(
    "phrase",
    [
        "I smell gas in my basement",
        "there's a gas leak in the kitchen",
        "my carbon monoxide alarm is going off",
        "the house is on fire",
        "my husband is not breathing",
        "she's unconscious on the floor",
        "I think I'm having a heart attack",
        "someone is hurt",
    ],
)
def test_emergencies_get_911_advice_and_an_alert_without_any_api_call(monkeypatch, phrase):
    """Found in live testing: the model wrote the transfer message as a note to
    the owner, so a caller reporting a gas leak never heard 'call 911'. The
    advice is now deterministic and can't depend on the model."""
    from app import agent, storage

    fake = FakeAnthropicClient([])  # any API call would assert
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)
    config = _config()
    storage.log_call_start("CA_911", config.client_id, "+15555550100")
    session = agent.start_session("CA_911", config, caller_number="+15555550100")
    reply, should_end, transfer_to = agent.run_turn(session, phrase)

    assert "911" in reply
    assert should_end is True and transfer_to == config.escalation_phone
    assert len(fake.messages.calls) == 0
    import sqlite3

    conn = sqlite3.connect(storage.DB_PATH)
    reason = conn.execute("SELECT reason FROM escalations WHERE call_sid='CA_911'").fetchone()[0]
    conn.close()
    assert reason == "possible_emergency"


@pytest.mark.parametrize(
    "phrase",
    [
        "I need a gas water heater installed",
        "can you inspect my fireplace",
        "my gas stove needs a tune-up",
        "the fire alarm battery keeps chirping",
        "do you service gas furnaces",
        "I need to reschedule, my chest hurts from moving boxes so I can't lift",
    ],
)
def test_ordinary_requests_do_not_trigger_the_emergency_path(phrase):
    from app import agent

    assert agent._EMERGENCY_RE.search(phrase) is None


def test_transfer_call_tool_returns_escalation_phone_for_implicit_requests(monkeypatch):
    """Covers requests the deterministic keyword backstop doesn't catch —
    the model's own transfer_call tool is still the path for these."""
    from app import agent

    fake = FakeAnthropicClient(
        [
            FakeResponse(
                content=[
                    FakeToolUseBlock(
                        id="t1",
                        name="transfer_call",
                        input={"reason": "caller is distressed", "handoff_message": "Connecting you now."},
                    )
                ],
                stop_reason="tool_use",
            )
        ]
    )
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)

    config = _config()
    session = agent.start_session("CA_TRANSFER", config)
    reply, should_end, transfer_to = agent.run_turn(
        session, "this is really upsetting, I don't think this is helping me at all"
    )

    assert should_end is True
    assert reply == "Connecting you now."
    assert transfer_to == config.escalation_phone
    assert len(fake.messages.calls) == 1  # confirms this one *did* go through the model


def test_tool_loop_exhaustion_fails_soft_instead_of_hanging(monkeypatch):
    from app import agent

    # The model keeps calling a tool and never produces a text reply or
    # end_call — must not loop forever or crash, must bail out gracefully
    # after cost_guard.MAX_TOOL_ITERATIONS_PER_TURN attempts.
    responses = [
        FakeResponse(
            content=[FakeToolUseBlock(id=f"t{i}", name="check_availability", input={"date": "2026-01-12"})],
            stop_reason="tool_use",
        )
        for i in range(10)
    ]
    fake = FakeAnthropicClient(responses)
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)

    from app import cost_guard

    session = agent.start_session("CA_LOOP", _config())
    reply, should_end, transfer_to = agent.run_turn(session, "keep checking forever")

    assert should_end is True
    assert transfer_to == session.config.escalation_phone      # it says it is connecting the caller, so it does
    assert len(fake.messages.calls) == cost_guard.MAX_TOOL_ITERATIONS_PER_TURN


def test_duration_limit_blocks_before_any_api_call(monkeypatch):
    from app import agent

    fake = FakeAnthropicClient([])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)

    config = _config()
    session = agent.start_session("CA_LIMIT2", config)
    session.started_at = datetime.now(timezone.utc) - timedelta(seconds=config.max_call_seconds + 5)
    reply, should_end, _transfer = agent.run_turn(session, "hello")

    assert should_end is True
    assert len(fake.messages.calls) == 0


@pytest.mark.parametrize("phrase", [
    "Am I talking to a real person or a robot?", "are you a robot", "is this a real person", "am I speaking with a human",
    "Are you a machine?", "is this an AI",
])
def test_asking_whether_it_is_a_robot_gets_an_answer_not_a_transfer(monkeypatch, phrase):
    from app import agent

    fake = FakeAnthropicClient([FakeResponse([FakeTextBlock("I'm an AI receptionist. Want me to connect you with a person?")], "end_turn")])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)
    session = agent.start_session("CA_IDENT", _config())
    reply, should_end, transfer_to = agent.run_turn(session, phrase)
    assert transfer_to is None and should_end is False and "AI" in reply
    assert len(fake.messages.calls) == 1                                  # the model answered, honestly
    assert "whether they are talking to a real person or a robot" in fake.messages.calls[0]["system"]


@pytest.mark.parametrize("phrase", [
    "I want to talk to a real person", "let me speak to a human please", "can I talk to a real person", "put me through to a person",
])
def test_explicit_requests_for_a_person_still_transfer_immediately(monkeypatch, phrase):
    from app import agent

    fake = FakeAnthropicClient([])
    monkeypatch.setattr(agent, "_anthropic_client", lambda: fake)
    session = agent.start_session("CA_IDENT2", _config())
    _, should_end, transfer_to = agent.run_turn(session, phrase)
    assert should_end is True and transfer_to == _config().escalation_phone and len(fake.messages.calls) == 0
