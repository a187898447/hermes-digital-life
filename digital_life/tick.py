"""The heartbeat: settle the body, walk the day forward, refresh the mood.

A single in-process thread drives every step; the database row is the only place
that remembers when the last step happened, so a reloaded plugin or a second
process cannot double-step the day.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import life
from .chemistry import float_or

LOG = logging.getLogger(__name__)

TICK_SECONDS = 300
MIN_GAP_MS = 60_000

def _storage():
    """Import the host-backed store lazily so importing this module never needs the host."""
    from . import storage as module  # noqa: PLC0415

    return module


_LOCK = threading.Lock()
_STEP_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_ENABLED: Any = None


def set_enabled_probe(probe) -> None:
    """Let the loop skip work when the host config turns the life layer off mid-run."""
    global _ENABLED
    _ENABLED = probe


def once(now_ms: float | None = None, *, force: bool = False) -> dict[str, Any]:
    """One idempotent step: settle → life.step → pulses → mood."""
    if not _STEP_LOCK.acquire(blocking=False):
        return {"ran": False, "reason": "busy"}
    try:
        return _once(now_ms, force=force)
    finally:
        _STEP_LOCK.release()


def _once(now_ms: float | None = None, *, force: bool = False) -> dict[str, Any]:
    now = float(now_ms if now_ms is not None else time.time() * 1000)
    from . import runtime

    state = runtime.settle_state(now)
    if state.get("emergency_stop"):
        return {"ran": False, "reason": "stopped"}
    last = float_or((state.get("life") or {}).get("last_tick_at"), 0.0)
    if not force and now - last < MIN_GAP_MS:
        return {"ran": False, "reason": "recent"}

    done: dict[str, Any] = {}

    def mutate(current: dict[str, Any]) -> dict[str, Any]:
        stepped = life.step(current, now)
        applied = life.apply_pulses(current, stepped["pulses"], now)
        from . import affect

        affect.refresh(current, now)
        from . import skills

        trained = []
        for item in stepped["finished"]:
            if item.get("kind") != "practice":
                continue
            name = str((item.get("source") or {}).get("name") or "")
            if name:
                gained = skills.gain(current, name, amount=skills.GAIN_PRACTICE)
                trained.append({**gained, "label": skills.NAMES.get(name, name)})
        done["trained"] = trained
        from . import gaps

        done["faded"] = gaps.fade_stale(current, now)
        current["life"]["last_tick_at"] = now
        done.update(stepped)
        done["pulses_applied"] = applied
        return current

    fresh = _storage().update_state(now, mutate)
    for trace in done.get("traces") or []:
        life.remember(trace)
    return {"ran": True, "reason": "tick", "life": life.summary(fresh, now),
            "mood": dict(fresh.get("mood") or {}), **done}


def start(interval_seconds: int = TICK_SECONDS) -> bool:
    """Start the background loop once; returns True when this call started it."""
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return False
        _STOP.clear()
        thread = threading.Thread(target=_loop, args=(max(30, int(interval_seconds)),),
                                  name="hermes-lover-tick", daemon=True)
        _THREAD = thread
        thread.start()
        return True


def stop(timeout: float = 2.0) -> None:
    global _THREAD
    with _LOCK:
        _STOP.set()
        thread = _THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout)
    with _LOCK:
        if _THREAD is thread:
            _THREAD = None


def running() -> bool:
    with _LOCK:
        return _THREAD is not None and _THREAD.is_alive()


def _enabled() -> bool:
    probe = _ENABLED
    try:
        return True if probe is None else bool(probe())
    except Exception:  # noqa: BLE001 - a broken probe must not stop the heartbeat
        return True


def _loop(interval: int) -> None:
    # Step once right away so a fresh plugin does not wait a whole interval.
    if _enabled():
        try:
            once()
        except Exception:  # noqa: BLE001 - the heartbeat must never kill the gateway
            LOG.exception("hermes-lover tick failed")
    while not _STOP.wait(interval):
        if not _enabled():
            continue
        try:
            once()
        except Exception:  # noqa: BLE001
            LOG.exception("hermes-lover tick failed")
