"""Transactional profile-scoped persistence through the Hermes plugin storage API."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from plugins.plugin_storage import plugin_db

PLUGIN_NAME = "hermes-lover"
STATE_ID = "life-state"
MAX_STATE_BYTES = 512 * 1024


def data_root() -> Path:
    """Resolve the active profile without creating its plugin data directory."""
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "plugin-data" / PLUGIN_NAME


def read_state(now_ms: float) -> dict[str, Any]:
    """Read life state without schema creation, settlement or initialization writes."""
    from .domain import default_state, normalize_state
    path = data_root() / "data.db"
    if not path.exists():
        return default_state(now_ms)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='life_state'").fetchone():
            return default_state(now_ms)
        row = conn.execute("SELECT payload FROM life_state WHERE id=?", (STATE_ID,)).fetchone()
        return normalize_state(json.loads(row[0]), now_ms) if row else default_state(now_ms)
    finally:
        conn.close()


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
        raise ValueError("hermes-lover state exceeds its storage limit")
    conn.execute(
        "INSERT INTO life_state(id, payload, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
        (STATE_ID, payload, now_ms),
    )
