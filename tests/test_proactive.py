"""Chat-driven proactive-message subscriptions."""

from __future__ import annotations

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


class DeliveryTargetTests(unittest.TestCase):
    def test_empty_targets_fall_back_to_local(self):
        self.assertEqual(proactive._deliver_targets([]), ["local"])

    def test_targets_are_deduplicated(self):
        targets = [{"chat_id": "A"}, {"chat_id": "A"}, {"chat_id": "B"}]
        self.assertEqual(proactive._deliver_targets(targets), ["qqbot:A", "qqbot:B"])

    def test_subscribing_keeps_operator_deliver_entries(self):
        targets = [{"chat_id": "C1"}]
        self.assertEqual(proactive._desired_deliver(["local", "qqbot:C1"], targets),
                         ["local", "qqbot:C1"])
        self.assertEqual(proactive._desired_deliver(["local"], targets), ["local", "qqbot:C1"])
        self.assertEqual(proactive._desired_deliver(["local", "qqbot:C1"], [], dropped={"C1"}), ["local"])
        self.assertEqual(proactive._desired_deliver(["local", "qqbot:C1"], []), ["local", "qqbot:C1"])
        self.assertEqual(proactive._desired_deliver(["qqbot:OPS"], []), ["qqbot:OPS"])


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


if __name__ == "__main__":
    unittest.main()
