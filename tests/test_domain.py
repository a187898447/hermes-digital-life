"""Unit tests for pure digital-life domain behavior."""

from __future__ import annotations

import math
import json
import unittest
from pathlib import Path

from digital_life.domain import (
    DEFAULT_CHEMICALS,
    attention_weights,
    find_gap_sentence,
    is_gap_worthy,
    is_unknown_claim,
    pick_temperature,
    decay_chemistry,
    decay_toward,
    default_state,
    normalize_state,
    normalize_temperament,
    parse_genome,
    rank_memories,
    reorder_formatted_memory_blocks,
    settle_rest,
)


class ChemistryTests(unittest.TestCase):
    def test_decay_toward_reference_vectors(self):
        self.assertEqual(decay_toward(90.0, 50.0, 0, 1000), 90.0)
        self.assertEqual(decay_toward(90.0, 50.0, 1000, 1000), 70.0)
        self.assertEqual(decay_toward(10.0, 50.0, 1000, 1000), 30.0)
        self.assertEqual(decay_toward(float("nan"), 20.0, 1000, 1000), 20.0)
        self.assertEqual(decay_toward(101.0, 50.0, 0, 1000), 100.0)
        self.assertEqual(decay_toward(70.0, 30.0, -1, 1000), 70.0)

    def test_pinned_upstream_reference_vectors(self):
        vectors = json.loads((Path(__file__).with_name("upstream-vectors.json")).read_text())
        decay_inputs = [(90, 50, 1000, 1000), (10, 50, 60000, 120000), (50, 20, 3600000, 1800000)]
        for inputs, expected in zip(decay_inputs, vectors["decayToward"]):
            self.assertAlmostEqual(decay_toward(*inputs), expected, places=12)
        raw_temperaments = [None, {"sensitivity": 9, "resilience": 0.1, "enthusiasm": 5},
                            {"sensitivity": 1.25, "resilience": 1.5, "enthusiasm": 65, "metabolism": 0.75}]
        for raw, expected in zip(raw_temperaments, vectors["normalizeTemperament"]):
            self.assertEqual(normalize_temperament(raw), {key: float(value) for key, value in expected.items()})
        sleep_inputs = [(0, 100, 3600000, 43200000), (50, 100, 600000, 43200000), (100, 100, 600000, 43200000)]
        for args, expected in zip(sleep_inputs, vectors["settleRest"]):
            actual = settle_rest(*args, False, {"quiet_hours": None})
            self.assertEqual(actual["mode"], expected["mode"])
            self.assertEqual(actual["sleep_ms"], expected["sleepMs"])
            self.assertAlmostEqual(actual["energy"], expected["energy"])

    def test_temperament_normalization_matches_default_ranges(self):
        self.assertEqual(normalize_temperament({"sensitivity": 9, "resilience": 0.1, "enthusiasm": 5}), {
            "sensitivity": 2.0, "resilience": 0.5, "enthusiasm": 20.0, "metabolism": 1.0,
        })

    def test_default_state_has_three_core_chemicals(self):
        state = default_state(1234)
        self.assertEqual(set(state["chemistry"]), set(DEFAULT_CHEMICALS))
        self.assertEqual(state["last_interaction_at"], 1234)
        self.assertEqual(state["energy"], 100.0)

    def test_state_normalization_rejects_non_finite_values(self):
        state = normalize_state({"energy": float("nan"), "chemistry": {"cortisol": float("inf")}}, 100)
        self.assertEqual(state["energy"], 100.0)
        self.assertEqual(state["chemistry"]["cortisol"], 20.0)

    def test_genome_parser_restores_missing_core_chemicals(self):
        genome, problems = parse_genome('{"chemicals": [{"id":"dopamine","baseline":65}]}')
        self.assertEqual(set(genome["chemicals"]), set(DEFAULT_CHEMICALS))
        self.assertEqual(genome["chemicals"]["dopamine"]["baseline"], 65.0)
        self.assertTrue(problems)

    def test_genome_parser_falls_back_on_malformed_input(self):
        genome, problems = parse_genome("not-json")
        self.assertEqual(set(genome["chemicals"]), set(DEFAULT_CHEMICALS))
        self.assertTrue(problems)

    def test_serotonin_stress_and_recovery_rules(self):
        source = {"dopamine": 50.0, "cortisol": 100.0, "serotonin": 50.0}
        stressed = decay_chemistry(source, 60_000, {"resilience": 1.0}, energy=90, high_tension_gap=False)
        self.assertLess(stressed["serotonin"], 49.5)
        calm = {"dopamine": 50.0, "cortisol": 0.0, "serotonin": 50.0}
        recovered = decay_chemistry(calm, 60_000, {"resilience": 1.0}, energy=90, high_tension_gap=False)
        self.assertGreater(recovered["serotonin"], 50.0)


