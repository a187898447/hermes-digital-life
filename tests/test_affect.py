"""Derived affect frame: labels, deltas, trend and migration."""

from __future__ import annotations

import unittest

from digital_life import affect
from digital_life.domain import default_state, normalize_state


def state_with(dopamine=50.0, cortisol=20.0, serotonin=50.0, energy=100.0):
    state = default_state(1_000_000)
    state["chemistry"] = {"dopamine": dopamine, "cortisol": cortisol, "serotonin": serotonin}
    state["energy"] = energy
    return state


class LabelTests(unittest.TestCase):
    def test_cortisol_dominates_when_high(self):
        self.assertEqual(affect.label(state_with(cortisol=75, energy=100)), "烦躁")

    def test_low_energy_reads_tired(self):
        self.assertEqual(affect.label(state_with(energy=12)), "疲惫")

    def test_low_serotonin_reads_low(self):
        self.assertEqual(affect.label(state_with(serotonin=20)), "低落")

    def test_high_dopamine_reads_energized(self):
        self.assertEqual(affect.label(state_with(dopamine=80, energy=60)), "来劲")

    def test_default_reads_plain(self):
        self.assertEqual(affect.label(state_with()), "平常")


class DeltaTests(unittest.TestCase):
    def test_no_history_means_zero_deltas(self):
        self.assertEqual(affect.deltas(state_with()), {field: 0.0 for field in affect.FIELDS})

    def test_deltas_compare_with_last_snapshot(self):
        state = state_with(energy=60)
        state["affect_history"] = [{"dopamine": 50.0, "cortisol": 20.0, "serotonin": 50.0, "energy": 80.0}]
        self.assertEqual(affect.deltas(state)["energy"], -20.0)

    def test_trend_reports_last_points(self):
        state = state_with(energy=20)
        state["affect_history"] = [
            {"dopamine": 50.0, "cortisol": 20.0, "serotonin": 50.0, "energy": 60.0},
            {"dopamine": 50.0, "cortisol": 20.0, "serotonin": 50.0, "energy": 40.0},
        ]
        self.assertEqual(affect.trend(state, "energy"), [60.0, 40.0, 20.0])


class RenderTests(unittest.TestCase):
    def test_render_has_mood_change_and_hint(self):
        state = state_with(energy=40)
        state["affect_history"] = [{"dopamine": 50.0, "cortisol": 20.0, "serotonin": 50.0, "energy": 55.0}]
        text = affect.render(state)
        self.assertIn("心情：", text)
        self.assertIn("−15.0", text)
        self.assertIn("倾向：", text)
        self.assertIn("近三轮能量", text)


class RecordAndMigrationTests(unittest.TestCase):
    def test_record_caps_history(self):
        state = state_with()
        for _ in range(12):
            affect.record(state, 1_000_000)
        self.assertEqual(len(state["affect_history"]), affect.HISTORY_CAP)

    def test_normalize_state_keeps_only_complete_entries(self):
        raw = default_state(1_000_000)
        raw["affect_history"] = [
            {"dopamine": 50, "cortisol": 20, "serotonin": 50, "energy": 42, "at": 1},
            {"energy": 42},
            "junk",
        ]
        state = normalize_state(raw, 1_000_000)
        self.assertEqual(len(state["affect_history"]), 1)
        self.assertEqual(state["affect_history"][0]["energy"], 42.0)
        self.assertEqual(state["affect_history"][0]["at"], 1.0)


if __name__ == "__main__":
    unittest.main()


class IntegrationTests(unittest.TestCase):
    """The frame must survive the real runtime transitions."""

    def _fake_update(self, state):
        def update_state(now_ms, mutate):
            result = mutate(state)
            return state if result is None else result
        return update_state

    def test_record_turn_snapshots_before_the_reward(self):
        from digital_life import runtime
        import sys, types
        sys.modules.setdefault("plugins", types.ModuleType("plugins"))
        stub = types.ModuleType("plugins.plugin_storage")
        stub.plugin_db = lambda *a, **k: None
        sys.modules.setdefault("plugins.plugin_storage", stub)
        from digital_life import storage

        state = state_with(energy=50)
        before = state["chemistry"]["dopamine"]
        with patch.object(storage, "update_state", self._fake_update(state)):
            runtime.record_turn(1_000_000, success=True)
        self.assertEqual(len(state["affect_history"]), 1)
        self.assertEqual(state["affect_history"][0]["dopamine"], before)
        self.assertGreater(state["chemistry"]["dopamine"], before)
        self.assertGreater(affect.deltas(state)["dopamine"], 0)

    def test_status_snapshot_exposes_the_affect_frame(self):
        from digital_life import runtime

        state = state_with(energy=10)
        with patch.object(runtime, "settle_state", return_value=state):
            snap = runtime.status_snapshot(1_000_000)
        self.assertEqual(snap["affect"]["mood"], "疲惫")
        self.assertIn("energy", snap["affect"]["deltas"])
        self.assertTrue(snap["affect"]["hint"])


from unittest.mock import patch  # noqa: E402 - kept at the bottom so earlier tests stay readable
