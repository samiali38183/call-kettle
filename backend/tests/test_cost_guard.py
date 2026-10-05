from datetime import datetime, timedelta, timezone

from app import cost_guard
from app.config import load_client_config


def test_under_limits_passes():
    config = load_client_config("demo_dental")
    result = cost_guard.check_limits(config=config, turn_count=1, started_at=datetime.now(timezone.utc))
    assert result.exceeded is False


def test_turn_limit_triggers():
    config = load_client_config("demo_dental")
    result = cost_guard.check_limits(
        config=config, turn_count=config.max_turns, started_at=datetime.now(timezone.utc)
    )
    assert result.exceeded is True
    assert result.reason == "max_turns_reached"


def test_duration_limit_triggers():
    config = load_client_config("demo_dental")
    started = datetime.now(timezone.utc) - timedelta(seconds=config.max_call_seconds + 10)
    result = cost_guard.check_limits(config=config, turn_count=1, started_at=started)
    assert result.exceeded is True
    assert result.reason == "max_duration_reached"


def test_trim_history_keeps_bounded_length():
    messages = [{"role": "user", "content": str(i)} for i in range(50)]
    trimmed = cost_guard.trim_history(messages)
    assert len(trimmed) <= cost_guard.MAX_HISTORY_MESSAGES
    assert trimmed[0] == messages[0]
    assert trimmed[-1] == messages[-1]


def test_trim_history_noop_when_short():
    messages = [{"role": "user", "content": "hi"}]
    assert cost_guard.trim_history(messages) == messages


# ---- long calls with tool use must still produce a valid conversation for the model API

class _Block:
    def __init__(self, type, **kw):
        self.type = type
        self.__dict__.update(kw)


def _long_tool_conversation(turns: int) -> list[dict]:
    """What run_turn builds: a user line, then an assistant tool_use, then a user tool_result, then the reply."""
    msgs = []
    for i in range(turns):
        msgs.append({"role": "user", "content": f"caller line {i}"})
        msgs.append({"role": "assistant", "content": [_Block("tool_use", id=f"t{i}", name="check_availability", input={})]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "{}"}]})
        msgs.append({"role": "assistant", "content": [_Block("text", text=f"reply {i}")]})
    return msgs


def _is_valid_for_api(msgs: list[dict]) -> bool:
    """Every tool_result must directly follow the assistant message holding its tool_use, and vice versa."""
    for i, m in enumerate(msgs):
        content = m["content"]
        if m["role"] == "user" and isinstance(content, list) and any(b.get("type") == "tool_result" for b in content):
            prev = msgs[i - 1] if i else None
            ids = {b.id for b in prev["content"] if getattr(b, "type", "") == "tool_use"} if prev and prev["role"] == "assistant" else set()
            if not ids or not {b["tool_use_id"] for b in content} <= ids:
                return False
        if m["role"] == "assistant" and isinstance(content, list) and any(getattr(b, "type", "") == "tool_use" for b in content):
            nxt = msgs[i + 1] if i + 1 < len(msgs) else None
            if nxt is None or nxt["role"] != "user" or not isinstance(nxt["content"], list):
                return False
    return True


import pytest


@pytest.mark.parametrize("turns", range(3, 15))
def test_trimmed_history_never_orphans_a_tool_call(turns):
    from app import cost_guard

    trimmed = cost_guard.trim_history(_long_tool_conversation(turns))
    assert _is_valid_for_api(trimmed), f"{turns} turns: trimming cut a tool call away from its result"
    assert trimmed[-1]["role"] == "assistant"          # the newest exchange is intact
    assert trimmed[0]["content"] == "caller line 0"     # the caller's opening request is kept


def test_short_histories_are_untouched():
    from app import cost_guard

    msgs = _long_tool_conversation(3)[:10]
    assert cost_guard.trim_history(msgs) == msgs
