"""Derived affect frame: labels, deltas, trend and migration."""

from __future__ import annotations

import sys
import time
import types
import unittest
from unittest.mock import patch

from digital_life import affect
from digital_life import events
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


class IntegrationTests(unittest.TestCase):
    """The frame must survive the real runtime transitions."""

    def _fake_update(self, state):
        def update_state(now_ms, mutate):
            result = mutate(state)
            return state if result is None else result
        return update_state

    def _fake_plugins(self):
        stub = types.ModuleType("plugins.plugin_storage")
        stub.plugin_db = lambda *a, **k: None
        return {"plugins": types.ModuleType("plugins"), "plugins.plugin_storage": stub}

    def test_record_turn_snapshots_before_the_reward(self):
        from digital_life import runtime

        state = state_with(energy=50)
        before = state["chemistry"]["dopamine"]
        with patch.dict(sys.modules, self._fake_plugins()):
            from digital_life import storage

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



def setUpModule():
    """Install one host stub for the whole module.

    ``patch.dict`` cannot be used here: restoring ``sys.modules`` would drop the
    modules imported inside the block, so ``digital_life.storage`` would be
    re-imported per test and ``patch.object`` would target a stale module.
    """
    if "plugins.plugin_storage" not in sys.modules:
        stub = types.ModuleType("plugins.plugin_storage")
        stub.plugin_db = lambda *args, **kwargs: None
        sys.modules.setdefault("plugins", types.ModuleType("plugins"))
        sys.modules["plugins.plugin_storage"] = stub


class EventImpulseTests(unittest.TestCase):
    """What the user did moves the chemistry, but only within the caps."""

    def _run(self, state, message, now_ms=1_000_000):
        from digital_life import runtime
        from digital_life import storage

        with patch.object(storage, "update_state", self._fake_update(state)):
            runtime.record_turn(now_ms, success=True, user_message=message)

    def _fake_update(self, state):
        def update_state(now_ms, mutate):
            result = mutate(state)
            return state if result is None else result
        return update_state

    def test_praise_moves_dopamine_up_and_cortisol_down(self):
        state = state_with(energy=50)
        before = affect.readings(state)
        self._run(state, "你真厉害")
        self.assertGreater(state["chemistry"]["dopamine"], before["dopamine"])
        self.assertLess(state["chemistry"]["cortisol"], before["cortisol"])
        self.assertEqual(state["affect_events"]["cooldowns"]["praise"], 1_000_000)

    def test_same_category_is_on_cooldown(self):
        state = state_with(energy=50)
        self._run(state, "你真厉害")
        total_after_first = state["affect_events"]["total"]
        self._run(state, "好强啊", now_ms=1_060_000)
        self.assertEqual(state["affect_events"]["total"], total_after_first)

    def test_one_insult_cannot_jump_the_cap(self):
        state = state_with(energy=50)
        baseline = affect.readings(state)
        self._run(state, "你个破程序，闭嘴")
        gained = state["chemistry"]["cortisol"] - baseline["cortisol"]
        self.assertLessEqual(gained, events.TURN_CAP + 0.001)
        self.assertGreater(gained, 0)

    def test_emergency_stop_skips_the_impulse(self):
        state = state_with(energy=50)
        state["emergency_stop"] = True
        before = affect.readings(state)
        self._run(state, "你个破程序")
        self.assertEqual(affect.readings(state), before)
        self.assertEqual(state["affect_events"]["total"], 0.0)

    def test_daily_cap_blocks_further_impulses(self):
        state = state_with(energy=50)
        today = time.strftime("%Y-%m-%d", time.localtime(1_000_000 / 1000))
        state["affect_events"] = {"day": today, "total": events.DAILY_CAP, "cooldowns": {}}
        before = affect.readings(state)
        self._run(state, "你真厉害")
        self.assertEqual(state["affect_events"]["cooldowns"], {})
        self.assertAlmostEqual(state["chemistry"]["cortisol"], before["cortisol"], delta=0.01)


    def test_code_words_are_not_insults(self):
        for message in ("这个垃圾回收器有问题", "滚轮事件没触发", "页面滚不动"):
            self.assertIsNone(events.classify(message))
        self.assertEqual(events.classify("滚"), "insult")


if __name__ == "__main__":
    unittest.main()
