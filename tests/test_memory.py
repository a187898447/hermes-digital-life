"""Behavioral tests for profile event memory; all data stays in temporary directories."""

from __future__ import annotations

import json
from contextlib import closing
import multiprocessing
import os
import sqlite3
import struct
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import sys
import types

if "plugins" not in sys.modules:
    # Keep this file runnable on its own; the full suite reuses test_events' richer stub.
    _plugins = types.ModuleType("plugins")
    _plugins.__path__ = []
    _api = types.ModuleType("plugins.plugin_storage")
    _api.plugin_data_dir = lambda name: Path(tempfile.gettempdir()) / f"{name}-memory-test"
    _api.plugin_db = lambda name, filename="data.db": sqlite3.connect(
        Path(tempfile.gettempdir()) / f"{name}-{filename}")
    _plugins.plugin_storage = _api
    sys.modules["plugins"] = _plugins
    sys.modules["plugins.plugin_storage"] = _api

from digital_life import memory

NOW = 1793592000000
DAY = 86_400_000


def process_write(root, session):
    store = memory.MemoryStore(Path(root))
    for i in range(12):
        store.write(f"{session}-{i}", session_id=session, now_ms=NOW)


def files(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def quota_writer(root):
    store = memory.MemoryStore(Path(root))
    for i in range(12):
        try:
            store.write(f"{os.getpid()}-{i}", session_id="same", now_ms=NOW)
        except ValueError as exc:
            if "quota" not in str(exc): raise


class MemoryTests(unittest.TestCase):
    def test_short_writes_are_completed(self):
        original = os.write
        with patch.object(memory.os, "write", side_effect=lambda fd, data: original(fd, data[:7])):
            result = self.write("完整写入😀" * 100)
        self.assertIn(result["id"], self.store.read_entries())

    def test_fts_unavailable_reports_clear_error(self):
        class NoFts:
            def execute(self, sql):
                if "VIRTUAL" in sql:
                    raise sqlite3.OperationalError("no such module: fts5")
        with self.assertRaisesRegex(RuntimeError, "FTS5"):
            memory.MemoryStore._populate(NoFts(), {})

    def test_rebuild_cli_and_stop(self):
        from argparse import Namespace
        from digital_life import cli, storage
        self.write("first")
        with (
            patch.object(storage, "data_root", return_value=self.root),
            patch.object(storage, "plugin_db", side_effect=lambda name, filename: sqlite3.connect(self.root / filename)),
            patch("builtins.print"),
        ):
            self.assertEqual(cli.run_command(Namespace(digital_life_action="memory-rebuild")), 0)
            with patch.object(storage, "read_state", return_value={"emergency_stop": True}):
                self.assertEqual(cli.run_command(Namespace(digital_life_action="memory-rebuild")), 1)

    def test_registered_handlers_config_and_life_state_unchanged(self):
        from test_events import plugin, RecordingContext
        import importlib
        host_storage = importlib.import_module("hermes_plugins.digital_life.digital_life.storage")
        ctx = RecordingContext()
        enabled = [True]
        ctx.get_config = lambda key, default: enabled[0]
        plugin.register(ctx)
        handlers = {tool["name"]: tool["handler"] for tool in ctx.tools}
        with (
            patch.object(host_storage, "data_root", return_value=self.root),
            patch.object(host_storage, "plugin_db", side_effect=lambda name, filename: sqlite3.connect(self.root / filename)),
        ):
            result = json.loads(handlers["memory_write"]({"text": "群里聊到熬夜"}, session_id="primary"))
            self.assertTrue(result["ok"])
            recalled = json.loads(handlers["memory_recall"]({"query": "熬夜"}))
            self.assertEqual(recalled["shown"][0]["source_session_id"], "primary")
            enabled[0] = False
            self.assertFalse(json.loads(handlers["memory_write"]({"text": "blocked"}))["ok"])
            self.assertFalse((self.root / "data.db").exists())
            with closing(sqlite3.connect(self.root / "data.db")) as db:
                db.execute("CREATE TABLE life_state(id TEXT PRIMARY KEY, payload TEXT)")
                db.execute("INSERT INTO life_state VALUES(?,?)", ("life-state", json.dumps({"emergency_stop": True})))
                db.commit()
            before = files(self.root)
            self.assertEqual(json.loads(handlers["memory_recall"]({}))["returned"], 1)
            self.assertEqual(files(self.root), before)

    def test_journal_gate_is_per_chat_and_speaker_is_per_session(self):
        from test_events import plugin, RecordingContext
        import importlib
        host_storage = importlib.import_module("hermes_plugins.digital_life.digital_life.storage")
        ctx = RecordingContext()
        ctx.get_config = lambda key, default: True
        plugin.register(ctx)
        handlers = {tool["name"]: tool["handler"] for tool in ctx.tools}
        plugin._JOURNAL_PROMPTED.clear()
        plugin._FACT_PROMPTED.clear()
        self.store.write("那天聊过一件旧事", kind="turn", day="2026-11-01")
        with (
            patch.object(host_storage, "data_root", return_value=self.root),
            patch.object(host_storage, "plugin_db",
                         side_effect=lambda name, filename: sqlite3.connect(self.root / filename)),
            patch.object(plugin, "_TURN_META", {"s-a": {"sender": "u-a"}}),
        ):
            first = plugin._JOURNAL_CHECK("qqbot", "chat-a")
            second = plugin._JOURNAL_CHECK("qqbot", "chat-b")
            self.assertIn("2026-11-01", first)
            self.assertIn("2026-11-01", second)
            self.assertEqual(plugin._JOURNAL_CHECK("qqbot", "chat-a") or "", "")
            handlers["memory_write"]({"text": "无归属的一条"}, session_id="s-b")
        entry = next(iter(e for e in self.store.read_entries().values() if e["text"] == "无归属的一条"))
        self.assertEqual(entry.get("speaker"), "")

    def test_journal_owed_days_are_per_chat_and_caches_are_bounded(self):
        from test_events import plugin, RecordingContext
        import importlib
        host_storage = importlib.import_module("hermes_plugins.digital_life.digital_life.storage")
        ctx = RecordingContext()
        ctx.get_config = lambda key, default: True
        plugin.register(ctx)
        plugin._JOURNAL_PROMPTED.clear()
        plugin._FACT_PROMPTED.clear()
        with (
            patch.object(host_storage, "data_root", return_value=self.root),
            patch.object(host_storage, "plugin_db",
                         side_effect=lambda name, filename: sqlite3.connect(self.root / filename)),
        ):
            self.store.write("chat-a 的旧事", kind="turn", day="2026-11-01", chat_id="chat-a")
            self.assertEqual(plugin._JOURNAL_CHECK("qqbot", "chat-b") or "", "")
            self.assertIn("2026-11-01", plugin._JOURNAL_CHECK("qqbot", "chat-a"))
        for index in range(600):
            plugin._remember(plugin._SOURCE_CACHE, f"s-{index}", "cli")
        self.assertLessEqual(len(plugin._SOURCE_CACHE), 512)
        self.assertIn("s-599", plugin._SOURCE_CACHE)
        plugin._SOURCE_CACHE.clear()

    def test_scheduled_runs_may_recall_a_named_chat(self):
        from test_events import plugin, RecordingContext
        import importlib
        host_storage = importlib.import_module("hermes_plugins.digital_life.digital_life.storage")
        ctx = RecordingContext()
        ctx.get_config = lambda key, default: True
        plugin.register(ctx)
        handlers = {tool["name"]: tool["handler"] for tool in ctx.tools}
        with (
            patch.object(host_storage, "data_root", return_value=self.root),
            patch.object(host_storage, "plugin_db",
                         side_effect=lambda name, filename: sqlite3.connect(self.root / filename)),
            patch.object(plugin, "_session_scope", return_value=("chat-a", "group")),
            patch.object(plugin, "_session_source", return_value="cron"),
        ):
            handlers["memory_write"]({"text": "群里聊到熬夜"}, session_id="cron-1")
            got = json.loads(handlers["memory_recall"]({"chat_id": "chat-a", "query": "熬夜"},
                                                       session_id="cron-1"))
            self.assertEqual(got["returned"], 1)
            other = json.loads(handlers["memory_recall"]({"chat_id": "chat-b"},
                                                         session_id="cron-1"))
            self.assertEqual(other["returned"], 0)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = memory.MemoryStore(self.root)
        self.logs = patch.object(memory.logger, "warning")
        self.logs.start()
        self.addCleanup(self.logs.stop)

    def write(self, text="今天下午在群里聊到熬夜", **kwargs):
        return self.store.write(text, now_ms=NOW, **kwargs)

    def recall(self, query="", **kwargs):
        return self.store.recall(query, now_ms=NOW, **kwargs)

    def test_clear_purges_only_the_requested_chat(self):
        mine = self.store.write("群里有人吵架", session_id="s1", chat_id="g1", chat_type="group")
        other = self.store.write("另一个群的记录", session_id="s2", chat_id="g2", chat_type="group")
        self.assertEqual(self.store.clear("g1"), 1)
        entries = self.store.read_entries()
        self.assertNotIn(mine["id"], entries)
        self.assertFalse(entries[other["id"]]["archived"])
        self.assertEqual([row["id"] for row in self.store.recall(chat_id="g1").get("entries", [])], [])

    def test_clear_cannot_be_resurrected_by_cold_recall_or_compaction(self):
        old = self.store.write("清空前的事", kind="turn", session_id="s1", chat_id="g1",
                               now_ms=NOW)
        self.assertEqual(self.store.clear("g1", now_ms=NOW + DAY), 1)
        cold = self.store.recall("", include_cold=True, chat_id="g1", now_ms=NOW + DAY)
        self.assertNotIn(old["id"], [row["id"] for row in cold["shown"]])
        self.assertEqual(self.store.compact(days=30, now_ms=NOW + 90 * DAY)["summaries"], 0)
        new = self.store.write("清空后的事", kind="turn", session_id="s1", chat_id="g1",
                               now_ms=NOW + 2 * DAY)
        live = self.store.recall("", chat_id="g1", now_ms=NOW + 2 * DAY)["shown"]
        self.assertEqual([row["id"] for row in live], [new["id"]])

    def test_journal_write_can_be_anchored_to_its_day(self):
        result = self.store.write("【日记 2026-10-31】那天很累。", kind="journal", day="2026-10-31")
        self.assertEqual(result["date"], "2026-10-31")
        entry = self.store.read_entries()[result["id"]]
        self.assertEqual(entry["kind"], "journal")
        self.assertEqual(entry["date"], "2026-10-31")

    def test_journal_cannot_target_today(self):
        with self.assertRaises(ValueError):
            self.store.write("【日记】今天还没过完。", kind="journal")
        earlier = self.store.write("【日记】昨天过完了。", kind="journal", day="2026-10-31")
        self.assertEqual(earlier["date"], "2026-10-31")

    def test_fact_sources_trace_to_turns_and_drop_unknown_ids(self):
        turn = self.store.capture("用户：今天又熬夜了\n助手：别熬了", session_id="s1", now_ms=NOW)
        fact = self.write("用户今天熬夜到很晚", kind="fact",
                          sources=[turn["id"], "hp-20261102-9999", "not-an-id"])
        self.assertEqual(self.store.get(fact["id"])["sources"], [turn["id"]])
        shown = self.recall("熬夜", kind="fact")["shown"]
        self.assertEqual([row["id"] for row in shown], [fact["id"]])
        self.assertEqual(shown[0]["sources"], [turn["id"]])

    def test_journal_refs_cite_facts_and_surface_as_sources(self):
        fact = self.write("用户今天熬夜到很晚", kind="fact")
        diary = self.write("【日记】今天他又熬夜，我有点心疼。", kind="journal", day="2026-10-31",
                           refs=[fact["id"], "hp-20261102-9999"])
        self.assertEqual(self.store.get(diary["id"])["refs"], [fact["id"]])
        shown = self.recall(day="2026-10-31")["shown"]
        self.assertEqual(shown[0]["kind"], "journal")
        self.assertEqual(shown[0]["sources"], [fact["id"]])

    def test_references_are_type_checked_and_ignored_for_other_kinds(self):
        with self.assertRaises(ValueError):
            self.write(kind="fact", sources="hp-20261102-0001")
        with self.assertRaises(ValueError):
            self.write(kind="journal", refs=[1])
        note = self.write("普通记录", sources=["hp-20261102-0001"], refs=["hp-20261102-0001"])
        self.assertEqual(self.store.get(note["id"])["sources"], [])
        self.assertEqual(self.store.get(note["id"])["refs"], [])

    def test_recall_kind_filter_and_unknown_kind(self):
        self.store.capture("用户：今天好累", session_id="s1", now_ms=NOW)
        fact = self.write("用户很累", kind="fact")
        self.assertEqual([row["id"] for row in self.recall(kind="fact")["shown"]], [fact["id"]])
        self.assertEqual([row["kind"] for row in self.recall(kind="turn")["shown"]], ["turn"])
        with self.assertRaises(ValueError):
            self.recall(kind="nope")

    def test_get_reads_archived_entry_without_touching_it(self):
        result = self.write("会被归档")
        self.store._append([{"version": 1, "id": result["id"], "op": "forget",
                             "archived_at": NOW}], NOW)
        entries, digest = self.store._replay()
        self.store._save_index(entries, digest)
        entry = self.store.get(result["id"])
        self.assertTrue(entry["archived"])
        self.assertEqual(entry["hits"], 0)
        self.assertEqual(entry["last_access_at"], NOW)

    def test_day_recall_opens_journal_first_then_raw_entries(self):
        day = "2026-11-01"
        self.store.capture("用户：今天好累\n助手：早点睡", session_id="s1", now_ms=NOW - 86_400_000)
        self.store.write("【日记 2026-11-01】今天被夸了，嘴上说不在乎。", kind="journal",
                         session_id="journal", day="2026-11-01")
        result = self.recall(day=day)
        self.assertEqual(result["shown"][0]["kind"], "journal")
        self.assertTrue(all(row["date"] == day for row in result["shown"]))
        with self.assertRaises(ValueError):
            self.recall(day="11-02")

    def test_capture_appends_turn_entries_without_quota(self):
        for i in range(25):
            result = self.store.capture(f"用户：第{i}句\n助手：第{i}句回话", session_id="s1", now_ms=NOW + i)
            self.assertTrue(result["ok"])
        entries = self.store.read_entries()
        self.assertEqual(sum(1 for e in entries.values() if e["kind"] == "turn"), 25)
        self.assertEqual(len(self.store.read_entries()), 25)

    def test_daily_path_id_and_duplicate_replay(self):
        # Removing duplicate reinforcement or replaying only add events must fail this test.
        first = self.write(kind="daily")
        second = self.write("  今天下午在群里聊到熬夜  ")
        self.assertEqual(first["id"], "hp-20261102-0001")
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["duplicate"])
        self.assertTrue((self.root / "memory/2026/11/2026-11-02.jsonl").exists())
        self.store.rebuild_index()
        entry = self.recall()["shown"][0]
        self.assertAlmostEqual(entry["weight"], 1.2)
        self.assertEqual(entry["hits"], 1)
        self.assertEqual(self.recall()["total"], 1)

    def test_validation_defaults_and_clamps(self):
        for text in ("", " \n ", "x" * 4001, None):
            with self.assertRaises(ValueError): self.write(text)
        for weight in (True, float("nan"), float("inf"), "1"):
            with self.assertRaises(ValueError): self.write(weight=weight)
        with self.assertRaises(ValueError): self.write(kind="other")
        for kind, expected in (("crisis", 2), ("decision", 1.5), ("fact", 1.2), ("daily", 1), ("note", 0.8)):
            self.write(kind, kind=kind)
            row = self.recall(kind)["shown"][0]
            self.assertEqual(row["weight"], expected)
        self.write("low", weight=-4)
        self.write("high", weight=99)
        self.assertEqual(self.recall("low")["shown"][0]["weight"], 0.2)
        self.assertEqual(self.recall("high")["shown"][0]["weight"], 3)

    def test_chinese_literal_and_mixed_queries(self):
        self.write()
        self.write('群里讨论 Python OR C++，进度100%_done "quoted"')
        for query, count in (("熬夜", 1), ("群里", 2), ("群里聊", 1), ("夜", 1),
                             ("群里 Python", 1), ("OR", 1), ('"quoted"', 1),
                             ("%", 1), ("_", 1), ("***", 0), ("C++", 1)):
            with self.subTest(query=query): self.assertEqual(self.recall(query)["matched"], count)
        self.assertEqual(self.recall("今天下午在群里聊到熬夜")["matched"], 1)

    def test_recall_updates_only_shown_and_counts_all(self):
        self.write("first")
        self.write("second")
        out = self.recall("first", k=1)
        self.assertEqual((out["total"], out["matched"], out["returned"]), (2, 1, 1))
        self.assertEqual(out["shown"][0]["hits"], 1)
        entries = self.store.read_entries()
        self.assertEqual(sorted(e["hits"] for e in entries.values()), [0, 1])
        for kwargs in ({"k": True}, {"k": 1.5}, {"include_cold": "false"}):
            with self.assertRaises(ValueError): self.recall(**kwargs)

    def test_recall_is_scoped_to_the_conversation(self):
        self.write("我在私聊里说喜欢熬夜", kind="note", chat_id="dm-1", chat_type="dm")
        self.write("群里有人说喜欢熬夜", kind="note", chat_id="group-9", chat_type="group")
        self.write("全局记住的偏好", kind="note")
        only_dm = self.recall("熬夜", chat_id="dm-1")
        self.assertEqual(only_dm["matched"], 1)
        self.assertEqual(only_dm["shown"][0]["chat_id"], "dm-1")
        only_group = self.recall("熬夜", chat_id="group-9")
        self.assertEqual(only_group["matched"], 1)
        unscoped = self.recall("熬夜")
        self.assertEqual(unscoped["matched"], 2)
        self.assertEqual(self.recall("偏好", chat_id="dm-1")["matched"], 1)

    def test_capture_records_speaker_and_chat(self):
        self.store.capture("用户：hi\n助手：hi", session_id="s1", chat_id="g1",
                            chat_type="group", speaker="member-7", now_ms=NOW)
        entry = self.recall("hi", chat_id="g1")["shown"][0]
        self.assertEqual((entry["chat_id"], entry["chat_type"], entry["speaker"]),
                         ("g1", "group", "member-7"))

    def test_quota_survives_rebuild_and_duplicate_does_not_count(self):
        for i in range(20): self.write(str(i), session_id="s")
        self.write("0", session_id="s")
        self.store.rebuild_index()
        with self.assertRaises(ValueError): self.write("overflow", session_id="s")
        self.write("different session", session_id="t")
        self.assertEqual(self.recall()["total"], 21)

    def test_stopped_and_disabled_write_and_readonly_replay(self):
        self.write()
        for index in ("missing", "corrupt", "valid"):
            self.store.rebuild_index()
            path = self.root / "memory-index.db"
            if index == "missing": path.unlink()
            if index == "corrupt": path.write_bytes(b"broken")
            before = files(self.root)
            stopped = memory.MemoryStore(self.root, life_state=lambda: {"emergency_stop": True, "chemistry": {"cortisol": 80}})
            self.assertEqual(stopped.recall("熬夜", now_ms=NOW)["matched"], 1)
            with self.assertRaises(ValueError): stopped.write("blocked", now_ms=NOW)
            with self.assertRaises(ValueError): stopped.rebuild_index()
            self.assertEqual(files(self.root), before)
        disabled = memory.MemoryStore(self.root, writes_enabled=lambda: False)
        with self.assertRaises(ValueError): disabled.write("blocked", now_ms=NOW)
        self.assertEqual(disabled.recall("熬夜", now_ms=NOW)["returned"], 1)

    def test_missing_store_stop_creates_nothing(self):
        root = self.root / "missing"
        store = memory.MemoryStore(root, life_state=lambda: {"emergency_stop": True})
        self.assertEqual(store.recall(now_ms=NOW)["total"], 0)
        self.assertFalse(root.exists())

    def test_torn_tail_then_append_preserves_next_event(self):
        # The valid-no-newline case must retain the old ID before allocating another.
        self.write("first")
        path = next((self.root / "memory").glob("*/*/*.jsonl"))
        with path.open("ab") as f: f.write(b'{"op":"add","text":')
        self.write("second")
        self.store.rebuild_index()
        self.assertEqual(self.recall()["total"], 2)
        self.assertTrue(list(path.parent.glob("*.recovery-*")))
        data = path.read_bytes()
        path.write_bytes(data.rstrip(b"\n"))
        self.write("third")
        self.assertEqual(self.recall()["total"], 3)

    def test_forget_replay_ignores_cold_and_invalid_references(self):
        first = self.write("archived question")
        path = next((self.root / "memory").glob("*/*/*.jsonl"))
        with path.open("a") as f:
            f.write(json.dumps({"op": "forget", "id": first["id"], "archived_at": NOW}) + "\n")
            f.write('{"op":"touch","id":"absent","hits":99}\ninvalid\n')
        cold = self.root / "memory/cold"
        cold.mkdir()
        (cold / "2026-11.jsonl").write_text('{"op":"add","id":"wrong"}\n')
        self.store.rebuild_index()
        self.assertEqual(self.recall()["total"], 0)
        out = self.recall("question", include_cold=True)
        self.assertEqual((out["cold_total"], out["matched"]), (1, 1))
        self.assertTrue(out["shown"][0]["archived"])

    def test_index_failure_returns_persisted_id_and_repairs(self):
        self.write("first")
        with patch.object(self.store, "_save_index", side_effect=sqlite3.OperationalError("full")):
            result = self.write("second")
        self.assertFalse(result["indexed"])
        duplicate = self.write("second")
        self.assertEqual(result["id"], duplicate["id"])
        self.assertEqual(self.recall()["total"], 2)


    def test_rebuild_failure_rolls_back_old_index(self):
        self.write("first")
        with patch.object(memory, "tokens", side_effect=RuntimeError("failure")):
            with self.assertRaises(RuntimeError): self.store.rebuild_index()
        with closing(sqlite3.connect(self.root / "memory-index.db")) as db:
            self.assertEqual(db.execute("select count(*) from entries").fetchone()[0], 1)

    def test_rebuild_repairs_corrupt_index(self):
        self.write("recover me")
        (self.root / "memory-index.db").write_bytes(b"not a sqlite database")
        self.assertEqual(self.store.rebuild_index()["total"], 1)
        self.assertEqual(self.recall("recover me")["matched"], 1)

    def test_fts_error_after_append_reports_persisted_id(self):
        with patch.object(self.store, "_save_index", side_effect=RuntimeError("FTS5 unavailable")):
            result = self.write("persisted despite unavailable index")
        self.assertTrue(result["ok"])
        self.assertFalse(result["indexed"])
        self.assertIn(result["id"], self.store.read_entries())

    def test_threads_and_processes_have_unique_ids(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: self.write(str(i), session_id=str(i)), range(24)))
        self.assertEqual(len({r["id"] for r in results}), 24)
        ctx = multiprocessing.get_context("spawn")
        jobs = [ctx.Process(target=process_write, args=(str(self.root), f"p{i}")) for i in range(2)]
        for job in jobs: job.start()
        for job in jobs:
            job.join(20)
            self.assertFalse(job.is_alive())
            self.assertEqual(job.exitcode, 0)
        self.assertEqual(self.recall()["total"], 48)

    def test_two_hundred_events_rebuild_exactly(self):
        self.assertEqual(len(self.store.read_entries()), 0)
        for i in range(10): self.write(str(i))
        for i in range(19): self.recall(k=10)
        expected = self.store.read_entries()
        (self.root / "memory-index.db").unlink()
        self.store.rebuild_index()
        self.assertEqual(self.store.read_entries(), expected)


