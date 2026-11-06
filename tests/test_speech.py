import unittest

try:
    from digital_life import speech
except ImportError:  # pragma: no cover
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from digital_life import speech


class SpeechLintTests(unittest.TestCase):
    def test_short_chat_passes(self):
        self.assertEqual(speech.lint(["在", "困了就睡", "？"]), [])

    def test_flags_abstract_pile(self):
        self.assertTrue(speech.lint(["这个功能的兼容性、可用性和稳定度都要再确认一下"]))

    def test_flags_dangling_connector(self):
        self.assertTrue(speech.lint(["所以。"]))

    def test_flags_repeated_character(self):
        self.assertTrue(speech.lint(["我我明天再去"]))

    def test_flags_backstage_talk(self):
        self.assertTrue(speech.lint(["这轮工具用超了，下一轮再建"]))

    def test_flags_colon_list_without_connectors(self):
        self.assertTrue(speech.lint(["我打算这么改：周三上线、两个页面、五点下班"]))

    def test_parse_polish_requires_same_shape(self):
        self.assertEqual(speech.parse_polish("a\n---\nb\n---\n闲聊", 2), ["a", "b"])
        self.assertIsNone(speech.parse_polish("a", 2))
        self.assertEqual(speech.parse_polish("a\n---\nb", 2), ["a", "b"])


if __name__ == "__main__":
    unittest.main()

class TicTests(unittest.TestCase):
    def test_receipt_and_settlement_are_flagged(self):
        from digital_life import speech
        self.assertIn("receipt", speech.tics("规则我记着：周四发一次"))
        self.assertIn("settlement", speech.tics("行，这账平了"))

    def test_filler_service_and_rhetorical_tail(self):
        from digital_life import speech
        self.assertIn("filler-opener", speech.tics("说实话，我也想学"))
        self.assertIn("service-offer", speech.tics("需要我帮你翻一下吗"))
        self.assertIn("rhetorical-tail", speech.tics("那就周四见，好不好"))

    def test_plain_chat_is_clean(self):
        from digital_life import speech
        self.assertEqual(speech.tics("那就周四再说，我等着"), [])
        self.assertEqual(speech.tics("V我50"), [])

class RepairTests(unittest.TestCase):
    def test_receipt_clause_is_dropped(self):
        from digital_life import speech
        self.assertEqual(speech.repair("规则我记着：周四发一次。"), "")
        self.assertEqual(speech.repair("规则我记着：周四发一次。V我50呢？"), "V我50呢？")

    def test_filler_prefix_is_dropped(self):
        from digital_life import speech
        self.assertEqual(speech.repair("说实话，我也没想明白。"), "我也没想明白。")

    def test_settlement_clause_is_dropped(self):
        from digital_life import speech
        self.assertEqual(speech.repair("行，这账平了。那就周四。"), "行，那就周四。")
