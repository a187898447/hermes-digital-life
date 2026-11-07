import base64
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


if __name__ == "__main__":
    unittest.main()
