import unittest
from unittest.mock import patch

from digital_life import minimax as hailuo


class CreateTests(unittest.TestCase):
    def test_create_sends_prompt_and_returns_task(self):
        calls = []

        def fake(path, *, key="", payload=None, timeout=60.0):
            calls.append((path, payload))
            return {"task_id": "task-1"}

        with patch.object(hailuo, "keys", lambda: ["k1"]), \
                patch.object(hailuo, "_request", fake):
            task, key = hailuo.create("  海边的鲸鱼  ", duration=10, resolution="1080P")
        self.assertEqual(task, "task-1")
        self.assertEqual(key, "k1")
        path, payload = calls[0]
        self.assertEqual(path, "/v1/video_generation")
        self.assertEqual(payload["prompt"], "海边的鲸鱼")
        self.assertEqual(payload["duration"], 10)
        self.assertEqual(payload["resolution"], "1080P")

    def test_unknown_duration_falls_back_to_six(self):
        seen = {}
        with patch.object(hailuo, "keys", lambda: ["k1"]), \
                patch.object(hailuo, "_request", lambda p, key="", payload=None, timeout=60.0:
                             seen.update(payload) or {"task_id": "t"}):
            hailuo.create("x", duration=99)
        self.assertEqual(seen["duration"], 6)

    def test_empty_prompt_is_refused(self):
        with self.assertRaises(hailuo.HailuoError):
            hailuo.create("   ")

    def test_missing_task_id_is_an_error(self):
        with patch.object(hailuo, "keys", lambda: ["k1"]), \
                patch.object(hailuo, "_request", lambda *a, **k: {}):
            with self.assertRaises(hailuo.HailuoError):
                hailuo.create("x")

    def test_a_quota_exhausted_key_falls_through_to_the_next(self):
        tried = []

        def fake(path, *, key="", payload=None, timeout=60.0):
            tried.append(key)
            if key == "k1":
                raise hailuo.HailuoError("MiniMax error 2056: quota")
            return {"task_id": "t2"}

        with patch.object(hailuo, "keys", lambda: ["k1", "k2"]), \
                patch.object(hailuo, "_request", fake):
            task, key = hailuo.create("x")
        self.assertEqual((task, key), ("t2", "k2"))
        self.assertEqual(tried, ["k1", "k2"])


class WaitTests(unittest.TestCase):
    def test_wait_returns_the_file_id(self):
        states = [{"status": "Queueing", "file_id": ""},
                  {"status": "Processing", "file_id": ""},
                  {"status": "Success", "file_id": "file-9"}]
        with patch.object(hailuo, "status", lambda task, **k: states.pop(0)):
            file_id = hailuo.wait("t", sleep=lambda *_a: None)
        self.assertEqual(file_id, "file-9")

    def test_failure_raises(self):
        with patch.object(hailuo, "status", lambda task, **k: {"status": "Fail", "file_id": ""}), \
                patch.object(hailuo.time, "time", side_effect=[0, 1, 2, 3]):
            with self.assertRaises(hailuo.HailuoError):
                hailuo.wait("t", sleep=lambda *_a: None)

    def test_timeout_names_the_last_state(self):
        with patch.object(hailuo, "status", lambda task, **k: {"status": "Processing", "file_id": ""}), \
                patch.object(hailuo.time, "time", side_effect=[0, 1, 100]):
            with self.assertRaises(hailuo.HailuoError) as ctx:
                hailuo.wait("t", timeout=5, sleep=lambda *_a: None)
        self.assertIn("Processing", str(ctx.exception))


class DownloadTests(unittest.TestCase):
    def test_download_writes_into_the_outbox(self):
        class Response:
            def __init__(self, body):
                self._body = body

            def read(self, *_args):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        written = {}

        def fake_write(name, data):
            written["name"] = name
            written["data"] = data
            return "path/" + name

        with patch.object(hailuo, "_request",
                          lambda p, **k: {"file": {"download_url": "https://x/v.mp4"}}), \
                patch.object(hailuo.urllib.request, "urlopen", lambda req, timeout=180: Response(b"mp4")), \
                patch.object(hailuo.outbox, "write_bytes", fake_write):
            path = hailuo.download("file-9")
        self.assertEqual(written["data"], b"mp4")
        self.assertTrue(written["name"].startswith("hailuo-file-9"))
        self.assertEqual(path, "path/" + written["name"])

    def test_no_download_url_is_an_error(self):
        with patch.object(hailuo, "keys", lambda: ["k1"]), \
                patch.object(hailuo, "_request", lambda p, **k: {"file": {}}):
            with self.assertRaises(hailuo.HailuoError):
                hailuo.download("f")


if __name__ == "__main__":
    unittest.main()


