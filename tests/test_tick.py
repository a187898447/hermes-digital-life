"""The heartbeat: one guarded step at a time, owned by the database row."""

import threading
import time
import unittest
from unittest import mock

from digital_life import affect, life, runtime as runtime_module, tick


def _state(now_ms):
    return {"chemistry": {"dopamine": 50.0, "cortisol": 20.0, "serotonin": 40.0},
            "energy": 80.0, "gaps": [], "emergency_stop": False,
            "life": life.new_life_state(now_ms), "mood": {}}


class OnceTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time() * 1000
        state = _state(self.now)
        self.state = state
        self.updates = 0

        def settle(_now_ms):
            return self.state

        def update(_now_ms, mutate):
            self.updates += 1
            return mutate(self.state) or self.state

        store = mock.Mock(update_state=update)
        self.patchers = [
            mock.patch.object(tick, "_storage", lambda: store),
            mock.patch.object(runtime_module, "settle_state", settle),
            mock.patch.object(life, "_books", return_value=[]),
            mock.patch.object(life, "remember"),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.addCleanup(lambda: [patcher.stop() for patcher in reversed(self.patchers)])

    def test_a_recent_step_is_skipped(self):
        self.state["life"]["last_tick_at"] = self.now - 30_000
        result = tick.once(self.now)
        self.assertFalse(result["ran"])
        self.assertEqual(result["reason"], "recent")
        self.assertEqual(self.updates, 0)

    def test_a_step_advances_the_day_and_stamps_the_row(self):
        result = tick.once(self.now)
        self.assertTrue(result["ran"])
        self.assertEqual(self.state["life"]["last_tick_at"], self.now)
        self.assertIn("phase", result["life"])
        self.assertEqual(self.state["mood"]["label"], affect.label(self.state))

    def test_force_ignores_the_guard(self):
        self.state["life"]["last_tick_at"] = self.now - 1_000
        self.assertTrue(tick.once(self.now, force=True)["ran"])

    def test_an_emergency_stop_freezes_the_day(self):
        self.state["emergency_stop"] = True
        result = tick.once(self.now)
        self.assertFalse(result["ran"])
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(self.updates, 0)


class ThreadTests(unittest.TestCase):
    def setUp(self):
        tick.stop(0.1)
        self.addCleanup(tick.stop)

    def test_start_is_idempotent_and_stop_is_clean(self):
        stop_event = threading.Event()
        with mock.patch.object(tick, "_loop", lambda interval: stop_event.wait(interval)):
            self.assertTrue(tick.start(30))
            self.assertFalse(tick.start(30))
            self.assertTrue(tick.running())
            stop_event.set()
            tick.stop(timeout=1.0)
        self.assertFalse(tick.running())


if __name__ == "__main__":
    unittest.main()
