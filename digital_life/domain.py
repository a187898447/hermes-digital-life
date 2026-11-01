"""Pure domain functions ported from digital-life-core.

Upstream: https://github.com/mashangshui/digital-life-core
Reviewed revision: d984665ff2e452cf9f57d845cfb93bfb9a64b6b1
See the repository LICENSE for attribution and license notices.
"""

from __future__ import annotations

import math
import re
import json
from datetime import datetime
from typing import Any

DEFAULT_TEMPERAMENT = {"sensitivity": 1.0, "resilience": 1.0, "enthusiasm": 50.0, "metabolism": 1.0}
TEMPERAMENT_RANGES = {"sensitivity": (0.5, 2.0), "resilience": (0.5, 2.0), "enthusiasm": (20.0, 80.0), "metabolism": (0.5, 2.0)}
DEFAULT_CHEMICALS = {
    "dopamine": {"baseline": 50.0, "initial": 50.0, "half_life_minutes": 45.0},
    "cortisol": {"baseline": 20.0, "initial": 20.0, "half_life_minutes": 30.0},
    "serotonin": {"baseline": 50.0, "initial": 50.0, "half_life_minutes": 120.0},
}
DEFAULT_SLEEP_CONFIG = {"recover_per_minute": 0.2, "sleep_recover_per_minute": 1.0, "sleep_energy_threshold": 20.0, "natural_cap": 60.0, "overshoot_decay_per_minute": 0.1, "quiet_hours": (0, 6), "step_minutes": 5}
MAX_SLEEP_STEPS = 2000
MINUTE_MS = 60_000.0


def clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    if not math.isfinite(value):
        return minimum
    return min(maximum, max(minimum, value))


def decay_toward(current: float, baseline: float, elapsed_ms: float, half_life_ms: float) -> float:
    if not math.isfinite(current):
        return baseline
    if elapsed_ms <= 0 or half_life_ms <= 0:
        return clamp(current)
    return clamp(baseline + (current - baseline) * 0.5 ** (elapsed_ms / half_life_ms))


def normalize_temperament(raw: Any) -> dict[str, float]:
    source = raw if isinstance(raw, dict) else {}
    result = {}
    for key, (minimum, maximum) in TEMPERAMENT_RANGES.items():
        try:
            value = float(source.get(key))
        except (TypeError, ValueError):
            value = DEFAULT_TEMPERAMENT[key]
        if not math.isfinite(value):
            value = DEFAULT_TEMPERAMENT[key]
        result[key] = clamp(value, minimum, maximum)
    result["resilience"] = max(0.1, result["resilience"])
    return result


def parse_genome(text: str) -> tuple[dict[str, Any], list[str]]:
    """Parse adopted DLC genome fields without allowing malformed input to disable defaults."""
    problems: list[str] = []
    try:
        raw = json.loads(text)
    except (TypeError, ValueError) as exc:
        return {"version": 1, "chemicals": dict(DEFAULT_CHEMICALS), "temperament": dict(DEFAULT_TEMPERAMENT)}, [f"invalid JSON: {exc}"]
    if not isinstance(raw, dict):
        return {"version": 1, "chemicals": dict(DEFAULT_CHEMICALS), "temperament": dict(DEFAULT_TEMPERAMENT)}, ["genome root must be an object"]
    chemicals = raw.get("chemicals")
    parsed = {}
    if isinstance(chemicals, list):
        for item in chemicals:
            if not isinstance(item, dict) or item.get("id") not in DEFAULT_CHEMICALS:
                continue
            key = item["id"]
            base = DEFAULT_CHEMICALS[key]
            parsed[key] = {
                **base,
                "baseline": clamp(float_or(item.get("baseline"), base["baseline"])),
                "initial": clamp(float_or(item.get("initial"), item.get("baseline", base["initial"]))),
                "half_life_minutes": max(0.1, float_or(item.get("halfLifeMinutes"), base["half_life_minutes"])),
            }
    else:
        problems.append("chemicals must be a list")
    missing = set(DEFAULT_CHEMICALS) - set(parsed)
    if missing:
        problems.append("missing core chemicals; defaults restored: " + ", ".join(sorted(missing)))
        for key in missing:
            parsed[key] = dict(DEFAULT_CHEMICALS[key])
    return {
        "version": int(float_or(raw.get("version"), 1)),
        "chemicals": parsed,
        "temperament": normalize_temperament(raw.get("temperament")),
    }, problems


