"""Behavioral tests for the gap pool: lifecycle, tension, epiphany, and echo locks."""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

# storage reaches into the host's plugin sqlite helper; tests only need the module object.
if "plugins.plugin_storage" not in sys.modules:
    host = types.ModuleType("plugins")
    host.plugin_storage = types.ModuleType("plugins.plugin_storage")
    host.plugin_storage.plugin_db = lambda *args, **kwargs: None
    sys.modules.setdefault("plugins", host)
    sys.modules["plugins.plugin_storage"] = host.plugin_storage

from digital_life import domain, gaps, runtime, storage

NOW = 1_793_592_000_000
HOUR = 3_600_000
MINUTE = 60_000


def fresh_state() -> dict:
    return domain.default_state(NOW)


def propose(state, summary, now_ms=NOW, **kwargs):
    return gaps.propose_gap(state, summary, "synthetic", now_ms, **kwargs)


class ProposeTests(unittest.TestCase):
    def test_new_gap_starts_at_base_tension(self):
        state = fresh_state()
        gap = propose(state, "what is the DSH boot protocol", query="DSH boot protocol")
        self.assertEqual(gap["status"], "open")
        self.assertEqual(gap["tension"], 80.0)
        self.assertEqual(gap["query"], "DSH boot protocol")
        self.assertEqual(len(state["gaps"]), 1)

    def test_similar_summary_reuses_the_same_row(self):
        state = fresh_state()
        first = propose(state, "what is the DSH boot protocol")
        second = propose(state, "DSH boot protocol?")
        self.assertIs(first, second)
        self.assertEqual(len(state["gaps"]), 1)

    def test_same_summary_in_another_chat_opens_its_own_gap(self):
        state = fresh_state()
        first = propose(state, "what is the DSH boot protocol", chat="g1")
        second = propose(state, "what is the DSH boot protocol", chat="g2")
        self.assertIsNot(first, second)
        self.assertEqual((first["chat"], second["chat"]), ("g1", "g2"))
        self.assertEqual(len(state["gaps"]), 2)
        again = propose(state, "DSH boot protocol?", chat="g1")
        self.assertIs(again, first)

    def test_resolved_synonym_revives_the_original_id(self):
        state = fresh_state()
        gap = propose(state, "what is the DSH boot protocol")
        gaps.close_gap(state, by="observed_search_success", now_ms=NOW + MINUTE, gap_id=gap["id"])
        revived = propose(state, "the DSH boot protocol", now_ms=NOW + 2 * MINUTE)
        self.assertEqual(revived["id"], gap["id"])
        self.assertEqual(revived["status"], "open")
        self.assertEqual(revived["revived_from"], "observed_search_success")
        self.assertEqual(revived["revive_count"], 1)
        self.assertIsNone(revived["closed_at"])
        self.assertEqual(len(state["gaps"]), 1)

    def test_open_gaps_are_never_dropped_by_the_soft_cap(self):
        state = fresh_state()
        for index in range(gaps.GAP_OPEN_WARN + 5):
            gap = {"id": f"gap-{index}", "summary": f"q-{index}-{index * 7919:08x}",
                   "status": "open", "created_at": NOW, "tension": 82.0}
            state["gaps"].append(gap)
        with self.assertLogs("digital_life.gaps", level="WARNING"):
            propose(state, "one more open loop")
        self.assertEqual(len(state["gaps"]), gaps.GAP_OPEN_WARN + 6)

    def test_resolved_gaps_expire_but_recent_ones_stay(self):
        state = fresh_state()
        old = {"id": "gap-old", "summary": "old", "status": "resolved", "closed_at": NOW - 31 * 24 * HOUR}
        recent = {"id": "gap-recent", "summary": "recent", "status": "resolved", "closed_at": NOW - HOUR}
        state["gaps"] = [old, recent]
        gaps.prune_gaps(state, NOW)
        self.assertEqual([gap["id"] for gap in state["gaps"]], ["gap-recent"])


class TensionTests(unittest.TestCase):
    def test_tension_grows_with_age(self):
        state = fresh_state()
        gap = propose(state, "who wrote the DSH book")
        self.assertAlmostEqual(gaps.gap_tension(gap, NOW + 2 * HOUR), 90.0)

    def test_tension_is_capped_and_paused_aware(self):
        state = fresh_state()
        gap = propose(state, "who wrote the DSH book")
        self.assertEqual(gaps.gap_tension(gap, NOW + 10 * HOUR), 100.0)
        gap["paused_ms"] = 2 * HOUR
        self.assertAlmostEqual(gaps.gap_tension(gap, NOW + 2 * HOUR), 80.0)

    def test_resolved_gap_has_no_tension(self):
        state = fresh_state()
        gap = propose(state, "who wrote the DSH book")
        gaps.close_gap(state, by="operator", now_ms=NOW + MINUTE, gap_id=gap["id"])
        self.assertEqual(gaps.gap_tension(gap, NOW + 10 * HOUR), 0.0)

    def test_relief_lowers_tension(self):
        state = fresh_state()
        gap = propose(state, "who wrote the DSH book")
        gap["relief"] = 15.0
        self.assertAlmostEqual(gaps.gap_tension(gap, NOW), 65.0)


