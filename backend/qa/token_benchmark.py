"""Offline token / TTS-character benchmark of the REAL agent path (app.agent.run_turn).

Hard boundaries (same spirit as qa/margin_benchmark.py):

* NEVER calls the network or a real model. A scripted fake client returns canned model responses; every
  ``messages.create`` request the agent builds is captured and measured. Tool results are canned too.
* Token counts are an ESTIMATE: ``ceil(characters / 4)`` of the JSON the agent sends (system + tools + messages).
  That is a proxy, not Anthropic's tokenizer. It is only comparable with itself (before/after), never with an
  invoice. ``measurement_scope`` in the report says so; callers must check it.
* Reply TTS characters come from the SCRIPTED model replies, so they say nothing about how verbose the live model
  is; they show how many characters a fixed-length reply costs and measure the fixed server-side phrases exactly.
  Live reply length is UNMEASURED offline (see docs/MARGIN_RECOVERY_REPORT.md).
"""
from __future__ import annotations

import json
import math
from contextlib import contextmanager
from dataclasses import dataclass

CHARS_PER_TOKEN = 4
ESTIMATOR = "ceil(json_chars/4)"
MEASUREMENT_SCOPE = "offline_estimate_chars_div_4_scripted_model_not_provider_tokens"
CLIENT_ID = "demo_hvac"


def est_tokens(obj) -> int:
    text = obj if isinstance(obj, str) else json.dumps(obj, default=_default, separators=(",", ":"), ensure_ascii=False)
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _default(o):
    if hasattr(o, "model_dump"):
        return o.model_dump()
    if hasattr(o, "__dict__"):
        return {k: v for k, v in vars(o).items() if not k.startswith("_")}
    return str(o)


@dataclass
class _Text:
    text: str
    type: str = "text"


@dataclass
class _ToolUse:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class _Resp:
    content: list
    stop_reason: str


def say(text):
    return _Resp([_Text(text)], "end_turn")


def tool(name, tool_input, n=1):
    return _Resp([_ToolUse(f"tu_{name}_{n}", name, tool_input)], "tool_use")


TOOL_RESULTS = {
    "check_availability": {"slots": ["09:00", "10:30", "13:00"]},
    "book_appointment": {"success": True, "booking_id": 41, "confirmation_text_sent": True},
    "find_my_appointments": {"appointments": [{"booking_id": 41, "service": "AC repair", "date": "2026-01-12", "time": "10:00"}]},
    "cancel_appointment": {"success": True},
    "reschedule_appointment": {"success": True},
    "escalate_to_human": {"escalated": True},
    "end_call": {"ended": True},
    "transfer_call": {"transferring": True},
}

# Each scenario is a list of caller turns: (caller_text, [scripted model responses for that turn, in order]).
SCENARIOS = {
    "hours_question": [
        ("Hi, what time do you close today?", [say("We close at five today. Anything else I can help with?")]),
    ],
    "booking_flow": [
        ("My air conditioner stopped cooling.", [say("I'm sorry about that. What day works for a technician?")]),
        ("Tomorrow morning if possible.", [tool("check_availability", {"date": "2026-01-06", "service": "AC repair"}),
                                           say("I have nine, ten thirty, or one. Which do you prefer?")]),
        ("Ten thirty works. I'm John Smith.", [say("Thanks John Smith. Is the number you're calling from the best one to reach you?")]),
        ("Yes it is.", [tool("book_appointment", {"caller_name": "John Smith", "caller_phone": "+15555550100", "service": "AC repair",
                                                  "date": "2026-01-06", "time": "10:30"}),
                        say("You're booked for ten thirty tomorrow, John Smith. A confirmation text is on its way.")]),
        ("No that's all, thanks, bye.", [tool("end_call", {"closing_message": "Thanks for calling, goodbye!"})]),
    ],
    "cancel_flow": [
        ("I need to cancel my appointment.", [tool("find_my_appointments", {}),
                                              say("I see AC repair on January twelfth at ten. Cancel that one?")]),
        ("Yes cancel it.", [tool("cancel_appointment", {"booking_id": 41}),
                            say("It's cancelled. Anything else?")]),
        ("No thanks.", [tool("end_call", {"closing_message": "Okay, goodbye!"})]),
    ],
    "price_then_callback": [
        ("How much does a tune up cost?", [say("The technician explains the cost before any work starts. Want me to book one?")]),
        ("Actually I'd rather talk about a warranty problem.", [tool("escalate_to_human", {
            "reason": "warranty_question", "caller_name": "Pat Lee", "caller_phone": "+15555550100",
            "summary": "Caller has a warranty question and wants a callback."}),
            say("I've passed that to the team, and they'll call you back shortly.")]),
    ],
    "long_conversation": [
        (f"Question number {i}: do you service heat pumps in my area?", [say("Yes, we do. What else would you like to know?")])
        for i in range(1, 11)
    ],
    "emergency_shortcut": [
        ("I smell gas in my kitchen!", []),   # answered by the server's safety net: no model call at all
    ],
}


