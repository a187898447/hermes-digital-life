import base64
import importlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import os
import sys
import types

_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HERMES_LOVER_POLISH", "0")
os.environ.setdefault("HERMES_LOVER_TICK", "0")
sys.path.insert(0, str(_ROOT))

_namespace = types.ModuleType("hermes_plugins")
_namespace.__path__ = [str(_ROOT)]
sys.modules.setdefault("hermes_plugins", _namespace)
try:
    from test_events import plugin  # the shared harness already loads the plugin
except ImportError:  # pragma: no cover - running this file on its own
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "hermes_plugins.hermes_lover", _ROOT / "__init__.py",
        submodule_search_locations=[str(_ROOT)])
    plugin = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = plugin
    _spec.loader.exec_module(plugin)

from digital_life import minimax, outbox


class ImageTests(unittest.TestCase):
    def test_base64_image_lands_in_the_outbox(self):
        raw = b"\xff\xd8fakejpeg"
        encoded = base64.b64encode(raw).decode()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(outbox, "root_dir", return_value=Path(tmp)), \
                mock.patch.object(minimax, "keys", lambda: ["k1"]), \
                mock.patch.object(minimax, "_request", lambda *a, **k: {"data": {"image_base64": [encoded]}}):
            path = minimax.create_image("一只鲸鱼在云上", aspect_ratio="16:9")
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(path.suffix, ".jpeg")

    def test_bad_ratio_falls_back_and_empty_prompt_fails(self):
        seen = {}

        def fake(path, *, key="", payload=None, timeout=60.0):
            seen.update(payload)
            return {"data": {"image_base64": [base64.b64encode(b"x").decode()]}}

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(outbox, "root_dir", return_value=Path(tmp)), \
                mock.patch.object(minimax, "keys", lambda: ["k1"]), \
                mock.patch.object(minimax, "_request", fake):
            minimax.create_image("x", aspect_ratio="7:3")
        self.assertEqual(seen["aspect_ratio"], "1:1")
        with self.assertRaises(minimax.MiniMaxError):
            minimax.create_image("   ")

    def test_no_image_in_the_response_is_an_error(self):
        with mock.patch.object(minimax, "keys", lambda: ["k1"]), \
                mock.patch.object(minimax, "_request", lambda *a, **k: {"data": {}}):
            with self.assertRaises(minimax.MiniMaxError):
                minimax.create_image("x")


class TranscriptionTests(unittest.TestCase):
    def test_audio_is_posted_as_multipart_and_text_comes_back(self):
        captured = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=0):
            captured["url"] = request.full_url
            captured["body"] = request.data
            captured["headers"] = dict(request.header_items())
            return Response(b'{"text": "hello there", "duration": 12.5}')

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "voice.mp3"
            audio.write_bytes(b"ID3fake")
            with mock.patch.object(minimax, "keys", lambda: ["k1"]), \
                    mock.patch.object(minimax.urllib.request, "urlopen", fake_urlopen):
                result = minimax.transcribe(audio, language="en")
        self.assertEqual(result["text"], "hello there")
        self.assertEqual(result["duration"], 12.5)
        self.assertIn("/v1/speech_to_text", captured["url"])
        body = captured["body"]
        self.assertIn(b'name="model"', body)
        self.assertIn(b"asr-1.0", body)
        self.assertIn(b'filename="voice.mp3"', body)

    def test_missing_or_unsupported_files_are_rejected_before_upload(self):
        with self.assertRaises(minimax.MiniMaxError):
            minimax.transcribe(Path("/nonexistent.mp3"))
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "notes.txt"
            bad.write_text("hi")
            with self.assertRaises(minimax.MiniMaxError):
                minimax.transcribe(bad)


class ModelGateTests(unittest.TestCase):
    def test_only_minimax_models_count(self):
        self.assertTrue(plugin._is_minimax_model("MiniMax-M3.1-Flash"))
        self.assertTrue(plugin._is_minimax_model("minimax-m3"))
        self.assertFalse(plugin._is_minimax_model("gpt-4o"))
        self.assertFalse(plugin._is_minimax_model(""))

    def test_media_tools_are_blocked_for_other_models(self):
        with mock.patch.object(plugin, "_MODEL_SEEN", {"name": "gpt-4o"}):
            for tool in plugin.MINIMAX_TOOLS:
                veto = plugin._gate_tool_call(tool, "session-1", platform="qqbot", sender="u")
                self.assertEqual(veto["action"], "block")
        with mock.patch.object(plugin, "_MODEL_SEEN", {"name": "MiniMax-M3.1-Flash"}):
            for tool in plugin.MINIMAX_TOOLS:
                self.assertIsNone(plugin._gate_tool_call(tool, "session-1", platform="qqbot", sender="u"))

    def test_the_middleware_remembers_the_model(self):
        with mock.patch.object(plugin, "_MODEL_SEEN", {"name": ""}):
            plugin._affect_request({"model": "MiniMax-M3.1-Flash", "messages": []}, session_id="")
            self.assertEqual(plugin._MODEL_SEEN["name"], "MiniMax-M3.1-Flash")


class QuietTurnTests(unittest.TestCase):
    def test_a_finished_picture_waits_for_the_turn_to_end(self):
        wake = importlib.import_module("hermes_plugins.digital_life.digital_life.wake")
        calls = []

        def busy():
            calls.append("check")
            return len(calls) < 3

        with mock.patch.object(wake, "agent_running", busy), \
                mock.patch.object(plugin.time, "sleep", lambda seconds: None):
            plugin._wait_for_quiet_turn(grace=0)
        self.assertGreaterEqual(len(calls), 3)


