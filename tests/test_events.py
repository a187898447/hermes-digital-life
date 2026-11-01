"""Tests for host-observed transitions and operator-only controls."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_db_dir = tempfile.TemporaryDirectory()
_db_path = Path(_db_dir.name) / "plugin.db"
core_plugins = types.ModuleType("plugins")
core_plugins.__path__ = []
storage_api = types.ModuleType("plugins.plugin_storage")
storage_api.plugin_db = lambda _name: sqlite3.connect(_db_path, timeout=10, isolation_level=None)
core_plugins.plugin_storage = storage_api
sys.modules.setdefault("plugins", core_plugins)
sys.modules.setdefault("plugins.plugin_storage", storage_api)
namespace = types.ModuleType("hermes_plugins")
namespace.__path__ = [str(ROOT)]
sys.modules.setdefault("hermes_plugins", namespace)

plugin_spec = importlib.util.spec_from_file_location(
    "hermes_plugins.digital_life", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
plugin = importlib.util.module_from_spec(plugin_spec)
sys.modules[plugin_spec.name] = plugin
plugin_spec.loader.exec_module(plugin)

from digital_life.domain import default_state
from digital_life.runtime import record_api_error, record_request, record_turn
from digital_life import storage


class RecordingContext:
    def __init__(self):
        self.tools = []
        self.hooks = []
        self.middleware = []
        self.cli = []
        self.sections = []
        self.blueprints = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def register_middleware(self, name, callback):
        self.middleware.append((name, callback))

    def register_cli_command(self, **kwargs):
        self.cli.append(kwargs)

    def register_automation_blueprint(self, *args, **kwargs):
        self.blueprints.append((args, kwargs))

    def register_system_prompt_section(self, *args, **kwargs):
        self.sections.append((args, kwargs))


class ObservedTransitionTests(unittest.TestCase):
    def setUp(self):
        self.state = default_state(1_000_000)
        self.patcher = patch.object(storage, "update_state", self._update)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def _update(self, now_ms, mutate):
        self.state = mutate(self.state)
        return self.state

    def test_provider_request_charges_fixed_cost_not_model_supplied_amount(self):
        record_request(1_010_000)
        self.assertAlmostEqual(self.state["energy"], 99.93333333333334)
        self.assertEqual(self.state["chemistry"]["dopamine"], 50)

    def test_completed_turn_reward_uses_fixed_rule(self):
        record_turn(1_010_000, success=True)
        self.assertEqual(self.state["chemistry"]["dopamine"], 55)

    def test_retryable_observed_network_error_raises_stress(self):
        record_api_error(1_010_000, error_type="ConnectionError", retryable=True)
        self.assertEqual(self.state["chemistry"]["cortisol"], 40)
        self.assertEqual(self.state["gaps"][-1]["source"], "api_request_error")

    def test_non_search_tool_cannot_create_gap(self):
        from digital_life.runtime import observe_tool_result
        observe_tool_result(1_010_000, tool_name="terminal", args={"command": "echo"}, result="")
        self.assertEqual(self.state["gaps"], [])

    def test_observed_empty_search_creates_deduplicated_gap(self):
        from digital_life.runtime import observe_tool_result
        observe_tool_result(1_010_000, tool_name="web_search", args={"query": "launch dates"}, result='{"results": []}')
        first_id = self.state["gaps"][0]["id"]
        observe_tool_result(1_020_000, tool_name="web_search", args={"query": "launch dates"}, result='{"results": []}')
        self.assertEqual(len(self.state["gaps"]), 1)
        self.assertEqual(self.state["gaps"][0]["id"], first_id)
        observe_tool_result(1_030_000, tool_name="web_search", args={"query": "launch dates"}, result='{"results": [{"title": "launch"}]}')
        self.assertEqual(self.state["gaps"][0]["status"], "resolved")
        self.assertEqual(self.state["gaps"][0]["closed_by"], "observed_search_success")

    def test_successful_search_closes_matching_gap_family(self):
        from digital_life.runtime import observe_tool_result
        self.state["gaps"] = [
            {"id": "a", "summary": "launch date for alpha", "status": "open", "created_at": 1_000_000, "tension": 80},
            {"id": "b", "summary": "launch date for alpha project", "status": "open", "created_at": 1_000_000, "tension": 80},
            {"id": "c", "summary": "weather in Kyoto", "status": "open", "created_at": 1_000_000, "tension": 80},
        ]
        observe_tool_result(1_010_000, tool_name="web_search", args={"query": "launch date for alpha"}, result='{"results": [{"title":"a"}]}')
        self.assertEqual([gap["status"] for gap in self.state["gaps"]], ["resolved", "resolved", "open"])

    def test_repeated_observation_reactivates_same_gap_id(self):
        from digital_life.runtime import observe_tool_result
        observe_tool_result(1_010_000, tool_name="web_search", args={"query": "launch dates"}, result='{"results": []}')
        gap_id = self.state["gaps"][0]["id"]
        self.state["gaps"][0]["status"] = "resolved"
        self.state["gaps"][0]["closed_by"] = "operator"
        observe_tool_result(1_020_000, tool_name="web_search", args={"query": "launch dates"}, result='{"results": []}')
        self.assertEqual(len(self.state["gaps"]), 1)
        self.assertEqual(self.state["gaps"][0]["id"], gap_id)
        self.assertEqual(self.state["gaps"][0]["status"], "open")

    def test_sqlite_transactions_preserve_concurrent_updates(self):
        self.patcher.stop()
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = []
            for worker in range(8):
                futures.append(pool.submit(self._write_gaps, worker))
            for future in futures:
                future.result()
        persisted = storage.load_state(2_000_000)
        self.assertEqual(len(persisted["gaps"]), 160)

    def _write_gaps(self, worker: int):
        for item in range(20):
            def append(state):
                state["gaps"].append({
                    "id": f"{worker}-{item}", "summary": "observed",
                    "created_at": 0, "tension": 1, "status": "open",
                })
                return state
            storage.update_state(worker * 100 + item, append)


class RegistrationTests(unittest.TestCase):
    def test_state_mutation_is_not_llm_callable_and_cli_control_is_registered(self):
        ctx = RecordingContext()
        plugin.register(ctx)
        self.assertEqual([entry["name"] for entry in ctx.tools], ["digital_life_status"])
        self.assertEqual([entry["name"] for entry in ctx.cli], ["digital-life"])
        self.assertEqual({kind for kind, _ in ctx.middleware}, {"llm_request"})
        self.assertEqual(ctx.blueprints[0][0], ("dream-reflection",))
        self.assertEqual(ctx.blueprints[0][1]["deliver_default"], "local")
        self.assertIn("Do not send a message", ctx.blueprints[0][1]["prompt_template"])
        self.assertIn("Do not", ctx.blueprints[0][1]["prompt_template"])

    def test_per_request_affect_preserves_both_supported_payload_shapes(self):
        with patch.object(plugin.digital_life.runtime, "affect_note", return_value="[observed affect]"), \
                patch.object(plugin.digital_life.runtime, "record_request"):
            chat = {"messages": [{"role": "user", "content": "hello"}], "temperature": 0.4}
            chat_result = plugin._affect_request(chat)
            responses = {"input": [{"role": "user", "content": "hello"}], "max_output_tokens": 40}
            responses_result = plugin._affect_request(responses)
        self.assertEqual(chat["messages"][0]["content"], "hello")
        self.assertEqual(chat_result["request"]["temperature"], 0.4)
        self.assertIn("[observed affect]", chat_result["request"]["messages"][0]["content"])
        self.assertEqual(responses["input"][0]["content"], "hello")
        self.assertEqual(responses_result["request"]["max_output_tokens"], 40)
        self.assertIn("[observed affect]", responses_result["request"]["input"][0]["content"])

    def test_memory_blocks_are_reordered_inside_openai_system_message(self):
        separator = "═" * 46
        entries = ["日常：周末吃了蛋糕", "危机：服务器发生故障", "普通记忆"]
        body = "\n§\n".join(entries)
        memory = f"{separator}\nMEMORY (your personal notes) [1% — {len(body)}/2200 chars]\n{separator}\n{body}"
        request = {"messages": [{"role": "system", "content": memory}, {"role": "user", "content": "hello"}]}
        with patch.object(plugin.digital_life.runtime, "reorder_prompt_memories", return_value=(memory.replace(entries[0], entries[1]).replace(entries[1], entries[0]), 1)), \
                patch.object(plugin.digital_life.runtime, "affect_note", return_value="[affect]"), \
                patch.object(plugin.digital_life.runtime, "record_request"):
            result = plugin._affect_request(request)
        self.assertEqual(result["request"]["messages"][0]["role"], "system")
        self.assertIn("§", result["request"]["messages"][0]["content"])
        self.assertEqual(request["messages"][1]["content"], "hello")

    def test_operator_cli_stop_requires_explicit_confirmation(self):
        from argparse import Namespace
        from digital_life import storage
        from digital_life.cli import run_command
        state = default_state(1_000_000)
        with patch.object(storage, "update_state", lambda now, mutate: mutate(state)), \
                patch("builtins.input", return_value="STOP"), patch("builtins.print"):
            result = run_command(Namespace(digital_life_action="stop"))
        self.assertEqual(result, 0)
        self.assertTrue(state["emergency_stop"])

    def test_cli_gap_resolution_is_operator_confirmed(self):
        from argparse import Namespace
        from digital_life import storage
        from digital_life.cli import run_command
        state = default_state(1_000_000)
        state["gaps"] = [{"id": "gap-1", "status": "open", "summary": "observed", "tension": 80}]
        with patch.object(storage, "update_state", lambda now, mutate: mutate(state)), \
                patch("builtins.input", return_value="RESOLVE"), patch("builtins.print"):
            result = run_command(Namespace(digital_life_action="resolve-gap", gap_id="gap-1"))
        self.assertEqual(result, 0)
        self.assertEqual(state["gaps"][0]["status"], "resolved")
        self.assertEqual(state["gaps"][0]["closed_by"], "operator")

    def test_request_middleware_preserves_original_and_adds_affect_to_user_content(self):
        request = {"messages": [{"role": "user", "content": "hello"}], "temperature": 0.4}
        with patch.object(plugin.digital_life.runtime, "affect_note", return_value="[affect]"):
            result = plugin._affect_request(request)
        self.assertEqual(request["messages"][0]["content"], "hello")
        self.assertEqual(result["request"]["temperature"], 0.4)
        self.assertTrue(result["request"]["messages"][0]["content"].endswith("[affect]"))


if __name__ == "__main__":
    unittest.main()
