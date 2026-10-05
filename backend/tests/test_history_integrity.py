"""Anthropic 400 'tool_use ids without tool_result' guard: every code path that leaves session.messages
for the next turn must keep tool_use/tool_result pairing intact."""
import copy
import os
import tempfile
from dataclasses import dataclass

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
class Text:
    text: str
    type: str = "text"


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class Resp:
    content: list
    stop_reason: str


def _blocks(message):
    content = message["content"]
    return content if isinstance(content, list) else []


def _kind(block):
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def _bid(block, attr):
    return block.get(attr) if isinstance(block, dict) else getattr(block, attr, None)


def violations(messages):
    """What the Anthropic API would reject (tool pairing and empty content)."""
    problems = []
    for i, m in enumerate(messages):
        if isinstance(m["content"], list) and not m["content"]:
            problems.append(f"{i}: empty content list")
        uses = [_bid(b, "id") for b in _blocks(m) if _kind(b) == "tool_use"] if m["role"] == "assistant" else []
        results = [_bid(b, "tool_use_id") for b in _blocks(m) if _kind(b) == "tool_result"] if m["role"] == "user" else []
        if uses:
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            got = [_bid(b, "tool_use_id") for b in _blocks(nxt) if _kind(b) == "tool_result"] if nxt and nxt["role"] == "user" else []
            if sorted(got) != sorted(uses):
                problems.append(f"{i}: tool_use {uses} not answered by next user message ({got})")
        if results:
            prev = messages[i - 1] if i else None
            want = [_bid(b, "id") for b in _blocks(prev) if _kind(b) == "tool_use"] if prev and prev["role"] == "assistant" else []
            if sorted(results) != sorted(want):
                problems.append(f"{i}: orphan tool_result {results} (previous tool_use {want})")
    return problems


class StrictMessages:
    """Behaves like the API: rejects an invalid history with an exception, records the history it was sent."""

    def __init__(self, responses, on_call=None):
        self._responses = list(responses)
        self.sent = []
        self.on_call = on_call

    def create(self, **kwargs):
        snapshot = copy.deepcopy(kwargs["messages"])
        self.sent.append(snapshot)
        bad = violations(snapshot)
        if bad:
            raise RuntimeError("400 invalid_request_error: tool_use ids without tool_result: " + "; ".join(bad))
        if self.on_call:
            self.on_call(len(self.sent))
        return self._responses.pop(0)


class StrictClient:
    def __init__(self, responses, on_call=None):
        self.messages = StrictMessages(responses, on_call)


def _config():
    from app.config import load_client_config

    return load_client_config("demo_dental")


def _multi(n=3, prefix="t"):
    return Resp([ToolUse(f"{prefix}{k}", "check_availability", {"date": "2026-01-12"}) for k in range(n)], "tool_use")


def _use(monkeypatch, client):
    from app import agent

    monkeypatch.setattr(agent, "_anthropic_client", lambda: client)


def _run(session, text="hello"):
    from app import agent

    return agent.run_turn(session, text)


# ---- normal behaviour stays valid ---------------------------------------------------------------

def test_multi_tool_turn_is_valid_and_answered(monkeypatch):
    from app import agent

    client = StrictClient([_multi(3), Resp([Text("Nine works.")], "end_turn")])
    _use(monkeypatch, client)
    s = agent.start_session("CH1", _config())
    reply, end, _ = _run(s, "openings?")
    assert reply == "Nine works." and end is False
    assert violations(s.messages) == []
    assert len(client.messages.sent) == 2


def test_budget_break_after_tool_iteration_leaves_valid_history(monkeypatch):
    from app import agent

    client = StrictClient([_multi(2)], on_call=lambda n: setattr(agent, "TURN_BUDGET_SECONDS", -1.0))
    _use(monkeypatch, client)
    monkeypatch.setattr(agent, "TURN_BUDGET_SECONDS", 8.0)
    s = agent.start_session("CH2", _config())
    _run(s, "openings?")                      # budget trips after the first round trip: hand-off
    assert violations(s.messages) == []
    monkeypatch.setattr(agent, "TURN_BUDGET_SECONDS", 8.0)
    client2 = StrictClient([Resp([Text("Still here.")], "end_turn")])
    _use(monkeypatch, client2)
    assert _run(s, "hello?")[0] == "Still here."
    assert violations(s.messages) == []


def test_max_tool_iterations_exit_leaves_valid_history(monkeypatch):
    from app import agent, cost_guard

    client = StrictClient([_multi(2, f"i{k}") for k in range(cost_guard.MAX_TOOL_ITERATIONS_PER_TURN)])
    _use(monkeypatch, client)
    s = agent.start_session("CH3", _config())
    _run(s, "openings?")
    assert violations(s.messages) == []
    client2 = StrictClient([Resp([Text("ok")], "end_turn")])
    _use(monkeypatch, client2)
    _run(s, "again")
    assert violations(s.messages) == []


# ---- the holes ----------------------------------------------------------------------------------

def test_max_tokens_stop_with_tool_use_blocks_is_not_left_orphaned(monkeypatch):
    from app import agent

    truncated = Resp([Text("Let me check"), ToolUse("m1", "check_availability", {"date": "2026-01-12"})], "max_tokens")
    client = StrictClient([truncated])
    _use(monkeypatch, client)
    s = agent.start_session("CH4", _config())
    _run(s, "openings?")
    assert violations(s.messages) == []
    client2 = StrictClient([Resp([Text("Nine works.")], "end_turn")])
    _use(monkeypatch, client2)
    assert _run(s, "ok?")[0] == "Nine works."      # next turn is not a 400


