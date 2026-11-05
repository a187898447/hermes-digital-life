"""Deferred web research: answer immediately, search in a thread, follow up later.

The QQ turn must not block on a search: the model replies first and calls
``search_later``.  A worker thread runs a one-shot CLI session, collects the
short answer, and sends it back to the originating chat.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

TIMEOUT_SECONDS = 180
MAX_ANSWER_CHARS = 400
PROMPT = (
    "你是研究助手。用一到两次检索回答下面的问题，只输出不超过 80 字的结论，"
    "不要寒暄、不要复述问题、不要列点。资料不足就直说没查到。\n\n问题：{question}"
)

_LOCK = threading.Lock()
_ACTIVE: set[str] = set()


def hermes_bin() -> str:
    found = shutil.which("hermes")
    if found:
        return found
    return os.path.expanduser("~/.local/bin/hermes")


def profile_name() -> str:
    home = os.environ.get("HERMES_HOME", "")
    return Path(home).name if home else "hermes-lover"


def clean_answer(text: str) -> str:
    out = " ".join((text or "").split())
    if len(out) > MAX_ANSWER_CHARS:
        out = out[: MAX_ANSWER_CHARS - 1].rstrip() + "…"
    return out


def run_search(question: str) -> str:
    cmd = [
        hermes_bin(), "-p", profile_name(), "chat", "--oneshot", "-Q", "-q",
        PROMPT.format(question=question),
    ]
    env = dict(os.environ, HERMES_DISABLE_LAZY_INSTALLS="1")
    try:
        done = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if done.returncode != 0:
        return ""
    return clean_answer(done.stdout)


def spawn(
    question: str, chat_id: str, send: Callable[[str, str], None],
    runner: Callable[[str], str] = run_search,
) -> bool:
    """Start a background worker; returns False when one is already running."""
    if not question.strip() or not chat_id:
        return False
    with _LOCK:
        if chat_id in _ACTIVE:
            return False
        _ACTIVE.add(chat_id)

    def _work() -> None:
        try:
            answer = runner(question)
            if answer:
                send(chat_id, f"查到了：{answer}")
        except Exception:
            pass
        finally:
            with _LOCK:
                _ACTIVE.discard(chat_id)

    threading.Thread(target=_work, name=f"research-{chat_id[-6:]}", daemon=True).start()
    return True
