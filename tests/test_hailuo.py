import unittest
from unittest.mock import patch

from digital_life import hailuo


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
