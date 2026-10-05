from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import ClientConfig

MAX_TOOL_ITERATIONS_PER_TURN = 4
MAX_HISTORY_MESSAGES = 20  # trims token growth on long calls before it eats margin


@dataclass
class LimitCheck:
    exceeded: bool
    reason: str | None = None


def check_limits(*, config: ClientConfig, turn_count: int, started_at: datetime) -> LimitCheck:
    if turn_count >= config.max_turns:
        return LimitCheck(True, "max_turns_reached")
    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    if elapsed >= config.max_call_seconds:
        return LimitCheck(True, "max_duration_reached")
    return LimitCheck(False)


def _is_plain_user_turn(message: dict) -> bool:
    """A caller's own words, not a tool result the model is waiting on."""
    return message["role"] == "user" and isinstance(message["content"], str)


def trim_history(messages: list[dict]) -> list[dict]:
    """Keep the caller's opening request plus the most recent exchanges.

    The cut always lands on a caller's own words, never between a tool call and
    its result. The model API rejects a tool result with no matching tool call,
    which used to turn any long, tool-heavy call into an "I'm having trouble"
    hand-off. If no safe cut exists the history is left whole: a few extra
    tokens cost less than a failed call.
    """
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    earliest_tail_start = len(messages) - (MAX_HISTORY_MESSAGES - 1)
    for i in range(max(earliest_tail_start, 1), len(messages)):
        if _is_plain_user_turn(messages[i]):
            head = messages[0] if _is_plain_user_turn(messages[0]) else None
            return ([head] if head else []) + messages[i:]
    return messages


def _bkind(block):
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def _battr(block, name):
    return block.get(name) if isinstance(block, dict) else getattr(block, name, None)


_SYNTHETIC_RESULT = '{"error": "that action did not complete; apologize briefly and offer to have a human follow up"}'


def repair_history(messages: list[dict]) -> list[dict]:
    """Make a message history the model API will accept: every assistant tool_use is answered by a tool_result
    in the very next user message, no tool_result is orphaned, and no message is empty.

    A healthy history is returned unchanged (same list). Missing results become synthetic error results (the
    model is told the action did not complete); orphan results and empty messages are dropped. Never raises on
    odd input: the worst case is the history is returned as it came.
    """
    try:
        out: list[dict] = []
        changed = False
        i = 0
        n = len(messages)
        while i < n:
            m = messages[i]
            content = m.get("content")
            if isinstance(content, list) and not content:
                changed = True
                i += 1
                continue
            uses = [_battr(b, "id") for b in content if _bkind(b) == "tool_use"] if m.get("role") == "assistant" and isinstance(content, list) else []
            if uses:
                out.append(m)
                nxt = messages[i + 1] if i + 1 < n else None
                nxt_content = nxt.get("content") if nxt and nxt.get("role") == "user" else None
                if isinstance(nxt_content, list):
                    have = {_battr(b, "tool_use_id"): b for b in nxt_content if _bkind(b) == "tool_result"}
                    extras = [b for b in nxt_content if _bkind(b) != "tool_result"]
                    blocks = [have[u] for u in uses if u in have]
                    blocks += [{"type": "tool_result", "tool_use_id": u, "content": _SYNTHETIC_RESULT, "is_error": True} for u in uses if u not in have]
                    blocks += extras
                    if len(blocks) != len(nxt_content) or any(a is not b for a, b in zip(blocks, nxt_content)):
                        changed = True
                        nxt = {**nxt, "content": blocks}
                    out.append(nxt)
                    i += 2
                else:
                    changed = True
                    out.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": u, "content": _SYNTHETIC_RESULT, "is_error": True} for u in uses]})
                    i += 1
                continue
            if m.get("role") == "user" and isinstance(content, list) and any(_bkind(b) == "tool_result" for b in content):
                changed = True                       # a tool_result here has no tool_use before it (answered ones are consumed above)
                kept = [b for b in content if _bkind(b) != "tool_result"]
                if kept:
                    out.append({**m, "content": kept})
                i += 1
                continue
            out.append(m)
            i += 1
        if out and out[0].get("role") != "user":
            changed = True
            out.insert(0, {"role": "user", "content": "(The call began and the greeting was played.)"})
        return out if changed else messages
    except Exception:
        return messages
