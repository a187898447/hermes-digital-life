"""Self-wake queue: the four locks, the bubble text, and the behaviour commitment."""

import unittest

from digital_life import domain, gaps, wake

NOW = 1_700_000_000_000.0
CHAT = "group-1"


def state_with_gap(summary="火星营业时间", *, age_ms=3_600_000.0, relief=0.0, chat_id=CHAT):
    state = domain.default_state(NOW)
    state["gaps"] = [{"id": "gap-1", "summary": summary, "status": "open", "tension": 80.0,
                      "relief": relief, "created_at": NOW - age_ms, "source": "assistant_unknown"}]
    state["wake"] = wake.new_wake_state()
    return state


def slot(state, chat_id=CHAT):
    return state["wake"]["chats"][chat_id]


class WakeLockTests(unittest.TestCase):
    def tearDown(self):
        wake.set_agent_running(False)

    def test_plan_queues_the_most_pressing_loop(self):
        state = state_with_gap()
        item = wake.plan_wake(state, NOW, chat_id=CHAT)
        self.assertEqual(item["gap_id"], "gap-1")
        self.assertEqual(len(slot(state)["queue"]), 1)
        self.assertTrue(wake.verify_queued(state, item["id"]))
        self.assertTrue(wake.mark_delivered(state, item["id"], NOW + 5))
        self.assertEqual(slot(state)["queue"], [])
        self.assertEqual(slot(state)["active"]["gap_id"], "gap-1")
        section = wake.section_text(state, NOW + 5, chat_id=CHAT)
        self.assertIn("火星营业时间", section)
        self.assertTrue(section.startswith("[digital-life:wake]"))
        self.assertLessEqual(len(section), wake.WAKE_SECTION_LIMIT)

    def test_lock_one_drops_a_same_loop_repeat_without_a_record(self):
        state = state_with_gap()
        item = wake.plan_wake(state, NOW, chat_id=CHAT)
        wake.mark_delivered(state, item["id"], NOW)
        self.assertIsNone(wake.plan_wake(state, NOW + 500, chat_id=CHAT))
        self.assertEqual(slot(state)["queue"], [])
        self.assertEqual(slot(state)["last_wake_at"], NOW)

    def test_lock_two_rechecks_the_disk_by_id(self):
        state = state_with_gap()
        item = wake.plan_wake(state, NOW, chat_id=CHAT)
        state["gaps"][0]["status"] = "resolved"
        self.assertFalse(wake.verify_queued(state, item["id"]))
        self.assertTrue(wake.cancel(state, item["id"]))
        self.assertEqual(slot(state)["queue"], [])

    def test_lock_three_deletes_the_queue_when_the_gap_closes(self):
        state = state_with_gap()
        wake.plan_wake(state, NOW, chat_id=CHAT)
        gaps.close_gap(state, by="operator", now_ms=NOW, gap_id="gap-1")
        self.assertEqual(slot(state)["queue"], [])

    def test_lock_three_also_clears_an_active_wake(self):
        state = state_with_gap()
        item = wake.plan_wake(state, NOW, chat_id=CHAT)
        wake.mark_delivered(state, item["id"], NOW)
        gaps.close_gap(state, by="operator", now_ms=NOW, gap_id="gap-1")
        self.assertIsNone(slot(state)["active"])
        self.assertEqual(wake.section_text(state, NOW, chat_id=CHAT), "")

    def test_lock_four_drops_while_a_turn_is_running(self):
        state = state_with_gap()
        wake.set_agent_running(True)
        self.assertIsNone(wake.plan_wake(state, NOW, chat_id=CHAT))
        self.assertNotIn(CHAT, state["wake"]["chats"])

    def test_echo_window_after_an_epiphany_blocks_wakes(self):
        state = state_with_gap()
        state["last_epiphany_at"] = NOW - 60_000
        self.assertIsNone(wake.plan_wake(state, NOW, chat_id=CHAT))

    def test_sibling_of_a_recent_close_is_not_woken(self):
        state = state_with_gap("火星营业时间")
        state["gaps"].append({"id": "gap-0", "summary": "火星营业时间几点", "status": "resolved",
                              "created_at": NOW - 600_000, "resolved_at": NOW - 60_000,
                              "closed_at": NOW - 60_000, "tension": 0.0, "relief": 0.0})
        self.assertIsNone(wake.plan_wake(state, NOW, chat_id=CHAT))

    def test_a_wake_without_a_chat_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            wake.plan_wake(state_with_gap(), NOW, chat_id="")
        with self.assertRaises(ValueError):
            wake.reconcile(state_with_gap(), NOW, chat_id="")


