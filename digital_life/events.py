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

_INSULT = re.compile(
    r"(傻[逼比]?|蠢|滚|废物|垃圾|闭嘴|破程序|有病|智障|脑残|蠢货|去死|妈[的比]|他妈的)"
)
_PRAISE = re.compile(
    r"(厉害|牛逼|牛啊|真牛|好强|太强|真棒|靠谱|可爱|喜欢你|辛苦|谢谢你|真聪明|高手|绝了)"
)


def classify(text: str) -> str | None:
    """Return ``praise``/``insult`` for a clear signal, else ``None``."""
    message = (text or "").strip()
    if not message or len(message) > MAX_LEN:
        return None
    if _INSULT.search(message):
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