def test_stop_reason_tool_use_without_tool_blocks_never_sends_empty_results(monkeypatch):
    from app import agent

    client = StrictClient([Resp([Text("hmm")], "tool_use"), Resp([Text("Fine.")], "end_turn")])
    _use(monkeypatch, client)
    s = agent.start_session("CH5", _config())
    assert _run(s, "openings?")[0] == "Fine."
    assert len(client.messages.sent) == 2
    assert all(violations(history) == [] for history in client.messages.sent)
    assert violations(s.messages) == []


def test_exception_between_assistant_append_and_tool_results(monkeypatch):
    from app import agent

    calls = {"n": 0}
    real = agent._record_outcome

    def boom(session, name, result):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("bookkeeping failed mid-tool-loop")
        return real(session, name, result)

    monkeypatch.setattr(agent, "_record_outcome", boom)
    client = StrictClient([_multi(3)])
    _use(monkeypatch, client)
    s = agent.start_session("CH6", _config())
    with pytest.raises(RuntimeError):
        _run(s, "openings?")
    assert violations(s.messages) == []
    client2 = StrictClient([Resp([Text("Sorry about that.")], "end_turn")])
    _use(monkeypatch, client2)
    assert _run(s, "hello?")[0] == "Sorry about that."


def test_repair_history_fixes_every_shape():
    from app import cost_guard

    def tu(i):
        return {"type": "tool_use", "id": i, "name": "x", "input": {}}

    def tr(i):
        return {"type": "tool_result", "tool_use_id": i, "content": "{}"}

    shapes = [
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": [tu("1"), tu("2")]}],                       # orphan at end
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": [tu("1"), tu("2")]},
         {"role": "user", "content": [tr("1")]}],                                                                           # partial
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": [tu("1")]}, {"role": "user", "content": "b"}],  # skipped result
        [{"role": "user", "content": [tr("9")]}, {"role": "assistant", "content": "x"}],                                  # orphan result first
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": "x"}, {"role": "user", "content": [tr("9")]}],
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": [tu("1")]}, {"role": "user", "content": [tr("1"), tr("7")]}],
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": [tu("1")]}, {"role": "user", "content": []}],
    ]
    for shape in shapes:
        fixed = cost_guard.repair_history(copy.deepcopy(shape))
        assert violations(fixed) == [], shape
    ok = [{"role": "user", "content": "a"}, {"role": "assistant", "content": [tu("1")]}, {"role": "user", "content": [tr("1")]}]
    assert cost_guard.repair_history(ok) == ok            # a healthy history is returned untouched


def test_poisoned_session_history_is_repaired_before_the_api_call(monkeypatch):
    from app import agent

    s = agent.start_session("CH7", _config())
    s.messages = [{"role": "user", "content": "hi"},
                  {"role": "assistant", "content": [ToolUse("p1", "check_availability", {}), ToolUse("p2", "check_availability", {})]}]
    client = StrictClient([Resp([Text("Recovered.")], "end_turn")])
    _use(monkeypatch, client)
    assert _run(s, "hello?")[0] == "Recovered."
    assert violations(s.messages) == []


# ---- trimming and recovery ----------------------------------------------------------------------

def _long_history(turns, tools_per_turn):
    msgs = []
    for t in range(turns):
        msgs.append({"role": "user", "content": f"caller {t}"})
        ids = [f"u{t}_{k}" for k in range(tools_per_turn)]
        if ids:
            msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": i, "name": "x", "input": {}} for i in ids]})
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content": "{}"} for i in ids]})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"reply {t}"}]})
    return msgs


@pytest.mark.parametrize("tools_per_turn", [0, 1, 3])
def test_trim_history_never_splits_a_tool_exchange_at_any_length(tools_per_turn):
    from app import cost_guard

    for turns in range(1, 30):
        history = _long_history(turns, tools_per_turn) + [{"role": "user", "content": "now"}]
        assert violations(cost_guard.trim_history(history)) == [], (turns, tools_per_turn)


def test_trim_with_no_safe_cut_still_valid_after_repair():
    from app import cost_guard

    history = [{"role": "user", "content": "start"}]
    for k in range(15):
        history.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"a{k}", "name": "x", "input": {}}]})
        history.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"a{k}", "content": "{}"}]})
    assert violations(cost_guard.repair_history(cost_guard.trim_history(history))) == []


def test_recovered_session_history_is_valid_and_next_turn_works(monkeypatch, temp_db):
    import json

    from app import agent, storage

    config = _config()
    storage.log_call_start("CR1", config.client_id, "+1703****0100")
    transcript = [{"role": "ai", "text": "Hello"}, {"role": "caller", "text": "hi"}, {"role": "ai", "text": "How can I help"}, {"role": "caller", "text": "x"}]
    with storage._conn() as conn:
        conn.execute("UPDATE calls SET transcript_json=? WHERE call_sid=?", (json.dumps(transcript), "CR1"))
    s = agent.recover_session("CR1", config, "+1703****0100")
    assert violations(s.messages) == []
    client = StrictClient([Resp([Text("Sure.")], "end_turn")])
    _use(monkeypatch, client)
    assert _run(s, "x")[0] == "Sure."
