#!/usr/bin/env python3
"""Deterministic setup gate for a hermes-lover profile.

Prints the real completion state of the onboarding checklist and the single
step the guide is allowed to advance next. The guide must obey NEXT instead of
guessing the order.

Usage:
    python3 setup-status.py [profile]

Exit code: 0 when everything is done, 1 otherwise.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN = "hermes-lover"


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def profile_dir(profile: str) -> Path:
    home = hermes_home()
    if profile == "default":
        return home
    return home / "profiles" / profile


def run(cmd: list[str], timeout: float = 25) -> subprocess.CompletedProcess | None:
    exe = shutil.which(cmd[0]) or str(Path.home() / ".local" / "bin" / cmd[0])
    try:
        return subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def main() -> int:
    profile = sys.argv[1] if len(sys.argv) > 1 else "hermes-lover"
    pdir = profile_dir(profile)
    rows: list[tuple[int, str, bool, str]] = []

    # Prerequisites: the profile exists and the plugin is enabled in it.
    profile_ok = pdir.is_dir()
    plugin_ok = False
    if profile_ok:
        try:
            cfg = (pdir / "config.yaml").read_text()
            plugin_ok = f"- {PLUGIN}" in cfg or f"{PLUGIN}:" in cfg
        except OSError:
            plugin_ok = False

    # Credentials: an OAuth token store or an inline api_key is present.
    creds_ok = False
    try:
        auth = pdir / "auth.json"
        creds_ok = auth.is_file() and auth.stat().st_size > 10
        if not creds_ok:
            creds_ok = "api_key" in (pdir / "config.yaml").read_text()
    except OSError:
        pass

    # Step 4: a real persona was written, not the stock Hermes template.
    soul_ok, soul_note = False, "缺少 SOUL.md"
    soul = pdir / "SOUL.md"
    if soul.is_file():
        text = soul.read_text(errors="replace")
        if "built by Nous Research" in text:
            soul_note = "还是 Hermes 默认模板"
        elif len(text.strip()) < 200:
            soul_note = "SOUL.md 太短，可能没写完"
        else:
            soul_ok, soul_note = True, text.strip().splitlines()[0][:40]
    rows.append((4, "人格 SOUL.md", soul_ok, soul_note))

    # Step 5: the plugin answers a status query with chemistry JSON.
    status_ok = False
    res = run(["hermes", "-p", profile, PLUGIN, "status"])
    if res and res.returncode == 0 and "chemistry" in (res.stdout or ""):
        status_ok = True
    rows.append((5, "状态可读", status_ok, "hermes-lover status 返回 JSON"))

    # Step 6: QQ credentials are in the profile .env.
    qq_ok = False
    try:
        env = (pdir / ".env").read_text()
        qq_ok = any(line.startswith("QQ_APP_ID=") and line.split("=", 1)[1].strip() for line in env.splitlines())
    except OSError:
        pass
    rows.append((6, "QQ 凭证", qq_ok, ".env 有 QQ_APP_ID" if qq_ok else "profile .env 里没有 QQ_APP_ID"))

    # Steps 7-8: the paused beat job exists and whether the operator enabled it.
    beat, beat_active = None, False
    try:
        data = json.loads((pdir / "cron" / "jobs.json").read_text())
        for job in data.get("jobs", []):
            if job.get("name") == "hermes-lover-beat":
                beat = job
                beat_active = bool(job.get("enabled"))
                break
    except (OSError, ValueError):
        pass
    rows.append((7, "主动消息任务", beat is not None, f"id={beat.get('id')}" if beat else "cron 里没有 hermes-lover-beat"))
    rows.append((8, "主动消息已启用", beat_active, "enabled" if beat_active else "仍是 paused，等用户确认后再 resume"))

    first_bad = None
    for step, label, ok, note in rows:
        mark = "OK  " if ok else "TODO"
        print(f"[{mark}] {step}  {label}  {note}")
        if not ok and first_bad is None:
            first_bad = step

    print()
    if not profile_ok:
        print("NEXT=2  这个 profile 还不存在：先跑 install.sh，不要在别处补步骤。")
        return 1
    if not plugin_ok:
        print(f"NEXT=2  profile 存在但插件没启用：先启用 {PLUGIN}，不要往下走。")
        return 1
    if first_bad is None:
        print("NEXT=done  初始化完成。")
        return 0
    print(f"NEXT={first_bad}  只做这一步，做完停下等用户；不要提前做后面的步骤。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
