"""Unit tests for pure digital-life domain behavior."""

from __future__ import annotations

import math
import json
import unittest
from pathlib import Path

from digital_life.domain import (
    DEFAULT_CHEMICALS,
    attention_weights,
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


if __name__ == "__main__":
    unittest.main()
