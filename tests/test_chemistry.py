"""P0: pure chemistry math, genome parsing, and the generic chemical pool."""

from __future__ import annotations

import unittest

from digital_life.chemical_pool import ChemicalPool
from digital_life.chemistry import (
    MINUTE_MS,
    apply_metabolism,
    clamp,
    decay_toward,
    float_or,
    normalize_temperament,
)
from digital_life.domain import DEFAULT_CHEMICALS as DOMAIN_CHEMICALS
from digital_life.domain import DEFAULT_TEMPERAMENT, decay_chemistry, default_state, normalize_state
from digital_life.genome import DEFAULT_CHEMICALS, default_genome, parse_genome


class ChemistryMathTests(unittest.TestCase):
    def test_decay_toward_reference_vectors(self):
        self.assertEqual(decay_toward(90.0, 50.0, 0, 1000), 90.0)
        self.assertEqual(decay_toward(90.0, 50.0, 1000, 1000), 70.0)
        self.assertEqual(decay_toward(10.0, 50.0, 1000, 1000), 30.0)
        self.assertEqual(decay_toward(float("nan"), 20.0, 1000, 1000), 20.0)
        self.assertEqual(decay_toward(101.0, 50.0, 0, 1000), 100.0)
        self.assertEqual(decay_toward(70.0, 30.0, -1, 1000), 70.0)

    def test_decay_is_monotone_toward_baseline_and_never_overshoots(self):
        previous = 100.0
        for minutes in range(0, 600, 15):
            value = decay_toward(100.0, 20.0, minutes * MINUTE_MS, 30 * MINUTE_MS)
            self.assertGreaterEqual(value, 20.0)
            self.assertLessEqual(value, previous + 1e-9)
            self.assertAlmostEqual(value, 20.0 + 80.0 * 0.5 ** (minutes / 30.0), places=9)
            previous = value
        self.assertAlmostEqual(decay_toward(20.0, 20.0, 10 ** 9, 1000), 20.0, places=9)

    def test_clamp_and_float_or_never_raise(self):
        self.assertEqual(clamp(float("inf")), 0.0)
        self.assertEqual(clamp(-3.0, 0.0, 100.0), 0.0)
        self.assertEqual(clamp(300.0, 0.0, 100.0), 100.0)
        self.assertEqual(float_or(None, 7.0), 7.0)
        self.assertEqual(float_or("bad", 7.0), 7.0)
        self.assertEqual(float_or("2.5", 7.0), 2.5)

    def test_temperament_resilience_floor(self):
        # PRD M5.5 names a 0.1 resilience floor, but the adopted TEMPERAMENT_RANGES clamp
        # (0.5-2.0) already bottoms out at 0.5 and the existing contract asserts 0.5.
        self.assertEqual(normalize_temperament({"resilience": 0.01})["resilience"], 0.5)
        self.assertEqual(normalize_temperament({"sensitivity": 9})["sensitivity"], 2.0)
        self.assertEqual(normalize_temperament({"enthusiasm": 500})["enthusiasm"], 80.0)

    def test_metabolism_scales_request_cost(self):
        self.assertAlmostEqual(apply_metabolism(100.0, 1.0), 99.95)
        self.assertAlmostEqual(apply_metabolism(100.0, 2.0), 99.9)
        self.assertEqual(apply_metabolism(0.01, 2.0), 0.0)


class GenomeTests(unittest.TestCase):
    def test_domain_reexports_same_defaults(self):
        self.assertEqual(set(DOMAIN_CHEMICALS), set(DEFAULT_CHEMICALS))

    def test_parse_bad_fields_degrade_per_field(self):
        genome, problems = parse_genome({
            "chemicals": [
                {"id": "dopamine", "baseline": "abc", "initial": None, "halfLifeMinutes": -5, "max": 60},
                {"id": "serotonin", "baseline": 61.5, "rules": "nope"},
                {"id": "oxytocin", "baseline": 30},
            ],
            "energy_rules": [{"event": "request", "delta": -0.05}],
            "drift_rules": [{"chemical": "serotonin", "per_minute": -1.5, "when": [{"key": "cortisol", "op": ">=", "value": 70}]}],
            "temperament": {"sensitivity": 99},
        })
        self.assertTrue(problems)
        dopamine = genome["chemicals"]["dopamine"]
        self.assertEqual(dopamine["baseline"], 50.0)
        self.assertEqual(dopamine["half_life_minutes"], 0.1)
        self.assertEqual(dopamine["max"], 60.0)
        self.assertEqual(genome["chemicals"]["serotonin"]["baseline"], 61.5)
        self.assertEqual(genome["chemicals"]["serotonin"]["min"], 0.0)
        self.assertNotIn("oxytocin", genome["chemicals"])
        self.assertEqual(genome["temperament"]["sensitivity"], 2.0)
        self.assertEqual(genome["energy_rules"], [{"event": "request", "delta": -0.05}])
        self.assertEqual(len(genome["drift_rules"]), 1)

    def test_parse_invalid_json_and_non_object_never_raise(self):
        for raw in ("not-json", None, 3, [1, 2], "[]"):
            genome, problems = parse_genome(raw)
            self.assertEqual(set(genome["chemicals"]), set(DEFAULT_CHEMICALS))
            self.assertTrue(problems)
            self.assertEqual(genome["temperament"], DEFAULT_TEMPERAMENT)

    def test_parse_partial_genome_restores_missing_core_chemicals(self):
        genome, problems = parse_genome('{"chemicals": [{"id":"dopamine","baseline":65}]}')
        self.assertEqual(set(genome["chemicals"]), set(DEFAULT_CHEMICALS))
        self.assertEqual(genome["chemicals"]["dopamine"]["baseline"], 65.0)
        self.assertTrue(any("missing core chemicals" in problem for problem in problems))

    def test_custom_rule_event_reaches_pool(self):
        genome, _ = parse_genome({"chemicals": [{"id": "dopamine", "rules": [{"event": "novelty", "delta": -7}]}]})
        state = {"chemistry": {"dopamine": 50.0, "cortisol": 20.0, "serotonin": 50.0}}
        ChemicalPool(genome).apply("novelty", state)
        self.assertEqual(state["chemistry"]["dopamine"], 43.0)