class CloseTests(unittest.TestCase):
    def test_close_by_match_text_closes_the_matching_gap(self):
        state = fresh_state()
        propose(state, "DSH boot protocol")
        unrelated = propose(state, "sourdough starter ratio")
        closed = gaps.close_gap(state, by="epiphany:web_search", now_ms=NOW + MINUTE,
                                match_text="DSH boot protocol")
        self.assertEqual(len(closed), 1)
        self.assertEqual(unrelated["status"], "open")
        for gap in closed:
            self.assertEqual(gap["closed_by"], "epiphany:web_search")
            self.assertEqual(gap["closed_at"], NOW + MINUTE)
            self.assertEqual(gap["tension"], 0.0)

    def test_close_by_id_only_touches_that_gap(self):
        state = fresh_state()
        first = propose(state, "DSH boot protocol")
        second = propose(state, "sourdough starter ratio")
        closed = gaps.close_gap(state, by="operator", now_ms=NOW + MINUTE, gap_id=first["id"])
        self.assertEqual([gap["id"] for gap in closed], [first["id"]])
        self.assertEqual(second["status"], "open")

    def test_close_without_match_is_a_noop(self):
        state = fresh_state()
        propose(state, "DSH boot protocol")
        self.assertEqual(gaps.close_gap(state, by="operator", now_ms=NOW), [])
        self.assertEqual(len(gaps.open_gaps(state["gaps"])), 1)

    def test_match_prefers_older_gap_on_tie(self):
        state = fresh_state()
        older = propose(state, "DSH boot protocol", now_ms=NOW)
        propose(state, "DSH boot protocol", now_ms=NOW + 5000)
        found = gaps.match_gap("DSH boot protocol", state["gaps"])
        self.assertEqual(found["id"], older["id"])


class EpiphanyTests(unittest.TestCase):
    def test_answered_gaps_uses_containment(self):
        state = fresh_state()
        gap = propose(state, "hermes plugin storage API")
        other = propose(state, "sourdough starter ratio")
        hits = gaps.answered_gaps(state["gaps"], "docs: the hermes plugin storage API keeps sqlite rows")
        self.assertEqual([hit["id"] for hit in hits], [gap["id"]])
        self.assertNotIn(other, hits)

    def test_echo_suppression_window(self):
        state = fresh_state()
        self.assertFalse(gaps.suppressed_by_echo(state, NOW))
        gaps.note_epiphany(state, NOW)
        self.assertTrue(gaps.suppressed_by_echo(state, NOW + 5 * MINUTE))
        self.assertFalse(gaps.suppressed_by_echo(state, NOW + 20 * MINUTE))

    def test_sibling_gate_window(self):
        state = fresh_state()
        gap = {"id": "gap-closed", "summary": "DSH boot protocol", "status": "resolved",
               "created_at": NOW, "closed_at": NOW + MINUTE, "closed_by": "epiphany:web_search"}
        sibling = {"id": "gap-open", "summary": "DSH boot protocol detail", "status": "open",
                   "created_at": NOW + 2 * MINUTE}
        state["gaps"] = [gap, sibling]
        self.assertEqual(gaps.sibling_of_recent_close(state["gaps"], sibling, NOW + 6 * MINUTE)["id"], "gap-closed")
        self.assertIsNone(gaps.sibling_of_recent_close(state["gaps"], sibling, NOW + 3 * HOUR))

    def test_proposing_a_sibling_revives_instead_of_twinning(self):
        state = fresh_state()
        gap = propose(state, "DSH boot protocol")
        gaps.close_gap(state, by="epiphany:web_search", now_ms=NOW, gap_id=gap["id"])
        revived = propose(state, "DSH boot protocol detail", now_ms=NOW + 5 * MINUTE)
        self.assertEqual(revived["id"], gap["id"])
        self.assertEqual(revived["status"], "open")
        self.assertEqual(len(state["gaps"]), 1)

    def test_retire_wake_drops_queued_entry(self):
        state = fresh_state()
        state["wake"] = {"queue": [{"gap_id": "gap-a"}, {"gap_id": "gap-b"}]}
        gaps.retire_wake(state, "gap-a")
        self.assertEqual(state["wake"]["queue"], [{"gap_id": "gap-b"}])


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.state = fresh_state()

        def fake_update(now_ms, mutate):
            result = mutate(self.state)
            if result is not None:
                self.state = result
            return self.state

        self.patch = patch.object(storage, "update_state", side_effect=fake_update)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_first_successful_search_closes_answered_gap_as_epiphany(self):
        gaps.propose_gap(self.state, "hermes plugin storage API", "assistant_unknown_claim", NOW)
        runtime.observe_tool_result(
            NOW + MINUTE, tool_name="web_search",
            args={"query": "bootstrap docs"},
            result='{"results": [{"title": "the hermes plugin storage API keeps sqlite rows"}]}',
        )
        closed = [gap for gap in self.state["gaps"] if gap["status"] == "resolved"]
        self.assertEqual(len(closed), 1)
        self.assertTrue(closed[0]["closed_by"].startswith("epiphany:"))
        self.assertEqual(self.state["last_epiphany_at"], NOW + MINUTE)
        self.assertIn("web_search", self.state["tools_succeeded"])

    def test_second_success_does_not_repeat_the_epiphany(self):
        self.state["tools_succeeded"] = ["web_search"]
        gaps.propose_gap(self.state, "hermes plugin storage API", "assistant_unknown_claim", NOW)
        runtime.observe_tool_result(
            NOW + MINUTE, tool_name="web_search",
            args={"query": "unrelated bootstrap"},
            result='{"results": [{"title": "the hermes plugin storage API keeps sqlite rows"}]}',
        )
        self.assertEqual(self.state["last_epiphany_at"], 0.0)

    def test_failed_search_opens_a_gap_with_query(self):
        runtime.observe_tool_result(NOW, tool_name="web_fetch", args={"url": "https://example.invalid"},
                                    result='{"error": "connection refused"}')
        self.assertEqual(len(self.state["gaps"]), 1)
        self.assertEqual(self.state["gaps"][0]["query"], "https://example.invalid")
        self.assertEqual(self.state["gaps"][0]["source"], "tool:web_fetch")

    def test_operator_close_records_provenance(self):
        gap = gaps.propose_gap(self.state, "hermes plugin storage API", "synthetic", NOW)
        self.assertTrue(runtime.resolve_gap(NOW + MINUTE, gap["id"]))
        self.assertEqual(gap["closed_by"], "operator")
        self.assertFalse(runtime.resolve_gap(NOW + 2 * MINUTE, "gap-missing"))


