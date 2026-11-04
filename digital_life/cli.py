"""Human-operated CLI controls for digital-life state."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time

from .runtime import resolve_gap, status_snapshot, stop_state


def format_core(snap: dict) -> str:
    """Plain-text view of the life state, shared by the CLI and the /core chat command."""
    chem = snap.get("chemistry") or {}
    gaps = snap.get("gaps") or []
    updated = time.strftime("%Y-%m-%d %H:%M",
                            time.localtime((snap.get("updated_at") or time.time() * 1000) / 1000))
    tail = f"（最近：{str(gaps[-1].get('summary', ''))[:40]}）" if gaps else ""
    from . import affect
    change = affect.deltas(snap)
    mood = affect.label(snap)
    hint = affect.hint(snap)
    return "\n".join([
        "digital-life-core 状态",
        f"  情绪   多巴胺 {float(chem.get('dopamine', 0)):.1f}  皮质醇 {float(chem.get('cortisol', 0)):.1f}  血清素 {float(chem.get('serotonin', 0)):.1f}",
        f"  能量   {float(snap.get('energy', 0)):.1f}/100",
        f"  心情   {mood}（愉悦 {affect.valence(snap):+.2f} / 紧张 {affect.arousal(snap):.2f}）",
        f"  变化   多巴胺 {change['dopamine']:+.1f}  皮质醇 {change['cortisol']:+.1f}  血清素 {change['serotonin']:+.1f}  能量 {change['energy']:+.1f}",
        f"  倾向   {hint}",
        f"  未解问题 {len(gaps)}{tail}",
        f"  紧急停止 {'是' if snap.get('emergency_stop') else '否'}",
        f"  最近更新 {updated}",
    ])


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    actions = subparser.add_subparsers(dest="digital_life_action", required=True)
    actions.add_parser("core", help="Show the current life-state values in plain text")
    actions.add_parser("status", help="Read current life state as JSON")
    actions.add_parser("stop", help="Freeze life-state transitions")
    actions.add_parser("resume", help="Resume life-state transitions")
    actions.add_parser("memory-rebuild", help="Rebuild the event-memory index from daily logs")
    compact = actions.add_parser("memory-compact", help="Fold old archived memories into cold summaries")
    compact.add_argument("--days", type=int, default=30,
                         help="Only compact entries unused for this many days (default: 30)")
    beats = actions.add_parser("beats", help="Enable or pause every proactive beat job")
    beats.add_argument("beats_action", choices=["enable", "disable"])
    resolve = actions.add_parser("resolve-gap", help="Close one observed gap by identifier")
    resolve.add_argument("gap_id")
    wake = actions.add_parser("wake", help="Manually wake for one open gap (opt-in self-wake demo)")
    wake.add_argument("--gap", default="", help="Gap identifier; defaults to the highest tension")
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
    if action == "memory-compact":
        from . import storage
        from .memory import MemoryStore
        store = MemoryStore(
            storage.data_root(),
            connect=lambda: storage.plugin_db(storage.PLUGIN_NAME, filename="memory-index.db"),
            life_state=lambda: storage.read_state(time.time() * 1000),
        )
        try:
            print(json.dumps(store.compact(args.days), ensure_ascii=False))
            return 0
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            print(f"Memory compaction failed: {exc}")
            return 1
    if action == "beats":
        from . import proactive
        changed = proactive.set_beats_enabled(args.beats_action == "enable")
        print(f"{'Enabled' if args.beats_action == 'enable' else 'Paused'} {changed} beat job(s).")
        return 0
    if action == "core":
        print(format_core(status_snapshot(now_ms)))
        return 0
    if action == "status":
        print(json.dumps(status_snapshot(now_ms), ensure_ascii=False, indent=2))
        return 0
    if action == "wake":
        from . import storage, wake as wake_module
        triggered: dict[str, object] = {}

        def mutate(state):
            triggered["active"] = wake_module.force_active(state, now_ms, gap_id=args.gap or None)

        storage.update_state(now_ms, mutate)
        active = triggered.get("active")
        if not isinstance(active, dict):
            print("No open gap to wake for.")
            return 1
        fresh = storage.read_state(now_ms)
        print(json.dumps({"active": active,
                          "section": wake_module.section_text(fresh, now_ms)}, ensure_ascii=False, indent=2))
        print("The digital-life:wake block is now part of the next turn's system prompt.")
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
