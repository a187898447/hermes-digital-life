"""Passive health check: run the doctor at most once per process and per hour.

Called from ``pre_llm_call`` so drift (a disabled plugin, a reverted host patch)
is noticed on the next conversation — but noticed, not silently undone. Repair is
an explicit operator action (``hermes-lover doctor --fix``); an unattended turn
must not rewrite tool sets or patches the operator may have removed on purpose.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger("hermes-lover.selfheal")

INTERVAL_MS = 3600 * 1000
_LOCK = threading.Lock()
_STARTED = False


def state_path() -> Path | None:
    try:
        from plugins.plugin_storage import plugin_data_dir  # noqa: PLC0415

        return Path(plugin_data_dir("hermes-lover")) / "doctor-selfheal.json"
    except Exception:
        return None


def is_due(path: Path, now_ms: float) -> bool:
    try:
        stamp = json.loads(path.read_text("utf-8")).get("at", 0)
        return now_ms - float(stamp) >= INTERVAL_MS
    except (OSError, ValueError, TypeError, AttributeError):
        return True


def _record(path: Path, now_ms: float, code: int, pending: list[str]) -> None:
    try:
        path.write_text(
            json.dumps({"at": now_ms, "ok": code == 0, "pending": pending}, ensure_ascii=False),
            "utf-8",
        )
    except OSError:
        logger.warning("hermes-lover self-heal could not record its result", exc_info=True)


def worker(path: Path, now_ms: float) -> None:
    """Run one read-only doctor pass; failures are logged and never raised."""
    try:
        from . import doctor  # noqa: PLC0415

        chunks: list[str] = []
        code = doctor.run(fix=False, as_json=True, out=chunks.append)
        pending: list[str] = []
        if chunks:
            try:
                checks = json.loads(chunks[-1]).get("checks") or []
                pending = [str(check.get("key")) for check in checks
                           if isinstance(check, dict) and check.get("status") == "FIX"]
            except (ValueError, AttributeError, TypeError):
                pending = []
        if pending:
            logger.info("hermes-lover self-check found items to fix: %s", ", ".join(pending))
        _record(path, now_ms, code, pending)
    except Exception:
        logger.warning("hermes-lover self-heal failed", exc_info=True)


def once() -> None:
    """Start at most one background repair per process and per hour."""
    global _STARTED
    path = state_path()
    if path is None:
        return
    now_ms = time.time() * 1000
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
        if not is_due(path, now_ms):
            return
    threading.Thread(
        target=worker,
        args=(path, now_ms),
        name="hermes-lover-selfheal",
        daemon=True,
    ).start()
