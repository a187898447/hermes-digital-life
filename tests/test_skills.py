import os
import unittest
from unittest import mock

from digital_life import life, skills


class SkillLevelTests(unittest.TestCase):
    def test_levels_rise_with_experience(self):
        state = {"skills": skills.new_skills()}
        self.assertEqual(skills.level(state, "make_image"), 0)
        for _ in range(6):
            skills.gain(state, "make_image")
        self.assertEqual(skills.level(state, "make_image"), 1)
        self.assertEqual(skills.label(state, "make_image"), "能看")

    def test_a_level_up_is_reported_once(self):
        state = {"skills": skills.new_skills()}
        ups = [skills.gain(state, "make_video")["up"] for _ in range(6)]
        self.assertEqual(ups, [False] * 5 + [True])

    def test_a_failure_counts_less_than_a_success(self):
        state = {"skills": skills.new_skills()}
        skills.gain(state, "make_image", ok=False)
        self.assertEqual(skills.entry(state, "make_image")["xp"], skills.GAIN_FAIL)
        self.assertEqual(skills.entry(state, "make_image")["uses"], 0)


class CraftTests(unittest.TestCase):
    def test_a_new_hand_may_only_ask_for_one_subject(self):
        text = skills.craft("make_image", 0, "一只橘猫坐在窗台上")
        self.assertIn("一只橘猫坐在窗台上", text)
        self.assertIn("只画一个主体", text)

    def test_the_budget_widens_and_then_disappears(self):
        self.assertIn("简单背景", skills.craft("make_image", 1, "一只橘猫"))
        self.assertEqual(skills.craft("make_image", 2, "一只橘猫"), "一只橘猫")
        self.assertEqual(skills.craft("make_image", 3, "一只橘猫"), "一只橘猫")

    def test_video_starts_with_one_subject_and_one_action(self):
        self.assertIn("固定机位", skills.craft("make_video", 0, "夜里下雨的街"))
        self.assertEqual(skills.craft("make_video", 2, "夜里下雨的街"), "夜里下雨的街")


class SummaryTests(unittest.TestCase):
    def test_summary_lists_every_tool(self):
        state = {"skills": skills.new_skills()}
        summary = skills.summary(state)
        self.assertEqual(set(summary), set(skills.NAMES))
        self.assertTrue(all(entry["level"] == 0 for entry in summary.values()))

    def test_normalize_drops_unknown_tools_and_bad_numbers(self):
        normalized = skills.normalize_skills({"levels": {"make_image": {"xp": "12"}, "nope": {"xp": 90}}})
        self.assertEqual(set(normalized["levels"]), {"make_image"})
        self.assertEqual(normalized["levels"]["make_image"]["level"], 0)


class PracticeTests(unittest.TestCase):
    def test_the_day_offers_a_practice_slot_while_a_hand_is_new(self):
        agenda = life.build_agenda(1_760_000_000_000, mood_label="平常",
                                   skills_state={"skills": skills.new_skills()})
        kinds = [item["kind"] for item in agenda]
        self.assertIn("practice", kinds)
        item = next(item for item in agenda if item["kind"] == "practice")
        self.assertEqual(item["source"]["type"], "skill")

    def test_no_practice_once_every_hand_is_skilled(self):
        state = {"skills": skills.new_skills()}
        for name in ("make_image", "make_video"):
            state["skills"]["levels"][name] = {"xp": 400, "uses": 40, "level": 3}
        agenda = life.build_agenda(1_760_000_000_000, mood_label="平常", skills_state=state)
        self.assertNotIn("practice", [item["kind"] for item in agenda])

    def test_finishing_a_practice_leaves_a_trace(self):
        trace = life._finish_practice({"title": "练一练画图"}, {"type": "skill", "name": "make_image"}, 1.0)
        self.assertIn("画图", trace["text"])
        self.assertEqual(trace["source"]["name"], "make_image")


class NegativeTests(unittest.TestCase):
    def test_a_new_hand_leaves_the_background_out(self):
        self.assertIn("背景", skills.negative_prompt(0))
        self.assertIn("多个主体", skills.negative_prompt(1))
        self.assertEqual(skills.negative_prompt(3), "")