class WakeCommitmentTests(unittest.TestCase):
    def test_resolved_loop_clears_the_commitment(self):
        state = state_with_gap()
        item = wake.plan_wake(state, NOW, chat_id=CHAT)
        wake.mark_delivered(state, item["id"], NOW)
        state["gaps"][0]["status"] = "resolved"
        self.assertEqual(wake.reconcile(state, NOW + 60_000, chat_id=CHAT), "done")
        self.assertIsNone(slot(state)["active"])

    def test_open_loop_presses_harder_by_ten(self):
        state = state_with_gap()
        item = wake.plan_wake(state, NOW, chat_id=CHAT)
        wake.mark_delivered(state, item["id"], NOW)
        before = state["gaps"][0]["relief"]
        self.assertEqual(wake.reconcile(state, NOW + 60_000, chat_id=CHAT), "unmet")
        self.assertAlmostEqual(state["gaps"][0]["relief"], before - wake.WAKE_PENALTY)
        self.assertIsNone(slot(state)["active"])

    def test_no_active_wake_means_no_reconcile_work(self):
        state = state_with_gap()
        self.assertEqual(wake.reconcile(state, NOW, chat_id=CHAT), "")

    def test_manual_trigger_picks_the_highest_tension_gap(self):
        state = state_with_gap()
        state["gaps"].append({"id": "gap-2", "summary": "另一件事", "status": "open", "tension": 80.0,
                              "relief": 0.0, "created_at": NOW, "source": "tool:web_fetch"})
        active = wake.force_active(state, NOW, chat_id=CHAT)
        self.assertEqual(active["gap_id"], "gap-1")
        self.assertTrue(active["manual"])
        self.assertIn("[digital-life:wake]", wake.section_text(state, NOW, chat_id=CHAT))

    def test_normalize_tolerates_garbage(self):
        self.assertEqual(wake.normalize_wake("nope"), wake.new_wake_state())
        self.assertEqual(wake.normalize_wake({"schema": 2, "chats": {"a": "nope"}})["chats"]["a"],
                         wake.new_chat_wake())
        cleaned = wake.normalize_wake({"schema": 2, "chats": {"g": {
            "queue": [{"id": "w", "gap_id": "g"}, 7, {"id": ""}],
            "last_wake_at": "x", "active": {"gap_id": "g"}}}})
        self.assertEqual(len(cleaned["chats"]["g"]["queue"]), 1)
        self.assertEqual(cleaned["chats"]["g"]["last_wake_at"], 0.0)
        self.assertEqual(cleaned["chats"]["g"]["active"]["gap_id"], "g")

    def test_legacy_single_slot_state_is_dropped_not_guessed(self):
        legacy = {"queue": [{"id": "w", "gap_id": "g"}], "last_wake_at": NOW,
                  "last_gap_id": "g", "active": {"gap_id": "g"}}
        cleaned = wake.normalize_wake(legacy)
        self.assertEqual(cleaned, {"schema": 2, "chats": {}})


class WakeChatScopeTests(unittest.TestCase):
    def tearDown(self):
        wake.set_agent_running(False)

    def test_two_chats_keep_their_own_cooldown_and_delivery(self):
        state = state_with_gap()
        first = wake.plan_wake(state, NOW, chat_id=CHAT)
        other = wake.plan_wake(state, NOW, chat_id="group-2")
        self.assertIsNotNone(first)
        self.assertIsNotNone(other)
        self.assertNotEqual(first["id"], other["id"])
        wake.mark_delivered(state, first["id"], NOW + 10)
        self.assertEqual(slot(state, CHAT)["last_wake_at"], NOW + 10)
        self.assertEqual(slot(state, "group-2")["last_wake_at"], 0.0)
        # group-2 still has its own bubble queued; delivering it only touches its slot.
        wake.mark_delivered(state, other["id"], NOW + 20)
        self.assertEqual(slot(state, "group-2")["last_wake_at"], NOW + 20)
        self.assertEqual(slot(state, CHAT)["last_wake_at"], NOW + 10)

    def test_reconcile_only_presses_the_chat_that_was_woken(self):
        state = state_with_gap()
        bumped = state["gaps"][0]
        for chat_id in (CHAT, "group-2"):
            item = wake.plan_wake(state, NOW, chat_id=chat_id)
            wake.mark_delivered(state, item["id"], NOW)
            self.assertEqual(wake.reconcile(state, NOW + 60_000, chat_id=chat_id), "unmet")
        self.assertAlmostEqual(bumped["relief"], -2 * wake.WAKE_PENALTY)

    def test_summary_counts_chats(self):
        state = state_with_gap()
        wake.plan_wake(state, NOW, chat_id=CHAT)
        wake.plan_wake(state, NOW, chat_id="group-2")
        summary = wake.summary(state, NOW)
        self.assertEqual(summary["chats"], 2)
        self.assertEqual(summary["queued"], 2)
        self.assertEqual(summary["active_gap"], None)


class WakeCooldownTests(unittest.TestCase):
    def tearDown(self):
        wake.set_agent_running(False)

    def test_cooldown_blocks_a_second_wake_inside_30_minutes(self):
        state = state_with_gap()
        item = wake.plan_wake(state, NOW, chat_id=CHAT)
        self.assertTrue(wake.mark_delivered(state, item["id"], NOW + 5))
        slot(state)["active"] = None
        self.assertIsNone(wake.plan_wake(state, NOW + 60_000, chat_id=CHAT))
        later = wake.plan_wake(state, NOW + 31 * 60_000, chat_id=CHAT)
        self.assertIsNotNone(later)

    def test_running_lock_expires_when_the_post_hook_is_lost(self):
        wake.set_agent_running(True)
        self.assertTrue(wake.agent_running())
        wake._RUNNING_AT = NOW - wake.WAKE_RUNNING_TTL_MS - 1
        self.assertFalse(wake.agent_running())
        self.assertIsNotNone(wake.plan_wake(state_with_gap(), NOW, chat_id=CHAT))


if __name__ == "__main__":
    unittest.main()
