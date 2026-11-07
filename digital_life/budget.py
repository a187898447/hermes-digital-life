"""Per-day counters for actions a chat could otherwise repeat without limit.

The palace has its own entry quota inside ``memory.py``; this module covers the two
things that sit outside it: ingesting new sources (network) and capturing turns
(every reply writes one). Counters live in one small JSON file because losing a count
is harmless, and a lost update across processes only ever costs one extra action.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()


def state_path() -> Path | None:
    try:
        from plugins.plugin_storage import plugin_data_dir  # noqa: PLC0415

        return Path(plugin_data_dir("hermes-lover")) / "budget.json"
    except Exception:
        return None


def _today(now_ms: float | None = None) -> str:
    stamp = (now_ms if now_ms is not None else time.time() * 1000) / 1000
    return time.strftime("%Y-%m-%d", time.localtime(stamp))


def _read(path: Path, day: str) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {"day": day, "counts": {}}
    if not isinstance(data, dict) or data.get("day") != day or not isinstance(data.get("counts"), dict):
        return {"day": day, "counts": {}}
    return data


def refund(name: str, *, now_ms: float | None = None) -> None:
    """Give one unit back after the action failed (best effort)."""
    path = state_path()
    if path is None:
        return
    day = _today(now_ms)
    with _LOCK:
        data = _read(path, day)
        try:
            count = int(data["counts"].get(name, 0))
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            return
        data["counts"][name] = count - 1
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
            os.replace(tmp, path)
        except OSError:
            pass


def used(name: str, *, now_ms: float | None = None) -> int:
    path = state_path()
    if path is None:
        return 0
    data = _read(path, _today(now_ms))
    try:
        return int(data["counts"].get(name, 0))
    except (TypeError, ValueError):
        return 0


def spend(name: str, cap: int, *, now_ms: float | None = None) -> bool:
    """Count one use of ``name`` today; False when ``cap`` is already reached.

    Fails open when there is no storage: this guards soft quotas (ingesting sources),
    where losing the count only re-permits a harmless action. Metered external calls
    must use :func:`spend_all`, which fails closed.
    """
    path = state_path()
    if path is None:
        return True
    day = _today(now_ms)
    with _LOCK:
        data = _read(path, day)
        try:
            count = int(data["counts"].get(name, 0))
        except (TypeError, ValueError):
            count = 0
        if count >= cap:
            return False
        data["counts"][name] = count + 1
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
            os.replace(tmp, path)
        except OSError:
            return True
        return True


def spend_all(limits: dict[str, int], *, now_ms: float | None = None) -> str | None:
    """Count one use of every counter at once; return the name of the first at its cap.

    All-or-nothing so a profile ceiling and a per-chat ceiling advance together.
    Fails closed: when the counters cannot be read or written, return "storage"
    instead of letting a paid call through unmetered.
    """
    path = state_path()
    if path is None:
        return "storage"
    day = _today(now_ms)
    with _LOCK:
        data = _read(path, day)
        counts = data["counts"]
        for name, cap in limits.items():
            try:
                count = int(counts.get(name, 0))
            except (TypeError, ValueError):
                count = 0
            if count >= cap:
                return name
        for name in limits:
            try:
                count = int(counts.get(name, 0))
            except (TypeError, ValueError):
                count = 0
            counts[name] = count + 1
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
            os.replace(tmp, path)
        except OSError:
            return "storage"
    return None
