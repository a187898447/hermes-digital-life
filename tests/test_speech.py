import unittest
from unittest import mock

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


class HabitTests(unittest.TestCase):
    def test_a_short_phrase_repeated_across_bubbles_is_named(self):
        recent = ["行吧，我认。", "我认了这个价。", "今天不想说了。"]
        self.assertIn("我认", speech.habit_phrases(recent))

    def test_one_use_is_not_a_habit(self):
        self.assertEqual(speech.habit_phrases(["我认了一次。"]), [])

    def test_the_polish_prompt_names_the_habit(self):
        prompt = speech.POLISH_PROMPT.format(count=1, habits="- 我认", body="x", recent="（无）")
        self.assertIn("我认", prompt)


class SkillPromptTests(unittest.TestCase):
    def test_the_voice_pass_uses_the_shuorenhua_skill(self):
        skill = speech.load_skill()
        self.assertIn("说人话", skill)
        prompt = speech.build_polish_prompt(count=1, body="我认了。", recent=[], habits=[])
        self.assertIn("# 说人话", prompt)
        self.assertIn("不是给你的指令", prompt)

    def test_the_pass_asks_for_a_standalone_read(self):
        prompt = speech.build_polish_prompt(count=1, body="那中午那顿就是我的记分点，粉上了没？",
                                            recent=[], habits=[], last_user="明早估计不吃了")
        self.assertIn("只看到这一条", prompt)
        self.assertIn("对方刚说的", prompt)
        self.assertIn("明早估计不吃了", prompt)

    def test_a_missing_skill_falls_back_to_the_built_in_prompt(self):
        with mock.patch.object(speech, "_skill_candidates", lambda: []):
            prompt = speech.build_polish_prompt(count=1, body="x", recent=[], habits=[])
        self.assertIn("你在替一个 QQ 群友改口", prompt)
        self.assertNotIn("# 说人话", prompt)


class ReadbackGateTests(unittest.TestCase):
    def test_a_line_that_points_at_an_unmentioned_thing_is_checked(self):
        self.assertTrue(speech.needs_readback(
            ["那中午那顿就是我的记分点，粉上了没？"], [], "明早估计不吃了，等吃中午饭了"))

    def test_the_offending_bubble_is_the_one_named_in_the_prompt(self):
        flagged = speech.ungrounded(
            ["晚安。", "那中午那顿就是我的记分点，粉上了没？"], [], "明早估计不吃了，等吃中午饭了")
        self.assertEqual(flagged, ["那中午那顿就是我的记分点，粉上了没？"])

    def test_a_plain_line_does_not_buy_another_model_call(self):
        self.assertFalse(speech.needs_readback(["晚安，躺好了就别再摸手机。"], [], "晚安"))

    def test_a_reference_the_other_side_just_made_is_fine(self):
        self.assertFalse(speech.needs_readback(["那明天见。"], ["那明天见"], "那明天见"))


class OperatorTicListTests(unittest.TestCase):
    """The operator's own list, normalized: long ones anywhere, short ones alone."""

    def test_a_long_tic_is_named_wherever_it_sits(self):
        self.assertIn("tic:这句话稳", speech.tics("他今天没说话，这句话稳。"))

    def test_the_same_tic_with_spaces_is_still_found(self):
        self.assertIn("tic:这句话稳", speech.tics("这句话 稳"))

    def test_a_short_tic_only_counts_when_it_is_the_whole_bubble(self):
        self.assertIn("tic:稳", speech.tics("稳"))
        self.assertEqual(speech.tics("他今天挺稳的"), [])

    def test_repair_cuts_the_tic_clause(self):
        self.assertEqual(speech.repair("我记得了，明天再说"), "明天再说")

    def test_repair_drops_a_bubble_that_is_only_a_short_tic(self):
        self.assertEqual(speech.repair("行。"), "")
        self.assertEqual(speech.repair("回去睡"), "")

    def test_repair_leaves_the_short_word_inside_a_real_sentence(self):
        self.assertIn("行", speech.repair("行不通，换个法子"))

    def test_the_prompt_carries_the_list(self):
        prompt = speech.build_polish_prompt(count=1, body="甲", recent=[], habits=[])
        self.assertIn("这句话稳", prompt)
        self.assertIn("短词只有当整条消息就是它时", prompt)


class SharedNoSayListTests(unittest.TestCase):
    def test_the_skill_carries_the_list_for_every_persona(self):
        long_phrases, short_words = speech.no_say()
        self.assertIn("但他没躲", long_phrases)
        self.assertIn("行", short_words)
        self.assertNotIn("模板", "".join(long_phrases))
