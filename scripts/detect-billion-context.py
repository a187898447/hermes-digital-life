#!/usr/bin/env python3
"""Detect whether billion-context (bili) is installed for this Hermes home.

Read-only and silent: it never installs, never starts a proxy, and always
prints one JSON object. A missing plugin or an unreadable install degrades to
``{"installed": false, "version": null}`` — billion-context is optional and
Hermes keeps working with its built-in compaction when it is absent.

Usage:
    python3 scripts/detect-billion-context.py [--pretty]
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import sys
from pathlib import Path

PLUGIN_ID = "billion-context"


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _manifest_version(directory: Path) -> str | None:
    for name in ("plugin.yaml", "plugin.json"):
        path = directory / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if name.endswith(".json"):
            try:
                data = json.loads(text)
            except (ValueError, TypeError):
                continue
            version = data.get("version") if isinstance(data, dict) else None
            return str(version) if version else None
        for line in text.splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "version":
                return value.strip().strip('"').strip("'") or None
    return None


def _plugin_dirs() -> list[Path]:
    home = hermes_home()
    candidates = [home / "plugins" / PLUGIN_ID, home / PLUGIN_ID]
    try:
        candidates.extend(sorted((home / "profiles").glob(f"*/plugins/{PLUGIN_ID}")))
    except OSError:
        pass
    return candidates


def detect() -> dict[str, object]:
    for directory in _plugin_dirs():
        try:
            if directory.is_dir():
                return {"installed": True, "version": _manifest_version(directory)}
        except OSError:
            continue
    try:
        version = importlib.metadata.version(PLUGIN_ID)
    except (importlib.metadata.PackageNotFoundError, ValueError, OSError):
        version = None
    return {"installed": bool(version), "version": str(version) if version else None}


def main(argv: list[str]) -> int:
    try:
        result = detect()
    except Exception:  # noqa: BLE001 - detection must never break a setup check
        result = {"installed": False, "version": None}
    print(json.dumps(result, ensure_ascii=False,
                     indent=2 if "--pretty" in argv else None))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
