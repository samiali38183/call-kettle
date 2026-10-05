"""Token / TTS budget regression for the real agent path (offline; see qa/token_benchmark.py).

Ceilings are ESTIMATED tokens (ceil(json_chars/4)), not provider tokens. They sit a few percent above the measured
value so a stray paragraph in the system prompt, a verbose tool description or a bigger max_tokens fails the suite
instead of silently eating margin. If you add prompt text on purpose, raise the ceiling in the same change and say why.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "qa" / "token_benchmark.py"

# input_tokens = sum over every model call of (system + tool schemas + messages), per scripted scenario.
INPUT_TOKEN_CEILING = {
    "hours_question": 2200,
    "booking_flow": 16600,
    "cancel_flow": 11650,
    "price_then_callback": 6800,
    "long_conversation": 24200,
    "emergency_shortcut": 0,
}
STATIC_PROMPT_PLUS_TOOLS_CEILING = 2180   # tokens resent on EVERY model call (demo_hvac, caller ID present)
MAX_REPLY_TOKENS_CEILING = 220            # a spoken reply is 1-2 sentences; tool calls with a summary fit well inside this
REPLY_TTS_CHARS_CEILING = {               # scripted replies: guards fixed phrases / harness drift, not live model verbosity
    "hours_question": 60, "booking_flow": 330, "cancel_flow": 120, "price_then_callback": 160,
    "long_conversation": 500, "emergency_shortcut": 100,
}


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("token_benchmark", PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["token_benchmark"] = module   # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module, {r["scenario"]: r for r in module.run_benchmark()["results"]}


def test_report_declares_it_is_an_estimate(bench):
    module, _ = bench
    rep = module.run_benchmark()
    assert "estimate" in rep["measurement_scope"] and "not_provider_tokens" in rep["measurement_scope"]


@pytest.mark.parametrize("name", sorted(INPUT_TOKEN_CEILING))
def test_input_tokens_within_budget(bench, name):
    _, results = bench
    assert results[name]["input_tokens"] <= INPUT_TOKEN_CEILING[name], results[name]


def test_static_prefix_resent_every_call_is_bounded(bench):
    _, results = bench
    r = results["booking_flow"]
    assert r["system_tokens_per_call"] + r["tool_schema_tokens_per_call"] <= STATIC_PROMPT_PLUS_TOOLS_CEILING


def test_max_tokens_is_sane_for_a_spoken_reply(bench):
    _, results = bench
    for r in results.values():
        assert r["max_tokens_setting"] <= MAX_REPLY_TOKENS_CEILING, r["scenario"]
    assert any(r["max_tokens_setting"] >= 150 for r in results.values() if r["model_calls"]), "too small to hold a tool call"


@pytest.mark.parametrize("name", sorted(REPLY_TTS_CHARS_CEILING))
def test_reply_tts_chars_within_budget(bench, name):
    _, results = bench
    assert results[name]["reply_tts_chars"] <= REPLY_TTS_CHARS_CEILING[name]


def test_history_growth_is_bounded_on_a_long_call(bench):
    _, results = bench
    r = results["long_conversation"]
    assert r["last_call_history_tokens"] <= 520   # trim_history keeps ~20 messages; the last call must not carry more
    assert r["model_calls"] == 10


def test_every_scripted_scenario_ran_through_the_real_agent(bench):
    _, results = bench
    for name, r in results.items():
        if name != "emergency_shortcut":
            assert r["model_calls"] >= 1 and r["system_tokens_per_call"] > 0, name


def test_spoken_brevity_instruction_keeps_required_readbacks_and_safety():
    from datetime import datetime

    from app import agent
    from app.config import load_client_config

    prompt = agent.build_system_prompt(load_client_config("demo_hvac"), "+15555550100")
    # brevity: a hard word cap, not just "1-2 short sentences"
    assert "25 words" in prompt
    # required confirmations are still demanded
    assert "say the full name back" in prompt
    assert "groups of digits" in prompt
    assert "service address" in prompt or "Ask for the service address" in prompt or "Do not ask for a street address" in prompt
    # safety / escalation language is unchanged, word for word
    assert ("SAFETY FIRST: if anyone may be in immediate danger (fire, gas smell, carbon monoxide alarm, someone injured, "
            "unconscious, or having a medical emergency), tell them to hang up and call 911 right now. That comes before "
            "anything else. Then call transfer_call so the owner is alerted, with a handoff_message that starts with the "
            "911 advice.") in prompt
    assert "Never claim to be human." in prompt
    assert "call transfer_call immediately" in prompt
    assert datetime  # (import kept local so the file loads without the app on sys.path)


def test_tool_descriptions_stay_short():
    from app import agent

    total = sum(len(t["description"]) for t in agent.TOOLS)
    assert total <= 960, total
    assert max(len(t["description"]) for t in agent.TOOLS) <= 245
