"""Background memory maintenance: fold cold entries and old turns on a slow cadence.

Called from ``pre_llm_call`` next to self-heal. It never blocks a reply: the work runs
on a daemon thread, at most once per week, and only touches the palace (archived cold
entries via ``compact`` and stale live turns via ``compact_turns``).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger("hermes-lover.compaction")

INTERVAL_MS = 7 * 24 * 3600 * 1000
COLD_DAYS = 30
TURN_DAYS = 30
_LOCK = threading.Lock()
_RUNNING = False


def state_path() -> Path | None:
    try:
        from plugins.plugin_storage import plugin_data_dir  # noqa: PLC0415

        return Path(plugin_data_dir("hermes-lover")) / "compaction-state.json"
    except Exception:
        return None


def is_due(path: Path, now_ms: float) -> bool:
    try:
        stamp = json.loads(path.read_text("utf-8")).get("at", 0)
        return now_ms - float(stamp) >= INTERVAL_MS
    except (OSError, ValueError, TypeError, AttributeError):
        return True


def run_once(store, now_ms: float) -> dict:
    """Run both folds; returns what happened. Never raises for data problems."""
    result: dict = {"ok": True}
    try:
        result["cold"] = store.compact(days=COLD_DAYS, now_ms=now_ms)
    except Exception as exc:  # noqa: BLE001 - maintenance must not break the chat
        logger.warning("hermes-lover cold compaction failed: %s", exc)
        result["cold_error"] = str(exc)
    try:
        result["turns"] = store.compact_turns(days=TURN_DAYS, now_ms=now_ms)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-lover turn compaction failed: %s", exc)
        result["turns_error"] = str(exc)
    return result


def _worker(store, path: Path, now_ms: float) -> None:
    global _RUNNING
    try:
        result = run_once(store, now_ms)
        try:
            path.write_text(json.dumps({"at": now_ms, "result": result}, ensure_ascii=False), "utf-8")
        except OSError:
            logger.warning("hermes-lover compaction could not record its result", exc_info=True)
    finally:
        with _LOCK:
            _RUNNING = False


def once(store_factory) -> None:
    """Fire one background maintenance pass when the weekly window elapsed."""
    global _RUNNING
    path = state_path()
    if path is None:
        return
    now_ms = time.time() * 1000
    if not is_due(path, now_ms):
        return
    with _LOCK:
        if _RUNNING:
            return
        _RUNNING = True
    try:
        store = store_factory()
        threading.Thread(target=_worker, args=(store, path, now_ms), name="hermes-lover-compaction",
                         daemon=True).start()
    except Exception:
        with _LOCK:
            _RUNNING = False
        logger.warning("hermes-lover compaction could not start", exc_info=True)
