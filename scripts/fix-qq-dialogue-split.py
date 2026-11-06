#!/usr/bin/env python3
"""Refresh the hermes-lover QQ dialogue-split patch.

Bubbles conversational replies (1-2 sentences per bubble, <=60 chars), keeps
quotes 「」『』“”（）、code fences, host notices and reference dumps whole.

  (default)  apply or refresh on every discovered adapter
  --check    exit 0 when patched everywhere
"""

from __future__ import annotations

import argparse
import glob
import py_compile
from pathlib import Path

MARKER = "# hermes-lover: dialogue split"
ANCHOR = "        return cls._qq_split_delivery_units(content) if cls._qq_should_split_short_chat_block(content) else [content]"
BRANCH = """        if not content:
            return []
        if len(content) > max_length:
            return cls.truncate_message(content, max_length)
        if cls._qq_is_host_notice(content):
            return []
        if not cls._qq_looks_like_reference_block(content):
            dialogue = cls._qq_dialogue_units(content)
            if len(dialogue) > 1:
                return dialogue
"""
INSERT = '''# hermes-lover: dialogue split — bubble conversations, keep data dumps whole.
    _QQ_DIALOGUE_MAX_CHARS = 60
    _QQ_DIALOGUE_MAX_BUBBLES = 10

    @classmethod
    def _qq_is_host_notice(cls, content: str) -> bool:
        text = content.strip()
        if not text:
            return False
        if text.startswith(("⏳", "⚠️", "♻️", "🔄", "✅ Gateway")) or text.startswith("Working —"):
            return True
        if "First-time tip" in text or "This notice won't appear again" in text:
            return True
        if text.startswith("(tip)") and "only shows once" in text:
            return True
        if ("Hermes is restarting" in text or "your current task will be interrupted" in text
                or "Hermes is back and ready" in text or "Gateway online" in text):
            return True
        return False

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
        limit = cls._QQ_DIALOGUE_MAX_CHARS
        text = " ".join(part.strip() for part in content.splitlines() if part.strip())
        if not text:
            return [content]
        openers, closers = "「『“（(《`", "」』”）)》`"
        sentences, buf, depth = [], "", 0
        for ch in text:
            buf += ch
            if ch in openers:
                depth += 1
            elif ch in closers:
                depth = max(0, depth - 1)
            elif depth == 0 and ch in "。！？!?":
                sentences.append(buf.strip())
                buf = ""
        if buf.strip():
            sentences.append(buf.strip())
        merged = []
        for sentence in sentences:
            if not sentence:
                continue
            if merged and len(sentence) < 8:
                merged[-1] = merged[-1] + sentence
            else:
                merged.append(sentence)
        pieces = []
        for sentence in merged:
            if len(sentence) <= limit * 2:
                pieces.append(sentence)
                continue
            buf, depth = "", 0
            for ch in sentence:
                buf += ch
                if ch in openers:
                    depth += 1
                elif ch in closers:
                    depth = max(0, depth - 1)
                elif depth == 0 and ch in "，,、；;：:" and len(buf) >= limit:
                    pieces.append(buf.strip())
                    buf = ""
            if buf.strip():
                pieces.append(buf.strip())
        units, current = [], ""
        for piece in pieces:
            if current and len(current) + len(piece) > limit:
                units.append(current)
                current = piece
            else:
                current = f"{current}{piece}" if current else piece
        if current:
            units.append(current)
        if len(units) > 1 and len(units[-1]) < 8:
            units[-2] = units[-2] + units[-1]
            units.pop()
        cap = cls._QQ_DIALOGUE_MAX_BUBBLES
        if len(units) > cap:
            units = units[:cap - 1] + ["".join(units[cap - 1:])]
        return units

'''


def find_targets() -> list[Path]:
    home = Path.home() / ".hermes"
    roots = [home / "hermes-agent" / "plugins" / "platforms" / "qqbot" / "adapter.py",
             Path("/usr/local/lib/hermes-agent/plugins/platforms/qqbot/adapter.py")]
    roots += [Path(p) for p in glob.glob(str(home / "installs" / "*" / "environments" / "*" / "workspace" / "plugins" / "platforms" / "qqbot" / "adapter.py"))]
    seen, out = set(), []
    for path in roots:
        if path.is_file() and str(path) not in seen:
            seen.add(str(path))
            out.append(path)
    return out


def refresh(text: str) -> str:
    lines = text.splitlines(keepends=True)
    try:
        mi = next(i for i, line in enumerate(lines) if MARKER in line)
        si = next(i for i in range(mi, len(lines))
                  if lines[i].strip() == "@classmethod" and "_split_text_for_qq_delivery" in lines[i + 1])
    except (StopIteration, IndexError):
        return text
    return "".join(lines[:mi] + INSERT.splitlines(keepends=True) + lines[si:])


def apply_one(path: Path) -> str:
    text = path.read_text()
    patched = refresh(text) if MARKER in text else None
    if patched is None:
        if ANCHOR not in text:
            return "跳过（未找到锚点）"
        head, sep, tail = text.partition("    @classmethod\n    def _split_text_for_qq_delivery")
        patched = head + INSERT + sep + tail.replace(ANCHOR, BRANCH.strip("\n"), 1)
    if patched == text:
        return "跳过（已是该版本）"
    backup = path.with_suffix(path.suffix + ".before-split")
    if not backup.exists():
        backup.write_text(text)
    path.write_text(patched)
    py_compile.compile(str(path), doraise=True)
    return "已更新"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    targets = find_targets()
    if not targets:
        print("未找到 QQ 适配器")
        return 1
    ok = 0
    for path in targets:
        state = ("OK" if MARKER in path.read_text() else "未打补丁") if args.check else apply_one(path)
        ok += state in ("已更新", "OK", "跳过（已是该版本）")
        print(f"{state}: {path}")
    if args.check:
        return 0 if ok == len(targets) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
