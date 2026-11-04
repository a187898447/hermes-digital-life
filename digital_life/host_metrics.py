"""Best-effort host vitals for the affect section (PRD M9.2).

The host machine is the body: CPU pressure, load, memory and free disk. Every
probe is optional — a missing ``psutil`` or a failing syscall degrades to the
subset that still works, and any unexpected error yields ``{}`` so a turn is
never blocked by telemetry.
"""

from __future__ import annotations

import os
import shutil
from typing import Any


def _percent(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return round(max(0.0, min(100.0, number)), 1)


_LAST_CPU: tuple[float, float] | None = None


def _cpu_percent(psutil) -> float | None:
    """Idle-to-busy ratio between two calls; None on the first call instead of a fake 0%."""
    global _LAST_CPU
    times = psutil.cpu_times()
    idle = float(times.idle)
    total = float(sum(times))
    previous, _LAST_CPU = _LAST_CPU, (idle, total)
    if previous is None or total <= previous[1]:
        return None
    busy = 1.0 - (idle - previous[0]) / (total - previous[1])
    return _percent(100.0 * busy)


def snapshot() -> dict[str, float]:
    """Return ``{cpu, load1, mem_used_pct, disk_free_pct}`` for whatever can be read."""
    data: dict[str, float] = {}
    try:
        try:
            load = os.getloadavg()[0]
            data["load1"] = round(max(0.0, float(load)), 2)
        except (OSError, AttributeError):
            pass
        try:
            usage = shutil.disk_usage("/")
            if usage.total:
                data["disk_free_pct"] = _percent(100.0 * usage.free / usage.total) or 0.0
        except OSError:
            pass
        try:
            import psutil
        except Exception:  # noqa: BLE001 - an optional body probe must never break a turn
            return data
        try:
            cpu = _cpu_percent(psutil)
            if cpu is not None:
                data["cpu"] = cpu
        except Exception:  # noqa: BLE001
            pass
        try:
            data["mem_used_pct"] = _percent(psutil.virtual_memory().percent)
        except Exception:  # noqa: BLE001
            pass
        return data
    except Exception:  # noqa: BLE001 - telemetry never raises into the request path
        return {}


def body_line(data: dict[str, float] | None = None) -> str:
    """One compact line (<=160 chars) describing the host body, or ``""``."""
    values = snapshot() if data is None else data
    if not isinstance(values, dict) or not values:
        return ""
    parts = [f"cpu {values['cpu']:.0f}%" if "cpu" in values else "",
             f"load1 {values['load1']:.1f}" if "load1" in values else "",
             f"mem {values['mem_used_pct']:.0f}%" if "mem_used_pct" in values else "",
             f"disk-free {values['disk_free_pct']:.0f}%" if "disk_free_pct" in values else ""]
    rendered = ", ".join(part for part in parts if part)
    if not rendered:
        return ""
    return f"Body (host vitals): {rendered}."[:160]
