"""Hormone genome: declared baselines, bounds, half-lives, and event rules.

Translated from digital-life-core ``src/genome.ts`` + ``genome.json``. The genome is
declarative so the chemical pool stays generic; it never carries live state.
``parse_genome`` degrades field by field and never raises, so one bad value cannot
disable the defaults.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from .chemistry import DEFAULT_TEMPERAMENT, clamp, float_or, normalize_temperament

# Core hormones. ``min``/``max`` are the pool bounds, ``rules`` are event deltas.
DEFAULT_CHEMICALS: dict[str, dict[str, Any]] = {
    "dopamine": {
        "baseline": 50.0, "initial": 50.0, "min": 0.0, "max": 100.0, "half_life_minutes": 45.0,
        "rules": [{"event": "turn_complete", "delta": 5.0}],
    },
    "cortisol": {
        "baseline": 20.0, "initial": 20.0, "min": 0.0, "max": 100.0, "half_life_minutes": 30.0,
        "rules": [{"event": "provider_error", "delta": 20.0, "scale": "sensitivity"}],
    },
    "serotonin": {
        "baseline": 50.0, "initial": 50.0, "min": 0.0, "max": 100.0, "half_life_minutes": 120.0,
        "rules": [],
    },
}

# One request costs 0.05 energy per unit of metabolism (PRD M6.3 / applyMetabolism).
DEFAULT_ENERGY_RULES: list[dict[str, Any]] = [{"event": "request", "delta": -0.05, "scale": "metabolism"}]

# Upstream ``driveSerotonin``: sustained cortisol drains serotonin; a sated, unstressed
# life slowly restores it, but only below 100. The first matching rule per chemical wins.
DEFAULT_DRIFT_RULES: list[dict[str, Any]] = [
    {
        "chemical": "serotonin",
        "per_minute": -1.5,
        "when": [{"key": "cortisol", "op": ">=", "value": 70.0}],
    },
    {
        "chemical": "serotonin",
        "per_minute": 0.8,
        "when": [
            {"key": "energy", "op": ">", "value": 80.0},
            {"key": "high_tension_gap", "op": "==", "value": False},
            {"key": "raw.serotonin", "op": "<", "value": 100.0},
        ],
    },
]

_SCALES = {"sensitivity", "metabolism"}
_OPS = {">", ">=", "<", "<=", "==", "!="}
_STATE_KEYS = {"energy", "high_tension_gap"}


def default_genome() -> dict[str, Any]:
    return {
        "version": 1,
        "chemicals": copy.deepcopy(DEFAULT_CHEMICALS),
        "energy_rules": copy.deepcopy(DEFAULT_ENERGY_RULES),
        "drift_rules": copy.deepcopy(DEFAULT_DRIFT_RULES),
        "temperament": dict(DEFAULT_TEMPERAMENT),
    }


def _parse_rule(raw: Any, chemical: str, problems: list[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("event"), str) or not raw["event"].strip():
        problems.append(f"{chemical}: dropped rule without a valid event")
        return None
    rule = {"event": raw["event"].strip(), "delta": float_or(raw.get("delta"), 0.0)}
    scale = raw.get("scale")
    if scale in _SCALES:
        rule["scale"] = scale
    elif scale is not None:
        problems.append(f"{chemical}: ignored unknown rule scale {scale!r}")
    return rule


def _parse_drift(raw: Any, problems: list[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("chemical"), str):
        problems.append("dropped drift rule without a chemical")
        return None
    if raw["chemical"] not in DEFAULT_CHEMICALS:
        problems.append(f"dropped drift rule for unknown chemical {raw['chemical']!r}")
        return None
    when = raw.get("when")
    conditions = []
    if isinstance(when, list):
        for condition in when:
            if not isinstance(condition, dict) or condition.get("op") not in _OPS:
                problems.append(f"dropped malformed drift condition for {raw['chemical']}")
                continue
            conditions.append({"key": str(condition.get("key", "")), "op": condition["op"],
                               "value": condition.get("value")})
    elif when is not None:
        problems.append(f"ignored non-list drift conditions for {raw['chemical']}")
    return {"chemical": raw["chemical"], "per_minute": float_or(raw.get("per_minute"), 0.0),
            "when": conditions}


def parse_genome(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Parse a genome document; malformed fields fall back to defaults per field."""
    problems: list[str] = []
    genome = default_genome()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            return genome, [f"invalid JSON: {exc}"]
    if not isinstance(raw, dict):
        return genome, ["genome root must be an object"]

    genome["version"] = int(float_or(raw.get("version"), 1))
    genome["temperament"] = normalize_temperament(raw.get("temperament"))

    chemicals = raw.get("chemicals")
    parsed: dict[str, dict[str, Any]] = {}
    if isinstance(chemicals, list):
        for item in chemicals:
            if not isinstance(item, dict) or item.get("id") not in DEFAULT_CHEMICALS:
                continue
            key = item["id"]
            base = DEFAULT_CHEMICALS[key]
            minimum = clamp(float_or(item.get("min"), base["min"]), 0.0, 100.0)
            maximum = clamp(float_or(item.get("max"), base["max"]), minimum, 100.0)
            rules = item.get("rules")
            parsed_rules: list[dict[str, Any]] = []
            if rules is None:
                parsed_rules = copy.deepcopy(base["rules"])
            elif not isinstance(rules, list):
                problems.append(f"{key}: rules must be a list")
            else:
                for rule in rules:
                    parsed_rule = _parse_rule(rule, key, problems)
                    if parsed_rule:
                        parsed_rules.append(parsed_rule)
            baseline_value = float_or(item.get("baseline"), base["baseline"])
            parsed[key] = {
                "baseline": clamp(baseline_value, minimum, maximum),
                "initial": clamp(float_or(item.get("initial"), baseline_value), minimum, maximum),
                "min": minimum,
                "max": maximum,
                "half_life_minutes": max(0.1, float_or(item.get("halfLifeMinutes"), base["half_life_minutes"])),
                "rules": parsed_rules,
            }
    else:
        problems.append("chemicals must be a list")
    missing = set(DEFAULT_CHEMICALS) - set(parsed)
    if missing:
        problems.append("missing core chemicals; defaults restored: " + ", ".join(sorted(missing)))
        for key in missing:
            parsed[key] = copy.deepcopy(DEFAULT_CHEMICALS[key])
    genome["chemicals"] = {key: parsed[key] for key in DEFAULT_CHEMICALS}

    energy_rules = raw.get("energy_rules")
    if isinstance(energy_rules, list):
        parsed_energy = []
        for rule in energy_rules:
            parsed_rule = _parse_rule(rule, "energy", problems)
            if parsed_rule:
                parsed_energy.append(parsed_rule)
        genome["energy_rules"] = parsed_energy
    elif energy_rules is not None:
        problems.append("energy_rules must be a list")

    drift_rules = raw.get("drift_rules")
    if isinstance(drift_rules, list):
        genome["drift_rules"] = [rule for rule in (_parse_drift(item, problems) for item in drift_rules) if rule]
    elif drift_rules is not None:
        problems.append("drift_rules must be a list")
    return genome, problems
