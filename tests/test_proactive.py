"""Chat-driven proactive-message subscriptions."""

from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
