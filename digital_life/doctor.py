"""Detect, and where safe repair, the host-side conditions hermes-lover needs.

The checks are deliberately shallow: read the profile config, compare the files
``install.sh`` ships, and call the host patch scripts with ``--check``.  Doctor
never imports host internals, so it still runs when the host moved them.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PLUGIN_NAME = "hermes-lover"

# install.sh ships exactly these files into the profile; anything else in the
# repository's scripts/ (fix scripts, one-off repairs) was never installed and
# must not be reported as a synchronization problem.
SHIPPED_SCRIPTS = (
    "setup-status.py",
    "qq-bind.py",
    "sync-persona.sh",
    "hermes-lover-beat.py",
    "hermes-lover-journal.py",
)
IGNORED_NAMES = {"__pycache__", ".DS_Store", ".pytest_cache"}

OK = "OK"
FIX = "FIX"
SKIP = "SKIP"
WARN = "WARN"

Runner = Callable[[list[str], dict[str, str]], tuple[int, str]]


@dataclass
class Check:
    key: str
    status: str
    detail: str
    fix: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def profile_home() -> Path:
    raw = os.environ.get("HERMES_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".hermes"


def profile_name(home: Path) -> str:
    """Name usable with ``hermes -p``; ``custom`` means a non-standard HERMES_HOME."""
    try:
        resolved = Path(home).expanduser().resolve()
        default = (Path.home() / ".hermes").resolve()
    except OSError:
        return "custom"
    if resolved == default:
        return "default"
    if resolved.parent.name == "profiles":
        return resolved.name
    return "custom"


def host_root(home: Path) -> Path:
    """Where the host keeps ``tools/``, ``installs/`` and ``hermes-agent/``.

    A named profile lives at ``<root>/profiles/<name>``; the patch scripts read
    ``HERMES_HOME`` as that installation root, not as the profile directory.
    """
    name = profile_name(home)
    if name not in ("default", "custom"):
        return Path(home).expanduser().resolve().parent.parent
    return Path(home).expanduser()


def studio_present() -> bool:
    candidates = []
    root = os.environ.get("HERMES_WEBUI_NPM_ROOT", "").strip()
    if root:
        candidates.append(Path(root) / "hermes-web-ui")
    candidates.extend([
        Path("/usr/local/lib/node_modules/hermes-web-ui"),
        Path.home() / ".npm-global" / "lib" / "node_modules" / "hermes-web-ui",
        Path.home() / ".local" / "lib" / "node_modules" / "hermes-web-ui",
    ])
    return any((candidate / "dist" / "server" / "index.js").is_file() for candidate in candidates)


def _env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    return env


def _default_runner(cmd: list[str], env: dict[str, str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180, env=env, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _first_line(text: str, limit: int = 160) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def _patch_problem(output: str) -> str:
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    problem = [line for line in lines if line.startswith(("未修复", "未应用", "需要"))]
    return "; ".join(problem[:2]) if problem else (lines[-1] if lines else "")


def _contains(value: Any, name: str) -> bool:
    if isinstance(value, (list, tuple, set)):
        return name in value
    if isinstance(value, dict):
        return name in value
    return False


def _strip_yaml_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
        elif char in ("'", '"'):
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def _split_unquoted(text: str, separator: str) -> tuple[str, str] | None:
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
        elif char in ("'", '"'):
            quote = char
        elif char == separator:
            return text[:index], text[index + 1:]
    return None


def _mapping_pair(text: str) -> tuple[str, str] | None:
    pair = _split_unquoted(text, ":")
    if pair is None:
        return None
    if pair[1] and not pair[1][0].isspace():
        return None  # a URL or Windows path, not a mapping entry
    return pair


def _split_inline(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
        elif char in ("'", '"'):
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def _yaml_scalar(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    if text.startswith("[") and text.endswith("]"):
        body = text[1:-1].strip()
        return [_yaml_scalar(part) for part in _split_inline(body)] if body else []
    if text.startswith("{") and text.endswith("}"):
        body = text[1:-1].strip()
        result: dict[str, Any] = {}
        if not body:
            return result
        for part in _split_inline(body):
            pair = _split_unquoted(part, ":")
            if pair is None:
                raise ValueError("内联映射缺少冒号")
            result[str(_yaml_scalar(pair[0]))] = _yaml_scalar(pair[1])
        return result
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            return json.loads(text)
        except ValueError:
            return text[1:-1]
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "~"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _parse_yaml_subset(text: str) -> Any:
    """Parse the mapping/list YAML that Hermes writes into config.yaml.

    The managed tools Python ships no third-party packages, so PyYAML is not
    importable where the plugin actually runs.  config.yaml only uses block
    mappings, indentation-less block lists, inline ``[]``/``{}``, and scalars;
    that is the subset handled here.
    """
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = _strip_yaml_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if "\t" in line[:indent + 1]:
            raise ValueError("缩进里有制表符")
        lines.append((indent, line.strip()))
    if not lines:
        return {}
    value, index = _parse_yaml_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise ValueError("存在无法归位的行")
    return value


def _parse_yaml_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if lines[index][1].startswith("-"):
        return _parse_yaml_list(lines, index, indent)
    return _parse_yaml_mapping(lines, index, indent)


def _parse_yaml_mapping(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        line_indent, text = lines[index]
        if line_indent < indent or (line_indent == indent and text.startswith("-")):
            break
        if line_indent > indent:
            raise ValueError("映射缩进异常")
        pair = _mapping_pair(text)
        if pair is None:
            raise ValueError("映射行缺少冒号")
        key = str(_yaml_scalar(pair[0]))
        rest = pair[1].strip()
        if rest:
            result[key] = _yaml_scalar(rest)
            index += 1
            continue
        index += 1
        if index < len(lines) and lines[index][0] > indent:
            value, index = _parse_yaml_block(lines, index, lines[index][0])
            result[key] = value
        elif index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("-"):
            value, index = _parse_yaml_list(lines, index, indent)
            result[key] = value
        else:
            result[key] = None
    return result, index


def _parse_yaml_list(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        line_indent, text = lines[index]
        if line_indent != indent or not text.startswith("-"):
            break
        rest = text[1:].strip()
        index += 1
        if not rest:
            if index < len(lines) and lines[index][0] > indent:
                value, index = _parse_yaml_block(lines, index, lines[index][0])
            else:
                value = None
        elif _mapping_pair(rest) is not None and not rest.startswith(("[", "{", '"', "'")):
            raise ValueError("暂不支持列表里的块映射")
        else:
            value = _yaml_scalar(rest)
        result.append(value)
    return result, index


def _load_config(home: Path) -> tuple[dict[str, Any] | None, str]:
    path = home / "config.yaml"
    if not path.is_file():
        return None, "profile 还没有 config.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"config.yaml 读取失败：{type(exc).__name__}"
    try:
        import yaml  # noqa: PLC0415 - optional; the managed tools Python ships no PyYAML
    except ImportError:
        try:
            data = _parse_yaml_subset(text)
        except (ValueError, IndexError) as exc:
            return None, f"config.yaml 解析失败：{exc}"
    else:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            return None, f"config.yaml 解析失败：{type(exc).__name__}"
    if not isinstance(data, dict):
        return None, "config.yaml 不是有效的映射"
    return data, ""


def _check_plugin(config: dict[str, Any] | None, config_error: str) -> Check:
    key = "plugin.enabled"
    if config is None:
        return Check(key, WARN, f"{config_error}，跳过启用检查")
    plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    enabled = plugins.get("enabled")
    disabled = plugins.get("disabled")
    by_update = plugins.get("disabled_by_update")
    if not _contains(enabled, PLUGIN_NAME) or _contains(disabled, PLUGIN_NAME) or _contains(by_update, PLUGIN_NAME):
        if _contains(by_update, PLUGIN_NAME):
            return Check(key, FIX, "宿主更新把插件停用了（plugins.disabled_by_update）", fix="plugin")
        if _contains(disabled, PLUGIN_NAME):
            return Check(key, FIX, "插件在 plugins.disabled 里", fix="plugin")
        return Check(key, FIX, "插件未出现在 plugins.enabled 里", fix="plugin")
    return Check(key, OK, "插件已启用")


def _check_toolsets(config: dict[str, Any] | None, config_error: str) -> Check:
    key = "toolsets.qqbot"
    if config is None:
        return Check(key, SKIP, f"{config_error}，跳过工具集检查")
    platforms = config.get("platforms") if isinstance(config.get("platforms"), dict) else {}
    qqbot = platforms.get("qqbot") if isinstance(platforms.get("qqbot"), dict) else {}
    if not qqbot.get("enabled"):
        return Check(key, SKIP, "未启用 QQ 平台")
    table = config.get("platform_toolsets") if isinstance(config.get("platform_toolsets"), dict) else {}
    toolsets = table.get("qqbot")
    required = ["digital_life", "no_mcp"]
    if not isinstance(toolsets, list):
        base = ["web", "vision", "todo"]
        return Check(key, FIX, "platform_toolsets.qqbot 缺失或不是列表", fix="toolsets",
                     payload={"toolsets": base + required})
    missing = [name for name in required if name not in toolsets]
    if missing:
        return Check(key, FIX, "platform_toolsets.qqbot 缺少 " + "、".join(missing), fix="toolsets",
                     payload={"toolsets": [*toolsets, *missing]})
    return Check(key, OK, "platform_toolsets.qqbot 含 digital_life 与 no_mcp")


def _patch_check(
    *, key: str, script: str, home: Path, repo: Path, runner: Runner, label: str,
    skip: str = "",
) -> Check:
    if skip:
        return Check(key, SKIP, skip)
    path = repo / "scripts" / script
    if not path.is_file():
        return Check(key, WARN, f"仓库里找不到 scripts/{script}")
    rc, output = runner(["python3", str(path), "--check"], _env(home))
    detail = _first_line(output)
    if rc == 0:
        return Check(key, OK, f"{label}已就位")
    if rc == 3 and "未找到" in output:
        return Check(key, SKIP, _first_line(output) or "本机没有可打补丁的宿主文件")
    if script == "fix-qq-dialogue-split.py" and "未找到" in output:
        return Check(key, SKIP, "本机没有可打补丁的 QQ 适配器")
    if rc == 1:
        return Check(key, FIX, f"{label}未打：{_patch_problem(output) or '--check 返回 1'}",
                     fix="patch", payload={"script": script, "label": label})
    return Check(key, WARN, f"{label}检查异常（rc={rc}）：{detail or '无输出'}")


STUDIO_CROSS_HINT = (
    "默认不打：profile 是数据边界，不是安全边界；确认要让 Studio 跨 profile 打开选项卡，"
    "再运行 python3 scripts/fix-studio-cross-profile.py"
)


def _check_studio_cross_profile(*, home: Path, repo: Path, runner: Runner, studio: bool) -> Check:
    """Hint only: the patch widens who may open a profile tab, so the operator opts in."""
    key = "patch.studio-cross-profile"
    if not studio:
        return Check(key, SKIP, "没有找到 Hermes Studio")
    path = repo / "scripts" / "fix-studio-cross-profile.py"
    if not path.is_file():
        return Check(key, WARN, "仓库里找不到 scripts/fix-studio-cross-profile.py")
    rc, output = runner(["python3", str(path), "--check"], _env(home))
    if rc == 0:
        return Check(key, OK, "已按你的选择应用跨 profile 补丁")
    if rc == 3 and "未找到" in output:
        return Check(key, SKIP, _first_line(output) or "本机没有可打补丁的宿主文件")
    if rc == 1:
        return Check(key, WARN, STUDIO_CROSS_HINT)
    return Check(key, WARN, f"Studio 跨 profile 补丁检查异常（rc={rc}）：{_first_line(output) or '无输出'}")


def _check_cron(config: dict[str, Any] | None, config_error: str) -> Check:
    key = "config.cron-wrap"
    if config is None:
        return Check(key, SKIP, f"{config_error}，跳过 cron 检查")
    cron = config.get("cron") if isinstance(config.get("cron"), dict) else {}
    wrap = cron.get("wrap_response", False)
    if wrap is False:
        return Check(key, OK, "cron.wrap_response 为 false")
    if wrap is True:
        return Check(key, WARN, "cron.wrap_response 为 true，重复包装会弄坏主动消息（需手动关）")
    return Check(key, OK, "未设置 cron.wrap_response（默认 false）")


def _check_wake(config: dict[str, Any] | None, config_error: str) -> Check:
    key = "config.wake"
    if config is None:
        return Check(key, SKIP, f"{config_error}，跳过 wake 检查")
    plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    entries = plugins.get("entries") if isinstance(plugins.get("entries"), dict) else {}
    entry = entries.get(PLUGIN_NAME) if isinstance(entries.get(PLUGIN_NAME), dict) else {}
    settings = entry.get("settings") if isinstance(entry.get("settings"), dict) else {}
    wake = settings.get("wake") if isinstance(settings.get("wake"), dict) else {}
    enabled = wake.get("enabled")
    if isinstance(enabled, bool):
        return Check(key, OK, f"wake.enabled={str(enabled).lower()}")
    return Check(key, WARN, "读不到 plugins.entries.hermes-lover.settings.wake.enabled")


def _iter_files(root: Path):
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in IGNORED_NAMES for part in path.relative_to(root).parts):
            continue
        yield path


def _same_file(src: Path, dst: Path) -> bool:
    try:
        if not dst.is_file():
            return False
        if src.stat().st_size != dst.stat().st_size:
            return False
        return src.read_bytes() == dst.read_bytes()
    except OSError:
        return False


def _sync_check(key: str, pairs: list[tuple[Path, Path]]) -> Check:
    stale = [(src, dst) for src, dst in pairs if not _same_file(src, dst)]
    if not stale:
        return Check(key, OK, f"{len(pairs)} 个文件与仓库一致")
    names = [dst.name for _, dst in stale]
    shown = "、".join(names[:3]) + (" 等" if len(names) > 3 else "")
    return Check(key, FIX, f"{len(stale)} 个文件缺失或过期：{shown}", fix="copy",
                 payload={"pairs": [(str(src), str(dst)) for src, dst in stale]})


def _check_soul(home: Path) -> Check:
    path = home / "SOUL.md"
    try:
        size = path.stat().st_size if path.is_file() else 0
    except OSError:
        size = 0
    if size > 0:
        return Check("sync.soul", OK, f"SOUL.md 已就位（{size} 字节）")
    if path.exists():
        return Check("sync.soul", WARN, "SOUL.md 是空文件；写入人格后再跑一次")
    return Check("sync.soul", WARN, "profile 还没有 SOUL.md；完成人格转译后再跑一次")


def _persona_pairs(repo: Path, home: Path) -> list[tuple[Path, Path]]:
    src_root = repo / "personas"
    return [(path, home / "personas" / path.relative_to(src_root)) for path in _iter_files(src_root)]


def _script_pairs(repo: Path, home: Path) -> list[tuple[Path, Path]]:
    pairs = []
    for name in SHIPPED_SCRIPTS:
        src = repo / "scripts" / name
        if src.is_file():
            pairs.append((src, home / "scripts" / name))
    return pairs


def _skill_pairs(repo: Path, home: Path) -> list[tuple[Path, Path]]:
    src_root = repo / ".hermes" / "skills"
    return [(path, home / "skills" / path.relative_to(src_root)) for path in _iter_files(src_root)]


def inspect(
    home: Path | None = None,
    repo: Path | None = None,
    runner: Runner | None = None,
    studio: bool | None = None,
) -> list[Check]:
    home = Path(home) if home is not None else profile_home()
    repo = Path(repo) if repo is not None else repository_root()
    runner = runner or _default_runner
    if studio is None:
        studio = studio_present()
    config, config_error = _load_config(home)
    patches_home = host_root(home)
    return [
        _check_plugin(config, config_error),
        _check_toolsets(config, config_error),
        _patch_check(key="patch.qq-dialogue-split", script="fix-qq-dialogue-split.py",
                     home=patches_home, repo=repo, runner=runner, label="QQ 对话拆分补丁"),
        _patch_check(key="patch.qq-shutdown-notice", script="fix-qq-shutdown-notice.py",
                     home=patches_home, repo=repo, runner=runner, label="QQ 关停通知过滤补丁"),
        _patch_check(key="patch.studio-bridge", script="fix-studio-bridge-python.py",
                     home=patches_home, repo=repo, runner=runner, label="Studio Python 桥补丁"),
        _check_studio_cross_profile(home=patches_home, repo=repo, runner=runner, studio=studio),
        _check_cron(config, config_error),
        _check_wake(config, config_error),
        _sync_check("sync.personas", _persona_pairs(repo, home)),
        _check_soul(home),
        _sync_check("sync.scripts", _script_pairs(repo, home)),
        _sync_check("sync.skills", _skill_pairs(repo, home)),
        _check_heartbeat(home),
        _check_memory(home),
    ]



LIFE_STALE_MINUTES = 30


def _check_heartbeat(home: Path) -> Check:
    """Read-only: did the day record say anything walked it forward recently?"""
    db = home / "plugin-data" / PLUGIN_NAME / "data.db"
    if not db.is_file():
        return Check("life.tick", SKIP, "还没有生活记录，插件启动后才会开始")
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT payload FROM life_state LIMIT 1").fetchone()
        finally:
            conn.close()
        state = json.loads(row[0]) if row else {}
    except (sqlite3.Error, ValueError, OSError):
        return Check("life.tick", SKIP, "读不到生活记录")
    life = state.get("life") if isinstance(state, dict) else None
    if not isinstance(life, dict):
        return Check("life.tick", SKIP, "还没有生活日程")
    last = float(life.get("last_tick_at") or 0.0)
    if not last:
        return Check("life.tick", SKIP, "心跳还没跑过")
    minutes = max(0.0, (time.time() * 1000 - last) / 60000)
    if minutes > LIFE_STALE_MINUTES:
        return Check("life.tick", WARN,
                     f"{minutes:.0f} 分钟没有心跳；网关可能在休眠，或者 life.enabled 关了")
    current_id = str(life.get("current") or "")
    item = next((entry for entry in life.get("agenda") or []
                 if isinstance(entry, dict) and entry.get("id") == current_id), None)
    title = str((item or {}).get("title") or "空档")
    return Check("life.tick", OK, f"{minutes:.0f} 分钟前刚走过 · {title}")


MEMORY_WARN_BYTES = 50 * 1024 * 1024


def _check_memory(home: Path) -> Check:
    """Warn (never repair) when the palace is big enough to slow every replay."""
    root = home / "plugin-data" / PLUGIN_NAME / "memory"
    total = 0
    try:
        total = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    except OSError:
        return Check("memory.size", SKIP, "读不到记忆目录")
    megabytes = total / (1024 * 1024)
    if total > MEMORY_WARN_BYTES:
        return Check("memory.size", WARN,
                     f"记忆库 {megabytes:.1f} MB，超过 50 MB；跑 hermes-lover memory-compact 收一收")
    return Check("memory.size", OK, f"{megabytes:.1f} MB")


def _hermes_prefix(home: Path) -> list[str] | None:
    """Command prefix for the profile; ``None`` for a custom HERMES_HOME."""
    name = profile_name(home)
    if name == "custom":
        return None
    if name == "default":
        return ["hermes"]
    return ["hermes", "-p", name]


def _set_toolsets(home: Path, toolsets: list[str], runner: Runner) -> bool:
    prefix = _hermes_prefix(home)
    if prefix is None or not toolsets:
        return False
    rc, _ = runner(
        [*prefix, "config", "set", "platform_toolsets.qqbot",
         json.dumps(toolsets, ensure_ascii=False)],
        _env(home),
    )
    return rc == 0


def _enable_plugin(home: Path, runner: Runner) -> bool:
    prefix = _hermes_prefix(home)
    if prefix is None:
        return False
    rc, _ = runner([*prefix, "plugins", "enable", PLUGIN_NAME], _env(home))
    return rc == 0


def _apply_patch(repo: Path, script: str, home: Path, runner: Runner) -> bool:
    path = repo / "scripts" / script
    if not path.is_file():
        return False
    rc, _ = runner(["python3", str(path)], _env(host_root(home)))
    return rc == 0


def _copy_pairs(pairs: list[tuple[str, str]]) -> bool:
    changed = False
    for src_name, dst_name in pairs:
        src, dst = Path(src_name), Path(dst_name)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            changed = True
        except OSError:
            continue
    return changed


def repair(
    checks: list[Check],
    home: Path | None = None,
    repo: Path | None = None,
    runner: Runner | None = None,
) -> list[str]:
    """Apply the safe repairs, returning the keys that were actually applied."""
    home = Path(home) if home is not None else profile_home()
    repo = Path(repo) if repo is not None else repository_root()
    runner = runner or _default_runner
    applied: list[str] = []
    for check in checks:
        if check.status != FIX:
            continue
        changed = False
        try:
            if check.fix == "plugin":
                changed = _enable_plugin(home, runner)
            elif check.fix == "toolsets":
                changed = _set_toolsets(home, list(check.payload.get("toolsets") or []), runner)
            elif check.fix == "patch":
                changed = _apply_patch(
                    repo, str(check.payload.get("script") or ""), home, runner)
            elif check.fix == "copy":
                changed = _copy_pairs(list(check.payload.get("pairs") or []))
        except (OSError, subprocess.SubprocessError):
            changed = False
        if changed:
            applied.append(check.key)
    return applied


def _summary(checks: list[Check]) -> str:
    counts = {status: sum(1 for check in checks if check.status == status)
              for status in (OK, FIX, WARN, SKIP)}
    return (f"结果：{len(checks)} 项检查，OK {counts[OK]}，FIX {counts[FIX]}，"
            f"WARN {counts[WARN]}，SKIP {counts[SKIP]}")


def run(
    fix: bool = False,
    as_json: bool = False,
    home: Path | None = None,
    repo: Path | None = None,
    runner: Runner | None = None,
    studio: bool | None = None,
    out: Callable[[str], None] = print,
) -> int:
    home = Path(home) if home is not None else profile_home()
    repo = Path(repo) if repo is not None else repository_root()
    runner = runner or _default_runner
    checks = inspect(home, repo, runner=runner, studio=studio)
    applied: list[str] = []
    if fix:
        applied = repair(checks, home, repo, runner)
        checks = inspect(home, repo, runner=runner, studio=studio)

    if as_json:
        out(json.dumps({
            "ok": not any(check.status == FIX for check in checks),
            "fixed": applied,
            "checks": [{"key": check.key, "status": check.status, "detail": check.detail}
                       for check in checks],
        }, ensure_ascii=False))
    else:
        if fix:
            if applied:
                out("已自动修复：" + "、".join(applied))
                out("重新检查：")
            else:
                out("没有可自动修复的项。")
        for check in checks:
            out(f"{check.status:<4} {check.key:<26} {check.detail}")
        out("")
        out(_summary(checks))
    return 1 if any(check.status == FIX for check in checks) else 0