def float_or(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def default_state(now_ms: float) -> dict[str, Any]:
    temperament = dict(DEFAULT_TEMPERAMENT)
    chemistry = {key: value["initial"] for key, value in DEFAULT_CHEMICALS.items()}
    chemistry["dopamine"] = temperament["enthusiasm"]
    return {"schema_version": 1, "chemistry": chemistry, "energy": 100.0, "temperament": temperament,
            "last_interaction_at": now_ms, "last_settled_at": now_ms, "gaps": [], "commitment": None,
            "emergency_stop": False, "wake": {"last_wake_at": None, "pending": None}}


def normalize_state(raw: Any, now_ms: float) -> dict[str, Any]:
    state = default_state(now_ms)
    if not isinstance(raw, dict):
        return state
    state["temperament"] = normalize_temperament(raw.get("temperament"))
    chemistry = raw.get("chemistry")
    state["chemistry"]["dopamine"] = state["temperament"]["enthusiasm"]
    if isinstance(chemistry, dict):
        for key, spec in DEFAULT_CHEMICALS.items():
            baseline = state["temperament"]["enthusiasm"] if key == "dopamine" else spec["initial"]
            state["chemistry"][key] = clamp(float_or(chemistry.get(key), baseline))
    state["energy"] = clamp(float_or(raw.get("energy"), 100.0))
    state["last_interaction_at"] = max(0.0, float_or(raw.get("last_interaction_at"), now_ms))
    state["last_settled_at"] = max(0.0, float_or(raw.get("last_settled_at"), now_ms))
    state["emergency_stop"] = raw.get("emergency_stop") is True
    state["commitment"] = raw.get("commitment") if isinstance(raw.get("commitment"), dict) else None
    wake = raw.get("wake")
    if isinstance(wake, dict):
        state["wake"] = {
            "last_wake_at": float_or(wake.get("last_wake_at"), 0.0) or None,
            "pending": wake.get("pending") if isinstance(wake.get("pending"), dict) else None,
        }
    gaps = raw.get("gaps")
    if isinstance(gaps, list):
        state["gaps"] = [gap for gap in gaps[-200:] if isinstance(gap, dict)]
    return state


def decay_chemistry(chemistry: dict[str, float], elapsed_ms: float, temperament: dict[str, float],
                    *, energy: float, high_tension_gap: bool) -> dict[str, float]:
    if elapsed_ms <= 0:
        return {key: clamp(value) for key, value in chemistry.items()}
    resilience = max(0.1, float_or(temperament.get("resilience"), 1.0))
    relaxed = {}
    for key, spec in DEFAULT_CHEMICALS.items():
        half_life = max(0.1, spec["half_life_minutes"] / resilience) * MINUTE_MS
        baseline = temperament.get("enthusiasm", spec["baseline"]) if key == "dopamine" else spec["baseline"]
        relaxed[key] = decay_toward(float_or(chemistry.get(key), spec["initial"]), baseline, elapsed_ms, half_life)
    minutes = elapsed_ms / MINUTE_MS
    if relaxed["cortisol"] >= 70:
        relaxed["serotonin"] = clamp(relaxed["serotonin"] - 1.5 * minutes)
    elif energy > 80 and not high_tension_gap and chemistry.get("serotonin", 50.0) < 100:
        relaxed["serotonin"] = clamp(min(100.0, relaxed["serotonin"] + 0.8 * minutes))
    return relaxed


def is_quiet_hour(timestamp_ms: float, quiet_hours: tuple[int, int] | None) -> bool:
    if quiet_hours is None:
        return False
    start, end = quiet_hours
    if start == end:
        return False
    hour = datetime.fromtimestamp(timestamp_ms / 1000).hour
    return start <= hour < end if start < end else hour >= start or hour < end


def settle_rest(energy: float, max_energy: float, elapsed_ms: float, now_ms: float, frozen: bool,
                config: dict[str, Any] | None = None, *, is_quiet_hour_fn=None) -> dict[str, Any]:
    cfg = {**DEFAULT_SLEEP_CONFIG, **(config or {})}
    cap = min(max_energy, max(0.0, float_or(cfg["natural_cap"], 60.0)))
    threshold = max(0.0, float_or(cfg["sleep_energy_threshold"], 20.0))
    if frozen:
        return {"energy": energy, "sleep_ms": 0, "mode": "frozen"}
    energy = max(0.0, min(max_energy, energy))
    elapsed = max(0.0, elapsed_ms)
    step_ms = max(1.0, float_or(cfg["step_minutes"], 5.0)) * MINUTE_MS
    if elapsed:
        step_ms = max(step_ms, elapsed / MAX_SLEEP_STEPS)
    sleep_ms = 0.0
    saw_deep = saw_night = False
    consumed = 0.0
    while consumed < elapsed:
        duration = min(step_ms, elapsed - consumed)
        at = now_ms - elapsed + consumed
        quiet = is_quiet_hour_fn(at) if is_quiet_hour_fn else is_quiet_hour(at, cfg["quiet_hours"])
        deep = energy < threshold
        if deep:
            saw_deep = True
            sleep_ms += duration
        elif quiet:
            saw_night = True
        minutes = duration / MINUTE_MS
        if energy >= cap:
            energy = max(cap, energy - float_or(cfg["overshoot_decay_per_minute"], 0.1) * minutes)
        else:
            rate = cfg["sleep_recover_per_minute"] if deep or quiet else cfg["recover_per_minute"]
            energy = min(cap, energy + float_or(rate, 0.2) * minutes)
        consumed += duration
    energy = max(0.0, min(max_energy, energy))
    mode = "deep-sleep" if saw_deep else "night-rest" if saw_night else "sated" if energy >= cap else "awake"
    return {"energy": energy, "sleep_ms": round(sleep_ms), "mode": mode}


STRESS_CORTISOL = 80
CALM_CORTISOL = 40
CRISIS_WEIGHT = 3.0
DAILY_WEIGHT = 0.1
RANK_DECAY = 0.9
ANXIOUS_TAGS = ["焦虑", "不安", "anxiety", "anxious", "stress", "危机"]
JOYFUL_TAGS = ["开心", "愉快", "喜悦", "joy", "happy", "delight"]
CRISIS_TAGS = ["危机", "危险", "濒死", "报错", "失败", "错误", "异常", "崩溃", "告警", "事故", "故障", "紧张", "失控", "警告", "失灵", "救", "逃", "焦虑", "压力", "崩了", "crisis", "danger", "error", "fail", "failed", "failure", "crash", "alert", "critical", "incident", "bug", "timeout", "exception", "emergency", "fatal"]
DAILY_TAGS = ["日常", "开心", "愉快", "快乐", "喜悦", "美食", "吃喝", "吃饭", "大餐", "闲聊", "八卦", "趣事", "放松", "散步", "玩笑", "奶茶", "咖啡", "happy", "joy", "joyful", "fun", "casual", "chat", "delight", "relax", "dinner", "lunch"]


def _has_tag(haystack: str, tag: str) -> bool:
    needle = tag.lower()
    if not needle:
        return False
    if re.fullmatch(r"[a-z0-9 _-]+", needle):
        return re.search(rf"(?:^|[^a-z0-9]){re.escape(needle)}(?:[^a-z0-9]|$)", haystack) is not None
    return needle in haystack


def classify_attention(entry: str, tags: list[str], anchor_cortisol: float | None = None) -> str:
    haystack = f"{entry} {' '.join(tags)}".lower()
    if any(_has_tag(haystack, tag) for tag in CRISIS_TAGS):
        return "crisis"
    if anchor_cortisol is not None and anchor_cortisol >= STRESS_CORTISOL:
        return "crisis"
    if any(_has_tag(haystack, tag) for tag in DAILY_TAGS):
        return "daily"
    return "neutral"


def attention_weights(cortisol: float) -> dict[str, float]:
    if cortisol >= STRESS_CORTISOL:
        return {"crisis": CRISIS_WEIGHT, "daily": DAILY_WEIGHT}
    if cortisol <= CALM_CORTISOL:
        return {"crisis": 1.0, "daily": 1.0}
    t = max(0.0, min(1.0, (cortisol - CALM_CORTISOL) / (STRESS_CORTISOL - CALM_CORTISOL)))
    return {"crisis": 1.0 + (CRISIS_WEIGHT - 1.0) * t, "daily": 1.0 + (DAILY_WEIGHT - 1.0) * t}


def anchor_key(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = re.sub(r"^(?:[-*•]+|\d+[.)])\s*", "", cleaned)
    return cleaned.lower()[:120]


def rank_memories(entries: list[str], cortisol: float, dopamine: float = 50.0,
                  *, anchors: dict[str, dict[str, Any]] | None = None) -> list[str]:
    anchors = anchors or {}
    weights = attention_weights(cortisol)
    scored = []
    for index, entry in enumerate(entries):
        anchor = anchors.get(anchor_key(entry), {})
        kind = classify_attention(entry, anchor.get("tags", []), anchor.get("cortisol"))
        haystack = f"{entry} {' '.join(anchor.get('tags', []))}".lower()
        affinity = 0
        if cortisol >= 70 and any(_has_tag(haystack, tag) for tag in ANXIOUS_TAGS):
            affinity += 10
        if dopamine >= 70 and any(_has_tag(haystack, tag) for tag in JOYFUL_TAGS):
            affinity += 10
        resonance = 1.0 + 0.02 * affinity
        score = RANK_DECAY ** index * weights.get(kind, 1.0) * resonance
        scored.append((-score, index, entry))
    return [entry for _weight, _index, entry in sorted(scored)]


def reorder_formatted_memory_blocks(prompt: str, cortisol: float, dopamine: float) -> tuple[str, int]:
    """Reorder rendered Hermes memory blocks without dropping any stored entry."""
    pattern = re.compile(
        r"═{46}\n(?:MEMORY \(your personal notes\)|USER PROFILE \(who the user is\)) "
        r"\[[^\]\n]*? (?P<used>[0-9,]+)/[0-9,]+ chars\]\n═{46}\n"
    )
    spans = []
    for match in pattern.finditer(prompt):
        units = int(match.group("used").replace(",", ""))
        end = _utf16_end(prompt, match.end(), units)
        if end is None:
            continue
        body = prompt[match.end():end]
        entries = body.split("\n§\n")
        ranked = rank_memories(entries, cortisol, dopamine)
        if len(ranked) == len(entries):
            spans.append((match.end(), end, "\n§\n".join(ranked)))
    for start, end, replacement in reversed(spans):
        prompt = prompt[:start] + replacement + prompt[end:]
    return prompt, len(spans)


def _utf16_end(text: str, start: int, code_units: int) -> int | None:
    consumed = 0
    index = start
    while index < len(text) and consumed < code_units:
        consumed += 2 if ord(text[index]) > 0xFFFF else 1
        index += 1
    return index if consumed == code_units else None
