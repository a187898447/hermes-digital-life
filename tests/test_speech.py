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

    def test_parse_polish_requires_same_shape(self):
        self.assertIsNone(speech.parse_polish("a\n---\nb\n---\nc", 2))
        self.assertEqual(speech.parse_polish("a\n---\nb", 2), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
