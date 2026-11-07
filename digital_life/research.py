"""Deferred web research: answer immediately, search in a thread, follow up later.

The QQ turn must not block on a search: the model replies first and calls
``search_later``.  A worker thread runs a one-shot CLI session, collects the
short answer, and sends it back to the originating chat.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable

TIMEOUT_SECONDS = 180
MAX_ANSWER_CHARS = 400
MAX_CHILDREN = 2
PROMPT = (
    "你是研究助手。用一到两次检索回答下面的问题，只输出不超过 80 字的结论，"
    "不要寒暄、不要复述问题、不要列点。资料不足就直说没查到。\n\n问题：{question}"
)

# A child session gets only the toolsets for its job: ``-t`` is the one fence the
# host honours at the process boundary. ``digital_life`` still carries all thirteen
# plugin tools, so the precise allow-list is enforced by ``_gate_tool_call``.
TOOLSETS = {"study": "digital_life", "task": "web,digital_life", "search": "web",
            "polish": "todo", "journal": "digital_life"}

_LOCK = threading.Lock()
_ACTIVE: set[str] = set()
_SLOTS = threading.BoundedSemaphore(MAX_CHILDREN)


def child_tag(kind: str, chat: str = "") -> str:
    """The prompt prefix a child session carries; the plugin reads it back from state.db.

    The tag is only the kind/chat hint. Whether the session really is a host-spawned
    one-shot child is decided from the host's ``sessions`` row, which every process
    sharing the database can read, so the hint cannot be forged from a QQ message.
    """
    tag = f"[lover-child:{kind}"
    if chat:
        tag += f" chat={chat}"
    return tag + "]"


def child_marker(kind: str, chat: str = "") -> str:
    """The prompt prefix a child session carries; the plugin reads it back from state.db.

    The origin chat rides along so background work writes memories where the
    conversation can see them, instead of under the child's own local session.
    """
    return child_tag(kind, chat)


def spawn_child(kind: str, prompt: str, *, timeout: float,
                extra_env: dict[str, str] | None = None) -> str:
    """Run one one-shot CLI child in its own process group; "" on any failure."""
    toolsets = TOOLSETS.get(kind)
    if not toolsets:
        return ""
    cmd = [hermes_bin(), "-p", profile_name(), "chat", "--oneshot", "-Q",
           "-t", toolsets, "-q", prompt]
    env = dict(os.environ, HERMES_DISABLE_LAZY_INSTALLS="1")
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, env=env, start_new_session=True)
    except OSError:
        return ""
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        return ""
    if proc.returncode != 0:
        return ""
    return out or ""


def hermes_bin() -> str:
    found = shutil.which("hermes")
    if found:
        return found
    return os.path.expanduser("~/.local/bin/hermes")


def profile_name() -> str:
    home = os.environ.get("HERMES_HOME", "")
    return Path(home).name if home else "hermes-lover"


def clean_answer(text: str, limit: int = MAX_ANSWER_CHARS) -> str:
    out = " ".join((text or "").split())
    if len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


def run_search(question: str) -> str:
    return clean_answer(spawn_child("search", PROMPT.format(question=question),
                               timeout=TIMEOUT_SECONDS))


def spawn(
    question: str, chat_id: str, send: Callable[[str, str], None],
    runner: Callable[[str], str] = run_search,
) -> bool:
    """Start a background worker; returns False when one is already running."""
    if not question.strip() or not chat_id:
        return False
    if not _SLOTS.acquire(blocking=False):
        return False
    with _LOCK:
        if chat_id in _ACTIVE:
            _SLOTS.release()
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
            _SLOTS.release()

    threading.Thread(target=_work, name=f"research-{chat_id[-6:]}", daemon=True).start()
    return True


TASK_TIMEOUT_SECONDS = 900


def run_task(prompt: str, *, kind: str = "task",
             extra_env: dict[str, str] | None = None) -> str:
    """Run a multi-step job in a one-shot background session."""
    return clean_answer(spawn_child(kind, prompt, timeout=TASK_TIMEOUT_SECONDS,
                                    extra_env=extra_env), limit=800)


def spawn_task(
    prompt: str, chat_id: str, send: Callable[[str, str], None],
    runner: Callable[[str], str] | None = None, *, kind: str = "task",
    extra_env: dict[str, str] | None = None,
    on_done: Callable[[bool, str], None] | None = None,
) -> bool:
    """Start a background multi-step job; its result is sent to the chat."""
    if not prompt.strip() or not chat_id:
        return False
    runner = runner or (lambda text: run_task(text, kind=kind, extra_env=extra_env))
    if not _SLOTS.acquire(blocking=False):
        return False
    key = f"task:{chat_id}"
    with _LOCK:
        if key in _ACTIVE:
            _SLOTS.release()
            return False
        _ACTIVE.add(key)

    def _work() -> None:
        answer, ok = "", False
        try:
            answer = runner(prompt)
            ok = True
            if answer:
                send(chat_id, answer)
        except Exception:
            ok = False
        finally:
            if on_done:
                try:
                    on_done(ok, answer)
                except Exception:
                    pass
            with _LOCK:
                _ACTIVE.discard(key)
            _SLOTS.release()

    threading.Thread(target=_work, name=f"task-{chat_id[-6:]}", daemon=True).start()
    return True
