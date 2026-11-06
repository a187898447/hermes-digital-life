"""Cheap deterministic checks for "does this read like a person said it".

The gate is deliberately conservative: it flags shapes that are hard to read
(sentence fragments, abstract-noun piles, dangling connectors, mixed scripts)
and leaves ordinary short chat alone.
"""

from __future__ import annotations

import re

_HAN = r"\u4e00-\u9fff"
_CONNECTORS = ("因为", "所以", "但是", "可是", "不过", "而且", "然后", "如果", "虽然", "于是")
_ABSTRACT = ("性", "化", "度", "感", "力")
_REPEAT = re.compile(r"(的的|了了|我我|是是|就就)")
_DANGLING_TAIL = re.compile(rf"(?:{'|'.join(_CONNECTORS)})[。！？!?…~]*$")
_LEAD_PRONOUN = re.compile(rf"^(它|这|那|其)[{_HAN}]{{1,5}}[。！？!?…~]*$")
_BACKSTAGE = re.compile(r"(工具额度|工具调用|本轮|下一轮|后台|进程|日志|参数|超了|建起来)")
_COLON_LIST = re.compile(r"[：:]")


def lint(lines: list[str]) -> list[tuple[int, str]]:
    """Return (index, reason) for bubbles that are likely hard to parse."""
    issues: list[tuple[int, str]] = []
    for index, raw in enumerate(lines):
        line = (raw or "").strip()
        if not line:
            continue
        body = re.sub(r"[\s。！？!?…~,，;；:：]+", "", line)
        if len(body) > 55 and not re.search(r"[，,。！？!?；;：:]", line):
            issues.append((index, "long-without-punctuation"))
        if len(re.findall(r"的", line)) >= 6 and len(body) > 30:
            issues.append((index, "too-many-de"))
        if _REPEAT.search(line):
            issues.append((index, "repeated-character"))
        if _DANGLING_TAIL.search(line):
            issues.append((index, "dangling-connector"))
        if _LEAD_PRONOUN.match(line):
            issues.append((index, "unclear-pronoun"))
        if _BACKSTAGE.search(line):
            issues.append((index, "backstage-talk"))
        if _COLON_LIST.search(line):
            tail = _COLON_LIST.split(line, 1)[1]
            if tail.count("、") >= 2 and len(tail) > 12:
                issues.append((index, "colon-list"))
        kinds = sum(1 for word in _ABSTRACT if word in line)
        total = sum(line.count(word) for word in _ABSTRACT)
        if kinds >= 2 and total >= 3 and len(body) > 16:
            issues.append((index, "abstract-noun-pile"))
    return issues


def needs_polish(text: str, splitter) -> bool:
    lines = [part for part in splitter(text).split("\n") if part.strip()]
    return bool(lines) and bool(lint(lines))


POLISH_PROMPT = """\
你在替一个 QQ 群友改口。下面几条消息意思对，但读起来像机器写的，或者意思绕。
要求：
1. 每条都改成正常中文口语，读一遍能听懂，不绕、不端着。
2. 保持原意和原说话人的口气，不增加信息，不解释，不客套。
3. 只输出 {count} 条，每条一行，用单独一行 --- 分隔；不要编号、不要 Markdown、不要引号。
4. 改不动就原样保留那一行。
5. 不要旁白、不要自评、不要“我改完了”这类话，除了改写结果什么都不要输出。

消息：
{body}
"""


def parse_polish(output: str, expected: int) -> list[str] | None:
    """Split the rewrite back into bubbles; None when the shape is unusable."""
    parts = [part.strip() for part in re.split(r"\n\s*---\s*\n", (output or "").strip()) if part.strip()]
    if not parts:
        return None
    if len(parts) < expected:
        return None
    parts = parts[:expected]
    if any(len(part) > 220 for part in parts):
        return None
    return parts
