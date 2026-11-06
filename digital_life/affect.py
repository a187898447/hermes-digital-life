"""Derived affect frame.

The chemistry values are the storage layer; this module turns them into the two
things a persona actually needs: a coarse mood label and the *change* since the
previous turn.  Everything here is pure and read-only so it can run inside the
request middleware.
"""

from __future__ import annotations

from typing import Any

HISTORY_CAP = 8
FIELDS = ("dopamine", "cortisol", "serotonin", "energy")
STALE_AFTER_MS = 24 * 60 * 60 * 1000


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _num(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and number not in (float("inf"), float("-inf")) else default


def readings(state: dict[str, Any]) -> dict[str, float]:
    chemistry = state.get("chemistry") or {}
    return {
        "dopamine": _num(chemistry.get("dopamine"), 50.0),
        "cortisol": _num(chemistry.get("cortisol"), 20.0),
        "serotonin": _num(chemistry.get("serotonin"), 50.0),
        "energy": _num(state.get("energy"), 100.0),
    }


def valence(state: dict[str, Any]) -> float:
    """Pleasant (+1) to unpleasant (-1)."""
    r = readings(state)
    raw = 0.45 * (r["dopamine"] - 50.0) / 50.0
    raw += 0.35 * (r["serotonin"] - 50.0) / 50.0
    raw -= 0.50 * (r["cortisol"] - 20.0) / 80.0
    return round(_clamp(raw), 2)


def arousal(state: dict[str, Any]) -> float:
    """Calm (0) to activated (1)."""
    r = readings(state)
    raw = 0.60 * _clamp(r["cortisol"] / 100.0, 0.0, 1.0)
    raw += 0.25 * _clamp(r["dopamine"] / 100.0, 0.0, 1.0)
    raw += 0.15 * _clamp((100.0 - r["energy"]) / 100.0, 0.0, 1.0)
    return round(_clamp(raw, 0.0, 1.0), 2)


def label(state: dict[str, Any]) -> str:
    r = readings(state)
    if r["cortisol"] >= 70:
        return "烦躁"
    if r["energy"] < 25:
        return "疲惫"
    if r["serotonin"] < 35:
        return "低落"
    if r["dopamine"] >= 70 and r["energy"] >= 40:
        return "来劲"
    return "平常"


HINTS = {
    "烦躁": "更冲、更短，少解释。",
    "疲惫": "能少说就少说，能不接就不接。",
    "低落": "冷淡克制，不主动找话。",
    "来劲": "愿意接梗，话可以多一点。",
    "平常": "按平时的节奏。",
}


def hint(state: dict[str, Any]) -> str:
    return HINTS[label(state)]


def _history(state: dict[str, Any]) -> list[dict[str, float]]:
    history = state.get("affect_history")
    if not isinstance(history, list):
        return []
    return [item for item in history if isinstance(item, dict)]


def deltas(state: dict[str, Any]) -> dict[str, float]:
    """Change since the previous recorded snapshot (zeros when there is none)."""
    history = _history(state)
    if not history:
        return {field: 0.0 for field in FIELDS}
    previous = history[-1]
    now = readings(state)
    return {field: round(now[field] - _num(previous.get(field), now[field]), 1) for field in FIELDS}


def trend(state: dict[str, Any], field: str = "energy", points: int = 3) -> list[float]:
    history = _history(state)
    now = readings(state)
    tail = history[-(points - 1):] if points > 1 else []
    values = [_num(item.get(field), now[field]) for item in tail]
    values.append(now[field])
    return [round(value, 1) for value in values[-points:]]


def record(state: dict[str, Any], now_ms: float) -> dict[str, Any] | None:
    """Append the current readings to the bounded history (call before an impulse)."""
    entry = dict(readings(state))
    entry["at"] = float(now_ms)
    history = _history(state) + [entry]
    state["affect_history"] = history[-HISTORY_CAP:]
    return state


def _signed(value: float) -> str:
    if abs(value) < 0.05:
        return "±0"
    text = f"{value:+.1f}"
    return text.replace("-", "−")


def render(state: dict[str, Any], now_ms: float | None = None) -> str:
    r = readings(state)
    change = deltas(state)
    mood = label(state)
    values = trend(state, "energy")
    shape = "样本不足"
    if len(values) >= 3:
        falling = all(a > b for a, b in zip(values, values[1:]))
        rising = all(a < b for a, b in zip(values, values[1:]))
        shape = "连续下降" if falling else ("连续回升" if rising else "有起伏")
    trend_text = "→".join(f"{value:.0f}" for value in values) + f"（{shape}）"

    lines = [
        f"[hermes-lover · 此刻] 心情：{mood}｜愉悦 {valence(state):+.2f} / 紧张 {arousal(state):.2f}",
        f"多巴胺 {r['dopamine']:.1f}（{_signed(change['dopamine'])}）｜皮质醇 {r['cortisol']:.1f}（{_signed(change['cortisol'])}）"
        f"｜血清素 {r['serotonin']:.1f}（{_signed(change['serotonin'])}）｜能量 {r['energy']:.1f}（{_signed(change['energy'])}）",
        f"近三轮能量 {trend_text}",
        f"倾向：{hint(state)}",
    ]
    history = _history(state)
    if now_ms and history:
        last_at = _num(history[-1].get("at"), 0.0)
        if last_at and now_ms - last_at > STALE_AFTER_MS:
            hours = (now_ms - last_at) / 3_600_000
            lines.append(f"（上次对话是 {hours:.0f} 小时前，上面的变化不代表刚才发生了什么）")
    return "\n".join(lines)


def refresh(state: dict[str, Any], now_ms: float) -> dict[str, Any]:
    """Keep one mood stamp next to the numeric history: label, when it started, frame."""
    current = state.get("mood") if isinstance(state.get("mood"), dict) else {}
    next_label = label(state)
    if current.get("label") != next_label:
        current = {"label": next_label, "since": float(now_ms)}
    else:
        current = dict(current)
        current.setdefault("since", float(now_ms))
    current["valence"] = valence(state)
    current["arousal"] = arousal(state)
    state["mood"] = current
    return current


def mood_since(state: dict[str, Any], now_ms: float) -> float:
    """Minutes the current mood has lasted (0 when unknown)."""
    current = state.get("mood") if isinstance(state.get("mood"), dict) else {}
    since = _num(current.get("since"), 0.0)
    return round(max(0.0, now_ms - since) / 60_000.0, 1) if since else 0.0