class ChemicalPoolTests(unittest.TestCase):
    def setUp(self):
        self.pool = ChemicalPool()

    def test_initial_and_normalized_chemistry_use_enthusiasm_for_dopamine(self):
        chemistry = self.pool.initial_chemistry({"enthusiasm": 62.0})
        self.assertEqual(chemistry["dopamine"], 62.0)
        self.assertEqual(chemistry["cortisol"], 20.0)
        normalized = self.pool.normalize_chemistry({"dopamine": 999, "serotonin": "x"}, {"enthusiasm": 62.0})
        self.assertEqual(normalized["dopamine"], 100.0)
        self.assertEqual(normalized["serotonin"], 50.0)
        self.assertEqual(set(normalized), set(DEFAULT_CHEMICALS))

    def test_pool_decay_matches_domain_decay(self):
        chemistry = {"dopamine": 54.3, "cortisol": 88.0, "serotonin": 71.2}
        temperament = {"sensitivity": 1.0, "resilience": 1.0, "enthusiasm": 50.0, "metabolism": 1.0}
        expected = decay_chemistry(chemistry, 42 * MINUTE_MS, temperament, energy=90.0, high_tension_gap=False)
        actual = self.pool.decay({"chemistry": chemistry, "temperament": temperament, "energy": 90.0},
                                 elapsed_ms=42 * MINUTE_MS)
        self.assertEqual(actual, expected)

    def test_drift_rules_reproduce_upstream_serotonin_drive(self):
        chemistry = {"dopamine": 50.0, "cortisol": 90.0, "serotonin": 80.0}
        reliability = {"chemistry": chemistry, "energy": 95.0}
        result = self.pool.decay(dict(reliability), elapsed_ms=10 * MINUTE_MS)
        relaxed = decay_toward(80.0, 50.0, 10 * MINUTE_MS, 120 * MINUTE_MS)
        self.assertAlmostEqual(result["serotonin"], relaxed - 1.5 * 10, places=9)

        calm = {"chemistry": {"dopamine": 50.0, "cortisol": 30.0, "serotonin": 40.0}, "energy": 95.0}
        recovered = self.pool.decay(calm, elapsed_ms=10 * MINUTE_MS)
        relaxed_calm = decay_toward(40.0, 50.0, 10 * MINUTE_MS, 120 * MINUTE_MS)
        self.assertAlmostEqual(recovered["serotonin"], relaxed_calm + 0.8 * 10, places=9)

        tense = {"chemistry": {"dopamine": 50.0, "cortisol": 30.0, "serotonin": 40.0}, "energy": 95.0,
                 "gaps": [{"status": "open", "tension": 90.0}]}
        self.assertAlmostEqual(self.pool.decay(tense, elapsed_ms=10 * MINUTE_MS)["serotonin"], relaxed_calm, places=9)

    def test_zero_elapsed_clamps_without_drifting(self):
        result = self.pool.decay({"chemistry": {"dopamine": 101.0, "cortisol": -5.0, "serotonin": 50.0}}, elapsed_ms=0)
        self.assertEqual(result["dopamine"], 100.0)
        self.assertEqual(result["cortisol"], 0.0)

    def test_apply_request_turn_and_provider_error(self):
        state = default_state(0)
        self.pool.apply("request", state)
        self.assertAlmostEqual(state["energy"], 100.0 - 0.05)
        before = state["chemistry"]["dopamine"]
        self.pool.apply("turn_complete", state)
        self.assertEqual(state["chemistry"]["dopamine"], before + 5.0)
        state["temperament"]["sensitivity"] = 1.5
        self.pool.apply("provider_error", state)
        self.assertAlmostEqual(state["chemistry"]["cortisol"], 20.0 + 30.0)

    def test_apply_ignores_unknown_events_and_respects_declared_bounds(self):
        state = default_state(0)
        snapshot = dict(state["chemistry"])
        self.pool.apply("nothing", state)
        self.assertEqual(state["chemistry"], snapshot)
        genome, _ = parse_genome({"chemicals": [{"id": "dopamine", "max": 60}]})
        pool = ChemicalPool(genome)
        for _ in range(10):
            pool.apply("turn_complete", state)
        self.assertEqual(state["chemistry"]["dopamine"], 60.0)

    def test_inflict_is_internal_and_clamped(self):
        state = default_state(0)
        self.assertEqual(self.pool.inflict(state, "cortisol", 500.0), 100.0)
        self.assertEqual(self.pool.inflict(state, "cortisol", -500.0), 0.0)
        self.assertIsNone(self.pool.inflict(state, "oxytocin", 10.0))

    def test_normalize_state_keeps_old_documents_and_adds_nothing(self):
        old = {"chemistry": {"dopamine": 20.0}, "energy": 50.0, "commitment": {"x": 1}, "wake": {"queued": True}}
        state = normalize_state(old, 5_000)
        self.assertEqual(set(state["chemistry"]), set(DEFAULT_CHEMICALS))
        self.assertNotIn("commitment", state)
        self.assertNotIn("wake", state)
        self.assertEqual(default_genome()["version"], 1)


if __name__ == "__main__":
    unittest.main()
