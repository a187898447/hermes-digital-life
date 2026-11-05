#!/usr/bin/env python3
"""Add a human typing pause between QQ bubbles sent by the host adapter."""
from __future__ import annotations

import os
import py_compile
import shutil
import sys
import time
from pathlib import Path

MARKER = "# hermes-lover: typing pace"
ANCHOR = '        last_result = SendResult(success=False, error="No chunks")'
OLD = ANCHOR + """
        for chunk in chunks:
            last_result = await self._send_chunk(chat_id, chunk, reply_to)"""
NEW = MARKER + """
        last_result = SendResult(success=False, error="No chunks")
        previous_chunk = ""
        for chunk in chunks:
            if previous_chunk:
                await asyncio.sleep(min(2.2, 0.6 + len(previous_chunk) * 0.03))
            last_result = await self._send_chunk(chat_id, chunk, reply_to)"""


def candidate_files():
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    files = [home / "hermes-agent" / "plugins" / "platforms" / "qqbot" / "adapter.py"]
    files += sorted(home.glob("installs/*/environments/*/workspace/plugins/platforms/qqbot/adapter.py"))
    return files


def apply_one(path: Path):
    text = path.read_text()
    if MARKER in text:
        return "已修复"
    if OLD not in text:
        return "跳过（该版本无发送循环锚点）"
    backup = path.with_name(path.name + f".bak-pace-{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(path, backup)
    path.write_text(text.replace(OLD, NEW, 1))
    py_compile.compile(str(path), doraise=True)
    return f"已应用（备份 {backup.name}）"


def remove_one(path: Path):
    text = path.read_text()
    if MARKER not in text:
        return "未打补丁"
    backups = sorted(path.parent.glob(path.name + ".bak-pace-*"))
    if not backups:
        return "无备份可恢复"
    shutil.copy2(backups[-1], path)
    return f"已恢复 {backups[-1].name}"


def main(argv):
    files = [p for p in candidate_files() if p.exists()]
    if not files:
        print("未找到 QQ 适配器")
        return 2
    for path in files:
        state = remove_one(path) if "--remove" in argv else apply_one(path)
        print(f"{state}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
