import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from digital_life import compaction


class FakeStore:
    def __init__(self):
        self.calls = []

    def compact(self, days=30, *, now_ms=None):
        self.calls.append(("cold", days))
        return {"ok": True, "groups": 1}

    def compact_turns(self, days=30, *, now_ms=None):
        self.calls.append(("turns", days))
        return {"ok": True, "groups": 1}


class CompactionTests(unittest.TestCase):
    def test_run_once_runs_both_folds(self):
        store = FakeStore()
        result = compaction.run_once(store, now_ms=1_000_000.0)
        self.assertEqual([call[0] for call in store.calls], ["cold", "turns"])
        self.assertEqual(result["cold"]["ok"], True)
        self.assertEqual(result["turns"]["ok"], True)

    def test_run_once_survives_one_fold_failing(self):
        class Broken(FakeStore):
            def compact(self, days=30, *, now_ms=None):
                raise RuntimeError("disk gone")

        result = compaction.run_once(Broken(), now_ms=1.0)
        self.assertIn("cold_error", result)
        self.assertIn("turns", result)

    def test_is_due_reads_the_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.assertTrue(compaction.is_due(path, now_ms=time.time() * 1000))
            path.write_text(json.dumps({"at": time.time() * 1000}), "utf-8")
            self.assertFalse(compaction.is_due(path, now_ms=time.time() * 1000))

    def test_once_records_a_run_and_does_not_repeat_within_the_window(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with patch.object(compaction, "state_path", return_value=path):
                compaction.once(lambda: store)
                deadline = time.time() + 2
                while not path.exists() and time.time() < deadline:
                    time.sleep(0.01)
                self.assertTrue(path.exists())
                written = json.loads(path.read_text("utf-8"))
                self.assertEqual(written["result"]["turns"]["groups"], 1)
                first = len(store.calls)
                compaction.once(lambda: store)  # inside the window: no second run
                time.sleep(0.1)
                self.assertEqual(len(store.calls), first)


if __name__ == "__main__":
    unittest.main()
