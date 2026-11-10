"""The life layer: agenda built from real material, stepped by the clock."""

import unittest
from datetime import datetime
from unittest import mock

from digital_life import life

NOW = datetime(2026, 11, 6, 10, 0).timestamp() * 1000
LATER = NOW + 30 * 60_000


class StateTests(unittest.TestCase):
    def test_normalize_keeps_only_well_formed_items(self):
        raw = {"day": "2026-11-06", "agenda": [{"id": "a", "kind": "read", "title": "读", "status": "pending"},
                                               {"id": "b", "kind": "nap", "title": "睡", "status": "pending"}],
               "current": "a", "traces": [{"text": "  "}, {"text": "真的"}],
               "pulse": {"day": "2026-11-06", "total": 3.0}, "last_tick_at": 5}
        state = life.normalize_life(raw)
        self.assertEqual([item["id"] for item in state["agenda"]], ["a"])
        self.assertEqual(state["current"], "a")
        self.assertEqual(len(state["traces"]), 1)
        self.assertEqual(state["last_tick_at"], 5.0)

    def test_current_is_dropped_when_the_agenda_no_longer_has_it(self):
        state = life.normalize_life({"current": "ghost", "agenda": []})
        self.assertIsNone(state["current"])

    def test_traces_are_capped(self):
        raw = {"traces": [{"text": f"第{i}条"} for i in range(life.MAX_TRACES + 5)]}
        self.assertEqual(len(life.normalize_life(raw)["traces"]), life.MAX_TRACES)


class ClockTests(unittest.TestCase):
    def test_phase_follows_the_local_clock(self):
        at = lambda hour, minute=0: datetime(2026, 11, 6, hour, minute).timestamp() * 1000
        self.assertEqual(life.phase(at(3)), "sleep")
        self.assertEqual(life.phase(at(7)), "morning")
        self.assertEqual(life.phase(at(12)), "day")
        self.assertEqual(life.phase(at(19)), "evening")
        self.assertEqual(life.phase(at(23, 30)), "late")
        self.assertEqual(life.phase(at(3), (2, 6)), "sleep")

    def test_agenda_size_follows_mood(self):
        self.assertEqual(life.agenda_size("疲惫"), 2)
        self.assertEqual(life.agenda_size("来劲"), 5)
        self.assertEqual(life.agenda_size("没听过的心情"), life.AGENDA_SIZE["平常"])


class AgendaTests(unittest.TestCase):
    BOOKS = [{"file": "a.md", "title": "夜航西飞", "offset": 0, "chars": 9000}]
    GAPS = [{"id": "gap-1", "status": "open", "summary": "要不要换个写法"}]

    def test_agenda_uses_real_books_and_open_loops(self):
        agenda = life.build_agenda(NOW, mood_label="平常", books=self.BOOKS, gaps=self.GAPS)
        self.assertEqual(agenda[0]["kind"], "read")
        self.assertEqual(agenda[0]["source"]["file"], "a.md")
        self.assertEqual(agenda[1]["kind"], "think")
        self.assertEqual(agenda[1]["source"]["id"], "gap-1")
        self.assertNotIn("要不要换个写法", agenda[1]["title"])  # one stream: no other chat's text
        self.assertGreaterEqual(len(agenda), 3)

    def test_no_material_means_no_read_or_think(self):
        agenda = life.build_agenda(NOW, books=[], gaps=[])
        self.assertNotIn("read", [item["kind"] for item in agenda])
        self.assertNotIn("think", [item["kind"] for item in agenda])

    def test_sleep_hours_have_no_rest_slot(self):
        midnight = datetime(2026, 11, 6, 2, 0).timestamp() * 1000
        agenda = life.build_agenda(midnight, books=self.BOOKS, gaps=[])
        self.assertNotIn("rest", [item["kind"] for item in agenda])


class ShelfTests(unittest.TestCase):
    def test_an_unfinished_book_wins_over_a_finished_one(self):
        books = [{"file": "a.md", "title": "读完了", "offset": 3000, "chars": 3000},
                 {"file": "b.md", "title": "读到一半", "offset": 1000, "chars": 3000}]
        self.assertEqual(life._next_book(books)["file"], "b.md")

    def test_without_an_unfinished_book_the_first_one_is_used(self):
        books = [{"file": "a.md", "title": "读完了", "offset": 3000, "chars": 3000}]
        self.assertEqual(life._next_book(books)["file"], "a.md")
        self.assertIsNone(life._next_book([]))

    def test_reading_resumes_at_the_stored_bookmark(self):
        state = {"life": life.new_life_state(), "gaps": [], "chemistry": {}}
        item = {"id": "act-1", "kind": "read", "title": "读《夜航西飞》", "minutes": 25,
                "status": "running", "started_at": NOW, "done_at": 0.0,
                "source": {"type": "library", "file": "a.md"}}
        state["life"].update({"day": life.local_day(NOW), "agenda": [item], "current": "act-1"})
        result = {"text": "下一页", "progress": "2/10", "part": 1, "finished": False}
        with mock.patch.object(life, "_books", return_value=AgendaTests.BOOKS), \
                mock.patch.object(life.library, "read", return_value=result) as read:
            out = life.step(state, LATER)
        read.assert_called_once()
        self.assertNotIn("part", read.call_args.kwargs)
        self.assertEqual(read.call_args.kwargs.get("max_chars"), life.library.MAX_CHARS)
        self.assertIn("2/10", out["traces"][0]["text"])


