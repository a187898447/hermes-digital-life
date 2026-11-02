#!/usr/bin/env python3
"""Build the daily digest of yesterday and store the model-written diary.

The diary is a first-level index over the dated event library: a later
question about a day hits the journal first, and only details it leaves out
need a second lookup in that day's raw entries.

Usage:
  hermes-lover-journal.py --digest            # yesterday's digest for the model
  hermes-lover-journal.py --store FILE        # store diary FILE as yesterday's journal
  hermes-lover-journal.py --digest --date 2026-11-01
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROFILE_HOME = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "hermes-lover"


def plugin_root() -> Path:
    candidates = [Path(__file__).resolve().parents[1] / "plugins" / PLUGIN_NAME]
    for path in candidates:
        if (path / "digital_life" / "memory.py").exists():
            return path
    raise SystemExit("找不到插件目录：hermes-lover 未安装在这个 profile 里")


def yesterday(today: str | None = None) -> str:
    if today:
        return today
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def store():
    plugin = plugin_root()
    sys.path.insert(0, str(plugin))
    from digital_life.memory import MemoryStore  # noqa: PLC0415

    root = PROFILE_HOME / "plugin-data" / PLUGIN_NAME
    index = root / "memory-index.db"

    def life_state():
        try:
            from digital_life import storage  # noqa: PLC0415

            return storage.read_state(time.time() * 1000)
        except Exception:
            pass
        try:
            db = sqlite3.connect(f"file:{root / 'data.db'}?mode=ro", uri=True)
            try:
                row = db.execute("SELECT payload FROM life_state WHERE id = 'life-state'").fetchone()
            finally:
                db.close()
            return json.loads(row[0]) if row else {}
        except Exception:
            return {"emergency_stop": False, "chemistry": {"cortisol": 20}}

    return MemoryStore(root, connect=lambda: sqlite3.connect(index), life_state=life_state)


def show_digest(day: str) -> int:
    memory = store()
    entries = memory.read_entries()
    rows = sorted((e for e in entries.values() if e.get("date") == day and not e.get("archived")),
                  key=lambda e: e["created_at"])
    state = memory.life_state()
    chemistry = state.get("chemistry", {}) or {}
    print(f"日期：{day}")
    print(f"当天记忆条数：{len(rows)}")
    print(f"状态：dopamine={chemistry.get('dopamine', '?')} cortisol={chemistry.get('cortisol', '?')} "
          f"serotonin={chemistry.get('serotonin', '?')} energy={state.get('energy', '?')}")
    journals = [e for e in rows if e["kind"] == "journal"]
    if journals:
        print("\n已有日记：")
        for entry in journals:
            print(f"- {entry['text']}")
    print("\n当天记录：")
    for entry in rows:
        if entry["kind"] == "journal":
            continue
        text = " ".join(entry["text"].split())
        print(f"- [{entry['id']}] {text[:160]}")
    return 0


def store_journal(day: str, text: str) -> int:
    text = text.strip()
    if not text:
        raise SystemExit("日记为空")
    prefix = f"【日记 {day}】"
    if not text.startswith(prefix):
        text = prefix + text
    # Anchor the entry to the day it describes, so a later "那天呢" lookup finds it.
    anchor = datetime.strptime(day, "%Y-%m-%d").replace(hour=12).timestamp() * 1000
    result = store().write(text, kind="journal", weight=2.5, session_id="journal", now_ms=anchor)
    print(result)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--digest", action="store_true", help="print yesterday's digest")
    parser.add_argument("--store", metavar="FILE", help="store the diary text from FILE")
    parser.add_argument("--date", help="override the day (YYYY-MM-DD)")
    args = parser.parse_args()
    day = yesterday(args.date)
    if args.store:
        return store_journal(day, Path(args.store).read_text(encoding="utf-8"))
    return show_digest(day)


if __name__ == "__main__":
    raise SystemExit(main())
