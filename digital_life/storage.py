"""Transactional profile-scoped persistence through the Hermes plugin storage API."""

from __future__ import annotations

import json
from typing import Any

from plugins.plugin_storage import plugin_db

PLUGIN_NAME = "digital-life"
STATE_ID = "life-state"
MAX_STATE_BYTES = 512 * 1024


def _connect():
    conn = plugin_db(PLUGIN_NAME)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS life_state (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL)"
    )
    return conn


def load_state(now_ms: float) -> dict[str, Any]:
    from .domain import default_state, normalize_state

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM life_state WHERE id = ?", (STATE_ID,)).fetchone()
        if row is None:
            state = default_state(now_ms)
            _write_unlocked(conn, state, now_ms)
        else:
            state = normalize_state(json.loads(row[0]), now_ms)
        conn.commit()
        return state
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_state(now_ms: float, mutate) -> dict[str, Any]:
    """Serialize one read/modify/write transition across threads and processes."""
    from .domain import default_state, normalize_state

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM life_state WHERE id = ?", (STATE_ID,)).fetchone()
        current = default_state(now_ms) if row is None else normalize_state(json.loads(row[0]), now_ms)
        next_state = mutate(current)
        if next_state is None:
            conn.commit()
            return current
        if not isinstance(next_state, dict):
            raise ValueError("state transition must return an object")
        normalized = normalize_state(next_state, now_ms)
        if normalized.get("emergency_stop"):
            normalized["last_settled_at"] = max(normalized["last_settled_at"], now_ms)
        _write_unlocked(conn, normalized, now_ms)
        conn.commit()
        return normalized
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _write_unlocked(conn, state: dict[str, Any], now_ms: float) -> None:
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_STATE_BYTES:
        raise ValueError("digital-life state exceeds its storage limit")
    conn.execute(
        "INSERT INTO life_state(id, payload, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
        (STATE_ID, payload, now_ms),
    )