class StepTests(unittest.TestCase):
    def _state(self):
        return {"life": life.new_life_state(), "gaps": [], "chemistry": {}}

    def test_a_new_day_builds_an_agenda_and_starts_one_item(self):
        state = self._state()
        with mock.patch.object(life, "_books", return_value=[]):
            out = life.step(state, NOW)
        self.assertEqual(len(out["started"]), 1)
        self.assertEqual(out["started"][0]["kind"], "note")
        self.assertEqual(out["started"][0]["status"], "running")
        self.assertEqual(state["life"]["current"], out["started"][0]["id"])

    def test_a_due_item_without_material_is_dropped_and_the_next_starts(self):
        state = self._state()
        with mock.patch.object(life, "_books", return_value=[]):
            life.step(state, NOW)
            out = life.step(state, NOW + 11 * 60_000)
        self.assertEqual(out["finished"], [])
        self.assertEqual(out["dropped"][0]["kind"], "note")  # no earlier trace to fold
        self.assertEqual(state["life"]["agenda"][0]["status"], "dropped")
        self.assertTrue(out["pulses"].get("cortisol", 0) > 0)
        self.assertEqual(len(out["started"]), 1)

    def test_reading_and_drawing_share_one_five_hour_window(self):
        state = self._state()
        day = life.local_day(NOW)
        state["life"].update({"day": day, "agenda": [], "current": None,
                              "long_at": NOW - 4 * 60 * 60_000})
        with mock.patch.object(life, "_books", return_value=AgendaTests.BOOKS):
            out = life.step(state, NOW)
        self.assertEqual(out["started"][0]["kind"], "note")  # read is still inside the window
        state["life"].update({"agenda": [], "current": None,
                              "long_at": NOW - 5 * 60 * 60_000 - 1,
                              "short_at": NOW})
        with mock.patch.object(life, "_books", return_value=AgendaTests.BOOKS):
            out = life.step(state, NOW)
        self.assertEqual(out["started"][0]["kind"], "read")

    def test_a_note_does_not_start_before_its_own_gap(self):
        state = self._state()
        state["life"].update({"day": life.local_day(NOW), "agenda": [], "current": None,
                              "long_at": NOW, "short_at": NOW - 30 * 60_000})
        with mock.patch.object(life, "_books", return_value=[]):
            out = life.step(state, NOW)
        self.assertEqual(out["started"], [])

    def test_the_day_does_not_stop_when_the_plan_is_used_up(self):
        state = self._state()
        state["life"].update({"day": life.local_day(NOW), "agenda": [], "current": None})
        with mock.patch.object(life, "_books", return_value=[]):
            out = life.step(state, NOW)
        self.assertEqual(len(out["started"]), 1)
        self.assertEqual(out["started"][0]["kind"], "note")
        self.assertEqual(len(state["life"]["agenda"]), 1)

    def test_the_next_activity_picks_what_was_done_longest_ago(self):
        state = self._state()
        state["gaps"] = [{"id": "gap-1", "status": "open", "summary": "x", "chat": "c"}]
        state["life"].update({"day": life.local_day(NOW), "traces": [
            {"kind": "read"}, {"kind": "think"}], "agenda": [], "current": None})
        with mock.patch.object(life, "_books", return_value=AgendaTests.BOOKS):
            nxt = life._next_activity(state, NOW)
        self.assertEqual(nxt["kind"], "note")
        state["life"]["traces"] = [{"kind": "read"}, {"kind": "note"}]
        with mock.patch.object(life, "_books", return_value=AgendaTests.BOOKS):
            nxt = life._next_activity(state, NOW)
        self.assertEqual(nxt["kind"], "think")

    def test_rest_finishes_without_a_trace_or_a_penalty(self):
        state = self._state()
        with mock.patch.object(life, "_books", return_value=[]):
            life.step(state, NOW)
        life.step(state, NOW + 25 * 60_000)  # note done/dropped, next starts
        agenda = state["life"]["agenda"]
        resting = next(item for item in agenda if item["kind"] == "rest")
        resting["status"] = "running"
        resting["started_at"] = NOW
        state["life"]["current"] = resting["id"]
        out = life.step(state, NOW + 30 * 60_000)
        self.assertEqual(out["finished"][0]["kind"], "rest")
        self.assertEqual(resting["status"], "done")
        self.assertLess(out["pulses"].get("cortisol", 0), 0)  # rest lowers it

    def test_a_think_trace_is_generic_and_keeps_its_chat(self):
        state = self._state()
        secret = "她说想换个工作"
        state["gaps"] = [{"id": "gap-1", "status": "open", "summary": secret, "chat": "chat-9"}]
        state["life"].update({
            "day": life.local_day(NOW),
            "agenda": [{"id": "act-1", "kind": "think", "title": f"想想「{secret[:6]}」",
                        "minutes": 10, "status": "running", "started_at": NOW, "done_at": 0.0,
                        "source": {"type": "gap", "id": "gap-1"}}],
            "current": "act-1"})
        out = life.step(state, NOW + 16 * 60_000)
        trace = out["traces"][0]
        self.assertEqual(trace["kind"], "think")
        self.assertEqual(trace["chat"], "chat-9")
        self.assertNotIn(secret, trace["text"])
        self.assertNotIn(secret, state["life"]["agenda"][0]["title"])  # legacy title scrubbed
        self.assertEqual(state["life"]["agenda"][0]["title"], life._THINK_TITLE)

    def test_a_read_without_a_library_entry_is_dropped(self):
        state = self._state()
        item = {"id": "act-1", "kind": "read", "title": "读《没这本书》", "minutes": 25,
                "status": "running", "started_at": NOW, "done_at": 0.0,
                "source": {"type": "library", "file": "ghost.md", "part": 0}}
        state["life"].update({"day": life.local_day(NOW), "agenda": [item], "current": "act-1"})
        with mock.patch.object(life, "_books", return_value=[]):
            out = life.step(state, LATER)
        self.assertEqual(out["dropped"][0]["id"], "act-1")
        self.assertGreater(out["pulses"]["cortisol"], 0)
        self.assertEqual(out["traces"], [])

    def test_a_finished_read_keeps_its_chat_and_stays_unshared(self):
        state = self._state()
        item = {"id": "act-1", "kind": "read", "title": "读《夜航西飞》", "minutes": 25,
                "status": "running", "started_at": NOW, "done_at": 0.0,
                "source": {"type": "library", "file": "a.md", "part": 0}}
        state.update({"gaps": [], "chemistry": {}})
        state["life"].update({"day": life.local_day(NOW), "agenda": [item], "current": "act-1"})
        book = dict(AgendaTests.BOOKS[0], ingested_by="chat-9")
        with mock.patch.object(life, "_books", return_value=[book]), \
                mock.patch.object(life.library, "read",
                                  return_value={"text": "夜里起飞的飞机", "progress": "10%",
                                                "part": 1, "finished": True}):
            out = life.step(state, LATER)
        trace = out["traces"][0]
        self.assertIn("夜航西飞", trace["text"])
        self.assertIn("夜里起飞的飞机", trace["text"])
        self.assertEqual(trace["chat"], "chat-9")
        self.assertFalse(trace["share"])


