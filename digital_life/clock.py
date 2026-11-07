"""The day boundary shared by quotas and the memory diary.

A day starts at 05:00: a chat at 01:00 is still yesterday's evening, and a quota
reset at midnight would hand out a fresh budget in the middle of a conversation.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

DAY_START_HOUR = 5


def day_key(now_ms: float | None = None) -> str:
    """The day a moment belongs to, under the one 05:00 rule."""
    stamp = (now_ms if now_ms is not None else time.time() * 1000) / 1000
    return (datetime.fromtimestamp(stamp) - timedelta(hours=DAY_START_HOUR)).strftime("%Y-%m-%d")
