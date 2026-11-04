"""Pure chemical math, translated from digital-life-core ``src/chemistry-core.ts``.

Upstream: https://github.com/mashangshui/digital-life-core
Reviewed revision: d984665ff2e452cf9f57d845cfb93bfb9a64b6b1

Everything in this module is a pure function: no state, no I/O, no timers. The
small floating-point helpers here are the reference vectors that the rest of the
domain (``genome``, ``chemical_pool``, ``domain``) builds on.
"""

from __future__ import annotations

import math
from typing import Any

DEFAULT_TEMPERAMENT = {"sensitivity": 1.0, "resilience": 1.0, "enthusiasm": 50.0, "metabolism": 1.0}
TEMPERAMENT_RANGES = {"sensitivity": (0.5, 2.0), "resilience": (0.5, 2.0), "enthusiasm": (20.0, 80.0), "metabolism": (0.5, 2.0)}
MINUTE_MS = 60_000.0


def clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    if not math.isfinite(value):
        return minimum
    return min(maximum, max(minimum, value))


def decay_toward(current: float, baseline: float, elapsed_ms: float, half_life_ms: float) -> float:
    """Lazy exponential decay toward ``baseline``; identical to upstream ``decayToward``.

    ``NaN`` snaps back to the baseline instead of poisoning the pool, non-positive
    elapsed time (or a non-positive half-life) is a clamp-only no-op.
    """
    if not math.isfinite(current):
        return baseline
    if elapsed_ms <= 0 or half_life_ms <= 0:
        return clamp(current)
    return clamp(baseline + (current - baseline) * 0.5 ** (elapsed_ms / half_life_ms))


def normalize_temperament(raw: Any) -> dict[str, float]:
    """Clamp the four temperament knobs; resilience never drops below 0.1."""
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


def float_or(value: Any, fallback: float) -> float:
    """Best-effort float coercion used by every tolerant parser in this package."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def apply_metabolism(energy: float, metabolism: float, cost: float = 0.05) -> float:
    """Charge one request against the energy pool, scaled by ``temperament.metabolism``."""
    return clamp(energy - cost * max(0.1, float_or(metabolism, 1.0)))