class SleepEnergyTests(unittest.TestCase):
    def test_deep_sleep_recovers_to_natural_cap(self):
        result = settle_rest(0, 100, 60 * 60_000, 12 * 60 * 60_000, False, is_quiet_hour_fn=lambda _at: False)
        self.assertEqual(result["energy"], 28)
        self.assertEqual(result["mode"], "deep-sleep")
        self.assertGreater(result["sleep_ms"], 0)

    def test_regular_rest_and_frozen_state(self):
        result = settle_rest(50, 100, 10 * 60_000, 12 * 60 * 60_000, False)
        self.assertEqual(result["energy"], 52)
        frozen = settle_rest(10, 100, 60 * 60_000, 12 * 60 * 60_000, True)
        self.assertEqual(frozen, {"energy": 10, "sleep_ms": 0, "mode": "frozen"})

    def test_overshoot_decays_toward_cap(self):
        result = settle_rest(100, 100, 10 * 60_000, 12 * 60 * 60_000, False)
        self.assertEqual(result["energy"], 99)


class MemoryAffinityTests(unittest.TestCase):
    def test_attention_weights_interpolate_and_clamp_at_thresholds(self):
        self.assertEqual(attention_weights(40), {"crisis": 1.0, "daily": 1.0})
        self.assertEqual(attention_weights(80), {"crisis": 3.0, "daily": 0.1})
        self.assertEqual(attention_weights(60), {"crisis": 2.0, "daily": 0.55})

    def test_crisis_is_reordered_but_every_entry_is_preserved(self):
        memories = ["日常：周末吃了蛋糕", "危机：服务器发生故障", "普通记忆"]
        ranked = rank_memories(memories, 90)
        self.assertEqual(len(ranked), len(memories))
        self.assertCountEqual(ranked, memories)
        self.assertEqual(ranked[0], "危机：服务器发生故障")

    def test_positive_affinity_is_only_a_bounded_resonance_boost(self):
        memories = ["普通記錄", "开心：一起喝咖啡"]
        ranked = rank_memories(memories, 20, dopamine=80)
        self.assertCountEqual(ranked, memories)
        self.assertEqual(ranked[0], "开心：一起喝咖啡")

    def test_rendered_hermes_memory_block_is_reordered_without_losing_entries(self):
        separator = "═" * 46
        entries = ["日常：周末吃了蛋糕😀", "危机：服务器发生故障", "普通记忆"]
        body = "\n§\n".join(entries)
        utf16_length = len(body.encode("utf-16-le")) // 2
        prompt = f"{separator}\nMEMORY (your personal notes) [1% — {utf16_length}/2200 chars]\n{separator}\n{body}\n\n## Plugin Context: stable-id\nkeep this"
        rewritten, blocks = reorder_formatted_memory_blocks(prompt, cortisol=90, dopamine=50)
        self.assertEqual(blocks, 1)
        self.assertLess(rewritten.index("危机：服务器发生故障"), rewritten.index("日常：周末吃了蛋糕"))
        self.assertIn("## Plugin Context: stable-id\nkeep this", rewritten)
        self.assertEqual(rewritten.count("§"), prompt.count("§"))
        for entry in entries:
            self.assertEqual(rewritten.count(entry), 1)

    def test_nested_header_inside_an_entry_does_not_extend_the_span(self):
        separator = "═" * 46
        nested_body = "tail-of-tail"
        nested = (
            f"{separator}\nMEMORY (your personal notes) [1% — {len(nested_body)}/2200 chars]\n"
            f"{separator}\n{nested_body}"
        )
        entries = ["日常：周末吃了蛋糕", "危机：服务器发生故障\n" + nested]
        body = "\n§\n".join(entries)
        prompt = (
            f"{separator}\nMEMORY (your personal notes) [1% — "
            f"{len(body.encode('utf-16-le')) // 2}/2200 chars]\n{separator}\n{body}"
        )
        rewritten, blocks = reorder_formatted_memory_blocks(prompt, cortisol=90, dopamine=50)
        self.assertEqual(blocks, 1)
        for entry in entries:
            self.assertEqual(rewritten.count(entry), 1)
        self.assertEqual(rewritten.count(nested_body), 1)

    def test_declared_length_mismatch_leaves_the_prompt_untouched(self):
        separator = "═" * 46
        body = "日常：周末吃了蛋糕"
        prompt = (
            f"{separator}\nMEMORY (your personal notes) [1% — "
            f"{len(body) + 1}/2200 chars]\n{separator}\n{body}"
        )
        rewritten, blocks = reorder_formatted_memory_blocks(prompt, cortisol=90, dopamine=50)
        self.assertEqual(blocks, 0)
        self.assertEqual(rewritten, prompt)