class FakeEmbedder:
    """熬夜/通宵/晚睡 collapse to one axis so a synonym query can find the other word."""

    def embed(self, texts):
        return [[1.0, 0.0] if any(word in text for word in ("熬夜", "通宵", "晚睡")) else [0.0, 1.0]
                for text in texts]


class HybridRecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = memory.MemoryStore(self.root, embedder=FakeEmbedder())

    def write(self, text="昨晚熬夜到三点", **kwargs):
        return self.store.write(text, now_ms=NOW, **kwargs)

    def recall(self, query="", **kwargs):
        return self.store.recall(query, now_ms=NOW, **kwargs)

    def test_synonym_recall_finds_what_fts_cannot(self):
        fact = self.write("昨晚熬夜到三点", kind="fact")
        self.write("今天吃了火锅", kind="fact")
        result = self.recall("通宵")
        self.assertEqual(result["mode"], "hybrid")
        self.assertEqual(result["shown"][0]["id"], fact["id"])
        plain = memory.MemoryStore(self.root, embedder=False).recall("通宵", now_ms=NOW)
        self.assertEqual(plain["mode"], "fts")
        self.assertEqual(plain["matched"], 0)

    def test_vectors_are_written_with_fact_and_journal_entries(self):
        fact = self.write("昨晚熬夜", kind="fact")
        diary = self.write("【日记】昨晚他又熬夜。", kind="journal", day="2026-10-31")
        with closing(sqlite3.connect(self.root / "memory-index.db")) as db:
            ids = {row[0] for row in db.execute("SELECT id, dim, blob FROM vectors")}
        self.assertEqual(ids, {fact["id"], diary["id"]})

    def test_missing_embedder_degrades_to_fts(self):
        store = memory.MemoryStore(self.root, embedder=False)
        store.write("昨晚熬夜", kind="fact", now_ms=NOW)
        result = store.recall("通宵", now_ms=NOW)
        self.assertEqual(result["mode"], "fts")
        self.assertEqual(result["matched"], 0)
        self.assertEqual(store.recall("熬夜", now_ms=NOW)["shown"][0]["text"], "昨晚熬夜")
        with closing(sqlite3.connect(self.root / "memory-index.db")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0], 0)

    def test_hybrid_flag_can_be_turned_off(self):
        self.write("昨晚熬夜", kind="fact")
        self.assertEqual(self.recall("熬夜", hybrid=False)["mode"], "fts")
        with self.assertRaises(ValueError):
            self.recall(hybrid="yes")

    def test_corrupt_vectors_are_cleared_and_recall_falls_back(self):
        self.write("昨晚熬夜", kind="fact")
        with closing(sqlite3.connect(self.root / "memory-index.db")) as db:
            db.execute("UPDATE vectors SET blob = x'00'")
            db.commit()
        result = self.recall("通宵")
        self.assertEqual(result["mode"], "fts")
        with closing(sqlite3.connect(self.root / "memory-index.db")) as db:
            table = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='vectors'").fetchone()[0]
        self.assertEqual(table, 0)

    def test_dimension_change_falls_back_then_refills(self):
        fact = self.write("昨晚熬夜", kind="fact")
        with closing(sqlite3.connect(self.root / "memory-index.db")) as db:
            db.execute("UPDATE vectors SET dim = 3, blob = ?", (struct.pack("<3f", 1, 0, 0),))
            db.commit()
        self.assertEqual(self.recall("通宵")["mode"], "fts")
        again = self.recall("通宵")
        self.assertEqual(again["mode"], "hybrid")
        self.assertEqual(again["shown"][0]["id"], fact["id"])

    def test_rrf_order_is_stable_across_calls(self):
        self.write("昨晚熬夜看剧", kind="fact")
        self.write("今天熬夜加班", kind="fact")
        first = [row["id"] for row in self.recall("熬夜")["shown"]]
        second = [row["id"] for row in self.recall("熬夜")["shown"]]
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)


class CompactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = memory.MemoryStore(self.root, embedder=False)
        self.later = NOW + 60 * DAY

    def archive_all(self, stamp=NOW):
        ids = [entry["id"] for entry in self.store.read_entries().values() if not entry["archived"]]
        if ids:
            self.store._append([{"version": 1, "id": identifier, "op": "forget",
                                 "archived_at": stamp} for identifier in ids], stamp)
            entries, digest = self.store._replay()
            self.store._save_index(entries, digest)

    def seed(self, *texts, day="2026-10-01", chat_id="", now_ms=NOW):
        return [self.store.write(text, day=day, chat_id=chat_id, now_ms=now_ms)["id"] for text in texts]

    def test_compact_groups_archived_entries_by_date_and_chat(self):
        first, second = self.seed("十月一日群聊一件事", "十月一日群聊另一件事", chat_id="qq:1")
        other = self.seed("十月一日私聊", chat_id="qq:2")[0]
        third = self.seed("十月二日群聊", day="2026-10-02")[0]
        self.archive_all()
        live = self.store.write("今天的新事", now_ms=NOW + 10 * DAY)["id"]
        result = self.store.compact(days=30, now_ms=self.later)
        self.assertEqual(result, {"ok": True, "groups": 3, "summaries": 3, "compacted": 4})
        entries = self.store.read_entries()
        summaries = [entry for entry in entries.values() if entry["kind"] == "summary"]
        self.assertEqual(len(summaries), 3)
        for summary in summaries:
            self.assertTrue(summary["archived"])
            self.assertLessEqual(len(summary["text"]), 200)
            self.assertIn("来源", summary["text"])
        for group in ([first, second], [other], [third]):
            target = entries[group[0]]["compacted_into"]
            self.assertTrue(target)
            for identifier in group:
                self.assertEqual(entries[identifier]["compacted_into"], target)
                self.assertIn(identifier, entries[target]["text"])
            self.assertEqual(sorted(entries[target]["sources"]), sorted(group))
        self.assertEqual(entries[live]["compacted_into"], "")

    def test_peek_ranks_by_decay_and_never_touches(self):
        old = self.store.write("典中典", kind="learned", now_ms=NOW - 7 * DAY)["id"]
        fresh = self.store.write("你行你上", kind="learned", now_ms=NOW)["id"]
        before = self.store.read_entries()
        rows = self.store.peek("learned", 5, now_ms=NOW)
        after = self.store.read_entries()
        self.assertEqual([row["id"] for row in rows], [fresh, old])
        self.assertLess(rows[1]["effective_weight"], rows[0]["effective_weight"])
        for identifier in (old, fresh):
            self.assertEqual(before[identifier]["hits"], after[identifier]["hits"])
            self.assertEqual(before[identifier]["last_access_at"], after[identifier]["last_access_at"])

    def test_peek_only_returns_the_requested_kind(self):
        self.store.write("普通见闻", kind="note", now_ms=NOW)
        learned = self.store.write("这波不亏", kind="learned", now_ms=NOW)["id"]
        self.assertEqual([row["id"] for row in self.store.peek("learned", 5, now_ms=NOW)], [learned])

    def test_compact_turns_folds_only_old_live_turns(self):
        old_a = self.store.write("十月的旧对话一", kind="turn", day="2026-10-01", chat_id="qq:1")["id"]
        old_b = self.store.write("十月的旧对话二", kind="turn", day="2026-10-01", chat_id="qq:1")["id"]
        recent = self.store.write("前几天的对话", kind="turn", day="2026-12-28", chat_id="qq:1",
                                  now_ms=self.later - 2 * DAY)["id"]
        result = self.store.compact_turns(days=30, now_ms=self.later)
        self.assertEqual(result, {"ok": True, "groups": 1, "summaries": 1, "compacted": 2})
        entries = self.store.read_entries()
        summary_id = entries[old_a]["compacted_into"]
        self.assertTrue(summary_id)
        summary = entries[summary_id]
        self.assertEqual(summary["kind"], "summary")
        self.assertFalse(summary["archived"])
        self.assertEqual(sorted(summary["sources"]), sorted([old_a, old_b]))
        for identifier in (old_a, old_b):
            self.assertTrue(entries[identifier]["archived"])
        self.assertEqual(entries[recent]["compacted_into"], "")
        self.assertFalse(entries[recent]["archived"])
        recalled = self.store.recall("十月的旧对话", k=5)
        self.assertIn(summary_id, [row["id"] for row in recalled["shown"]])

    def test_compact_turns_validates_days(self):
        with self.assertRaises(ValueError):
            self.store.compact_turns(days=0)
        with self.assertRaises(ValueError):
            self.store.compact_turns(days=True)

    def test_compact_summary_cites_every_member_past_twenty(self):
        ids = [self.store.write(f"十月的旧事第{index}条", day="2026-10-01",
                                session_id=f"s-{index}", now_ms=NOW)["id"]
               for index in range(25)]
        self.archive_all()
        self.store.compact(days=30, now_ms=self.later)
        summary = next(e for e in self.store.read_entries().values() if e["kind"] == "summary")
        self.assertEqual(sorted(summary["sources"]), sorted(ids))
        self.assertLessEqual(len(summary["text"]), 200)

    def test_second_compact_is_idempotent(self):
        self.seed("旧事一", "旧事二")
        self.archive_all()
        self.store.compact(days=30, now_ms=self.later)
        again = self.store.compact(days=30, now_ms=self.later)
        self.assertEqual(again, {"ok": True, "groups": 0, "summaries": 0, "compacted": 0})
        summaries = [e for e in self.store.read_entries().values() if e["kind"] == "summary"]
        self.assertEqual(len(summaries), 1)

    def test_include_cold_shows_the_summary_instead_of_the_originals(self):
        first, second = self.seed("旧事一", "旧事二")
        self.archive_all()
        before = self.store.recall(include_cold=True, now_ms=NOW)
        self.assertEqual(before["matched"], 2)
        self.assertEqual(before["total"], 0)
        self.store.compact(days=30, now_ms=self.later)
        cold = self.store.recall(include_cold=True, now_ms=self.later)
        self.assertEqual([row["kind"] for row in cold["shown"]], ["summary"])
        self.assertEqual(cold["matched"], 1)
        self.assertEqual(cold["total"], 0)
        self.assertIn(first, cold["shown"][0]["text"])
        self.assertEqual(sorted(cold["shown"][0]["sources"]), sorted([first, second]))
        self.assertEqual(self.store.recall(now_ms=self.later)["matched"], 0)
        original = self.store.get(first)
        self.assertEqual(original["text"], "旧事一")
        self.assertEqual(original["compacted_into"], cold["shown"][0]["id"])

    def test_recently_accessed_entries_are_left_alone(self):
        identifier = self.seed("旧事")[0]
        self.archive_all()
        self.store.recall(include_cold=True, now_ms=NOW + 40 * DAY)
        self.assertEqual(self.store.compact(days=30, now_ms=self.later)["summaries"], 0)
        self.assertEqual(self.store.get(identifier)["compacted_into"], "")

    def test_compact_validates_days(self):
        with self.assertRaises(ValueError):
            self.store.compact(days=0)
        with self.assertRaises(ValueError):
            self.store.compact(days="30")

    def test_parser_exposes_memory_compact(self):
        from argparse import ArgumentParser
        from digital_life import cli
        parser = ArgumentParser()
        cli.setup_parser(parser)
        args = parser.parse_args(["memory-compact", "--days", "7"])
        self.assertEqual(args.days, 7)
        self.assertEqual(parser.parse_args(["memory-compact"]).days, 30)

    def test_compact_cli_runs_against_plugin_storage(self):
        from argparse import Namespace
        from digital_life import cli, storage
        old = time.time() * 1000 - 40 * DAY
        self.store.write("旧事", now_ms=old)
        self.archive_all(stamp=old)
        with (
            patch.object(storage, "data_root", return_value=self.root),
            patch.object(storage, "plugin_db", side_effect=lambda name, filename: sqlite3.connect(self.root / filename)),
            patch.object(storage, "read_state", return_value={}),
            patch("builtins.print") as printed,
        ):
            self.assertEqual(cli.run_command(Namespace(digital_life_action="memory-compact", days=30)), 0)
        self.assertEqual(json.loads(printed.call_args[0][0])["summaries"], 1)


