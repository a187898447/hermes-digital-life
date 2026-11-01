#!/usr/bin/env python3
"""Make Studio's per-profile bridge workers see Hermes' real dependencies.

Hermes pins child processes (for example the Studio agent-bridge workers) to
its managed ``tools`` python.  That runtime ships no third-party packages, and
children started through the ``-I -c`` launcher never import
``hermes_bootstrap``, so the install venv with ``ruamel.yaml`` & friends is
missing.  Every tool module that imports them (terminal, file, memory, skills,
web, ...) then fails to load and the agent silently loses most of its tools.
Only Studio sessions are affected, and only for non-default profiles -- but
install.sh always creates a new profile, so a normal install would hit this.

The fix is a ``sitecustomize.py`` inside each managed ``tools`` python: it runs
under ``-I`` and adds the newest install venv's site-packages with
``site.addsitedir`` (which also processes the editable-install ``.pth`` files).

Usage:
    python3 scripts/fix-studio-bridge-python.py           # apply (idempotent)
    python3 scripts/fix-studio-bridge-python.py --check   # exit 1 if missing
    python3 scripts/fix-studio-bridge-python.py --remove  # undo our file only
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

MARKER = "HERMES_STUDIO_BRIDGE_FIX"
SITECUSTOMIZE = '''"""Expose the Hermes install environment to this managed python.

Written by fix-studio-bridge-python.py ({marker}).
Hermes pins child processes (Studio agent-bridge workers) to this interpreter,
but this runtime ships no third-party packages and those children never run
hermes_bootstrap.  Adding the install venv here -- in a hook CPython runs even
under ``-I`` -- restores the tool modules that import ruamel.yaml and friends.
"""

import glob
import os
import site
import sys

try:
    _added = False
    for _venv in sorted(
        glob.glob(os.path.join(os.path.expanduser("~"), ".hermes", "installs", "*",
                               "environments", "*", "venv")),
        key=os.path.getmtime,
        reverse=True,
    ):
        for _site in sorted(glob.glob(os.path.join(_venv, "lib", "python3*",
                                                   "site-packages"))):
            if not os.path.isdir(os.path.join(_site, "ruamel")):
                continue
            if _site not in sys.path:
                site.addsitedir(_site)
            _added = True
            break
        if _added:
            break
except Exception:  # never break the interpreter's site initialisation
    pass
'''

INSTALLS = ("installs", "*", "environments", "*", "venv")


def hermes_home() -> Path:
    raw = os.environ.get("HERMES_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".hermes"


def tools_site_dirs(home: Path) -> list[Path]:
    return sorted((home / "tools").glob("python-*/lib/python3*/site-packages"))


def has_install_venv(home: Path) -> bool:
    for venv in sorted((home / "installs").glob("/".join(INSTALLS[1:]))):
        for site_dir in venv.glob("lib/python3*/site-packages"):
            if (site_dir / "ruamel").is_dir():
                return True
    return False


def patch_state(site_dir: Path) -> str:
    """missing | owned | ours | foreign."""
    target = site_dir / "sitecustomize.py"
    if not target.exists():
        return "missing"
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "foreign"
    if MARKER in text:
        return "ours"
    return "foreign"


def apply(home: Path) -> int:
    dirs = tools_site_dirs(home)
    if not dirs:
        print("未找到 Hermes 托管的 python 运行时（不需要修复）。")
        return 0
    if not has_install_venv(home):
        print("未找到带依赖的 Hermes 安装环境，跳过；先运行一次 hermes 再试。")
        return 0
    body = SITECUSTOMIZE.format(marker=MARKER)
    wrote = skipped = 0
    for site_dir in dirs:
        state = patch_state(site_dir)
        target = site_dir / "sitecustomize.py"
        if state == "foreign":
            print(f"跳过（已有他人 sitecustomize.py）: {target}")
            skipped += 1
            continue
        if state in {"ours", "missing"}:
            if target.exists() and target.read_text(encoding="utf-8") == body:
                continue
            target.write_text(body, encoding="utf-8")
            wrote += 1
    print(f"Studio 桥接环境: 已写入 {wrote} 个运行时，跳过 {skipped} 个。"
          " 已打开的 Studio 会话需新开一个对话才生效。")
    return 0


def check(home: Path) -> int:
    dirs = tools_site_dirs(home)
    if not dirs:
        print("OK: 没有需要修复的运行时。")
        return 0
    missing = [d for d in dirs if patch_state(d) != "ours"]
    if not missing:
        print("OK: Studio 桥接环境已修复。")
        return 0
    for site_dir in missing:
        print(f"未修复: {site_dir}")
    return 1


def remove(home: Path) -> int:
    removed = 0
    for site_dir in tools_site_dirs(home):
        target = site_dir / "sitecustomize.py"
        if patch_state(site_dir) == "ours":
            target.unlink()
            removed += 1
    print(f"已移除 {removed} 个补丁文件。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="只检查，不写入")
    group.add_argument("--remove", action="store_true", help="只删除本工具写入的文件")
    args = parser.parse_args()
    home = hermes_home()
    if args.check:
        return check(home)
    if args.remove:
        return remove(home)
    return apply(home)


if __name__ == "__main__":
    sys.exit(main())
