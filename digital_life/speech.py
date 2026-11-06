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
# Tics are not readability problems, so they need their own shapes: a receipt,
# a settlement, a filler opener, a service offer, or a rhetorical tail.
_RECEIPT = re.compile(r"(?:我|规则|要求|约定|这条|你说的|指令)?(?:记着|记下了|记住了|收着|收下了|收着了|已收到|已归档)")
_SETTLE = re.compile(r"(这账(?:就)?平了|算我输|认栽|就这么定了|成交|一笔勾销|结案)")
_FILLER = re.compile(r"^(?:说实话|说真的|其实|老实说|不过|反正|倒是|总的来说|讲道理)")
_SERVICE = re.compile(r"^(?:需要我|要不要我|有什么可以|随时(?:找我|说)|希望能|祝你|加油)")
_RHETORICAL = re.compile(r"(要不要|是不是|对吧|懂了吗|行不行|好不好)[。！？!?…~]*$")


def tics(line: str) -> list[str]:
    """Named AI-flavour shapes in one bubble (empty when clean)."""
    found: list[str] = []
    if _RECEIPT.search(line):
        found.append("receipt")
    if _SETTLE.search(line):
        found.append("settlement")
    if _FILLER.search(line):
        found.append("filler-opener")
    if _SERVICE.search(line):
        found.append("service-offer")
    if _RHETORICAL.search(line):
        found.append("rhetorical-tail")
    return found


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
        for kind in tics(line):
            issues.append((index, kind))
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
4. 去掉“我记着/记下了/收着了/已收到”这种回收到的话：记住就直接记住，不要说在记。
5. 去掉“说实话/其实/反正/不过”这种垫场词，去掉“不错（吧）？”“要不要我…？”这种反问收尾。
6. 改不动就原样保留那一行。
7. 不要旁白、不要自评、不要“我改完了”这类话，除了改写结果什么都不要输出。

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