@contextmanager
def _patched_agent(model_responses):
    """Route the real run_turn through a scripted fake client with no DB, network or tool side effects."""
    from app import agent, tools

    captured = []

    class _Messages:
        def create(self, **kwargs):
            # Deep-copy the request as sent: run_turn keeps mutating session.messages afterwards.
            captured.append(json.loads(json.dumps(kwargs, default=_default)))
            if not model_responses:
                raise AssertionError("scripted benchmark ran out of model responses")
            return model_responses.pop(0)

    class _Client:
        messages = _Messages()

    saved = (agent._anthropic_client, agent._dispatch_tool, agent._log_turn, agent.record_usage, tools.escalate_to_human)
    agent._anthropic_client = lambda: _Client()
    agent._dispatch_tool = lambda session, name, tool_input: dict(TOOL_RESULTS.get(name, {}))
    agent._log_turn = lambda *a, **k: None
    agent.record_usage = lambda *a, **k: None
    tools.escalate_to_human = lambda **k: {"escalated": True}
    try:
        yield captured
    finally:
        agent._anthropic_client, agent._dispatch_tool, agent._log_turn, agent.record_usage, tools.escalate_to_human = saved


def run_scenario(name: str, turns=None) -> dict:
    from app import agent, summary
    from app.config import load_client_config

    turns = SCENARIOS[name] if turns is None else turns
    config = load_client_config(CLIENT_ID)
    responses = [r for _, rs in turns for r in rs]
    session = agent.CallSession(call_sid=f"bench-{name}", client_id=config.client_id, config=config,
                                caller_number="+15555550100")
    replies, transcript = [], []
    with _patched_agent(responses) as captured:
        for caller_text, _ in turns:
            transcript.append({"role": "caller", "text": caller_text})
            reply, should_end, _transfer = agent.run_turn(session, caller_text)
            replies.append(reply)
            transcript.append({"role": "ai", "text": reply})
            if should_end:
                break
    calls = []
    for req in captured:
        system, tool_schemas, msgs = req.get("system", ""), req.get("tools", []), req.get("messages", [])
        calls.append({
            "system_tokens": est_tokens(system), "tool_schema_tokens": est_tokens(tool_schemas),
            "message_tokens": est_tokens(msgs), "max_tokens": req.get("max_tokens"),
            "messages": len(msgs),
        })
    for c in calls:
        c["input_tokens"] = c["system_tokens"] + c["tool_schema_tokens"] + c["message_tokens"]
    summary_input = est_tokens(summary._PROMPT) + est_tokens("\n".join(f"{t['role'].upper()}: {t['text']}" for t in transcript))
    tts_chars = sum(len(r) for r in replies)
    return {
        "scenario": name,
        "caller_turns": len(replies),
        "model_calls": len(calls),
        "calls": calls,
        "system_tokens_per_call": calls[0]["system_tokens"] if calls else 0,
        "tool_schema_tokens_per_call": calls[0]["tool_schema_tokens"] if calls else 0,
        "history_tokens_sent_total": sum(c["message_tokens"] for c in calls),
        "last_call_history_tokens": calls[-1]["message_tokens"] if calls else 0,
        "input_tokens": sum(c["input_tokens"] for c in calls),
        "max_tokens_setting": max((c["max_tokens"] or 0) for c in calls) if calls else 0,
        "summary_input_tokens": summary_input if any(t["role"] == "caller" for t in transcript) else 0,
        "reply_tts_chars": tts_chars,
        "tts_chars_per_reply": round(tts_chars / len(replies), 1) if replies else 0,
    }


def fixed_phrase_chars() -> dict:
    """Exact characters of the server-side fixed phrases the caller hears (English)."""
    from app import agent
    return {key: len(val[0]) for key, val in agent._MSGS.items()} | {
        "safe_retreat": len(agent._SAFE_RETREAT[0]), "non_english_handoff": len(agent.NON_ENGLISH_HANDOFF),
    }


def run_benchmark() -> dict:
    results = [run_scenario(n) for n in SCENARIOS]
    return {
        "measurement_scope": MEASUREMENT_SCOPE, "estimator": ESTIMATOR, "client": CLIENT_ID,
        "results": results, "fixed_phrase_chars": fixed_phrase_chars(),
        "totals": {"input_tokens": sum(r["input_tokens"] for r in results),
                   "reply_tts_chars": sum(r["reply_tts_chars"] for r in results)},
    }


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    rep = run_benchmark()
    print(f"scope={rep['measurement_scope']} estimator={rep['estimator']}")
    hdr = f"{'scenario':22}{'turns':>6}{'calls':>6}{'sys':>6}{'tools':>6}{'in_tok':>8}{'hist_tot':>9}{'last_hist':>10}{'maxtok':>7}{'sumry':>6}{'tts':>6}"
    print(hdr)
    for r in rep["results"]:
        print(f"{r['scenario']:22}{r['caller_turns']:>6}{r['model_calls']:>6}{r['system_tokens_per_call']:>6}"
              f"{r['tool_schema_tokens_per_call']:>6}{r['input_tokens']:>8}{r['history_tokens_sent_total']:>9}"
              f"{r['last_call_history_tokens']:>10}{r['max_tokens_setting']:>7}{r['summary_input_tokens']:>6}{r['reply_tts_chars']:>6}")
    print("fixed phrase chars:", rep["fixed_phrase_chars"])
    print("totals:", rep["totals"])
