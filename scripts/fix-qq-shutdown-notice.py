#!/usr/bin/env python3
"""Stop Hermes from broadcasting shutdown/restart banners into QQ chats.

The gateway tells every active chat when it is going down or restarting. In a QQ
group those banners read as spam and get split into several bubbles. This patch
makes the shutdown notifier skip qqbot targets entirely (per-session notices and
the home-channel broadcast).

Modes:
  (default)  apply the patch to every discovered gateway/run_shutdown.py
  --check    exit 0 when patched everywhere, 3 when no host file was found
  --remove   restore the most recent backup (or revert the patch text)
"""

from __future__ import annotations

import argparse
import glob
import os
import py_compile
import shutil
import sys
import time
from pathlib import Path

MARKER = "# hermes-lover: qq shutdown notice"

SESSION_ANCHOR = """            source, platform_str, chat_id, thread_id, profile = target
            dedup_key = _delivery_target_key(platform_str, chat_id, thread_id, profile=profile)"""

SESSION_PATCHED = """            source, platform_str, chat_id, thread_id, profile = target
            # hermes-lover: qq shutdown notice — QQ chats read these banners as spam.
            if "qqbot" in str(platform_str):
                continue
            dedup_key = _delivery_target_key(platform_str, chat_id, thread_id, profile=profile)"""

HOME_ANCHOR = """            if not self._notice_allowed(platform, "home channel", platform_cfg):
                continue"""

HOME_PATCHED = """            if not self._notice_allowed(platform, "home channel", platform_cfg):
                continue
            # hermes-lover: qq shutdown notice — never broadcast banners into QQ chats.
            if "qqbot" in str(platform.value):
                continue"""


def candidate_files() -> list[Path]:
    home = Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()
    roots = [
        home / "hermes-agent" / "gateway" / "run_shutdown.py",
        Path("/usr/local/lib/hermes-agent/gateway/run_shutdown.py"),
    ]
    roots += [
        Path(p)
        for p in glob.glob(
            str(home / "installs" / "*" / "environments" / "*" / "workspace" / "gateway" / "run_shutdown.py")
        )
    ]
    seen, out = set(), []
    for path in roots:
        if path.is_file() and str(path) not in seen:
            seen.add(str(path))
            out.append(path)
    return out


def is_patched(text: str) -> bool:
    return SESSION_PATCHED in text and HOME_PATCHED in text


def patch_text(text: str) -> str | None:
    if is_patched(text):
        return None
    if SESSION_ANCHOR in text:
        text = text.replace(SESSION_ANCHOR, SESSION_PATCHED, 1)
    elif "hermes-lover: qq shutdown notice" not in text:
        return None
    if HOME_ANCHOR in text:
        text = text.replace(HOME_ANCHOR, HOME_PATCHED, 1)
    if not is_patched(text):
        return None
    return text


def unpatch_text(text: str) -> str | None:
    if not is_patched(text):
        return None
    text = text.replace(SESSION_PATCHED, SESSION_ANCHOR, 1)
    text = text.replace(HOME_PATCHED, HOME_ANCHOR, 1)
    return text


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp-shutdown")
    tmp.write_text(text)
    py_compile.compile(str(tmp), doraise=True)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args(argv)

    files = candidate_files()
    if not files:
        print("未找到 gateway/run_shutdown.py，跳过")
        return 3

    if args.check:
        missing = [p for p in files if not is_patched(p.read_text(errors="ignore"))]
        for path in missing:
            print(f"未打补丁: {path}")
        print("OK" if not missing else f"{len(missing)} 个文件未打补丁")
        return 0 if not missing else 1

    if args.remove:
        removed = 0
        for path in files:
            text = path.read_text(errors="ignore")
            new = unpatch_text(text)
            if new is None:
                continue
            backups = sorted(path.parent.glob(path.name + ".bak-shutdown-*"))
            if backups:
                shutil.copy2(backups[-1], path)
            else:
                write_atomic(path, new)
            removed += 1
        print(f"已移除 {removed} 个补丁")
        return 0

    applied = 0
    for path in files:
        text = path.read_text(errors="ignore")
        new = patch_text(text)
        if new is None:
            continue
        backup = path.with_name(path.name + f".bak-shutdown-{time.strftime('%Y%m%d%H%M%S')}")
        if not backup.exists():
            shutil.copy2(path, backup)
        write_atomic(path, new)
        applied += 1
        print(f"已应用（备份 {backup.name}）: {path}")
    print("OK" if applied or files else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
