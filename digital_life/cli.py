"""Human-operated CLI controls for digital-life state."""

from __future__ import annotations

import argparse
import json
import time

from .runtime import resolve_gap, status_snapshot, stop_state


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    actions = subparser.add_subparsers(dest="digital_life_action", required=True)
    actions.add_parser("status", help="Read current life state")
    actions.add_parser("stop", help="Freeze life-state transitions")
    actions.add_parser("resume", help="Resume life-state transitions")
    resolve = actions.add_parser("resolve-gap", help="Close one observed gap by identifier")
    resolve.add_argument("gap_id")
    subparser.set_defaults(func=run_command)


def run_command(args: argparse.Namespace) -> int:
    action = args.digital_life_action
    now_ms = time.time() * 1000
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
