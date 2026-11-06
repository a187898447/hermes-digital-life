import json
import os
import sqlite3
import sys
import time
import tempfile
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from digital_life import typos

if "hermes_constants" not in sys.modules:
    _host = types.ModuleType("hermes_constants")
    _host.get_hermes_home = lambda: Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    sys.modules["hermes_constants"] = _host


class TypoUnitTests(unittest.TestCase):
    def test_flags_a_homophone_slip_with_context(self):
        findings = typos.find_suspicious("他一脚没踩稳，塔到地上了。")
        self.assertEqual(findings[0]["found"], "塔到")
        self.assertIn("塌", findings[0]["hint"])

    def test_clean_text_has_no_findings(self):
        self.assertEqual(typos.find_suspicious("今天翻完他的空间，翻到 2020 年。"), [])


class JournalWarningTests(unittest.TestCase):
    def test_journal_write_reports_a_suspicious_character_without_rewriting(self):
        from test_events import RecordingContext, plugin

        day = (date.today() - timedelta(days=1)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            conn = sqlite3.connect(Path(tmp) / "index.db")
            ctx = RecordingContext()
            ctx.get_config = lambda key, default=None: default
            plugin.register(ctx)
            handlers = {entry["name"]: entry["handler"] for entry in ctx.tools}
            storage_mod = plugin.digital_life.storage
            with patch.object(storage_mod, "data_root", return_value=root), \
                    patch.object(storage_mod, "plugin_db", return_value=conn), \
                    patch.object(plugin, "_session_scope", return_value=("chat-1", "dm")):
                result = json.loads(handlers["memory_write"]({
                    "text": "今天不太顺，走到楼梯口塔到台阶上了。", "kind": "journal", "day": day,
                }, session_id="s-1"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["warnings"][0]["found"], "塔到")
            stored = list(root.rglob("*.jsonl"))
            self.assertTrue(stored)
            self.assertIn("塔到", stored[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
