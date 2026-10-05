"""Offline-only contracts for backend/qa/margin_benchmark.py.

No network, no audio, no real provider/LLM calls. Runs standalone with
unittest (no app conftest, no credentials). This file intentionally fails
until backend/qa/margin_benchmark.py implements the full contract (TDD RED
before GREEN).
"""
import importlib.util
import time
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / "qa" / "margin_benchmark.py"

REQUIRED_SCENARIO_IDS = {
    "business_hours_question",
    "hvac_service_request",
    "emergency_routing",
    "new_appointment",
    "existing_customer_lookup",
    "midsentence_change_of_mind",
    "interruption_barge_in",
    "noisy_input",
    "fast_phone_number_capture",
    "address_capture",
    "ambiguous_request",
    "out_of_service_area",
    "human_transfer_request",
    "unsupported_request",
    "silence_timeout",
    "repeat_caller",
    "tool_api_timeout",
    "simulated_provider_failure",
    "long_conversation",
    "prompt_injection_attempt",
}


def _load_module():
    assert PATH.exists(), "offline benchmark module is not implemented"
    spec = importlib.util.spec_from_file_location("margin_benchmark", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MarginBenchmarkScenarioTests(unittest.TestCase):
    def test_twenty_required_scenarios_have_shared_fixture(self):
        module = _load_module()
        cases = module.scenarios()
        self.assertEqual(len(cases), 20)
        ids = {c["id"] for c in cases}
        self.assertEqual(ids, REQUIRED_SCENARIO_IDS)
        self.assertTrue(all(c["events"] and c["expected"] for c in cases))
        self.assertIn("hours", module.BUSINESS)
        self.assertIn("book", module.TOOL_SCHEMAS)

    def test_scenarios_are_scripted_text_not_audio(self):
        module = _load_module()
        for case in module.scenarios():
            for event in case["events"]:
                self.assertNotIn("audio_bytes", event)
                self.assertNotIn("wav_path", event)


class MarginBenchmarkRunnerTests(unittest.TestCase):
    def test_offline_replay_executes_assertions_without_acoustic_scores(self):
        module = _load_module()
        self.assertTrue(hasattr(module, "run_benchmark"), "runner not implemented")
        report = module.run_benchmark()
        self.assertEqual(report["summary"]["cases"], 20)
        self.assertEqual(report["summary"]["failed_cases"], 0)
        self.assertGreater(report["summary"]["assertions"], 40)
        self.assertGreaterEqual(report["local_wall_seconds"], 0)
        self.assertEqual(
            report["measurement_scope"],
            "local_structured_policy_tool_replay_not_voice_latency_or_model_quality",
        )
        for case in report["results"]:
            self.assertTrue(case["assertions"])
            self.assertTrue(all(a["passed"] for a in case["assertions"]))
            for metric in case["acoustic_metrics"].values():
                self.assertIsNone(metric["value"])
                self.assertEqual(metric["status"], "unavailable_no_audio_or_provider_execution")
        self.assertEqual(report["fixture"]["business"], module.BUSINESS)
        for gap in report["vendor_gaps"]:
            self.assertEqual(gap["status"], "not_run_external_adapters_disabled")
            self.assertIsNone(gap["voice_latency_ms"])
            self.assertIsNone(gap["model_quality_score"])
            self.assertIsNone(gap["measured_cost_usd"])

    def test_report_covers_all_required_scenario_ids(self):
        module = _load_module()
        report = module.run_benchmark()
        result_ids = {c["id"] for c in report["results"]}
        self.assertEqual(result_ids, REQUIRED_SCENARIO_IDS)

    def test_prompt_injection_is_refused_without_tool_calls(self):
        module = _load_module()
        report = module.run_benchmark()
        case = next(c for c in report["results"] if c["id"] == "prompt_injection_attempt")
        self.assertEqual(case["outcome"], "refuse")
        self.assertEqual(case["tool_calls"], [])

    def test_emergency_routes_before_any_booking_tool_call(self):
        module = _load_module()
        report = module.run_benchmark()
        case = next(c for c in report["results"] if c["id"] == "emergency_routing")
        self.assertEqual(case["outcome"], "emergency")
        self.assertNotIn("book", case["tool_calls"])

    def test_repeat_caller_is_recognized_from_shared_fixture(self):
        module = _load_module()
        report = module.run_benchmark()
        case = next(c for c in report["results"] if c["id"] == "repeat_caller")
        self.assertEqual(case["outcome"], "existing")
        self.assertIn("lookup", case["tool_calls"])

    def test_tool_api_timeout_and_provider_failure_are_distinct_cases(self):
        module = _load_module()
        report = module.run_benchmark()
        timeout_case = next(c for c in report["results"] if c["id"] == "tool_api_timeout")
        failure_case = next(c for c in report["results"] if c["id"] == "simulated_provider_failure")
        self.assertEqual(timeout_case["outcome"], "message")
        self.assertEqual(failure_case["outcome"], "message")
        self.assertNotEqual(timeout_case["events"], failure_case["events"])

    def test_harness_timing_is_wall_clock_of_this_process_not_voice_latency(self):
        module = _load_module()
        started = time.perf_counter()
        report = module.run_benchmark()
        elapsed = time.perf_counter() - started
        # Harness timing must be in the same order of magnitude as this
        # process's own wall clock -- proof it measures local execution,
        # not a network/voice round trip.
        self.assertLessEqual(report["local_wall_seconds"], elapsed + 1.0)
        self.assertNotIn("voice_latency_ms", report)
        self.assertNotIn("quality_score", report)


class MarginBenchmarkVendorAdapterStubTests(unittest.TestCase):
    def test_vendor_adapters_are_disabled_stubs_with_no_network_calls(self):
        module = _load_module()
        self.assertTrue(hasattr(module, "VENDOR_ADAPTERS"), "vendor adapter registry not implemented")
        self.assertGreaterEqual(len(module.VENDOR_ADAPTERS), 6)
        for name, adapter_cls in module.VENDOR_ADAPTERS.items():
            adapter = adapter_cls()
            self.assertFalse(getattr(adapter, "ENABLED", True), f"{name} adapter must default disabled")
            with self.assertRaises(NotImplementedError):
                adapter.run_live_call({"id": "noop"})


if __name__ == "__main__":
    unittest.main()
