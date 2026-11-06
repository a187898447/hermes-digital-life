"""The plugin module is only loaded by the host, so a NameError in a handler
shows up as a broken tool at runtime, not at import. This static check keeps
helpers that are called but no longer defined (e.g. after a dead-code sweep)
from shipping again.
"""
import ast
import builtins
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PluginNamesTests(unittest.TestCase):
    def test_private_helpers_are_defined(self):
        tree = ast.parse((ROOT / "__init__.py").read_text())
        defined = set(dir(builtins))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Import):
                defined.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                defined.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
        called = {node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id.startswith("_")}
        missing = sorted(name for name in called if name not in defined)
        self.assertEqual(missing, [], f"called but never defined in plugin: {missing}")


if __name__ == "__main__":
    unittest.main()
