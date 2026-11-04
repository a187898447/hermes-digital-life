"""Generic chemical pool: lazy decay, genome rules, and internal hormone delivery.

Adapted from digital-life-core ``src/chemical-pool.ts``. The pool owns no clock and
no storage: ``decay`` is computed from elapsed time, ``apply`` maps an observed
lifecycle event to the rule deltas declared in the genome, and ``inflict`` is the
internal equivalent of upstream ``ctx.inflict`` for hormones produced inside the
plugin. None of these are exposed as model-callable tools.
"""

from __future__ import annotations

import copy
from typing import Any

from .chemistry import MINUTE_MS, clamp, decay_toward, float_or
from .genome import default_genome

_MISSING = object()


def _condition_matches(condition: dict[str, Any], relaxed: dict[str, float], raw: dict[str, Any],
                       energy: float, high_tension_gap: bool) -> bool:
    key = str(condition.get("key") or "")
    op = condition.get("op")
    if key.startswith("raw."):
        name = key[4:]
        lhs = raw.get(name, relaxed.get(name))
    elif key == "energy":
        lhs = energy
    elif key == "high_tension_gap":
        lhs = high_tension_gap
    else:
        lhs = relaxed.get(key)
    rhs = condition.get("value")
    if lhs is _MISSING or lhs is None:
        return False
    if op in ("==", "!="):
        equal = lhs == rhs if isinstance(rhs, bool) else float_or(lhs, 0.0) == float_or(rhs, 0.0)
        return equal if op == "==" else not equal
    number, expected = float_or(lhs, 0.0), float_or(rhs, 0.0)
    if op == ">":
        return number > expected
    if op == ">=":
        return number >= expected
    if op == "<":
        return number < expected
    if op == "<=":
        return number <= expected
    return False