class NoteTests(unittest.TestCase):
    def test_a_note_does_not_replay_a_book_title(self):
        self.assertEqual(life._note_title("读《夜航西飞》"), "读了一会儿书")
        self.assertEqual(life._note_title("练了一会儿口语"), "练了一会儿口语")


class PulseTests(unittest.TestCase):
    def test_pulses_scale_into_the_daily_budget(self):
        state = {"life": life.new_life_state(), "chemistry": {"dopamine": 50.0, "cortisol": 20.0,
                                                              "serotonin": 40.0}}
        applied = life.apply_pulses(state, {"dopamine": 100.0}, NOW)
        self.assertLessEqual(applied["dopamine"], life.LIFE_PULSE_CAP)
        self.assertEqual(state["life"]["pulse"]["total"], applied["dopamine"])

    def test_the_budget_resets_on_a_new_day(self):
        state = {"life": life.new_life_state(), "chemistry": {"dopamine": 50.0}}
        state["life"]["pulse"] = {"day": "2026-11-05", "total": life.LIFE_PULSE_CAP}
        applied = life.apply_pulses(state, {"dopamine": 4.0}, NOW)
        self.assertEqual(applied["dopamine"], 4.0)


class RememberTests(unittest.TestCase):
    def setUp(self):
        self.written = []
        life.set_writer(lambda text, chat_id="": self.written.append((text, chat_id)))

    def tearDown(self):
        life.set_writer(None)

    def test_a_trace_goes_to_the_installed_writer(self):
        life.remember({"text": "读了十页"})
        self.assertEqual(self.written, [("读了十页", "")])

    def test_a_chat_scoped_trace_keeps_its_chat(self):
        life.remember({"text": "又琢磨了一会儿", "chat": "chat-1"})
        self.assertEqual(self.written, [("又琢磨了一会儿", "chat-1")])

    def test_blank_traces_stay_out_of_memory(self):
        life.remember({"text": "   "})
        self.assertEqual(self.written, [])

    def test_a_failing_writer_never_breaks_the_tick(self):
        life.set_writer(lambda _text, _chat="": (_ for _ in ()).throw(RuntimeError("disk full")))
        life.remember({"text": "还是写了"})  # must not raise


if __name__ == "__main__":
    unittest.main()
