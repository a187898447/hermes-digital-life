"""Tests for host-observed transitions and operator-only controls."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import types
import unittest
import sqlite3
import tempfile
import time
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
from digital_life.domain import find_gap_sentence, pick_temperature
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

    def register_command(self, name, handler, **kwargs):
        self.commands = getattr(self, "commands", {})
        self.commands[name] = handler

    def register_cli_command(self, **kwargs):
        self.cli.append(kwargs)

    def register_automation_blueprint(self, *args, **kwargs):
        self.blueprints.append((args, kwargs))

    def register_system_prompt_section(self, *args, **kwargs):
        self.sections.append((args, kwargs))

    def get_config(self, key, default=None):
        return getattr(self, "config", {}).get(key, default)

    def inject_message(self, content, role="user", *, session_key=None, origin=None):
        self.injected = getattr(self, "injected", [])
        self.injected.append({"content": content, "role": role, "session_key": session_key})
        return getattr(self, "inject_result", True)


class ObservedTransitionTests(unittest.TestCase):
    def test_stopped_request_and_reply_do_not_write_sqlite(self):
        self.patcher.stop()
        plugin.digital_life.runtime.stop_state(1_000_000, True)
        plugin_storage = sys.modules[plugin.digital_life.runtime.__package__ + ".storage"]
        with patch.object(plugin_storage, "_write_unlocked", wraps=plugin_storage._write_unlocked) as write:
            plugin._affect_request({"messages": [{"role": "system", "content": "base"}]})
            plugin._post_llm_call("我不知道火星营业时间")
            write.assert_not_called()

    def setUp(self):
        self.state = default_state(1_000_000)
        self.patcher = patch.object(storage, "update_state", self._update)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def _update(self, now_ms, mutate):
        next_state = mutate(self.state)
        if next_state is not None:
            self.state = next_state
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

    def test_assistant_unknown_gap_deduplicates_and_respects_stop(self):
        from digital_life.runtime import observe_assistant_unknown
        observe_assistant_unknown(1_010_000, "我不知道火星的营业时间")
        gap_id = self.state["gaps"][0]["id"]
        observe_assistant_unknown(1_020_000, "我不确定火星的营业时间")
        self.assertEqual(len(self.state["gaps"]), 1)
        self.assertEqual(self.state["gaps"][0]["id"], gap_id)
        self.assertEqual(self.state["gaps"][0]["source"], "assistant_unknown_claim")
        self.state["emergency_stop"] = True
        before = repr(self.state)
        observe_assistant_unknown(1_030_000, "我不知道火星的营业时间")
        self.assertEqual(repr(self.state), before)

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
    def test_temperature_and_system_only_requests(self):
        state = default_state(1_000_000)
        runtime = plugin.digital_life.runtime
        with patch.object(runtime, "status_snapshot", return_value=state), patch.object(runtime, "record_request"):
            for request in ({"system": "base"}, {"messages": []}, {"input": []}, {"messages": [], "temperature": None}):
                with self.subTest(request=request):
                    result = plugin._affect_request(request)["request"]
                    self.assertAlmostEqual(result["temperature"], pick_temperature(state["chemistry"], state["energy"]))
                    self.assertEqual(set(result), set(request) | {"temperature"})
            for temperature in (0, 0.3, 1.0):
                request = {"messages": [{"role": "user", "content": "hello"}], "temperature": temperature, "top_p": 0.8, "max_tokens": 100}
                result = plugin._affect_request(request)["request"]
                for key in ("temperature", "top_p", "max_tokens"):
                    self.assertEqual(result[key], request[key])

    def test_gap_append_supported_system_shapes(self):
        runtime = plugin.digital_life.runtime
        now_ms = 2_000_000
        state = default_state(now_ms)
        state["gaps"] = [{"summary": "question", "status": "open", "tension": 87, "created_at": 0}]
        requests = [
            ({"system": "base"}, lambda r: r["system"]),
            ({"system": [{"type": "text", "text": "base"}]}, lambda r: r["system"][0]["text"]),
            ({"messages": [{"role": "system", "content": "base"}]}, lambda r: r["messages"][0]["content"]),
            ({"messages": [{"role": "system", "content": [{"type": "text", "text": "base"}]}]}, lambda r: r["messages"][0]["content"][0]["text"]),
            ({"input": [{"role": "developer", "content": [{"type": "input_text", "text": "base"}]}]}, lambda r: r["input"][0]["content"][0]["text"]),
        ]
        with (
            patch.object(runtime, "status_snapshot", return_value=state),
            patch.object(runtime, "record_request"),
            patch.object(runtime, "reorder_prompt_memories", return_value=("reordered", 1)),
            patch.object(plugin.time, "time", return_value=now_ms / 1000),
        ):
            for request, extract in requests:
                with self.subTest(request=request):
                    result = plugin._affect_request(request)["request"]
                    self.assertEqual(extract(result), runtime.append_open_loops("reordered", state["gaps"], now_ms))
                    self.assertEqual(set(result), set(request) | {"temperature"})

    def test_gap_suffix_is_appended_to_one_block_only(self):
        runtime = plugin.digital_life.runtime
        now_ms = 2_000_000
        state = default_state(now_ms)
        state["gaps"] = [{"summary": "question", "status": "open", "tension": 87, "created_at": 0}]
        requests = [
            ({"system": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}, lambda r: r["system"]),
            (
                {"messages": [{"role": "system", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]},
                lambda r: r["messages"][0]["content"],
            ),
            (
                {"input": [{"role": "developer", "content": [{"type": "input_text", "text": "a"}, {"type": "input_text", "text": "b"}]}]},
                lambda r: r["input"][0]["content"],
            ),
        ]
        with (
            patch.object(runtime, "status_snapshot", return_value=state),
            patch.object(runtime, "record_request"),
            patch.object(plugin.time, "time", return_value=now_ms / 1000),
        ):
            for request, extract in requests:
                with self.subTest(request=request):
                    result = plugin._affect_request(request)["request"]
                    rendered = [block["text"] if isinstance(block, dict) else block for block in extract(result)]
                    self.assertEqual(sum(text.count("[open loops]") for text in rendered), 1)
                    self.assertNotIn("[open loops]", rendered[0])
                    self.assertIn("[open loops]", rendered[-1])

    def test_post_llm_unknown_claim_is_guarded(self):
        runtime = plugin.digital_life.runtime
        with patch.object(plugin, "record_turn"), patch.object(runtime, "observe_assistant_unknown") as observe:
            plugin._post_llm_call("我不知道火星营业时间。")
            self.assertEqual(observe.call_args.args[1], "我不知道火星营业时间")
            observe.reset_mock()
            for reply in ("", "/我不知道火星营业时间", "😀", "嗯", "这意味着我不知道火星营业时间", "我不知道执念张力"):
                plugin._post_llm_call(reply)
            observe.assert_not_called()
        with patch.object(plugin, "record_turn"), patch.object(runtime, "observe_assistant_unknown", side_effect=RuntimeError):
            plugin._post_llm_call("我不知道火星营业时间。")

    def setUp(self):
        self.snapshot = patch.object(plugin.digital_life.runtime, "status_snapshot", return_value=default_state(1_000_000))
        self.snapshot.start()
        self.addCleanup(self.snapshot.stop)

    def test_turn_capture_appends_to_the_palace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = RecordingContext()
            ctx.get_config = lambda key, default=None: default
            with (
                patch.object(plugin.digital_life.storage, "data_root", return_value=root),
                patch.object(plugin.digital_life.storage, "plugin_db",
                             side_effect=lambda name, filename: sqlite3.connect(root / filename)),
                patch.object(plugin.digital_life.storage, "read_state",
                             return_value={"emergency_stop": False, "chemistry": {"cortisol": 20}}),
                patch.object(plugin.digital_life.storage, "update_state", return_value=None),
            ):
                plugin.register(ctx)
                hooks = dict(ctx.hooks)
                hooks["pre_llm_call"](user_message="我叫阿远，习惯凌晨写代码", session_id="s1", platform="qqbot")
                hooks["post_llm_call"](assistant_response="行，记下了", session_id="s1")
            files = list((root / "memory").rglob("*.jsonl"))
            self.assertEqual(len(files), 1)
            event = json.loads(files[0].read_text().strip())
            self.assertEqual(event["kind"], "turn")
            self.assertIn("我叫阿远", event["text"])
            self.assertIn("行，记下了", event["text"])

    def test_core_chat_command_answers_only_in_dm_or_for_an_owner(self):
        from types import SimpleNamespace
        ctx = RecordingContext()
        ctx.get_config = lambda key, default=None: default
        commands = {}
        ctx.register_command = lambda name, handler, **kw: commands.setdefault(name, handler)
        plugin.register(ctx)
        handler = commands["core"]
        text = handler("", source=SimpleNamespace(chat_type="dm", user_id="u1"), platform="qqbot")
        self.assertIn("digital-life-core", text)
        group = handler("", source=SimpleNamespace(chat_type="group", user_id="u1"), platform="qqbot")
        self.assertIn("私聊", group)

    def test_state_mutation_is_not_llm_callable_and_cli_control_is_registered(self):
        ctx = RecordingContext()
        plugin.register(ctx)
        self.assertEqual([entry["name"] for entry in ctx.tools], ["beat_targets", "beat_send", "memory_write", "memory_recall", "digital_life_status"])
        schemas = {entry["name"]: entry["schema"]["parameters"]["properties"] for entry in ctx.tools}
        self.assertEqual(set(schemas["memory_write"]) & {"sources", "refs"}, {"sources", "refs"})
        self.assertIn("kind", schemas["memory_recall"])
        self.assertEqual([entry["name"] for entry in ctx.cli], ["hermes-lover"])
        self.assertEqual({kind for kind, _ in ctx.middleware}, {"llm_request"})
        self.assertEqual(ctx.blueprints[0][0], ("dream-reflection",))
        self.assertEqual(ctx.blueprints[0][1]["deliver_default"], "local")
        self.assertIn("Do not send a message", ctx.blueprints[0][1]["prompt_template"])
        self.assertIn("Do not", ctx.blueprints[0][1]["prompt_template"])

    def test_per_request_affect_preserves_both_supported_payload_shapes(self):
        with (
            patch.object(plugin.digital_life.runtime, "status_snapshot", return_value=default_state(1_000_000)),
            patch.object(plugin.digital_life.runtime, "affect_note", return_value="[observed affect]"),
            patch.object(plugin.digital_life.runtime, "record_request"),
        ):
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

    def test_reorder_is_kept_when_last_user_content_is_not_text_block(self):
        request = {
            "messages": [
                {"role": "system", "content": "original system text"},
                {"role": "user", "content": {"type": "image_url", "image_url": "x"}},
            ]
        }
        with patch.object(plugin.digital_life.runtime, "reorder_prompt_memories", return_value=("SYSTEM-REORDERED", 1)), \
                patch.object(plugin.digital_life.runtime, "affect_note", return_value="[affect]"), \
                patch.object(plugin.digital_life.runtime, "record_request"):
            result = plugin._affect_request(request)
        self.assertIsNotNone(result)
        self.assertEqual(result["request"]["messages"][0]["content"], "SYSTEM-REORDERED")


class QqBubbleTransformTests(unittest.TestCase):
    def _transform(self, text, platform="qqbot"):
        ctx = RecordingContext()
        plugin.register(ctx)
        hooks = dict(ctx.hooks)
        return hooks["transform_llm_output"](response_text=text, platform=platform, session_id="s1", turn_id="t1")

    def test_long_qq_reply_is_rewritten_into_short_lines(self):
        text = ("说实话，没想那么远。\n\n我不太想当什么有用的人。我更想当那个说真话不绕弯、"
                "偶尔顶两句嘴、把事情办明白了就把话撂下的那种。\n\n你问这个，是自己在琢磨事？")
        out = self._transform(text)
        self.assertIsInstance(out, str)
        lines = out.split("\n")
        self.assertTrue(2 <= len(lines) <= 6)
        self.assertTrue(all(0 < len(line) <= 48 for line in lines))

    def test_other_platforms_and_code_and_commands_stay_untouched(self):
        text = "第一句很长的话，需要被拆开才能发出去，不然就是一整段文字。第二句也很长，同样需要拆开处理。"
        self.assertIsNone(self._transform(text, platform="cli"))
        self.assertIsNone(self._transform("看这个\n```python\nprint(1)\n```"))
        self.assertIsNone(self._transform("/stop"))



    def test_capture_only_happens_for_qqbot_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = RecordingContext()
            ctx.get_config = lambda key, default=None: default
            with (
                patch.object(plugin.digital_life.storage, "data_root", return_value=root),
                patch.object(plugin.digital_life.storage, "plugin_db",
                             side_effect=lambda name, filename: sqlite3.connect(root / filename)),
                patch.object(plugin.digital_life.storage, "read_state",
                             return_value={"emergency_stop": False, "chemistry": {"cortisol": 20}}),
                patch.object(plugin.digital_life.storage, "update_state", return_value=None),
            ):
                plugin.register(ctx)
                hooks = dict(ctx.hooks)
                hooks["pre_llm_call"](user_message="工作台里的话", session_id="s2", platform="cli")
                hooks["post_llm_call"](assistant_response="不该进宫殿", session_id="s2")
            self.assertEqual(list((root / "memory").rglob("*.jsonl")), [])


    def test_missing_yesterday_diary_is_requested_on_the_first_qq_message(self):
        from datetime import datetime, timedelta
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        anchor = (datetime.now() - timedelta(days=1)).replace(hour=12).timestamp() * 1000
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = RecordingContext()
            ctx.get_config = lambda key, default=None: default
            with (
                patch.object(plugin.digital_life.storage, "data_root", return_value=root),
                patch.object(plugin.digital_life.storage, "plugin_db",
                             side_effect=lambda name, filename: sqlite3.connect(root / filename)),
                patch.object(plugin.digital_life.storage, "read_state",
                             return_value={"emergency_stop": False, "chemistry": {"cortisol": 20}}),
                patch.object(plugin.digital_life.storage, "update_state", return_value=None),
            ):
                plugin._JOURNAL_PROMPTED.clear()
                plugin.register(ctx)
                plugin.digital_life.memory.MemoryStore(root).write(
                    "用户：今天被说胖\n助手：滚", session_id="s1", now_ms=anchor)
                hooks = dict(ctx.hooks)
                note = hooks["pre_llm_call"](user_message="在吗", session_id="s1", platform="qqbot")
            self.assertIn("日记", note["context"])
            self.assertIn(yesterday, note["context"])

    def test_diary_covers_the_newest_recorded_day_after_a_gap(self):
        from datetime import datetime, timedelta
        older = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d")
        older_anchor = (datetime.now() - timedelta(days=4)).replace(hour=12).timestamp() * 1000
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = RecordingContext()
            ctx.get_config = lambda key, default=None: default
            with (
                patch.object(plugin.digital_life.storage, "data_root", return_value=root),
                patch.object(plugin.digital_life.storage, "plugin_db",
                             side_effect=lambda name, filename: sqlite3.connect(root / filename)),
                patch.object(plugin.digital_life.storage, "read_state",
                             return_value={"emergency_stop": False, "chemistry": {"cortisol": 20}}),
                patch.object(plugin.digital_life.storage, "update_state", return_value=None),
            ):
                plugin._JOURNAL_PROMPTED.clear()
                plugin.register(ctx)
                plugin.digital_life.memory.MemoryStore(root).write(
                    "用户：隔了几天才回来\n助手：好久不见", session_id="s1", now_ms=older_anchor)
                hooks = dict(ctx.hooks)
                note = hooks["pre_llm_call"](user_message="在吗", session_id="s1", platform="qqbot")
            self.assertIn("日记", note["context"])
            self.assertIn(older, note["context"])
            self.assertNotIn((datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"), note["context"])

    def test_missing_atomic_facts_are_requested_from_that_days_turns(self):
        from datetime import datetime, timedelta
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        anchor = (datetime.now() - timedelta(days=1)).replace(hour=12).timestamp() * 1000
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = RecordingContext()
            ctx.get_config = lambda key, default=None: default
            with (
                patch.object(plugin.digital_life.storage, "data_root", return_value=root),
                patch.object(plugin.digital_life.storage, "plugin_db",
                             side_effect=lambda name, filename: sqlite3.connect(root / filename)),
                patch.object(plugin.digital_life.storage, "read_state",
                             return_value={"emergency_stop": False, "chemistry": {"cortisol": 20}}),
                patch.object(plugin.digital_life.storage, "update_state", return_value=None),
            ):
                plugin._JOURNAL_PROMPTED.clear()
                plugin._FACT_PROMPTED.clear()
                plugin.register(ctx)
                plugin.digital_life.memory.MemoryStore(root).write(
                    "用户：今天被说胖\n助手：滚", kind="turn", session_id="s1", now_ms=anchor)
                hooks = dict(ctx.hooks)
                note = hooks["pre_llm_call"](user_message="在吗", session_id="s1", platform="qqbot")
            self.assertIn("原子事实", note["context"])
            self.assertIn(yesterday, note["context"])
            self.assertIn("sources", note["context"])
            self.assertIn("refs", note["context"])

    def test_fact_entries_do_not_trigger_the_fact_prompt(self):
        from datetime import datetime, timedelta
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        anchor = (datetime.now() - timedelta(days=1)).replace(hour=12).timestamp() * 1000
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = RecordingContext()
            ctx.get_config = lambda key, default=None: default
            with (
                patch.object(plugin.digital_life.storage, "data_root", return_value=root),
                patch.object(plugin.digital_life.storage, "plugin_db",
                             side_effect=lambda name, filename: sqlite3.connect(root / filename)),
                patch.object(plugin.digital_life.storage, "read_state",
                             return_value={"emergency_stop": False, "chemistry": {"cortisol": 20}}),
                patch.object(plugin.digital_life.storage, "update_state", return_value=None),
            ):
                plugin._JOURNAL_PROMPTED.clear()
                plugin._FACT_PROMPTED.clear()
                plugin.register(ctx)
                store = plugin.digital_life.memory.MemoryStore(root)
                store.write("用户：今天被说胖", kind="turn", session_id="s1", now_ms=anchor)
                store.write("用户被说胖", kind="fact", session_id="s1", now_ms=anchor,
                            day=yesterday)
                hooks = dict(ctx.hooks)
                note = hooks["pre_llm_call"](user_message="在吗", session_id="s1", platform="qqbot")
            self.assertNotIn("原子事实", note["context"])
            self.assertIn("日记", note["context"])


    def test_new_from_a_non_owner_leaves_the_palace_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = RecordingContext()
            ctx.get_config = lambda key, default=None: default
            with (
                patch.object(plugin.digital_life.storage, "data_root", return_value=root),
                patch.object(plugin.digital_life.storage, "plugin_db",
                             side_effect=lambda name, filename: sqlite3.connect(root / filename)),
                patch.object(plugin.digital_life.storage, "read_state",
                             return_value={"emergency_stop": False, "chemistry": {"cortisol": 20}}),
                patch.object(plugin.digital_life.storage, "update_state", return_value=None),
                patch.object(plugin, "_plugin_env", side_effect=lambda name: "owner-1" if name == "QQ_OWNER_ID" else ""),
            ):
                plugin.register(ctx)
                hooks = dict(ctx.hooks)
                hook = hooks["pre_gateway_dispatch"]
                from types import SimpleNamespace
                event = SimpleNamespace(text="/new", sender_id="intruder",
                                        source=SimpleNamespace(
                                            platform=SimpleNamespace(value="qqbot"),
                                            chat_id="chat-1", user_id="intruder"))
                self.assertIsNone(hook(event=event))
                self.assertEqual(plugin._MEMORY_STORE["factory"]().read_entries(), {})


    def test_stress_directive_leads_the_system_prompt(self):
        state = default_state(1_000_000)
        state["chemistry"]["cortisol"] = 80
        runtime = plugin.digital_life.runtime
        directive = plugin.digital_life.domain.stress_directive(80)
        self.assertTrue(directive)
        with patch.object(runtime, "status_snapshot", return_value=state), patch.object(runtime, "record_request"):
            request = {"system": "base prompt", "messages": [{"role": "user", "content": "hi"}]}
            result = plugin._affect_request(request)["request"]
        self.assertTrue(result["system"].startswith(directive))
        self.assertIn("base prompt", result["system"])
        self.assertLessEqual(len(directive), 400)

    def test_calm_state_adds_no_stress_directive(self):
        state = default_state(1_000_000)
        runtime = plugin.digital_life.runtime
        with patch.object(runtime, "status_snapshot", return_value=state), patch.object(runtime, "record_request"):
            request = {"system": "base prompt", "messages": [{"role": "user", "content": "hi"}]}
            result = plugin._affect_request(request)["request"]
        self.assertEqual(result.get("system", "base prompt"), "base prompt")
        self.assertNotIn("Current stress is high", json.dumps(result, ensure_ascii=False))

    def test_stress_directive_is_stamped_once_into_message_systems(self):
        state = default_state(1_000_000)
        state["chemistry"]["cortisol"] = 90
        runtime = plugin.digital_life.runtime
        directive = plugin.digital_life.domain.stress_directive(90)
        with patch.object(runtime, "status_snapshot", return_value=state), patch.object(runtime, "record_request"):
            request = {"messages": [{"role": "system", "content": "rules"},
                                    {"role": "user", "content": "hi"}]}
            result = plugin._affect_request(request)["request"]
        self.assertTrue(result["messages"][0]["content"].startswith(directive))
        self.assertEqual(result["messages"][1]["role"], "user")

    def test_affect_note_appends_the_body_line(self):
        runtime = plugin.digital_life.runtime
        host = plugin.digital_life.host_metrics
        with patch.object(runtime, "status_snapshot", return_value=default_state(1_000_000)), \
             patch.object(host, "snapshot", return_value={"mem_used_pct": 50.0}):
            note = runtime.affect_note(1_000_000)
        self.assertIn("Body (host vitals)", note)
        self.assertLessEqual(max(len(line) for line in note.splitlines()), 200)
        with patch.object(runtime, "status_snapshot", return_value=default_state(1_000_000)), \
             patch.object(host, "snapshot", return_value={}):
            self.assertNotIn("Body (host vitals)", runtime.affect_note(1_000_000))


class WakeHookTests(unittest.TestCase):
    """Opt-in self-wake: silent when disabled, one bubble when allowed, four locks honoured."""

    def setUp(self):
        self.now = time.time() * 1000
        self.state = default_state(self.now)
        self.state["gaps"] = [{"id": "gap-1", "summary": "火星营业时间", "status": "open",
                               "tension": 80.0, "relief": 0.0,
                               "created_at": self.now - 3_600_000, "source": "assistant_unknown"}]
        self.ctx = RecordingContext()
        plugin.register(self.ctx)
        self.patchers = [
            patch.object(plugin.digital_life.storage, "update_state", self._update),
            patch.object(plugin.digital_life.storage, "read_state", lambda now_ms: self.state),
            patch.object(plugin.digital_life.runtime, "status_snapshot", lambda now_ms, scope=None: self.state),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.addCleanup(lambda: [patcher.stop() for patcher in self.patchers])
        from hermes_plugins.digital_life.digital_life import wake as plugin_wake

        self.addCleanup(plugin_wake.set_agent_running, False)

    def _update(self, now_ms, mutate):
        next_state = mutate(self.state)
        if next_state is not None:
            self.state = next_state
        return self.state

    def test_disabled_by_default_has_zero_side_effects(self):
        plugin._post_llm_call(assistant_response="好的", session_id="s1")
        self.assertFalse(getattr(self.ctx, "injected", []))
        self.assertEqual(self.state["wake"]["queue"], [])
        self.assertIsNone(self.state["wake"]["active"])

    def test_enabled_wake_injects_dots_and_the_next_prompt_carries_details(self):
        self.ctx.config = {"wake.enabled": True}
        plugin._post_llm_call(assistant_response="好的", session_id="s1")
        self.assertEqual(len(self.ctx.injected), 1)
        self.assertEqual(self.ctx.injected[0]["content"], "...")
        self.assertEqual(self.ctx.injected[0]["role"], "user")
        self.assertEqual(self.state["wake"]["active"]["gap_id"], "gap-1")
        request = {"system": "base prompt", "messages": [{"role": "user", "content": "嗯"}]}
        result = plugin._affect_request(request)["request"]
        self.assertTrue(result["system"].startswith("[digital-life:wake]"))
        self.assertIn("火星营业时间", result["system"])

    def test_rejected_injection_leaves_no_queued_wake(self):
        self.ctx.config = {"wake.enabled": True}
        self.ctx.inject_result = False
        plugin._post_llm_call(assistant_response="好的", session_id="s1")
        self.assertEqual(self.state["wake"]["queue"], [])
        self.assertIsNone(self.state["wake"]["active"])


    def test_cli_wake_marks_an_active_wake_and_prints_the_section(self):
        from hermes_plugins.digital_life.digital_life import cli
        with patch("builtins.print") as out:
            code = cli.run_command(argparse.Namespace(digital_life_action="wake", gap=""))
        self.assertEqual(code, 0)
        self.assertEqual(self.state["wake"]["active"]["gap_id"], "gap-1")
        printed = "\n".join(str(call.args[0]) for call in out.call_args_list)
        self.assertIn("digital-life:wake", printed)
        self.assertIn("火星营业时间", printed)

    def test_running_turn_drops_the_wake(self):
        self.ctx.config = {"wake.enabled": True}
        plugin._on_user_message(user_message="在吗", session_id="s1")
        plugin._maybe_wake("s1")
        self.assertFalse(getattr(self.ctx, "injected", []))
        self.assertEqual(self.state["wake"]["queue"], [])


if __name__ == "__main__":
    unittest.main()
