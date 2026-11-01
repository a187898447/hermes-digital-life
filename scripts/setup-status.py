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
NUWA = "huashu-nuwa"


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


def persona_draft() -> str | None:
    """Name of a local 女娲 persona skill (the bootstrap skill does not count)."""
    root = hermes_home() / "skills" / "personas"
    if not root.is_dir():
        return None
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name != NUWA and (child / "SKILL.md").is_file():
            return child.name
    return None


def main() -> int:
    profile = sys.argv[1] if len(sys.argv) > 1 else PLUGIN
    pdir = profile_dir(profile)
    rows: list[tuple[str, bool, str]] = []  # (label, ok, note)

    # Prerequisites: profile + plugin import the agent, so they must exist.
    profile_ok = pdir.is_dir()
    plugin_ok = False
    if profile_ok:
        try:
            cfg = (pdir / "config.yaml").read_text()
            plugin_ok = f"- {PLUGIN}" in cfg or f"{PLUGIN}:" in cfg
        except OSError:
            pass

    # Step 1 (optional): a 女娲 draft. Step 4 can bypass it with the 001 persona.
    draft = persona_draft()
    soul = pdir / "SOUL.md"
    soul_text = soul.read_text(errors="replace") if soul.is_file() else ""
    soul_custom = bool(soul_text.strip()) and "built by Nous Research" not in soul_text and len(soul_text.strip()) >= 200
    if draft:
        rows.append(("人格草稿（可跳过）", True, f"已有 {draft}"))
    elif soul_custom:
        rows.append(("人格草稿（可跳过）", True, "未造草稿；已用现成人格落地，无需再补"))
    else:
        rows.append(("人格草稿（可跳过）", False, "未造草稿；第 4 步可直接选 001 参考人格"))

    rows.append(("profile + 插件", profile_ok and plugin_ok,
                 f"{pdir}" if profile_ok else "profile 不存在"))
    creds_ok = False
    try:
        auth = pdir / "auth.json"
        creds_ok = auth.is_file() and auth.stat().st_size > 10
        if not creds_ok:
            creds_ok = "api_key" in (pdir / "config.yaml").read_text()
    except OSError:
        pass
    rows.append(("模型凭证", creds_ok, "已配（本会话能跑即证明可用）" if creds_ok else "没找到 auth.json/api_key"))

    # Step 4: a real persona, not the stock Hermes template.
    if soul_custom:
        rows.append(("人格 SOUL.md", True, soul_text.strip().splitlines()[0][:40]))
    elif soul.is_file():
        rows.append(("人格 SOUL.md", False, "还是 Hermes 默认模板"))
    else:
        rows.append(("人格 SOUL.md", False, "缺少 SOUL.md"))

    # Step 5: the plugin answers a status query with chemistry JSON.
    res = run(["hermes", "-p", profile, PLUGIN, "status"])
    rows.append(("状态可读", bool(res and res.returncode == 0 and "chemistry" in (res.stdout or "")),
                 "hermes-lover status 返回 JSON"))

    # Step 6: QQ credentials live in the profile .env.
    qq_ok = False
    try:
        qq_ok = any(line.startswith("QQ_APP_ID=") and line.split("=", 1)[1].strip()
                    for line in (pdir / ".env").read_text().splitlines())
    except OSError:
        pass
    rows.append(("QQ 凭证", qq_ok, ".env 有 QQ_APP_ID" if qq_ok else "profile .env 里没有 QQ_APP_ID"))

    # Steps 7-8: the paused beat job exists, and whether the operator enabled it.
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

    first_bad = None
    for idx, (label, ok, note) in enumerate(rows, start=1):
        if ok:
            mark = "OK  "
        elif idx == 1:  # draft is optional; never blocks the flow
            mark = "OPT "
        else:
            mark = "TODO"
        print(f"[{mark}] {idx}  {label:<14} {note}")
        if not ok and idx > 1 and first_bad is None:
            first_bad = idx

    print()
    if not profile_ok:
        print("NEXT=2  profile 不存在：先跑 install.sh。")
        return 1
    if not plugin_ok:
        print(f"NEXT=2  profile 存在但插件没启用：先启用 {PLUGIN}。")
        return 1
    if first_bad is None:
        print("NEXT=done  初始化完成。")
        return 0
    print(f"NEXT={first_bad}  只做这一步，做完停下等用户；不要提前做后面的步骤。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