class StateNormalizationTests(unittest.TestCase):
    def test_old_state_round_trips_through_normalization(self):
        raw = {"schema_version": 0, "chemistry": {"dopamine": {"value": 60}}, "energy": 42,
               "gaps": [{"id": "gap-old", "summary": "x", "status": "open", "created_at": NOW}]}
        state = domain.normalize_state(raw, NOW + 1000)
        self.assertEqual(state["energy"], 42.0)
        self.assertEqual(state["tools_succeeded"], [])
        self.assertEqual(state["last_epiphany_at"], 0.0)
        self.assertEqual(state["gaps"][0]["id"], "gap-old")

    def test_new_keys_survive_normalization(self):
        state = domain.normalize_state(
            {"tools_succeeded": ["web_search", 7, ""], "last_epiphany_at": NOW}, NOW)
        self.assertEqual(state["tools_succeeded"], ["web_search", ""])
        self.assertEqual(state["last_epiphany_at"], NOW)


class SiblingWindowTests(unittest.TestCase):
    def test_close_gap_only_touches_the_given_chat(self):
        state = domain.default_state(NOW)
        base = {"summary": "DeepSeek 有 app 吗", "query": "DeepSeek 有 app 吗", "status": "open",
                "tension": 0.5, "created_at": NOW}
        state["gaps"] = [{"id": "g-a", **base, "chat": "chat-a"},
                         {"id": "g-b", **base, "chat": "chat-b"}]
        closed = gaps.close_gap(state, by="observed_search_success", now_ms=NOW + MINUTE,
                                match_text="DeepSeek 有 app 吗", chat="chat-a")
        self.assertEqual([gap["id"] for gap in closed], ["g-a"])
        self.assertEqual(state["gaps"][0]["status"], "resolved")
        self.assertEqual(state["gaps"][1]["status"], "open")

    def test_resolved_gap_without_timestamps_is_not_a_recent_sibling(self):
        resolved = {"id": "gap-old", "summary": "火星营业时间", "status": "resolved"}
        fresh = {"id": "gap-new", "summary": "火星营业时间", "status": "open", "created_at": NOW}
        self.assertIsNone(gaps.sibling_of_recent_close([resolved, fresh], fresh, NOW))

    def test_resolved_gap_within_the_window_is_a_sibling(self):
        resolved = {"id": "gap-old", "summary": "火星营业时间", "status": "resolved",
                    "closed_at": NOW - 5 * 60_000}
        fresh = {"id": "gap-new", "summary": "火星营业时间", "status": "open", "created_at": NOW}
        self.assertIs(gaps.sibling_of_recent_close([resolved, fresh], fresh, NOW), resolved)


if __name__ == "__main__":
    unittest.main()
