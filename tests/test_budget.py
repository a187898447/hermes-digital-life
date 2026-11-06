import tempfile
import unittest
from pathlib import Path
from unittest import mock

from digital_life import budget

DAY = 86_400_000
NOW = 1_700_000_000_000


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "budget.json"
        patcher = mock.patch.object(budget, "state_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_spend_stops_at_the_cap(self):
        self.assertTrue(budget.spend("read", 2, now_ms=NOW))
        self.assertTrue(budget.spend("read", 2, now_ms=NOW))
        self.assertFalse(budget.spend("read", 2, now_ms=NOW))
        self.assertEqual(budget.used("read", now_ms=NOW), 2)

    def test_counts_are_separate_per_name(self):
        budget.spend("read", 1, now_ms=NOW)
        self.assertTrue(budget.spend("capture", 1, now_ms=NOW))
        self.assertFalse(budget.spend("read", 1, now_ms=NOW))

    def test_a_new_day_gets_a_fresh_count(self):
        self.assertTrue(budget.spend("read", 1, now_ms=NOW))
        self.assertFalse(budget.spend("read", 1, now_ms=NOW))
        self.assertTrue(budget.spend("read", 1, now_ms=NOW + DAY))
        self.assertEqual(budget.used("read", now_ms=NOW + DAY), 1)

    def test_missing_or_broken_file_counts_as_zero(self):
        self.assertEqual(budget.used("read", now_ms=NOW), 0)
        self.path.write_text("{not json", "utf-8")
        self.assertEqual(budget.used("read", now_ms=NOW), 0)
        self.assertTrue(budget.spend("read", 1, now_ms=NOW))

    def test_no_storage_means_no_limit(self):
        with mock.patch.object(budget, "state_path", return_value=None):
            for _ in range(5):
                self.assertTrue(budget.spend("read", 1, now_ms=NOW))


if __name__ == "__main__":
    unittest.main()
