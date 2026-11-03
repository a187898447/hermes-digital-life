"""Human-operated CLI controls for digital-life state."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time

from .runtime import resolve_gap, status_snapshot, stop_state


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    actions = subparser.add_subparsers(dest="digital_life_action", required=True)
    actions.add_parser("status", help="Read current life state")
    actions.add_parser("stop", help="Freeze life-state transitions")
    actions.add_parser("resume", help="Resume life-state transitions")
    actions.add_parser("memory-rebuild", help="Rebuild the event-memory index from daily logs")
    beats = actions.add_parser("beats", help="Enable or pause every proactive beat job")
    beats.add_argument("beats_action", choices=["enable", "disable"])
    resolve = actions.add_parser("resolve-gap", help="Close one observed gap by identifier")
    resolve.add_argument("gap_id")
    subparser.set_defaults(func=run_command)


def run_command(args: argparse.Namespace) -> int:
    action = args.digital_life_action
    now_ms = time.time() * 1000
    if action == "memory-rebuild":
        from . import storage
        from .memory import MemoryStore
        store = MemoryStore(
            storage.data_root(),
            connect=lambda: storage.plugin_db(storage.PLUGIN_NAME, filename="memory-index.db"),
            life_state=lambda: storage.read_state(time.time() * 1000),
        )
        try:
            print(json.dumps(store.rebuild_index(), ensure_ascii=False))
            return 0
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            print(f"Memory rebuild failed: {exc}")
            return 1
    if action == "beats":
        from . import proactive
        changed = proactive.set_beats_enabled(args.beats_action == "enable")
        print(f"{'Enabled' if args.beats_action == 'enable' else 'Paused'} {changed} beat job(s).")
        return 0
    if action == "status":
        print(json.dumps(status_snapshot(now_ms), ensure_ascii=False, indent=2))
        return 0
    if action == "resolve-gap":
        confirmation = input(f"Type RESOLVE to close gap {args.gap_id}: ").strip()
        if confirmation != "RESOLVE":
            print("No state change made.")
            return 1
        resolved = resolve_gap(now_ms, args.gap_id)
        print("Gap resolved." if resolved else "No open gap with that identifier.")
        return 0 if resolved else 1
    confirmation = input(f"Type {action.upper()} to confirm: ").strip()
    if confirmation != action.upper():
        print("No state change made.")
        return 1
    stop_state(now_ms, stopped=(action == "stop"))
    print(f"Digital-life state {'stopped' if action == 'stop' else 'resumed'}.")
    return 0