class ChemicalPool:
    """One configured pool; ``genome`` defaults to the shipped declaration."""

    def __init__(self, genome: dict[str, Any] | None = None):
        self.genome = copy.deepcopy(genome) if genome else default_genome()
        if "chemicals" not in self.genome:
            self.genome = default_genome()

    @property
    def chemicals(self) -> dict[str, dict[str, Any]]:
        return self.genome["chemicals"]

    def _spec(self, chemical: str) -> dict[str, Any] | None:
        spec = self.chemicals.get(chemical)
        return spec if isinstance(spec, dict) else None

    def clamp_chemical(self, chemical: str, value: float) -> float:
        spec = self._spec(chemical)
        if spec is None:
            return clamp(value)
        return clamp(float_or(value, spec["baseline"]), float_or(spec.get("min"), 0.0),
                     float_or(spec.get("max"), 100.0))

    def initial_chemistry(self, temperament: dict[str, Any] | None = None) -> dict[str, float]:
        enthusiasm = float_or((temperament or {}).get("enthusiasm"), self.chemicals["dopamine"]["baseline"])
        result = {}
        for key, spec in self.chemicals.items():
            baseline = enthusiasm if key == "dopamine" else spec["initial"]
            result[key] = self.clamp_chemical(key, baseline)
        return result

    def normalize_chemistry(self, raw: Any, temperament: dict[str, Any] | None = None) -> dict[str, float]:
        source = raw if isinstance(raw, dict) else {}
        enthusiasm = float_or((temperament or {}).get("enthusiasm"), self.chemicals["dopamine"]["baseline"])
        result = {}
        for key, spec in self.chemicals.items():
            fallback = enthusiasm if key == "dopamine" else spec["initial"]
            result[key] = self.clamp_chemical(key, float_or(source.get(key), fallback))
        return result

    def decay(self, state: dict[str, Any], now_ms: float = 0.0, *, elapsed_ms: float | None = None,
              energy: float | None = None, high_tension_gap: bool | None = None) -> dict[str, float]:
        """Return ``state``'s chemistry after lazy decay and genome drift rules.

        ``state`` is only read; the caller owns persistence and ``last_settled_at``.
        """
        raw = state.get("chemistry") if isinstance(state.get("chemistry"), dict) else {}
        temperament = state.get("temperament") if isinstance(state.get("temperament"), dict) else {}
        if elapsed_ms is None:
            settled_at = float_or(state.get("last_settled_at"), float_or(now_ms, 0.0))
            elapsed_ms = max(0.0, float_or(now_ms, settled_at) - settled_at)
        elapsed_ms = max(0.0, float_or(elapsed_ms, 0.0))
        if energy is None:
            energy = float_or(state.get("energy"), 100.0)
        if high_tension_gap is None:
            high_tension_gap = any(
                isinstance(gap, dict) and gap.get("status") == "open" and float_or(gap.get("tension"), 0.0) >= 70
                for gap in state.get("gaps") or []
            )
        if elapsed_ms <= 0:
            return {key: self.clamp_chemical(key, float_or(raw.get(key), spec["initial"]))
                    for key, spec in self.chemicals.items()}
        resilience = max(0.1, float_or(temperament.get("resilience"), 1.0))
        relaxed: dict[str, float] = {}
        for key, spec in self.chemicals.items():
            half_life = max(0.1, float_or(spec.get("half_life_minutes"), 30.0) / resilience) * MINUTE_MS
            baseline = float_or(temperament.get("enthusiasm"), spec["baseline"]) if key == "dopamine" else spec["baseline"]
            relaxed[key] = decay_toward(
                float_or(raw.get(key), spec["initial"]), baseline, elapsed_ms, half_life,
            )
        minutes = elapsed_ms / MINUTE_MS
        drifted: set[str] = set()
        for rule in self.genome.get("drift_rules") or []:
            chemical = rule.get("chemical")
            if chemical in drifted or chemical not in relaxed:
                continue
            conditions = rule.get("when") or []
            if not all(_condition_matches(c, relaxed, raw, float_or(energy, 100.0), bool(high_tension_gap))
                       for c in conditions):
                continue
            relaxed[chemical] = self.clamp_chemical(
                chemical, relaxed[chemical] + float_or(rule.get("per_minute"), 0.0) * minutes,
            )
            drifted.add(chemical)
        return relaxed

    def apply(self, event: Any, state: dict[str, Any]) -> dict[str, Any]:
        """Apply every genome rule matching ``event`` to ``state`` in place."""
        name = event if isinstance(event, str) else str((event or {}).get("type") or "")
        if not name:
            return state
        chemistry = state.get("chemistry")
        if not isinstance(chemistry, dict):
            chemistry = self.initial_chemistry(state.get("temperament") if isinstance(state.get("temperament"), dict) else None)
            state["chemistry"] = chemistry
        temperament = state.get("temperament") if isinstance(state.get("temperament"), dict) else {}
        for key, spec in self.chemicals.items():
            for rule in spec.get("rules") or []:
                if rule.get("event") != name:
                    continue
                scale = rule.get("scale")
                factor = 1.0
                if scale == "sensitivity":
                    factor = float_or(temperament.get("sensitivity"), 1.0)
                elif scale == "metabolism":
                    factor = float_or(temperament.get("metabolism"), 1.0)
                chemistry[key] = self.clamp_chemical(
                    key, float_or(chemistry.get(key), spec["initial"]) + float_or(rule.get("delta"), 0.0) * factor,
                )
        for rule in self.genome.get("energy_rules") or []:
            if rule.get("event") != name:
                continue
            factor = float_or(temperament.get("metabolism"), 1.0) if rule.get("scale") == "metabolism" else 1.0
            state["energy"] = clamp(
                float_or(state.get("energy"), 100.0) + float_or(rule.get("delta"), 0.0) * factor,
            )
        return state

    def inflict(self, state: dict[str, Any], chemical: str, amount: float) -> float | None:
        """Internal-only hormone delivery (upstream ``ctx.inflict`` semantics)."""
        spec = self._spec(chemical)
        if spec is None:
            return None
        chemistry = state.get("chemistry")
        if not isinstance(chemistry, dict):
            chemistry = self.initial_chemistry(state.get("temperament") if isinstance(state.get("temperament"), dict) else None)
            state["chemistry"] = chemistry
        value = self.clamp_chemical(chemical, float_or(chemistry.get(chemical), spec["initial"]) + float_or(amount, 0.0))
        chemistry[chemical] = value
        return value
