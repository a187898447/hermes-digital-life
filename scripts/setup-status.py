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
    profile = sys.argv[1] if len(sys.argv) > 1 else PLUGIN
    pdir = profile_dir(profile)
    rows: list[tuple[str, bool, str]] = []  # (label, ok, note)

    # Prerequisites are not steps: profile + plugin + credentials make the
    # agent runnable at all.  If one is missing, report it instead of a step.
    profile_ok = pdir.is_dir()
    plugin_ok = False
    if profile_ok:
        try:
            cfg = (pdir / "config.yaml").read_text()
            plugin_ok = f"- {PLUGIN}" in cfg or f"{PLUGIN}:" in cfg
        except OSError:
            pass
    creds_ok = False
    try:
        auth = pdir / "auth.json"
        creds_ok = auth.is_file() and auth.stat().st_size > 10
        if not creds_ok:
            creds_ok = "api_key" in (pdir / "config.yaml").read_text()
    except OSError:
        pass

    # Step 1: a real persona, not the stock Hermes template.
    soul = pdir / "SOUL.md"
    soul_text = soul.read_text(errors="replace") if soul.is_file() else ""
    soul_custom = bool(soul_text.strip()) and "built by Nous Research" not in soul_text and len(soul_text.strip()) >= 200
    if soul_custom:
        rows.append(("人格 SOUL.md", True, soul_text.strip().splitlines()[0][:40]))
    elif soul.is_file():
        rows.append(("人格 SOUL.md", False, "还是 Hermes 默认模板（选 001 或转译草稿）"))
    else:
        rows.append(("人格 SOUL.md", False, "缺少 SOUL.md（选 001 或转译草稿）"))

    # Step 2: status is readable and the state db has a backup.
    res = run(["hermes", "-p", profile, PLUGIN, "status"])
    status_ok = bool(res and res.returncode == 0 and "chemistry" in (res.stdout or ""))
    backups = sorted((pdir / "plugin-data" / PLUGIN).glob("data.db.bak-*"))
    rows.append(("状态可读 + 备份", status_ok and bool(backups),
                 "status 返回 JSON" + ("，已有备份" if backups else "；还没有 data.db 备份")))

    # Step 3: QQ credentials live in the profile .env. An already-bound QQ app in
    # the default profile can be reused instead of creating a new q.qq.com app.
    def qq_key_present(env: Path) -> bool:
        try:
            return any(line.startswith("QQ_APP_ID=") and line.split("=", 1)[1].strip()
                       for line in env.read_text().splitlines())
        except OSError:
            return False

    keys_ok = qq_key_present(pdir / ".env")
    enabled_cfg = run(["hermes", "-p", profile, "config", "get", "platforms.qqbot.enabled"])
    platform_on = bool(enabled_cfg and (enabled_cfg.stdout or "").strip().lower().startswith("true"))
    qq_ok = keys_ok and platform_on
    if qq_ok:
        detail = ".env 有 QQ_APP_ID，platforms.qqbot 已启用"
    elif not keys_ok:
        detail = "跑 `hermes -p <角色名> gateway setup`，平台选 QQ Bot 扫码"
    else:
        detail = "凭证已写入，但 platforms.qqbot 未启用：hermes -p <角色名> config set platforms.qqbot.enabled true"
    rows.append(("QQ 绑定（扫码）", qq_ok, detail))

    # Steps 4-5: the paused beat job exists, and whether the operator enabled it.
    beat, beat_active = None, False
    try:
        for job in json.loads((pdir / "cron" / "jobs.json").read_text()).get("jobs", []):
            if job.get("name") == f"{PLUGIN}-beat":
                beat = job
                beat_active = bool(job.get("enabled"))
                break
    except (OSError, ValueError):
        pass
    rows.append(("主动消息任务", beat is not None, f"id={beat.get('id')}" if beat else "cron 里没有 beat"))
    rows.append(("主动消息已启用", beat_active,
                 "enabled" if beat_active else "仍是 paused；用户确认后再 resume"))

    prereq = []
    if not profile_ok:
        prereq.append("profile 不存在")
    if not plugin_ok:
        prereq.append(f"{PLUGIN} 未启用")
    if not creds_ok:
        prereq.append("凭证未配")
    if prereq:
        print("前置未满足: " + "，".join(prereq))
        print("NEXT=install  先跑 install.sh / setup 把前置补齐，再回到第 1 步。")
        return 1

    first_bad = None
    for idx, (label, ok, note) in enumerate(rows, start=1):
        mark = "OK  " if ok else "TODO"
        print(f"[{mark}] {idx}  {label:<14} {note}")
        if not ok and first_bad is None:
            first_bad = idx

    print()
    if first_bad is None:
        print("NEXT=done  初始化完成。")
        return 0
    print(f"NEXT={first_bad}  只做这一步，做完停下等用户；不要提前做后面的步骤。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
