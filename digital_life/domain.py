"""Pure domain functions ported from digital-life-core.

Upstream: https://github.com/mashangshui/digital-life-core
Reviewed revision: d984665ff2e452cf9f57d845cfb93bfb9a64b6b1
See the repository LICENSE for attribution and license notices.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from .chemical_pool import ChemicalPool
from .chemistry import (  # re-exported: the public pure-math surface stays importable from domain
    DEFAULT_TEMPERAMENT,
    MINUTE_MS,
    TEMPERAMENT_RANGES,
    clamp,
    decay_toward,
    float_or,
    normalize_temperament,
)
from .genome import DEFAULT_CHEMICALS, parse_genome

DEFAULT_SLEEP_CONFIG = {"recover_per_minute": 0.2, "sleep_recover_per_minute": 1.0, "sleep_energy_threshold": 20.0, "natural_cap": 60.0, "overshoot_decay_per_minute": 0.1, "quiet_hours": (0, 6), "step_minutes": 5}
MAX_SLEEP_STEPS = 2000


def pick_temperature(chemistry: dict[str, Any], energy: float) -> float:
    """Map current chemistry and energy to the task-defined request temperature."""
    # B-class policy: only cortisol-before-dopamine precedence is borrowed from upstream.
    cortisol = float_or(chemistry.get("cortisol"), 20.0)
    dopamine = float_or(chemistry.get("dopamine"), 50.0)
    serotonin = float_or(chemistry.get("serotonin"), 50.0)
    if cortisol >= 80:
        return 1.0
    if dopamine >= 70:
        return 0.85
    temperature = 0.70
    temperature += 0.20 * clamp((cortisol - 40.0) / 40.0, 0.0, 1.0)
    temperature += 0.10 * clamp((70.0 - serotonin) / 35.0, 0.0, 1.0)
    temperature += 0.10 * clamp((dopamine - 50.0) / 20.0, 0.0, 1.0)
    if float_or(energy, 100.0) < 20.0:
        temperature -= 0.15
    return min(1.10, max(0.30, temperature))


_UNKNOWN_RE = re.compile(r"我[^，。；！？\n]{0,4}(?:不知道|不晓得|不清楚|不明白|不确定|没找到|没有找到|查不到|搜不到|没查到|说不准|搞不清|弄不清|无法确定|无从得知)")
_INQUIRY_RE = re.compile(r"我[^，。；！？\n]{0,3}(?:想知道|想查|想弄明白|想搞清楚|想确认|想了解|查一下|查查|查查看)")
_SELF_REFERENTIAL_RE = re.compile(
    r"执念|缺口|心结|内省|唤醒|潜意识|化学池|激素|张力|状态文件|收割|闭环|gap|genome|chemicals|digital_life_", re.I
)
_EXPOSITORY_RE = re.compile(
    r"意味着|差别在于|也就是说|换句话说|这说明|之所以|因此|总的来说|总之|区别在于|某种意义上|某种程度上|另一方面|与此同时|关键在于", re.I
)
_MARKDOWN_RE = re.compile(r"(?:```.*?```|`([^`]*)`|!\[[^]]*\]\([^)]*\)|\[([^]]+)\]\([^)]*\)|[*_~>#])", re.S)


def _clean_gap_candidate(text: str) -> str:
    text = _MARKDOWN_RE.sub(lambda match: match.group(1) or match.group(2) or "", text)
    text = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|[○●✓✔☑]\s*)", "", text)
    return text.strip().strip(" \t\r\n|:：-—")


def _is_meaningful_gap_candidate(text: str) -> bool:
    return len(text) >= 4 and sum(char.isalnum() for char in text) >= 3


def is_unknown_claim(text: str) -> bool:
    return bool(text and _UNKNOWN_RE.search(text))


def is_gap_worthy(sentence: str) -> bool:
    if not sentence or _SELF_REFERENTIAL_RE.search(sentence) or _EXPOSITORY_RE.search(sentence):
        return False
    return is_unknown_claim(sentence) or bool(_INQUIRY_RE.search(sentence))


def find_gap_sentence(text: str, max_len: int = 100) -> str | None:
    """Extract the longest meaningful uncertainty/inquiry sentence, if any."""
    if not text or text.lstrip().startswith("/"):
        return None
    # B-class adaptation: upstream's UNKNOWN_RE/INQUIRY_RE/EXPOSITORY_RE mix English and Chinese.
    # This implementation borrows the extraction/filtering structure but uses task-defined Chinese terms.
    candidates = []
    for raw in re.split(r"[。！？!?\n]+", text):
        raw = raw.strip()
        if not raw:
            continue
        if "|" in raw:
            parts = [_clean_gap_candidate(cell) for cell in raw.split("|")]
            parts = [part for part in parts if _is_meaningful_gap_candidate(part) and is_gap_worthy(part)]
            candidate = max(parts, key=len, default="")
        else:
            candidate = _clean_gap_candidate(raw)
        if not _is_meaningful_gap_candidate(candidate) or not is_gap_worthy(candidate):
            continue
        candidates.append(candidate)
    if not candidates:
        return None
    result = max(candidates, key=len)[:max_len]
    return result.rstrip(" \t，,、；;:：。.!！?？") or None


_CONFIG_TEMPERAMENT: dict[str, float] | None = None
_CONFIG_STRESS = 80.0


def configure(*, temperament: Any = None, stress_cortisol: Any = None) -> None:
    """Apply operator knobs; both are clamped and fall back to the defaults."""
    global _CONFIG_TEMPERAMENT, _CONFIG_STRESS
    _CONFIG_TEMPERAMENT = normalize_temperament(temperament) if isinstance(temperament, dict) else None
    _CONFIG_STRESS = clamp(float_or(stress_cortisol, 80.0), 0.0, 100.0)


def stress_threshold() -> float:
    return _CONFIG_STRESS


def default_state(now_ms: float) -> dict[str, Any]:
    temperament = dict(_CONFIG_TEMPERAMENT or DEFAULT_TEMPERAMENT)
    chemistry = ChemicalPool().initial_chemistry(temperament)
    return {"schema_version": 1, "chemistry": chemistry, "energy": 100.0, "temperament": temperament,
            "last_interaction_at": now_ms, "last_settled_at": now_ms, "gaps": [],
            "emergency_stop": False, "tools_succeeded": [], "last_epiphany_at": 0.0,
            "wake": {"queue": [], "last_wake_at": 0.0, "last_gap_id": "", "active": None},
            "affect_history": []}


def normalize_state(raw: Any, now_ms: float) -> dict[str, Any]:
    state = default_state(now_ms)
    if not isinstance(raw, dict):
        return state
    state["temperament"] = normalize_temperament(raw.get("temperament"))
    state["chemistry"] = ChemicalPool().normalize_chemistry(raw.get("chemistry"), state["temperament"])
    state["energy"] = clamp(float_or(raw.get("energy"), 100.0))
    state["last_interaction_at"] = max(0.0, float_or(raw.get("last_interaction_at"), now_ms))
    state["last_settled_at"] = max(0.0, float_or(raw.get("last_settled_at"), now_ms))
    state["emergency_stop"] = raw.get("emergency_stop") is True
    gaps = raw.get("gaps")
    if isinstance(gaps, list):
        state["gaps"] = [gap for gap in gaps[-200:] if isinstance(gap, dict)]
    succeeded = raw.get("tools_succeeded")
    if isinstance(succeeded, list):
        state["tools_succeeded"] = [str(name)[:60] for name in succeeded[-32:] if isinstance(name, str)]
    state["last_epiphany_at"] = max(0.0, float_or(raw.get("last_epiphany_at"), 0.0))
    history = raw.get("affect_history")
    if isinstance(history, list):
        from .affect import FIELDS, HISTORY_CAP  # noqa: PLC0415 - avoid an import cycle at module load

        cleaned = []
        for item in history[-HISTORY_CAP:]:
            if not isinstance(item, dict) or any(key not in item for key in FIELDS):
                continue
            entry = {key: float_or(item.get(key), 0.0) for key in FIELDS}
            if isinstance(item.get("at"), (int, float)):
                entry["at"] = float(item["at"])
            cleaned.append(entry)
        state["affect_history"] = cleaned
    from . import wake as wake_module  # noqa: PLC0415 - lazy to keep domain import-light

    state["wake"] = wake_module.normalize_wake(raw.get("wake"))
    return state


def decay_chemistry(chemistry: dict[str, float], elapsed_ms: float, temperament: dict[str, float],
                    *, energy: float, high_tension_gap: bool) -> dict[str, float]:
    """Thin compatibility wrapper; the rule engine lives in ``ChemicalPool.decay``."""
    return ChemicalPool().decay(
        {"chemistry": chemistry, "temperament": temperament, "energy": energy},
        0.0, elapsed_ms=elapsed_ms, energy=energy, high_tension_gap=high_tension_gap,
    )


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

STRESS_DIRECTIVE_LIMIT = 400


def stress_directive(cortisol: float) -> str:
    """Short, bounded instruction injected ahead of memory when stress is high (PRD M7.2).

    Stress narrows attention; it must never read as permission to drop or fabricate
    memories, so the directive states both limits explicitly.
    """
    value = float_or(cortisol, 0.0)
    if value < stress_threshold():
        return ""
    text = (
        "Current stress is high. Keep attention on the immediate problem: answer what was asked, "
        "concretely and briefly, and defer optional exploration. Do not invent certainty; if something "
        "is unknown, say so. The memories supplied here are all still present — stress changes their "
        "salience, never their existence, so do not imply anything was lost. Stay calm."
    )
    return text[:STRESS_DIRECTIVE_LIMIT]
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
    if anchor_cortisol is not None and anchor_cortisol >= stress_threshold():
        return "crisis"
    if any(_has_tag(haystack, tag) for tag in DAILY_TAGS):
        return "daily"
    return "neutral"


def attention_weights(cortisol: float) -> dict[str, float]:
    if cortisol >= stress_threshold():
        return {"crisis": CRISIS_WEIGHT, "daily": DAILY_WEIGHT}
    if cortisol <= CALM_CORTISOL:
        return {"crisis": 1.0, "daily": 1.0}
    span = max(1.0, stress_threshold() - CALM_CORTISOL)
    t = max(0.0, min(1.0, (cortisol - CALM_CORTISOL) / span))
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
    last_end = 0
    for match in pattern.finditer(prompt):
        if match.start() < last_end:
            # A header-like line nested inside an entry must not extend the outer span.
            continue
        units = int(match.group("used").replace(",", ""))
        end = _utf16_end(prompt, match.end(), units)
        if end is None:
            continue
        last_end = end
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
