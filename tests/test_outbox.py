import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from digital_life import outbox


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "outbox"
        self.root.mkdir()
        patcher = mock.patch.dict(os.environ, {"HERMES_LOVER_OUTBOX": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def video(self, name="clip.mp4", body=b"fake-mp4"):
        path = self.root / name
        path.write_bytes(body)
        return path

    def test_a_video_in_the_outbox_resolves(self):
        path = self.video()
        self.assertEqual(outbox.resolve("clip.mp4"), path.resolve())

    def test_absolute_and_parent_paths_are_refused(self):
        self.video()
        for name in ("/etc/passwd", "~/clip.mp4", "../clip.mp4", "a/../../clip.mp4", ""):
            with self.assertRaises(ValueError):
                outbox.resolve(name)

    def test_a_symlink_cannot_lead_out_of_the_folder(self):
        secret = Path(self.tmp.name) / "secret.mp4"
        secret.write_bytes(b"not yours")
        (self.root / "link.mp4").symlink_to(secret)
        with self.assertRaises(ValueError):
            outbox.resolve("link.mp4")

    def test_only_video_suffixes_and_real_files(self):
        self.video("notes.txt")
        with self.assertRaises(ValueError):
            outbox.resolve("notes.txt")
        with self.assertRaises(ValueError):
            outbox.resolve("missing.mp4")

    def test_the_size_cap_is_enforced(self):
        self.video("big.mp4", b"x" * 64)
        with mock.patch.object(outbox, "MAX_BYTES", 32):
            with self.assertRaises(ValueError) as caught:
                outbox.resolve("big.mp4")
        self.assertIn("MB", str(caught.exception))

    def test_a_chat_only_sees_its_own_media_and_the_shared_root(self):
        mine = self.root / "shared.mp4"
        mine.write_bytes(b"operator")
        own = outbox.chat_dir("chat-a") / "voice.mp3"
        own.write_bytes(b"a")
        other = outbox.chat_dir("chat-b") / "voice.mp3"
        other.write_bytes(b"b")
        self.assertEqual(outbox.resolve("voice.mp3", "chat-a"), own.resolve())
        self.assertEqual(outbox.resolve("shared.mp4", "chat-a"), mine.resolve())
        self.assertNotEqual(outbox.resolve("voice.mp3", "chat-b"), own.resolve())
        # A chat cannot name another chat's file by its hashed folder either.
        with self.assertRaises(ValueError):
            outbox.resolve(f"{other.parent.name}/voice.mp3", "chat-a")

    def test_write_bytes_refuses_a_preplaced_symlink(self):
        outside = Path(self.tmp.name) / "outside.mp4"
        outside.write_bytes(b"keep")
        link = outbox.chat_dir("chat-a") / "gen.mp4"
        link.symlink_to(outside)
        with self.assertRaises(ValueError):
            outbox.write_bytes("gen.mp4", b"bytes", chat_id="chat-a")
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_old_generated_media_is_swept_but_the_shared_root_is_not(self):
        folder = outbox.chat_dir("chat-a")
        old = folder / "old.mp4"
        old.write_bytes(b"old")
        shared = self.root / "operator.mp4"
        shared.write_bytes(b"operator")
        stale = 1_000_000.0
        os.utime(old, (stale, stale))
        os.utime(shared, (stale, stale))
        with mock.patch.object(outbox, "KEEP_DAYS", 30):
            outbox.write_bytes("new.mp4", b"new", chat_id="chat-a")
        self.assertFalse(old.exists())
        self.assertTrue(shared.exists())

    def test_write_bytes_stores_a_generated_clip(self):
        target = outbox.write_bytes("gen.mp4", b"bytes")
        self.assertEqual(target.read_bytes(), b"bytes")
        self.assertEqual(outbox.resolve("gen.mp4"), target)
        with self.assertRaises(ValueError):
            outbox.write_bytes("gen.exe", b"bytes")


if __name__ == "__main__":
    unittest.main()
