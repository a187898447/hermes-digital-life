"""Structural guards that keep the run-4 classes of bugs from creeping back."""

import ast
import inspect
import unittest
from pathlib import Path

from digital_life import memory, wake

ROOT = Path(__file__).resolve().parents[1]
# research.py is the one spawn channel; doctor and the /restart command may run
# commands on the operator's behalf. Everything else must not start processes.
SUBPROCESS_ALLOWED = {"__init__.py", "digital_life/research.py", "digital_life/doctor.py"}


def modules():
    yield from ROOT.glob("digital_life/*.py")
    yield ROOT / "__init__.py"


class ChatScopedApiTests(unittest.TestCase):
    def test_wake_calls_that_read_or_write_a_slot_require_a_chat(self):
        for name in ("plan_wake", "reconcile", "section_text", "force_active"):
            signature = inspect.signature(getattr(wake, name))
            parameter = signature.parameters.get("chat_id")
            self.assertIsNotNone(parameter, name)
            self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY, name)
            self.assertIs(parameter.default, inspect.Parameter.empty, name)

    def test_revoke_requires_the_calling_chat(self):
        parameter = inspect.signature(memory.MemoryStore.revoke).parameters["chat_id"]
        self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_peek_can_be_scoped_to_one_chat(self):
        parameter = inspect.signature(memory.MemoryStore.peek).parameters["chat_id"]
        self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)


class SubprocessChannelTests(unittest.TestCase):
    def test_only_the_approved_modules_start_processes(self):
        offenders = []
        for path in modules():
            relative = path.relative_to(ROOT).as_posix()
            if relative in SUBPROCESS_ALLOWED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(a.name == "subprocess" for a in node.names):
                    offenders.append(relative)
                elif (isinstance(node, ast.ImportFrom) and node.module == "subprocess"):
                    offenders.append(relative)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