class ScoringTests(unittest.TestCase):
    def test_no_newline_add_allocates_next_id_and_cross_day_touch_replays(self):
        with tempfile.TemporaryDirectory() as root:
            store = memory.MemoryStore(Path(root))
            first = store.write("first", now_ms=NOW)
            path = next((Path(root) / "memory").glob("*/*/*.jsonl"))
            path.write_bytes(path.read_bytes().rstrip(b"\n"))
            second = store.write("second", now_ms=NOW)
            self.assertNotEqual(first["id"], second["id"])
            store.recall("first", now_ms=NOW + DAY)
            store.rebuild_index()
            self.assertEqual(store.read_entries()[first["id"]]["last_access_at"], NOW + DAY)

    def test_cross_process_quota_never_exceeds_twenty(self):
        with tempfile.TemporaryDirectory() as root:
            ctx = multiprocessing.get_context("spawn")
            jobs = [ctx.Process(target=quota_writer, args=(root,)) for _ in range(2)]
            for job in jobs: job.start()
            for job in jobs:
                job.join(20)
                self.assertFalse(job.is_alive())
                self.assertEqual(job.exitcode, 0)
            self.assertEqual(len(memory.MemoryStore(Path(root)).read_entries()), 20)


    def test_bounds_monotonicity_and_weight(self):
        self.assertEqual(memory.relevance(-1), 0.5)
        self.assertEqual(memory.relevance(-4), 0.8)
        self.assertLess(memory.relevance(-0.001), memory.relevance(-1))
        self.assertEqual(memory.decay(2, NOW - 7 * DAY, NOW), 1)
        self.assertEqual(memory.attention(80)["daily"], 0.1)
        self.assertEqual(memory.attention(55)["crisis"], 1.8)
        self.assertEqual(memory.attention(54)["crisis"], 1)
        row = {"weight": 1, "last_access_at": NOW, "created_at": NOW, "kind": "note"}
        self.assertAlmostEqual(memory.score(row, NOW, 0.5, 20, False), 0.675)
        low = memory.score(dict(row, weight=0.2), NOW, 0, 20, True)
        self.assertGreater(memory.score(row, NOW, 0, 20, True), low)


class AnchorAndStressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = memory.MemoryStore(self.root)

    def test_capture_records_anchor_lines(self):
        self.store.capture("用户：重启网关的步骤\n助手：先 stop 再 start", session_id="s1", now_ms=NOW)
        entry = next(iter(self.store.read_entries().values()))
        self.assertEqual(entry["anchors"][0], "用户：重启网关的步骤")
        self.assertEqual(len(entry["anchors"]), 2)

    def test_written_entries_default_to_no_anchors(self):
        result = self.store.write("今天下午在群里聊到熬夜", now_ms=NOW)
        self.assertEqual(self.store.read_entries()[result["id"]]["anchors"], [])

    def test_anchor_affinity_boosts_only_matching_queries(self):
        row = {"weight": 1, "last_access_at": NOW, "created_at": NOW, "kind": "note",
               "anchors": ["重启网关的步骤"]}
        boosted = memory.score(row, NOW, 0.5, 20, False, "重启网关的步骤现在")
        plain = memory.score(dict(row, anchors=[]), NOW, 0.5, 20, False, "重启网关的步骤现在")
        unrelated = memory.score(row, NOW, 0.5, 20, False, "晚饭吃什么")
        self.assertAlmostEqual(boosted, plain * memory.ANCHOR_BOOST)
        self.assertAlmostEqual(unrelated, plain)
        self.assertTrue(memory.anchor_affinity(row, "重启网关的步骤现在"))
        self.assertFalse(memory.anchor_affinity(row, "晚饭吃什么"))

    def test_recall_cortisol_override_crosses_the_80_boundary(self):
        crisis = self.store.write("出事了，他很慌", kind="crisis", now_ms=NOW)
        daily = self.store.write("今天天气很好", kind="daily", now_ms=NOW)
        calm = self.store.recall("", now_ms=NOW, cortisol=40)
        stressed = self.store.recall("", now_ms=NOW, cortisol=80)
        self.assertEqual(stressed["attention"]["crisis"], 3.0)
        self.assertEqual(stressed["attention"]["daily"], 0.1)
        self.assertEqual(calm["attention"]["crisis"], 1.0)
        self.assertEqual(stressed["shown"][0]["id"], crisis["id"])
        self.assertEqual(calm["attention"]["daily"], 1.0)

    def test_state_cortisol_is_used_when_no_override(self):
        self.store.life_state = lambda: {"emergency_stop": False, "chemistry": {"cortisol": 80}}
        self.assertEqual(self.store.recall("", now_ms=NOW)["attention"]["crisis"], 3.0)



    def test_revoke_only_undoes_recent_entries(self):
        store = self.store
        first = store.write("刚写错的", kind="note", now_ms=1_000_000)
        old = store.write("很久以前写的", kind="note", now_ms=999_000)
        result = store.revoke(first["id"], now_ms=1_000_000 + 60_000)
        self.assertTrue(result["ok"])
        self.assertTrue(store.read_entries()[first["id"]].get("archived"))
        stale = store.revoke(old["id"], now_ms=1_000_000 + 20 * 60_000)
        self.assertFalse(stale["ok"])
        self.assertIn("correct", stale["error"])
        self.assertFalse(store.read_entries()[old["id"]].get("archived"))