class SignalDomainTests(unittest.TestCase):
    def test_longest_sentence_and_markdown_table_candidate(self):
        short = "我不知道天气"
        long = "我想弄明白火星上的沙尘暴为什么持续这么久"
        self.assertEqual(find_gap_sentence(short + "。" + long), long)
        table = "| 很长但是没有认知诉求的旁注文字应该忽略 | **我不知道营业时间** |"
        self.assertEqual(find_gap_sentence(table), "我不知道营业时间")
        self.assertEqual(len(find_gap_sentence("我不知道" + "未知的事" * 40)), 100)
        self.assertFalse(is_gap_worthy("我去同步文件"))

    def test_gap_ages_top_three_and_oversized_summary(self):
        from digital_life.runtime import append_open_loops
        now = 3 * 86_400_000
        gaps = [
            {"status": "open", "tension": tension, "summary": str(tension), "created_at": now - age}
            for tension, age in ((20, 60_000), (90, 86_400_000), (80, 3_600_000), (70, 1_200_000))
        ]
        text = append_open_loops("base", gaps, now)
        self.assertTrue(text.startswith("base\n\n[open loops] tension=90"))
        self.assertIn("（1d）", text)
        self.assertIn("（1h）", text)
        self.assertIn("（20m）", text)
        self.assertNotIn("tension=20", text)
        gaps[1]["summary"] = "x" * 1000
        suffix = append_open_loops("base", gaps, now)[4:]
        self.assertEqual(len(suffix), 400)
        self.assertIn("tension=90", suffix)

    def test_temperature_policy_thresholds_continuous_energy_and_clamp(self):
        self.assertEqual(pick_temperature({"cortisol": 80, "dopamine": 100, "serotonin": 0}, 0), 1.0)
        self.assertEqual(pick_temperature({"cortisol": 79, "dopamine": 70, "serotonin": 0}, 0), 0.85)
        self.assertAlmostEqual(pick_temperature({"cortisol": 20, "dopamine": 50, "serotonin": 50}, 100), 0.70 + 0.10 * 20 / 35)
        self.assertAlmostEqual(pick_temperature({"cortisol": 20, "dopamine": 50, "serotonin": 50}, 19), 0.70 + 0.10 * 20 / 35 - 0.15)
        self.assertEqual(pick_temperature({"cortisol": 1000, "dopamine": 50, "serotonin": 0}, 100), 1.0)
        self.assertGreaterEqual(pick_temperature({"cortisol": -100, "dopamine": -100, "serotonin": 1000}, 0), 0.3)

    def test_unknown_claim_and_gap_candidate_filters(self):
        self.assertTrue(is_unknown_claim("我真的不知道火星的营业时间"))
        self.assertTrue(is_gap_worthy("我想查一下火星的营业时间"))
        self.assertFalse(is_gap_worthy("这个意味着我不知道火星营业时间"))
        self.assertFalse(is_gap_worthy("我不知道执念张力如何增长"))
        self.assertEqual(find_gap_sentence("😀。我不知道火星的营业时间。"), "我不知道火星的营业时间")
        self.assertIsNone(find_gap_sentence("/我不知道火星营业时间"))
        self.assertIsNone(find_gap_sentence("😀。嗯。"))
        self.assertEqual(find_gap_sentence("| ○ | 80 | 我不知道火星营业时间 | 注释 |"), "我不知道火星营业时间")
        result = find_gap_sentence("我不知道火星的营业时间。这个意味着我不知道缺口状态。")
        self.assertEqual(result, "我不知道火星的营业时间")

    def test_gap_append_is_byte_identical_without_open_gaps_and_bounded(self):
        from digital_life.runtime import append_open_loops
        self.assertEqual(append_open_loops("abc", [], 1_000_000), "abc")
        gaps = [
            {"status": "open", "tension": tension, "created_at": 0, "summary": f"gap-{tension}-" + "x" * 180}
            for tension in (95, 85, 75, 65)
        ]
        result = append_open_loops("system", gaps, 2 * 86_400_000)
        suffix = result[len("system\n\n"): ]
        self.assertLessEqual(len(suffix), 400)
        self.assertIn("tension=95", suffix)
        self.assertNotIn("tension=65", suffix)
        self.assertIn("2d", suffix)

if __name__ == "__main__":
    unittest.main()
