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

    def test_the_day_rolls_over_at_five_not_midnight(self):
        from datetime import datetime

        late = datetime(2026, 11, 7, 3, 0).timestamp() * 1000
        self.assertTrue(budget.spend("read", 1, now_ms=late))
        # 04:00 is the same evening: still capped.
        self.assertFalse(budget.spend("read", 1, now_ms=late + 3_600_000))
        # 06:00 belongs to the next day: a fresh count.
        self.assertTrue(budget.spend("read", 1, now_ms=late + 3 * 3_600_000))

    def test_missing_or_broken_file_counts_as_zero(self):
        self.assertEqual(budget.used("read", now_ms=NOW), 0)
        self.path.write_text("{not json", "utf-8")
        self.assertEqual(budget.used("read", now_ms=NOW), 0)
        self.assertTrue(budget.spend("read", 1, now_ms=NOW))

    def test_no_storage_means_no_limit(self):
        with mock.patch.object(budget, "state_path", return_value=None):
            for _ in range(5):
                self.assertTrue(budget.spend("read", 1, now_ms=NOW))

    def test_spend_all_advances_every_counter_together(self):
        self.assertIsNone(budget.spend_all({"image:chat": 2, "image:__profile__": 3},
                                           now_ms=NOW))
        self.assertEqual(budget.used("image:chat", now_ms=NOW), 1)
        self.assertEqual(budget.used("image:__profile__", now_ms=NOW), 1)

    def test_spend_all_stops_at_the_first_capped_bucket_without_charging_any(self):
        budget.spend("image:__profile__", 1, now_ms=NOW)
        hit = budget.spend_all({"image:chat": 5, "image:__profile__": 1}, now_ms=NOW)
        self.assertEqual(hit, "image:__profile__")
        # Neither counter moved: a refused call must not eat the chat's quota.
        self.assertEqual(budget.used("image:chat", now_ms=NOW), 0)
        self.assertEqual(budget.used("image:__profile__", now_ms=NOW), 1)

    def test_spend_all_fails_closed_without_storage(self):
        with mock.patch.object(budget, "state_path", return_value=None):
            self.assertEqual(budget.spend_all({"image:chat": 1}, now_ms=NOW), "storage")

    def test_spend_all_fails_closed_when_the_count_cannot_be_saved(self):
        with mock.patch.object(Path, "write_text", side_effect=OSError("disk full")):
            self.assertEqual(budget.spend_all({"image:chat": 1}, now_ms=NOW), "storage")


if __name__ == "__main__":
    unittest.main()