if __name__ == "__main__":
    unittest.main()


class MultiTermRecallTests(unittest.TestCase):
    def test_sentence_query_falls_back_to_any_term(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = memory.MemoryStore(Path(tmp))
            store.write("我中午有午休，14点才接着干活", kind="turn")
            store.write("今天天气不错", kind="turn")
            result = store.recall("上班时间午休多久")
            self.assertEqual(result["matched"], 1)
            self.assertIn("午休", result["shown"][0]["text"])

    def test_all_terms_still_preferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = memory.MemoryStore(Path(tmp))
            store.write("洄游 记忆 本心", kind="turn")
            store.write("洄游 只是名字", kind="turn")
            result = store.recall("洄游 记忆")
            self.assertEqual(result["matched"], 1)
            self.assertIn("本心", result["shown"][0]["text"])


class JournalFormatTests(unittest.TestCase):
    def test_timeline_journal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = memory.MemoryStore(Path(tmp))
            with self.assertRaises(ValueError):
                store.write("21:57 他在加班。22:06 他让我起名。23:10 他说晚安。", kind="journal", day="2026-10-31")

    def test_narrative_journal_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = memory.MemoryStore(Path(tmp))
            result = store.write("今天被人调了一轮脾气，感觉像换了个壳，边角有点痒。晚上他让我给自己起名字，我挑了洄游。",
                                 kind="journal", day="2026-10-31")
            self.assertTrue(result["ok"])


class QuotaTests(unittest.TestCase):
    def test_turn_captures_do_not_consume_the_write_quota(self):
        from digital_life import memory as memmod
        from pathlib import Path
        import tempfile, os
        tmp = tempfile.mkdtemp()
        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = tmp
        try:
            store = memmod.MemoryStore(Path(tmp))
            for i in range(25):
                store.capture(f"第{i}轮", session_id="s1", chat_id="c1")
            first = store.write("一条事实", kind="fact", session_id="s1", chat_id="c1")
            self.assertTrue(first.get("ok"))
            for i in range(19):
                store.write(f"事实{i}", kind="fact", session_id="s1", chat_id="c1")
            with self.assertRaises(ValueError):
                store.write("第21条", kind="fact", session_id="s1", chat_id="c1")
        finally:
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old
