#!/usr/bin/env python3
"""Monitor gate for hermes-lover proactive beats.

The cron job runs every five minutes between 09:00 and 21:00 and calls this
script before the agent. The script prints an unchanged line on almost every
tick, so Hermes suppresses the agent run; when one of the day's jittered slots
arrives it prints ``FIRE <SLOT>`` once, which changes the monitor output and
makes Hermes run the agent exactly once for that slot.

Slots are 10:00, 14:00, 16:00 and 20:00 local time, each shifted by a
deterministic random offset of ±60 minutes per day (seeded by the date, so the
same day always yields the same plan even across restarts).

Debug: pass ``--at 2026-11-02T10:30`` to evaluate as if it were that moment.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

SLOTS = (10, 14, 16, 20)
SLOT_NAMES = {10: "MORNING", 14: "MIDDAY", 16: "AFTERNOON", 20: "NIGHT"}
JITTER_MINUTES = 60
SCAN_START, SCAN_END = 9, 21  # inclusive hours the cron schedule covers


def hermes_home() -> Path:
    """The profile home that owns this script: <home>/scripts/hermes-lover-beat.py."""
    return Path(__file__).resolve().parents[1]


def state_path() -> Path:
    return hermes_home() / "cron" / "hermes-lover-beat.json"


def load_state() -> dict:
    try:
        data = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False))
    tmp.replace(path)


def plan_for(day: date) -> list[tuple[datetime, str]]:
    rng = random.Random(day.toordinal())
    plan = []
    for hour in SLOTS:
        shifted = datetime(day.year, day.month, day.day, hour) + timedelta(
            minutes=rng.randint(-JITTER_MINUTES, JITTER_MINUTES)
        )
        # Keep every slot inside the window the cron schedule actually covers.
        shifted = max(shifted, datetime(day.year, day.month, day.day, SCAN_START, 0))
        shifted = min(shifted, datetime(day.year, day.month, day.day, SCAN_END, 0))
        plan.append((shifted, SLOT_NAMES[hour]))
    return sorted(plan)


def render(state: dict, now: datetime) -> str:
    today = now.date().isoformat()
    if state.get("date") != today:
        state["date"] = today
        state["fired"] = []

    fired = state.setdefault("fired", [])
    due = [(when, name) for when, name in plan_for(now.date()) if now >= when and name not in fired]
    if due:
        # Missed several slots while asleep: fire once, for the latest one, so a
        # long outage does not turn into a burst of messages.
        latest = max(due, key=lambda item: item[0])
        for _when, name in due:
            fired.append(name)
        state["payload"] = f"FIRE {latest[1]}"
    save_state(state)
    return str(state.get("payload") or "")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--at", help="evaluate as if now were this local time (YYYY-MM-DDTHH:MM)")
    args = parser.parse_args(argv)

    now = datetime.fromisoformat(args.at) if args.at else datetime.now()
    state = load_state()
    sys.stdout.write(render(state, now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
