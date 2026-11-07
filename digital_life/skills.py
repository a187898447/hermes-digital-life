"""Hands that get steadier with use: a small proficiency model for the media tools.

The level is data, not a prompt. A novice's picture really is a stick figure: the
generation prompt is padded with a crude style while the level is low, and the
padding lifts as the skill grows. Practice in the daily agenda grants a little
experience without spending a generation.
"""

from __future__ import annotations

from typing import Any

SCHEMA = 1
NAMES = {
    "make_image": "画图",
    "make_video": "拍视频",
}
THRESHOLDS = (0, 30, 90, 200)
LABELS = ("手生", "能看", "像样", "熟练")
GAIN_OK = 10
GAIN_FAIL = 2
GAIN_PRACTICE = 6

# Only the low levels get a style clause: at 像样 and above the prompt is passed
# through as written, so the improvement is visible rather than permanent padding.
STYLES = {
    ("make_image", 0): "风格：简笔画，单色线条，儿童涂鸦，画面粗糙，不要细节、不要阴影、不要写实",
    ("make_image", 1): "风格：简单线稿，平涂上色，细节很少",
    ("make_video", 0): "画面：手机随手拍，固定机位，动作简单，光线平，不要运镜",
    ("make_video", 1): "画面：简单镜头，固定机位，少剪辑",
}


def new_skills() -> dict[str, Any]:
    return {"schema": SCHEMA, "levels": {}}


def normalize_skills(raw: Any) -> dict[str, Any]:
    state = new_skills()
    if not isinstance(raw, dict):
        return state
    levels = raw.get("levels")
    if isinstance(levels, dict):
        for name, entry in levels.items():
            if name in NAMES and isinstance(entry, dict):
                state["levels"][name] = _clean_entry(entry)
    return state


def _clean_entry(entry: dict[str, Any]) -> dict[str, Any]:
    try:
        xp = max(0.0, float(entry.get("xp") or 0.0))
    except (TypeError, ValueError):
        xp = 0.0
    try:
        uses = max(0, int(entry.get("uses") or 0))
    except (TypeError, ValueError):
        uses = 0
    return {"xp": xp, "uses": uses, "level": level_of(xp)}


def level_of(xp: float) -> int:
    level = 0
    for index, floor in enumerate(THRESHOLDS):
        if xp >= floor:
            level = index
    return level


def entry(state: dict[str, Any], name: str) -> dict[str, Any]:
    skills = state.get("skills") if isinstance(state.get("skills"), dict) else {}
    levels = skills.get("levels") if isinstance(skills.get("levels"), dict) else {}
    return _clean_entry(levels.get(name) or {})


def level(state: dict[str, Any], name: str) -> int:
    return int(entry(state, name)["level"])


def label(state: dict[str, Any], name: str) -> str:
    return LABELS[min(level(state, name), len(LABELS) - 1)]


def craft(name: str, current: int, prompt: str) -> str:
    """Padding that makes a low-level generation visibly crude."""
    clause = STYLES.get((name, min(current, 2)))
    text = " ".join(str(prompt or "").split())
    return f"{text}。{clause}" if clause and text else text


def gain(state: dict[str, Any], name: str, *, ok: bool = True, amount: float | None = None) -> dict[str, Any]:
    """Add experience; returns the new entry plus whether a level was crossed."""
    if name not in NAMES:
        return {"name": name, "level": 0, "up": False, "xp": 0.0}
    skills = state.setdefault("skills", new_skills())
    levels = skills.setdefault("levels", {})
    before = level_of(float((levels.get(name) or {}).get("xp") or 0.0))
    add = GAIN_OK if ok else GAIN_FAIL
    if amount is not None:
        add = float(amount)
    current = _clean_entry(levels.get(name) or {})
    current["xp"] = current["xp"] + add
    if ok:
        current["uses"] = current["uses"] + 1
    current["level"] = level_of(current["xp"])
    levels[name] = current
    return {"name": name, "level": current["level"], "up": current["level"] > before, "xp": current["xp"]}


def weakest(state: dict[str, Any], names: list[str] | None = None) -> str:
    pool = [name for name in (names or list(NAMES)) if name in NAMES]
    return min(pool, key=lambda name: (level(state, name), entry(state, name)["xp"]))


def summary(state: dict[str, Any]) -> dict[str, Any]:
    return {name: {"level": level(state, name), "label": label(state, name),
                   "xp": round(entry(state, name)["xp"], 1), "uses": entry(state, name)["uses"]}
            for name in NAMES}


def line(state: dict[str, Any]) -> str:
    """One short line for the system prompt: names and levels only."""
    parts = [f"{NAMES[name]}{LABELS[min(level(state, name), 3)]}" for name in NAMES]
    return "手上的活：" + "、".join(parts)
