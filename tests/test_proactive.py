"""Chat-driven proactive-message subscriptions."""

from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from digital_life import proactive


class ClassifyTests(unittest.TestCase):
    def test_forward_requests_add_the_chat(self):
        for text in ("以后主动一点找我", "你可以多主动一些", "有空多来找我聊", "活跃点嘛"):
            self.assertEqual(proactive.classify(text), "add", text)

    def test_quiet_requests_remove_the_chat(self):
        for text in ("这几天安静一点", "别主动发了", "别烦我", "少说点"):
            self.assertEqual(proactive.classify(text), "remove", text)

    def test_quiet_wins_over_an_embedded_forward_word(self):
        self.assertEqual(proactive.classify("不用主动找我"), "remove")

    def test_unrelated_messages_are_ignored(self):
        for text in ("帮我看看第几步了", "", "   ", "今天天气怎么样"):
            self.assertIsNone(proactive.classify(text), text)

    def test_very_long_messages_are_ignored(self):
        self.assertIsNone(proactive.classify("主动一点" + "。" * 400))


class SubscriptionFileTests(unittest.TestCase):
    def test_missing_file_means_nobody_subscribed_and_roundtrip_persists(self):
        @contextmanager
        def no_lock():
            yield

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proactive-targets.json"
            with (
                patch.object(proactive, "_targets_path", lambda: path),
                patch.object(proactive, "_targets_lock", no_lock),
                patch.object(proactive, "sync_beat_delivery"),
            ):
                self.assertEqual(proactive.subscribed_chats(), set())
                self.assertEqual(proactive.subscribe({"chat_id": "A", "chat_type": "group"}),
                                 ("added", 1))
                self.assertEqual(proactive.subscribed_chats(), {"A"})
                self.assertEqual(proactive.subscribe({"chat_id": "A"}), ("present", 1))
                self.assertEqual(proactive.unsubscribe("A"), ("removed", 0))
                self.assertEqual(proactive.subscribed_chats(), set())


    def test_track_chat_subscribes_once_with_a_float_timestamp(self):
        @contextmanager
        def no_lock():
            yield

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proactive-targets.json"
            with (
                patch.object(proactive, "_targets_path", lambda: path),
                patch.object(proactive, "_targets_lock", no_lock),
                patch.object(proactive, "sync_beat_delivery"),
            ):
                self.assertTrue(proactive.track_chat({"chat_id": "B", "chat_type": "dm"}))
                self.assertFalse(proactive.track_chat({"chat_id": "B"}))
                target = proactive.load_targets()[0]
                self.assertEqual(target["chat_id"], "B")
                self.assertIsInstance(target["added_at"], float)


class LockTests(unittest.TestCase):
    def _with_fake_storage(self, root):
        import sys
        import types

        plugins = sys.modules.setdefault("plugins", types.ModuleType("plugins"))
        storage = types.ModuleType("plugins.plugin_storage")
        storage.plugin_data_dir = lambda name: root
        old = getattr(plugins, "plugin_storage", None)
        plugins.plugin_storage = storage
        sys.modules["plugins.plugin_storage"] = storage
        self.addCleanup(self._restore, plugins, old)

    @staticmethod
    def _restore(plugins, old):
        import sys

        if old is None:
            plugins.__dict__.pop("plugin_storage", None)
            sys.modules.pop("plugins.plugin_storage", None)
        else:
            plugins.plugin_storage = old
            sys.modules["plugins.plugin_storage"] = old

    def test_lock_creates_its_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._with_fake_storage(root)
            with proactive._targets_lock():
                self.assertTrue((root / "proactive.lock").exists())

    def test_lock_degrades_when_the_lock_file_cannot_be_opened(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._with_fake_storage(Path(tmp))
            reached = []
            with patch("builtins.open", side_effect=OSError("read-only")):
                with proactive._targets_lock():
                    reached.append(True)
            self.assertEqual(reached, [True])


class HostAPIDegradeTests(unittest.TestCase):
    @staticmethod
    def _unavailable():
        raise proactive.HostAPIUnavailable("plugins.plugin_storage")

    def test_missing_plugin_storage_fails_closed_without_raising(self):
        with patch.object(proactive, "_plugin_data_dir", self._unavailable):
            self.assertEqual(proactive.load_targets(), [])
            self.assertEqual(proactive.subscribed_chats(), set())
            self.assertEqual(proactive.subscribe({"chat_id": "A"}), ("unavailable", 0))
            self.assertEqual(proactive.unsubscribe("A"), ("unavailable", 0))
            self.assertFalse(proactive.track_chat({"chat_id": "A"}))
            reached = []
            with proactive._targets_lock():
                reached.append(True)
            self.assertEqual(reached, [True])
            with patch.object(proactive, "resolve_chat",
                              return_value={"chat_id": "A", "chat_type": "dm"}):
                self.assertIsNone(proactive.handle_message("主动一点", "s-1", "qqbot"))

    def test_missing_cron_jobs_keeps_subscriptions_and_returns_zero(self):
        with patch.dict(sys.modules, {"cron.jobs": None}):
            self.assertFalse(proactive.sync_beat_delivery())
            self.assertEqual(proactive.set_beats_enabled(True), 0)
            self.assertFalse(proactive.ensure_chat_job({"chat_id": "A"}))


if __name__ == "__main__":
    unittest.main()
