#!/usr/bin/env python3
"""QQ Bot scan-to-configure for hermes-lover, split so a chat agent can drive it.

    start  create a bind task, render the QR, print the URL, save state
    wait   poll until the user scanned, then write .env + config, restart gateway
    cancel forget the pending task

The Hermes gateway wizard is a TUI that needs keystrokes; this script calls the
same q.qq.com bind endpoints directly, so the agent never needs the user to run a
command. The only human step is scanning the code and confirming in QQ.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATE_NAME = "qq-bind.json"
ENV_KEYS = ("QQ_APP_ID", "QQ_CLIENT_SECRET", "QQ_ALLOWED_USERS", "QQ_ALLOW_ALL_USERS")
WAKE_FILE = "wake-words.json"


def hermes_home(profile: str) -> Path:
    root = Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()
    return root / "profiles" / profile


def ensure_deps() -> None:
    try:
        import httpx  # noqa: F401
        import qrcode  # noqa: F401
        return
    except ImportError:
        pass
    venv = Path("~/.hermes/hermes-agent/venv/bin/python3").expanduser()
    if venv.exists() and Path(sys.executable) != venv:
        os.execv(str(venv), [str(venv), __file__, *sys.argv[1:]])
    sys.exit("缺少 httpx/qrcode，且找不到 Hermes 自带的 venv python3。")


def onboard():
    root = os.environ.get("HERMES_AGENT_ROOT") or str(Path("~/.hermes/hermes-agent").expanduser())
    sys.path.insert(0, root)
    from plugins.platforms.qqbot.crypto import decrypt_secret
    from plugins.platforms.qqbot.onboard import (
        BindStatus, _create_bind_task, _poll_bind_result, build_connect_url)
    return BindStatus, _create_bind_task, _poll_bind_result, build_connect_url, decrypt_secret


def state_path(profile: str) -> Path:
    d = hermes_home(profile) / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / STATE_NAME


def write_env(profile: str, app_id: str, secret: str, openid: str, allow_all: bool = False) -> Path:
    env = hermes_home(profile) / ".env"
    keep = [l for l in env.read_text().splitlines() if not l.startswith(tuple(k + "=" for k in ENV_KEYS))]
    keep += [f"QQ_APP_ID={app_id}", f"QQ_CLIENT_SECRET={secret}"]
    if allow_all:
        keep.append("QQ_ALLOW_ALL_USERS=true")
    if openid:
        keep.append(f"QQ_ALLOWED_USERS={openid}")
    env.write_text("\n".join(keep) + "\n")
    os.chmod(env, 0o600)
    return env


def load_wake_words(profile: str, explicit: str = "") -> list[str]:
    """Wake words belong to the persona, not to the binder: file first, flag second."""
    if explicit:
        raw = [word.strip() for word in explicit.split(",")]
    else:
        path = hermes_home(profile) / WAKE_FILE
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            return []
        raw = data if isinstance(data, list) else []
    seen: list[str] = []
    for word in raw:
        word = str(word).strip()
        if word and word not in seen and len(word) <= 32:
            seen.append(word)
    return seen[:12]


def configure(profile: str, allow_all: bool = False, wake_words: list[str] | None = None) -> None:
    pairs = [
        ("platforms.qqbot.enabled", "true"),
        ("platforms.qqbot.extra.group_policy", "open" if allow_all else "allowlist"),
        ("platform_toolsets.qqbot", '["web","vision","todo","digital_life","no_mcp"]'),
        ("platforms.qqbot.extra.group_full_mode", "true"),
        ("platforms.qqbot.extra.group_ambient", "false"),
        ("platforms.qqbot.extra.group_followup_window", "180"),
        ("platforms.qqbot.extra.group_followup_max_messages", "5"),
    ]
    if wake_words:
        pairs.append(("platforms.qqbot.extra.group_wake_words",
                      json.dumps(wake_words, ensure_ascii=False)))
    for key, value in pairs:
        subprocess.run(["hermes", "-p", profile, "config", "set", key, value],
                       capture_output=True, text=True)


def cmd_start(profile: str, allow_all: bool = False, show_qr: bool = False,
              wake_words: str = "") -> int:
    ensure_deps()
    BindStatus, create, _poll, build_url, _decrypt = onboard()
    import qrcode

    task_id, key = create()
    url = build_url(task_id)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=1)
    qr.add_data(url)
    qr.make(fit=True)
    print("用手机 QQ 扫码，或打开下面的链接（只发给绑定人，别贴到群里）：\n")
    if show_qr:
        qr.print_ascii(invert=True)
    print(f"\n{url}\n")
    print("扫码确认后运行：qq-bind.py wait --profile " + profile)
    state_path(profile).write_text(json.dumps({"task_id": task_id, "key": key, "url": url,
                                               "allow_all": allow_all,
                                               "wake_words": wake_words}))
    os.chmod(state_path(profile), 0o600)
    return 0


def cmd_wait(profile: str, timeout: int, allow_all: bool = False) -> int:
    ensure_deps()
    BindStatus, _create, poll, _build, decrypt = onboard()
    path = state_path(profile)
    if not path.exists():
        sys.exit("还没有进行中的扫码任务，先运行：qq-bind.py start --profile " + profile)
    state = json.loads(path.read_text())
    allow_all = bool(state.get("allow_all", allow_all))
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            status, app_id, enc, openid = poll(state["task_id"])
        except Exception:
            time.sleep(2)
            continue
        if status != last:
            print({"pending": "等待扫码…", "completed": "已扫码"}.get(status.name.lower(), status.name))
            last = status
        if status == BindStatus.COMPLETED:
            secret = decrypt(enc, state["key"])
            env = write_env(profile, app_id, secret, openid, allow_all=allow_all)
            words = load_wake_words(profile, str(state.get("wake_words", "")))
            configure(profile, allow_all=allow_all, wake_words=words)
            subprocess.run(["hermes", "gateway", "restart"], capture_output=True, text=True)
            path.unlink(missing_ok=True)
            print(f"\n绑定完成：App ID {app_id}")
            print(f"凭证已写入 {env}")
            if allow_all:
                print("QQ 机器人已启用，网关已重启；已开启全用户访问，任何人都能私聊/群聊它。")
            else:
                print("QQ 机器人已启用，网关已重启；目前只允许白名单账号，群聊需重跑 start --allow-all。")
            return 0
        if status == BindStatus.EXPIRED:
            path.unlink(missing_ok=True)
            print("二维码已过期，重新运行：qq-bind.py start --profile " + profile)
            return 3
        time.sleep(2)
    print("等待超时；重新运行 start 出新的二维码。")
    return 4


def cmd_cancel(profile: str) -> int:
    state_path(profile).unlink(missing_ok=True)
    print("已清除待扫码任务。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "wait", "cancel"))
    parser.add_argument("--profile", default=os.environ.get("HERMES_PROFILE") or "hermes-lover")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--allow-all", action="store_true", help="open DMs/groups to any QQ user")
    parser.add_argument("--qr", action="store_true", help="also render the QR in this terminal")
    parser.add_argument("--wake-words", default="",
                        help="comma-separated wake words; defaults to the profile's wake-words.json")
    args = parser.parse_args()
    if args.action == "start":
        return cmd_start(args.profile, allow_all=args.allow_all, show_qr=args.qr,
                         wake_words=args.wake_words)
    if args.action == "wait":
        return cmd_wait(args.profile, args.timeout, allow_all=args.allow_all)
    return cmd_cancel(args.profile)


if __name__ == "__main__":
    sys.exit(main())
