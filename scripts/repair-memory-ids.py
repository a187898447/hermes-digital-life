#!/usr/bin/env python3
"""Renumber duplicate memory event ids in the dated event log.

Duplicate ids happen when two writers race the same day sequence; the replay
then drops every duplicate add ("duplicate id"), so those turns vanish from
recall even though the raw JSONL still has them. This script keeps the first
occurrence of an id and renumbers the later ones to the next free sequence,
preserving file order.

    python3 repair-memory-ids.py ~/.hermes/profiles/p/plugin-data/p/memory --dry-run
    python3 repair-memory-ids.py ... --apply
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path


def repair_file(path: Path, apply: bool) -> tuple[int, int]:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    events: list[dict | None] = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            events.append(None)

    seen: set[str] = set()
    next_seq: int = 0
    changed = 0
    for raw, event in zip(lines, events):
        if not isinstance(event, dict):
            continue
        identifier = event.get("id")
        if not isinstance(identifier, str):
            continue
        if event.get("op") == "add":
            if identifier not in seen:
                seen.add(identifier)
                continue
            changed += 1
            while True:
                next_seq += 1
                candidate = f"hp-{identifier.split('-')[1]}-{next_seq:04d}"
                if candidate not in seen:
                    break
            event["id"] = candidate
            seen.add(candidate)

    if not changed:
        return 0, len(seen)
    if not apply:
        return changed, len(seen)

    backup = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
    shutil.copy2(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for event, line in zip(events, lines):
            handle.write(line if event is None else json.dumps(event, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return changed, len(seen)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--apply", action="store_true", default=False)
    parser.add_argument("--dry-run", dest="apply", action="store_false")
    args = parser.parse_args(argv)

    if not args.root.is_dir():
        print(f"not a directory: {args.root}", file=sys.stderr)
        return 2

    total = 0
    for path in sorted(args.root.rglob("*.jsonl")):
        changed, ids = repair_file(path, args.apply)
        if changed:
            total += changed
            print(f"{path.name}: {changed} duplicate id(s), {ids} unique")
    if not total:
        print("no duplicate ids found")
    elif not args.apply:
        print(f"dry run: {total} duplicate id(s); pass --apply to rewrite")
    else:
        print(f"rewrote {total} duplicate id(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