class HandlerTests(unittest.TestCase):
    def setUp(self):
        import importlib
        self.hl = sys.modules["hermes_plugins.digital_life"]
        self.budget = importlib.import_module("hermes_plugins.digital_life.digital_life.budget")
        self.minimax = importlib.import_module("hermes_plugins.digital_life.digital_life.minimax")
        self.sent = []
        self.hl._TURN_META["s-media"] = {"platform": "qqbot"}
        self.addCleanup(self.hl._TURN_META.pop, "s-media", None)
        for item in (mock.patch.object(self.hl, "_session_scope", lambda session_id: ("chat-1", "dm")),
                     mock.patch.object(self.hl, "_host_send_message_tool", lambda: self.sent.append)):
            item.start()
            self.addCleanup(item.stop)

    def _inline(self, work):
        work()
        return True

    def test_image_is_drawn_and_sent_with_a_media_line(self):
        from pathlib import Path as P
        with mock.patch.object(self.minimax, "create_image", return_value=P("/tmp/x.jpeg")), \
                mock.patch.object(self.budget, "spend", return_value=True), \
                mock.patch.object(self.hl, "_run_media_later", self._inline):
            out = json.loads(self.hl._make_image({"prompt": "一只橘猫", "caption": "画好了"},
                                                 session_id="s-media"))
        self.assertTrue(out["ok"])
        self.assertTrue(out["queued"])
        self.assertEqual(self.sent[0]["target"], "qqbot:chat-1")
        self.assertIn("MEDIA:/tmp/x.jpeg", self.sent[0]["message"])
        self.assertIn("画好了", self.sent[0]["message"])

    def test_the_image_call_answers_before_the_work_runs(self):
        started = []
        with mock.patch.object(self.hl, "_run_media_later", lambda work: started.append(work) or True), \
                mock.patch.object(self.budget, "spend", return_value=True):
            out = json.loads(self.hl._make_image({"prompt": "一只橘猫"}, session_id="s-media"))
        self.assertTrue(out["queued"])
        self.assertEqual(len(started), 1)

    def test_a_picture_of_herself_carries_the_mood(self):
        from pathlib import Path as P
        seen = {}

        def fake_create(prompt, **kwargs):
            seen["prompt"] = prompt
            return P("/tmp/x.jpeg")

        with mock.patch.object(self.minimax, "create_image", fake_create), \
                mock.patch.object(self.hl, "_mood_flavor", lambda: "情绪底色是来劲，体力72"), \
                mock.patch.object(self.hl, "_character_image", lambda: "/tmp/ref.jpg"), \
                mock.patch.object(self.budget, "spend", return_value=True), \
                mock.patch.object(self.hl, "_run_media_later", self._inline):
            self.hl._make_image({"prompt": "坐在窗台上", "character": True}, session_id="s-media")
        self.assertIn("情绪底色是来劲", seen["prompt"])
        self.assertIn("坐在窗台上", seen["prompt"])

    def test_a_refused_image_quota_is_reported(self):
        with mock.patch.object(self.budget, "spend", return_value=False), \
                mock.patch.object(self.minimax, "create_image") as create:
            out = json.loads(self.hl._make_image({"prompt": "x"}, session_id="s-media"))
        self.assertFalse(out["ok"])
        create.assert_not_called()

    def test_transcription_reads_from_the_inbox_and_returns_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            (inbox / "voice.mp3").write_bytes(b"ID3")
            lib = importlib.import_module("hermes_plugins.digital_life.digital_life.library")
            with mock.patch.object(lib, "inbox_root", lambda: inbox), \
                    mock.patch.object(self.minimax, "transcribe",
                                      return_value={"text": "你好", "duration": 3}), \
                    mock.patch.object(self.budget, "spend", return_value=True):
                out = json.loads(self.hl._transcribe_audio({"name": "voice.mp3"}, session_id="s-media"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["text"], "你好")

    def test_speech_is_made_and_sent_with_a_media_line(self):
        from pathlib import Path as P
        with mock.patch.object(self.minimax, "create_speech", return_value=P("/tmp/v.mp3")), \
                mock.patch.object(self.budget, "spend", return_value=True), \
                mock.patch.object(self.hl, "_run_media_later", self._inline):
            out = json.loads(self.hl._make_speech({"text": "晚安", "caption": "听一下"},
                                                  session_id="s-media"))
        self.assertTrue(out["ok"])
        self.assertIn("MEDIA:/tmp/v.mp3", self.sent[0]["message"])
        self.assertIn("听一下", self.sent[0]["message"])

    def test_speech_quota_and_failures_refund(self):
        with mock.patch.object(self.budget, "spend", return_value=False):
            out = json.loads(self.hl._make_speech({"text": "x"}, session_id="s-media"))
        self.assertFalse(out["ok"])
        with mock.patch.object(self.minimax, "create_speech", side_effect=RuntimeError("boom")), \
                mock.patch.object(self.budget, "spend", return_value=True), \
                mock.patch.object(self.budget, "refund") as refund, \
                mock.patch.object(self.hl, "_run_media_later", self._inline):
            out = json.loads(self.hl._make_speech({"text": "x"}, session_id="s-media"))
        self.assertTrue(out["queued"])
        refund.assert_called_once()
        self.assertTrue(any("没做出来" in item["message"] for item in self.sent))

    def test_audio_outside_the_folders_is_refused(self):
        out = json.loads(self.hl._transcribe_audio({"name": "/etc/passwd"}, session_id="s-media"))
        self.assertFalse(out["ok"])
        self.assertIn("inbox", out["error"])


if __name__ == "__main__":
    unittest.main()
