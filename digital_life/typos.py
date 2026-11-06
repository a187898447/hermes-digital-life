"""Cheap, high-precision checks for obvious wrong characters in written memories.

The journal is written by the model itself; a homophone slip ("塔到" for "塌到")
or a leftover placeholder is worth flagging back to the writer, but the palace must
never silently rewrite someone's words. ``find_suspicious`` only reports.
"""

from __future__ import annotations

import re

# (pattern, hint). Kept deliberately small and context-bound: a broad the/得/地
# checker would bury the writer in noise.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"塔[到陷]"), "「塔」多用于建筑，这里应是「塌」或「掉」"),
    (re.compile(r"因该"), "应为「应该」"),
    (re.compile(r"以经"), "应为「已经」"),
    (re.compile(r"在次"), "应为「再次」"),
    (re.compile(r"即然"), "应为「既然」"),
    (re.compile(r"竞然"), "应为「竟然」"),
    (re.compile(r"做为"), "应为「作为」"),
    (re.compile(r"按装"), "应为「安装」"),
    (re.compile(r"的的|了了|是是|我我|你你"), "疑似重复字"),
    (re.compile(r"\[\s*\.\.\.\s*\]|\{[^}\n]{0,20}\}|TODO|待补"), "疑似未写完的占位符"),
)


def find_suspicious(text: str) -> list[dict[str, str]]:
    """Return the suspicious spots in ``text`` without changing it."""
    if not isinstance(text, str) or not text.strip():
        return []
    findings: list[dict[str, str]] = []
    for pattern, hint in _RULES:
        for match in pattern.finditer(text):
            start = max(0, match.start() - 12)
            end = min(len(text), match.end() + 12)
            findings.append({
                "found": match.group(0),
                "hint": hint,
                "context": text[start:end].replace("\n", " "),
            })
    return findings[:5]
