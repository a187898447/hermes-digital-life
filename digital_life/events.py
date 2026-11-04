"""Deterministic, model-free classification of what the user just did.

No network, no model call: a word list plus the message length.  Anything
ambiguous stays unclassified so the chemistry keeps moving only on signals we
can defend in a test.
"""

from __future__ import annotations

import re

MAX_LEN = 120
COOLDOWN_MS = 10 * 60 * 1000
TURN_CAP = 8.0
DAILY_CAP = 40.0

DELTAS = {
    "praise": {"dopamine": 6.0, "serotonin": 2.0, "cortisol": -3.0},
    "insult": {"cortisol": 8.0, "dopamine": -3.0},
}

# Unambiguous abuse anywhere in the line.
_HARD_INSULT = re.compile(r"(傻[逼比]?|废物|闭嘴|破程序|智障|脑残|去死|他妈的|妈的)")
# Everyday words that are only abuse when they stand alone: 「垃圾回收器」和「滚轮」不是骂人。
_BOUNDARY = r"(?:^|[\s，。！？,.!?、…~])"
_SOFT_INSULT = re.compile(
    _BOUNDARY + r"(滚|垃圾|有病|蠢货|蠢)" + r"(?:$|[\s，。！？,.!?、…~])"
)
# Praise words are specific enough to match as substrings.
_PRAISE = re.compile(
    r"(厉害|牛逼|牛啊|真牛|好强|太强|真棒|靠谱|可爱|喜欢你|辛苦|谢谢你|真聪明|高手|绝了)"
)


def classify(text: str) -> str | None:
    """Return ``praise``/``insult`` for a clear signal, else ``None``.

    Messages longer than :data:`MAX_LEN` are never classified: this project's chats
    carry long logs and error dumps, and a whole paragraph of complaint should not
    read as abuse.
    """
    message = (text or "").strip()
    if not message or len(message) > MAX_LEN:
        return None
    if _HARD_INSULT.search(message) or _SOFT_INSULT.search(message):
        return "insult"
    if _PRAISE.search(message):
        return "praise"
    return None


def scaled_deltas(category: str, remaining: float) -> dict[str, float] | None:
    """Scale one event so it neither breaks the per-turn nor the daily cap."""
    base = DELTAS.get(category)
    if not base:
        return None
    magnitude = sum(abs(value) for value in base.values())
    budget = min(TURN_CAP, max(0.0, remaining))
    if budget <= 0:
        return None
    factor = min(1.0, budget / magnitude)
    if factor <= 0:
        return None
    return {key: value * factor for key, value in base.items()}
