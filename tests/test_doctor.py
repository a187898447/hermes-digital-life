"""Tests for the ``hermes-lover doctor`` host checks."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from digital_life import doctor


DEFAULT_CONFIG = {
    "plugins": {
        "enabled": ["hermes-lover"],
        "disabled": [],
        "entries": {"hermes-lover": {"settings": {"wake": {"enabled": True}}}},
    },
    "platforms": {"qqbot": {"enabled": True}},
    "platform_toolsets": {"qqbot": ["web", "vision", "todo", "digital_life", "no_mcp"]},
    "cron": {"wrap_response": False},
}


class FakeRunner:
    def __init__(self, checks=None):
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.checks = checks or {}

    def __call__(self, cmd, env):
        self.calls.append((list(cmd), dict(env)))
        if "--check" in cmd:
            return self.checks.get(Path(cmd[1]).name, (0, "ok"))
        return (0, "patched")


class FixingRunner(FakeRunner):
    """A fake host that also applies ``plugins enable`` / ``config set``."""

    def __init__(self, config_path: Path, checks=None):
        super().__init__(checks)
        self.config_path = config_path
        self.patched: set[str] = set()

    def _write(self, config) -> None:
        self.config_path.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def __call__(self, cmd, env):
        self.calls.append((list(cmd), dict(env)))
        if "--check" in cmd:
            script = Path(cmd[1]).name
            if script in self.patched:
                return (0, "OK: patched")
            return self.checks.get(script, (0, "ok"))
        if cmd and cmd[0] == "hermes":
            config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
            if "plugins" in cmd and "enable" in cmd:
                plugins = config.setdefault("plugins", {})
                enabled = plugins.setdefault("enabled", [])
                if doctor.PLUGIN_NAME not in enabled:
                    enabled.append(doctor.PLUGIN_NAME)
                plugins.pop("disabled_by_update", None)
                plugins["disabled"] = []
            elif "config" in cmd and "set" in cmd:
                config.setdefault("platform_toolsets", {})["qqbot"] = json.loads(cmd[-1])
            self._write(config)
            return (0, "ok")
        if cmd and cmd[0] == "python3" and len(cmd) > 1 and cmd[1].endswith(".py"):
            self.patched.add(Path(cmd[1]).name)
        return (0, "patched")


class Fixture:
    def __init__(self, tmp: str, config: dict | None = None, named: bool = False):
        self.root = Path(tmp)
        self.repo = self.root / "repo"
        self.home = (self.root / "profiles" / "hermes-lover") if named else (self.root / "home")
        self.home.mkdir(parents=True)
        self.runner = FakeRunner()
        self.named = named
        self._build_repo()
        self.write_config(DEFAULT_CONFIG if config is None else config)
        self.sync_home()

    def fixing_runner(self, checks=None) -> FixingRunner:
        self.runner = FixingRunner(self.home / "config.yaml", checks)
        return self.runner

    def _build_repo(self) -> None:
        scripts = self.repo / "scripts"
        scripts.mkdir(parents=True)
        for name in doctor.SHIPPED_SCRIPTS:
            (scripts / name).write_text(f"# {name}\n", encoding="utf-8")
        for name in ("fix-qq-dialogue-split.py", "fix-studio-bridge-python.py",
                     "fix-studio-cross-profile.py"):
            (scripts / name).write_text(f"# {name}\n", encoding="utf-8")
        personas = self.repo / "personas" / "001-example"
        personas.mkdir(parents=True)
        (personas / "SOUL.md").write_text("# persona\n", encoding="utf-8")
        (self.repo / "personas" / "README.md").write_text("readme\n", encoding="utf-8")
        skill = self.repo / ".hermes" / "skills" / "hermes-lover-guide"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# guide\n", encoding="utf-8")

    def write_config(self, config: dict) -> None:
        (self.home / "config.yaml").write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def sync_home(self) -> None:
        for src_root, dst_root in (
            (self.repo / "personas", self.home / "personas"),
            (self.repo / ".hermes" / "skills", self.home / "skills"),
        ):
            for src in src_root.rglob("*"):
                if src.is_file():
                    dst = dst_root / src.relative_to(src_root)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(src.read_bytes())
        (self.home / "scripts").mkdir(parents=True, exist_ok=True)
        for name in doctor.SHIPPED_SCRIPTS:
            (self.home / "scripts" / name).write_bytes(
                (self.repo / "scripts" / name).read_bytes())
        (self.home / "SOUL.md").write_text("persona card\n", encoding="utf-8")

    def inspect(self):
        return doctor.inspect(home=self.home, repo=self.repo, runner=self.runner, studio=False)

    def by_key(self, checks):
        return {check.key: check for check in checks}


class DoctorCheckTests(unittest.TestCase):
    def test_healthy_profile_has_no_fix_and_reports_every_area(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(tmp)
            checks = fixture.inspect()
            statuses = {check.key: check.status for check in checks}
            self.assertEqual(len(checks), 11)
            self.assertEqual([check.key for check in checks], [
                "plugin.enabled", "toolsets.qqbot", "patch.qq-dialogue-split",
                "patch.studio-bridge", "patch.studio-cross-profile", "config.cron-wrap",
                "config.wake", "sync.personas", "sync.soul", "sync.scripts", "sync.skills",
            ])
            self.assertEqual(statuses["plugin.enabled"], doctor.OK)
            self.assertEqual(statuses["toolsets.qqbot"], doctor.OK)
            self.assertEqual(statuses["patch.studio-cross-profile"], doctor.SKIP)
            self.assertNotIn(doctor.FIX, statuses.values())
            output = []
            self.assertEqual(doctor.run(home=fixture.home, repo=fixture.repo, runner=fixture.runner,
                                      studio=False, out=output.append), 0)
            self.assertIn("结果：11 项检查", output[-1])

    def test_disabled_plugin_is_a_fix_and_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = json.loads(json.dumps(DEFAULT_CONFIG))
            config["plugins"]["enabled"] = []
            fixture = Fixture(tmp, config)
            check = fixture.by_key(fixture.inspect())["plugin.enabled"]
            self.assertEqual(check.status, doctor.FIX)
            self.assertEqual(check.fix, "plugin")
            self.assertEqual(doctor.run(home=fixture.home, repo=fixture.repo, runner=fixture.runner,
                                      studio=False, out=lambda _: None), 1)

    def test_disabled_by_update_is_a_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = json.loads(json.dumps(DEFAULT_CONFIG))
            config["plugins"]["disabled_by_update"] = {"hermes-lover": {"enabled": False}}
            fixture = Fixture(tmp, config)
            check = fixture.by_key(fixture.inspect())["plugin.enabled"]
            self.assertEqual(check.status, doctor.FIX)
            self.assertIn("更新", check.detail)

    def test_missing_toolset_is_a_fix_with_the_complete_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = json.loads(json.dumps(DEFAULT_CONFIG))
            config["platform_toolsets"]["qqbot"] = ["web", "vision", "todo", "no_mcp"]
            fixture = Fixture(tmp, config)
            check = fixture.by_key(fixture.inspect())["toolsets.qqbot"]
            self.assertEqual(check.status, doctor.FIX)
            self.assertEqual(check.payload["toolsets"],
                             ["web", "vision", "todo", "no_mcp", "digital_life"])

    def test_qqbot_disabled_makes_the_toolset_check_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = json.loads(json.dumps(DEFAULT_CONFIG))
            config["platforms"]["qqbot"]["enabled"] = False
            fixture = Fixture(tmp, config)
            check = fixture.by_key(fixture.inspect())["toolsets.qqbot"]
            self.assertEqual(check.status, doctor.SKIP)

    def test_patch_states_map_to_skip_fix_and_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(tmp)
            fixture.runner.checks = {
                "fix-qq-dialogue-split.py": (1, "未找到 QQ 适配器文件。"),
                "fix-studio-bridge-python.py": (1, "缺少 sitecustomize"),
                "fix-studio-cross-profile.py": (0, "ok"),
            }
            checks = fixture.by_key(fixture.inspect())
            self.assertEqual(checks["patch.qq-dialogue-split"].status, doctor.SKIP)
            self.assertEqual(checks["patch.studio-bridge"].status, doctor.FIX)
            self.assertEqual(checks["patch.studio-bridge"].payload["script"],
                             "fix-studio-bridge-python.py")
            self.assertEqual(checks["patch.studio-cross-profile"].status, doctor.SKIP)

    def test_stale_persona_is_a_fix_and_missing_soul_is_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(tmp)
            (fixture.home / "personas" / "001-example" / "SOUL.md").write_text("old\n")
            (fixture.home / "SOUL.md").unlink()
            checks = fixture.by_key(fixture.inspect())
            self.assertEqual(checks["sync.personas"].status, doctor.FIX)
            self.assertEqual(checks["sync.personas"].payload["pairs"],
                             [(str(fixture.repo / "personas" / "001-example" / "SOUL.md"),
                               str(fixture.home / "personas" / "001-example" / "SOUL.md"))])
            self.assertEqual(checks["sync.soul"].status, doctor.WARN)

    def test_unshipped_repository_scripts_are_not_sync_problems(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(tmp)
            (fixture.repo / "scripts" / "repair-memory-ids.py").write_text("# extra\n")
            check = fixture.by_key(fixture.inspect())["sync.scripts"]
            self.assertEqual(check.status, doctor.OK)

    def test_json_output_is_one_object_with_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = json.loads(json.dumps(DEFAULT_CONFIG))
            config["plugins"]["enabled"] = []
            fixture = Fixture(tmp, config)
            output = []
            code = doctor.run(home=fixture.home, repo=fixture.repo, runner=fixture.runner,
                                      studio=False, as_json=True, out=output.append)
            self.assertEqual(code, 1)
            payload = json.loads("\n".join(output))
            self.assertFalse(payload["ok"])
            self.assertEqual(len(payload["checks"]), 11)
            self.assertEqual(payload["checks"][0]["status"], doctor.FIX)

    def test_config_missing_is_a_warning_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(tmp)
            (fixture.home / "config.yaml").unlink()
            checks = fixture.by_key(fixture.inspect())
            self.assertEqual(checks["plugin.enabled"].status, doctor.WARN)
            self.assertEqual(checks["config.wake"].status, doctor.SKIP)

    def test_fix_repairs_and_second_pass_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = json.loads(json.dumps(DEFAULT_CONFIG))
            config["plugins"]["enabled"] = []
            config["platform_toolsets"]["qqbot"] = ["web", "no_mcp"]
            fixture = Fixture(tmp, config, named=True)
            runner = fixture.fixing_runner({
                "fix-qq-dialogue-split.py": (1, "未修复: adapter.py\n需要运行本脚本以应用拆分补丁。"),
                "fix-studio-bridge-python.py": (1, "未修复: site-packages"),
            })
            (fixture.home / "personas" / "001-example" / "SOUL.md").write_text("old\n")
            output = []
            code = doctor.run(fix=True, home=fixture.home, repo=fixture.repo, runner=runner,
                              studio=False, out=output.append)
            self.assertEqual(code, 0)
            self.assertIn("已自动修复", output[0])
            saved = yaml.safe_load((fixture.home / "config.yaml").read_text(encoding="utf-8"))
            self.assertEqual(saved["plugins"]["enabled"], ["hermes-lover"])
            self.assertIn("digital_life", saved["platform_toolsets"]["qqbot"])
            self.assertEqual(
                (fixture.home / "personas" / "001-example" / "SOUL.md").read_text(),
                (fixture.repo / "personas" / "001-example" / "SOUL.md").read_text())

            second = []
            code = doctor.run(fix=True, home=fixture.home, repo=fixture.repo, runner=runner,
                              studio=False, out=second.append)
            self.assertEqual(code, 0)
            self.assertEqual(second[0], "没有可自动修复的项。")
            self.assertNotIn(doctor.FIX, [check.status for check in fixture.inspect()])

    def test_fix_leaves_a_custom_home_config_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = json.loads(json.dumps(DEFAULT_CONFIG))
            config["plugins"]["enabled"] = []
            fixture = Fixture(tmp, config)
            runner = fixture.fixing_runner()
            output = []
            code = doctor.run(fix=True, home=fixture.home, repo=fixture.repo, runner=runner,
                              studio=False, out=output.append)
            self.assertEqual(code, 1)
            self.assertFalse([cmd for cmd, _ in runner.calls if cmd and cmd[0] == "hermes"])
            saved = yaml.safe_load((fixture.home / "config.yaml").read_text(encoding="utf-8"))
            self.assertEqual(saved["plugins"]["enabled"], [])
            self.assertIn("没有可自动修复的项。", output)

    def test_config_subset_parser_reads_the_hermes_yaml_shape(self):
        text = """
model:
  default: MiniMax-M3.1-Flash
plugins:
  enabled:
  - hermes-lover
  disabled: []
  disabled_by_update: {}
  entries:
    hermes-lover:
      settings:
        wake:
          enabled: false
platforms:
  qqbot:
    enabled: true
platform_toolsets:
  qqbot:
  - web
  - digital_life
  - no_mcp
mcp_servers:
  studio:
    args:
    - https://example.test/a:b
    - "1"
cron:
  wrap_response: false
"""
        data = doctor._parse_yaml_subset(text)
        self.assertEqual(data["plugins"]["enabled"], ["hermes-lover"])
        self.assertEqual(data["plugins"]["disabled"], [])
        self.assertEqual(data["plugins"]["disabled_by_update"], {})
        self.assertFalse(data["plugins"]["entries"]["hermes-lover"]["settings"]["wake"]["enabled"])
        self.assertEqual(data["platform_toolsets"]["qqbot"], ["web", "digital_life", "no_mcp"])
        self.assertEqual(data["mcp_servers"]["studio"]["args"], ["https://example.test/a:b", "1"])
        self.assertFalse(data["cron"]["wrap_response"])


if __name__ == "__main__":
    unittest.main()