class ReferenceTests(unittest.TestCase):
    def test_a_local_photo_becomes_a_data_uri(self):
        import tempfile
        from pathlib import Path as P
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
            handle.write(b"\xff\xd8fakejpg")
            name = handle.name
        try:
            uri = hailuo._as_reference(name)
            self.assertTrue(uri.startswith("data:image/jpeg;base64,"))
        finally:
            P(name).unlink()

    def test_a_url_passes_through_and_a_missing_file_raises(self):
        self.assertEqual(hailuo._as_reference("https://x/y.jpg"), "https://x/y.jpg")
        with self.assertRaises(hailuo.MiniMaxError):
            hailuo._as_reference("/nope/missing.jpg")

    def test_the_first_frame_reaches_the_task(self):
        seen = {}

        def fake(path, *, key="", payload=None, timeout=60.0):
            seen.update(payload or {})
            return {"task_id": "t-1"}

        with patch.object(hailuo, "keys", lambda: ["k"]), patch.object(hailuo, "_request", fake), \
                patch.object(hailuo, "_as_reference", lambda value: "data:image/jpeg;base64,AAA"):
            hailuo.create("她回头", first_frame="/tmp/x.jpg")
        self.assertIn("first_frame_image", seen)


class ModelChoiceTests(unittest.TestCase):
    def test_the_live_model_carries_its_style(self):
        seen = {}

        def fake(path, *, key="", payload=None, timeout=60.0):
            seen.update(payload or {})
            return {"data": {"image_base64": ["aGk="]}}

        with patch.object(hailuo, "keys", lambda: ["k"]), patch.object(hailuo, "_request", fake), \
                patch.object(hailuo.outbox, "write_bytes", lambda name, raw: "out.jpeg"):
            hailuo.create_image("一只鲸鱼", model=hailuo.IMAGE_LIVE_MODEL, style="漫画")
        self.assertEqual(seen["model"], "image-01-live")
        self.assertEqual(seen["style"]["style_type"], "漫画")

    def test_the_detailed_model_ignores_an_unknown_style(self):
        seen = {}

        def fake(path, *, key="", payload=None, timeout=60.0):
            seen.update(payload or {})
            return {"data": {"image_base64": ["aGk="]}}

        with patch.object(hailuo, "keys", lambda: ["k"]), patch.object(hailuo, "_request", fake), \
                patch.object(hailuo.outbox, "write_bytes", lambda name, raw: "out.jpeg"):
            hailuo.create_image("一只猫", model=hailuo.IMAGE_MODEL, style="漫画")
        self.assertEqual(seen["model"], "image-01")
        self.assertNotIn("style", seen)


class SpeechTests(unittest.TestCase):
    def _fake(self, statuses):
        calls = []

        def fake(path, *, key="", payload=None, timeout=60.0):
            calls.append(path)
            if path == "/v1/t2a_async_v2":
                return {"task_id": "t-1"}
            return statuses.pop(0)

        return fake, calls

    def test_polls_the_task_until_the_audio_is_ready(self):
        fake, calls = self._fake([{"status": "Processing"}, {"status": "success", "file_id": "f-1"}])
        with patch.object(hailuo, "keys", lambda: ["k"]), \
                patch.object(hailuo, "_request", fake), \
                patch.object(hailuo, "_download_file", return_value=__import__("pathlib").Path("/tmp/a.mp3")) as dl:
            path = hailuo.create_speech("你好", sleep=lambda *_a: None)
        self.assertEqual(path.name, "a.mp3")
        self.assertEqual(calls[0], "/v1/t2a_async_v2")
        self.assertIn("t2a_async_query_v2", calls[1])
        dl.assert_called_once_with("f-1", key="k")

    def test_a_failed_task_raises(self):
        fake, _ = self._fake([{"status": "failed"}])
        with patch.object(hailuo, "keys", lambda: ["k"]), patch.object(hailuo, "_request", fake):
            with self.assertRaises(hailuo.MiniMaxError):
                hailuo.create_speech("x", sleep=lambda *_a: None)

    def test_empty_text_is_refused(self):
        with self.assertRaises(hailuo.MiniMaxError):
            hailuo.create_speech("   ")

    def test_the_default_voice_is_the_sweet_chinese_girl(self):
        seen = {}

        def fake(path, *, key="", payload=None, timeout=60.0):
            if path == "/v1/t2a_async_v2":
                seen.update(payload or {})
                return {"task_id": "t-1"}
            return {"status": "success", "file_id": "f-1"}

        with patch.object(hailuo, "keys", lambda: ["k"]), patch.object(hailuo, "_request", fake), \
                patch.object(hailuo, "_download_file", return_value=__import__("pathlib").Path("/tmp/a.mp3")):
            hailuo.create_speech("你好", sleep=lambda *_a: None)
        self.assertEqual(seen["voice_setting"]["voice_id"], hailuo.TTS_DEFAULT_VOICE)
        self.assertEqual(hailuo.TTS_DEFAULT_VOICE, "Chinese_sweet_girl_nv1")

    def test_the_voice_and_speed_reach_the_task(self):
        seen = {}

        def fake(path, *, key="", payload=None, timeout=60.0):
            if path == "/v1/t2a_async_v2":
                seen.update(payload or {})
                return {"task_id": "t-1"}
            return {"status": "success", "file_id": "f-1"}

        with patch.object(hailuo, "keys", lambda: ["k"]), patch.object(hailuo, "_request", fake), \
                patch.object(hailuo, "_download_file", return_value=__import__("pathlib").Path("/tmp/a.mp3")):
            hailuo.create_speech("你好", voice="male-qn-qingse", speed=1.4, sleep=lambda *_a: None)
        self.assertEqual(seen["voice_setting"]["voice_id"], "male-qn-qingse")
        self.assertEqual(seen["voice_setting"]["speed"], 1.4)
        self.assertEqual(seen["audio_setting"]["format"], "mp3")
