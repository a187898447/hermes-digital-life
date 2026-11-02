#!/usr/bin/env python3
"""Let Hermes Studio deliver clarify/approval answers to the session's own profile.

Studio 0.8.x binds a chat connection to one profile and then validates every
clarify/approval answer against that connection profile.  When the conversation
belongs to a different profile (for example `hermes-lover`) the answer is
rejected with

    Profile "<session profile>" is not available on this connection

and the agent waits for the full timeout.  This script rewrites that guard so a
conversation may be served by any profile that exists on this machine.  It only
touches Studio's own JS bundle; Hermes itself is never modified.

Usage:
    fix-studio-cross-profile.py            # apply (idempotent)
    fix-studio-cross-profile.py --check    # exit 1 when not applied
    fix-studio-cross-profile.py --remove   # restore the original guard
    fix-studio-cross-profile.py --dir DIR  # override the Studio package dir
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

MARKER = "HERMES_LOVER_STUDIO_CROSS_PROFILE"

ORIGINAL = 'if(G!==m)throw new Error(`Profile "${G}" is not available on this connection`);'

PATCHED = (
    "if(G!==m){/*" + MARKER + "*/try{"
    'const _fs=require("node:fs"),_p=require("node:path"),'
    '_h=require("node:os").homedir(),'
    '_ok=G==="default"?_fs.existsSync(_p.join(_h,".hermes","config.yaml"))'
    ':_fs.existsSync(_p.join(_h,".hermes","profiles",G,"config.yaml"));'
    'if(!_ok)throw new Error(`Profile "${G}" is not available on this connection`);'
    '}catch(_e){throw new Error(`Profile "${G}" is not available on this connection`);}}'
)


def studio_dirs(override: str | None) -> list[Path]:
    if override:
        return [Path(override)]
    candidates = []
    npm_root = os.environ.get("HERMES_WEBUI_NPM_ROOT")
    if npm_root:
        candidates.append(Path(npm_root) / "hermes-web-ui")
    candidates += [
        Path("/usr/local/lib/node_modules/hermes-web-ui"),
        Path.home() / ".npm-global/lib/node_modules/hermes-web-ui",
        Path.home() / ".local/lib/node_modules/hermes-web-ui",
    ]
    return candidates


def find_bundle(dirs: list[Path]) -> Path | None:
    for d in dirs:
        p = d / "dist" / "server" / "index.js"
        if p.is_file():
            return p
    return None


def node_available() -> bool:
    return shutil.which("node") is not None


def node_check(path: Path) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["node", "--check", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return r.returncode == 0, (r.stderr or "").strip()


def count(text: str, needle: str) -> int:
    return text.count(needle)


def apply(bundle: Path) -> int:
    text = bundle.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"已打好补丁，无需重复: {bundle}")
        return 0
    if count(text, ORIGINAL) != 1:
        print(
            "跳过：没有找到唯一的 profile 校验语句（Studio 版本可能已变化）。"
            f" {bundle}",
            file=sys.stderr,
        )
        return 0
    if not node_available():
        print("跳过：没有找到 node，无法校验补丁后的语法。", file=sys.stderr)
        return 0

    backup = bundle.with_name(bundle.name + f".bak-{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(bundle, backup)
    patched = bundle.with_name(".__cross_profile_check.js")
    patched.write_text(text.replace(ORIGINAL, PATCHED, 1), encoding="utf-8")

    ok, err = node_check(patched)
    if not ok:
        patched.unlink(missing_ok=True)
        print(f"跳过：补丁后语法校验失败，已保留原文件。\n{err}", file=sys.stderr)
        return 1
    os.replace(patched, bundle)
    print(f"已让 Studio 接受会话所属 profile 的答复: {bundle}")
    print(f"备份: {backup}")
    print("重启 Studio 后生效: hermes-web-ui restart")
    return 0


def remove(bundle: Path) -> int:
    text = bundle.read_text(encoding="utf-8")
    if MARKER not in text:
        print(f"未打补丁，无需移除: {bundle}")
        return 0
    if count(text, PATCHED) != 1:
        print("没有找到唯一的已打补丁语句，未做修改。", file=sys.stderr)
        return 1
    backup = bundle.with_name(bundle.name + f".bak-{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(bundle, backup)
    bundle.write_text(text.replace(PATCHED, ORIGINAL, 1), encoding="utf-8")
    print(f"已恢复 Studio 原始校验: {bundle}")
    print(f"备份: {backup}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="只在未打补丁时返回 1")
    ap.add_argument("--remove", action="store_true", help="恢复原始校验")
    ap.add_argument("--dir", default=None, help="Hermes Studio 包目录")
    args = ap.parse_args()

    bundle = find_bundle(studio_dirs(args.dir))
    if bundle is None:
        if args.check:
            print("没有找到 Hermes Studio，无需处理。")
            return 0
        print("没有找到 Hermes Studio，跳过。")
        return 0

    if args.remove:
        return remove(bundle)
    if args.check:
        applied = MARKER in bundle.read_text(encoding="utf-8", errors="ignore")
        print(("已应用" if applied else "未应用") + f": {bundle}")
        return 0 if applied else 1
    return apply(bundle)


if __name__ == "__main__":
    raise SystemExit(main())
