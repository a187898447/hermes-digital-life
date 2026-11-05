#!/usr/bin/env python3
"""Make the QQ adapter bubble every conversational reply.

The stock adapter only splits "chat-like" blocks: 2-6 lines, every line <=48 chars.
Longer conversational replies are delivered as a wall of text. Hermes Lover's owner
wants conversations always split (one or two sentences per bubble), while reference
dumps (search results, file contents, code, tables) stay whole.

Modes:
  (default)  apply the patch to every discovered adapter file
  --check    exit 0 when patched everywhere, 1 otherwise
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

MARKER = "# hermes-lover: dialogue split"
ANCHOR = "        return cls._qq_split_delivery_units(content) if cls._qq_should_split_short_chat_block(content) else [content]"

INSERT = '''    # hermes-lover: dialogue split — bubble conversations, keep data dumps whole.
    _QQ_DIALOGUE_MAX_CHARS = 60
    _QQ_DIALOGUE_MAX_BUBBLES = 10

    @classmethod
    def _qq_looks_like_reference_block(cls, content: str) -> bool:
        text = content.strip()
        if "```" in text:
            return True
        if len(text) > 1500:
            return True
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return False
        structured = sum(
            1 for line in lines
            if line.lstrip().startswith(("- ", "* ", "#", ">", "|"))
            or line.lstrip()[:2].rstrip(".").isdigit() and line.lstrip()[1:2] == "."
        )
        if structured / len(lines) >= 0.4:
            return True
        labelled = sum(
            1 for line in lines
            if len(line) <= 80 and (line.count(":") + line.count("：")) >= 1
            and line.split(":")[0].split("：")[0].strip().isascii()
        )
        if len(lines) >= 2 and labelled == len(lines):
            return True
        return any(
            key in text
            for key in ("Model:", "Provider:", "Profile:", "Context usage:", "Session:",
                        "Tools:", "Tokens:", "模型：", "会话：", "配置：")
        )

    @classmethod
    def _qq_dialogue_units(cls, content: str) -> list:
        import re as _re
        text = " ".join(part.strip() for part in content.splitlines() if part.strip())
        raw_pieces = [p.strip() for p in _re.split(r"(?<=[。！？!?…~])", text) if p.strip()]
        pieces = []
        limit = cls._QQ_DIALOGUE_MAX_CHARS * 2
        for piece in raw_pieces:
            if len(piece) <= limit:
                pieces.append(piece)
                continue
            buffer = ""
            for chunk in _re.split(r"(?<=[，,、；;：:])", piece):
                while len(chunk) > limit:
                    if buffer:
                        pieces.append(buffer)
                        buffer = ""
                    pieces.append(chunk[:limit])
                    chunk = chunk[limit:]
                if buffer and len(buffer) + len(chunk) > limit:
                    pieces.append(buffer)
                    buffer = chunk
                else:
                    buffer = f"{buffer}{chunk}" if buffer else chunk
            if buffer:
                pieces.append(buffer)
        units = []
        current = ""
        for piece in pieces:
            if current and len(current) + len(piece) > cls._QQ_DIALOGUE_MAX_CHARS:
                units.append(current)
                current = piece
            else:
                current = f"{current}{piece}" if current else piece
        if current:
            units.append(current)
        if len(units) > cls._QQ_DIALOGUE_MAX_BUBBLES:
            total = sum(len(unit) for unit in units)
            budget = max(cls._QQ_DIALOGUE_MAX_CHARS, -(-total // cls._QQ_DIALOGUE_MAX_BUBBLES))
            while True:
                packed = []
                current = ""
                for unit in units:
                    if current and len(current) + len(unit) > budget:
                        packed.append(current)
                        current = unit
                    else:
                        current = f"{current}{unit}" if current else unit
                if current:
                    packed.append(current)
                if len(packed) <= cls._QQ_DIALOGUE_MAX_BUBBLES:
                    break
                budget = budget * 3 // 2 + 1
            units = packed
        return units

'''

BRANCH = '''        if not cls._qq_looks_like_reference_block(content):
            dialogue = cls._qq_dialogue_units(content)
            if len(dialogue) > 1:
                return dialogue
        return cls._qq_split_delivery_units(content) if cls._qq_should_split_short_chat_block(content) else [content]
'''


def candidate_files() -> list[Path]:
    home = Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()
    roots = [
        home / "hermes-agent" / "plugins" / "platforms" / "qqbot" / "adapter.py",
        Path("/usr/local/lib/hermes-agent/plugins/platforms/qqbot/adapter.py"),
    ]
    roots += [Path(p) for p in glob.glob(str(home / "installs" / "*" / "environments" / "*" / "workspace" / "plugins" / "platforms" / "qqbot" / "adapter.py"))]
    seen, out = set(), []
    for path in roots:
        if path.is_file() and str(path) not in seen:
            seen.add(str(path))
            out.append(path)
    return out


def patch_text(text: str):
    if MARKER in text:
        return text
    if ANCHOR not in text:
        return None
    head, sep, tail = text.partition("    @classmethod\n    def _split_text_for_qq_delivery")
    if not sep:
        raise SystemExit("未找到 _split_text_for_qq_delivery 定义")
    text = head + INSERT + sep + tail
    text = text.replace(ANCHOR, BRANCH.strip("\n"), 1)
    return text


def apply_one(path: Path) -> str:
    text = path.read_text()
    if MARKER in text:
        return "已修复"
    if patch_text(text) is None:
        return "跳过（该版本无拆分锚点）"
    backup = path.with_name(path.name + f".bak-{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(path, backup)
    path.write_text(patch_text(text))
    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as exc:
        shutil.copy2(backup, path)
        raise SystemExit(f"补丁后语法错误，已回滚：{exc}")
    return f"已应用（备份 {backup.name}）"


def remove_one(path: Path) -> str:
    text = path.read_text()
    if MARKER not in text:
        return "未打补丁"
    backups = sorted(path.parent.glob(path.name + ".bak-*"))
    if backups:
        shutil.copy2(backups[-1], path)
        return f"已还原 {backups[-1].name}"
    for line in BRANCH.strip("\n").splitlines():
        text = text.replace(line + "\n", "")
    start = text.find(INSERT.splitlines()[0])
    if start != -1:
        end = text.find("    @classmethod\n    def _split_text_for_qq_delivery", start)
        if end > start:
            text = text[:start] + text[end:]
    path.write_text(text)
    return "已移除补丁"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args()
    targets = candidate_files()
    if not targets:
        print("未找到 QQ 适配器文件。")
        return 1
    ok = 0
    for path in targets:
        if args.check:
            body = path.read_text()
            state = "OK" if MARKER in body else ("跳过" if ANCHOR not in body else "未修复")
        elif args.remove:
            state = remove_one(path)
        else:
            state = apply_one(path)
        if state.startswith(("已修复", "OK", "跳过")):
            ok += 1
        print(f"{state}: {path}")
    if args.check:
        print("OK: QQ 对话拆分已生效。" if ok == len(targets) else "需要运行本脚本以应用拆分补丁。")
        return 0 if ok == len(targets) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
