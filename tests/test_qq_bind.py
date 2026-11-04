from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "qq-bind.py"
spec = importlib.util.spec_from_file_location("qq_bind_script", SCRIPT)
qq_bind = importlib.util.module_from_spec(spec)
sys.modules["qq_bind_script"] = qq_bind
spec.loader.exec_module(qq_bind)

WRITTEN = '["web","vision","todo","digital_life","no_mcp"]'


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class ConfigureTests(unittest.TestCase):
    def _fake_cli(self, mode: str):
        writes: dict[str, str] = {}
        calls: list[list[str]] = []

        def run(argv, capture_output=True, text=True):
            calls.append(list(argv))
            verb, key = argv[4], argv[5]
            if mode == "silent-noop":
                return _completed()
            if verb == "set":
                writes[key] = argv[6]
                return _completed()
            if "--json" in argv:
                return _completed(json.dumps(_decode(writes.get(key, "")), ensure_ascii=False))
            return _completed(_yaml_list(_decode(writes.get(key, ""))))

        return run, calls

    def test_list_toolsets_read_back_as_json_is_not_a_failure(self):
        run, _ = self._fake_cli("ok")
        with patch.object(qq_bind.subprocess, "run", run):
            self.assertEqual(qq_bind.configure("p", allow_all=False, wake_words=[]), [])

    def test_plain_yaml_read_back_still_matches(self):
        run, calls = self._fake_cli("ok")

        def run_failing_json(argv, capture_output=True, text=True):
            if "--json" in argv:
                calls.append(list(argv))
                return _completed("", returncode=2)
            return run(argv)

        with patch.object(qq_bind.subprocess, "run", run_failing_json):
            self.assertEqual(qq_bind.configure("p", allow_all=False, wake_words=[]), [])
        self.assertTrue(any("--json" in call for call in calls))

    def test_string_stored_wake_words_match_the_written_list(self):
        run, _ = self._fake_cli("ok")

        def run_quoted(argv, capture_output=True, text=True):
            if argv[4] == "get" and "--json" in argv:
                key = argv[5]
                stored = json.dumps(["小鲸鱼", "鲸鱼"])
                return _completed(json.dumps(stored, ensure_ascii=False))
            return run(argv)

        with patch.object(qq_bind.subprocess, "run", run_quoted):
            failed = qq_bind.configure("p", allow_all=True, wake_words=["小鲸鱼", "鲸鱼"])
        self.assertEqual([line for line in failed if "wake_words" in line], [])

    def test_silent_noop_is_reported(self):
        run, _ = self._fake_cli("silent-noop")
        with patch.object(qq_bind.subprocess, "run", run):
            failed = qq_bind.configure("p", allow_all=False, wake_words=[])
        self.assertTrue(failed)
        self.assertIn("did not stick", failed[0])


def _decode(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _yaml_list(value) -> str:
    if isinstance(value, list):
        return "\n".join(f"- {item}" for item in value) + "\n"
    return f"{value}\n"


if __name__ == "__main__":
    unittest.main()
