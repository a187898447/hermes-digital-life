"""Behavioral tests for profile event memory; all data stays in temporary directories."""

from __future__ import annotations

import json
from contextlib import closing
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

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
        host_storage = plugin.digital_life.storage
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

    def test_day_recall_opens_journal_first_then_raw_entries(self):
        day = "2026-11-02"
        self.store.capture("用户：今天好累\n助手：早点睡", session_id="s1", now_ms=NOW)
        self.store.write("【日记 2026-11-02】今天被夸了，嘴上说不在乎。", kind="journal",
                         session_id="journal", now_ms=NOW + 1)
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


if __name__ == "__main__":
    unittest.main()
