"""Proactive-message subscriptions driven by what people say in chat.

A message that asks the bot to be more forward ("主动一点", "多来找我") adds that
chat to the beat job's delivery list; a message that asks for quiet ("安静一点",
"别主动") removes it. The beat job itself is never created or resumed here — only
its ``deliver`` targets change, so an operator still controls whether beats run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class HostAPIUnavailable(RuntimeError):
    """A host-internal module this plugin reads moved or disappeared."""

PLUGIN_NAME = "hermes-lover"
BEAT_JOB_NAME = "hermes-lover-beat"
BEAT_JOB_PREFIX = BEAT_JOB_NAME + "-"
BEAT_SCHEDULE = "*/5 9-21 * * *"
CHAT_BEAT_PROMPT = (
    "这是 hermes-lover 发给 QQ 会话 {chat_type} {chat_id} 的主动消息，监控输出里的 FIRE 标记表示刚刚到点。按顺序做："
    "1) 读当前 profile 的 SOUL.md 人格；2) 调 memory_recall(chat_id=\"{chat_id}\") 看这个会话最近聊过什么、对方是谁；"
    "3) 参考 hermes-lover 当前状态（激素、精力、gap）；4) 结合时间段，用这个人物在这个会话里的口吻写一句话（不超过 40 字，接得上刚才的话题，不报状态数字，不客套，不复读）；"
    "5) 只输出这句话，不要解释，也不要自己调用发送工具——投递目标由这个任务的 Deliver 决定。"
)
TARGETS_FILE = "proactive-targets.json"

# Order matters: a request that contains a quiet phrase must win over the
# "主动" it may also contain ("不用主动找我").
_DISABLE = re.compile(
    r"(安静一点|安静点|安静些|别主动|不要主动|不用主动|别再主动|少主动|别找我|别烦|少说点|少发点|别打扰|消停|闭嘴|静音)"
)
_ENABLE = re.compile(
    r"(主动一点|主动点|主动些|多主动|主动找我|多来找我|多找我|来找我|多理我|理我一下|活跃点|活泼点|多聊|多跟我说|主动聊|主动说)"
)


def classify(text: str) -> str | None:
    """Return ``"add"``, ``"remove"`` or ``None`` for one incoming user message."""
    body = (text or "").strip()
    if not body or len(body) > 300:
        return None
    if _DISABLE.search(body):
        return "remove"
    if _ENABLE.search(body):
        return "add"
    return None


def _home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _plugin_data_dir():
    try:
        from plugins.plugin_storage import plugin_data_dir  # noqa: PLC0415

        return plugin_data_dir
    except (ImportError, AttributeError) as exc:
        raise HostAPIUnavailable("plugins.plugin_storage") from exc


def _targets_path() -> Path:
    return _plugin_data_dir()(PLUGIN_NAME) / TARGETS_FILE


def _load_targets_strict() -> list[dict[str, Any]]:
    try:
        data = json.loads(_targets_path().read_text())
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("chat_id")]


def load_targets() -> list[dict[str, Any]]:
    try:
        return _load_targets_strict()
    except HostAPIUnavailable:
        logger.warning("hermes-lover: host plugin storage is unavailable; subscriptions are off")
        return []


def subscribed_chats() -> set[str]:
    """Chat ids that opted in. A missing file means nobody opted in: fail closed."""
    return {str(item["chat_id"]) for item in load_targets()}


@contextmanager
def _targets_lock():
    """Serialise read-modify-write of the subscription file across processes."""
    try:
        import fcntl  # noqa: PLC0415 - POSIX only; Windows falls back to no lock
    except ImportError:  # pragma: no cover
        yield
        return
    try:
        plugin_data_dir = _plugin_data_dir()  # noqa: PLC0415
    except HostAPIUnavailable:
        yield
        return

    path = plugin_data_dir(PLUGIN_NAME) / "proactive.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+", encoding="utf-8")
    except OSError:
        yield
        return
    with handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _save_targets(targets: list[dict[str, Any]]) -> None:
    path = _targets_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(targets, ensure_ascii=False, indent=2))
    tmp.replace(path)


def resolve_chat(session_id: str) -> dict[str, str] | None:
    """Read the chat behind ``session_id`` from the profile's session store."""
    if not session_id:
        return None
    db = _home() / "state.db"
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT chat_id, chat_type, session_key FROM sessions"
            " WHERE id = ? OR session_key = ? LIMIT 1",
            (session_id, session_id),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row and row[0]:
        return {"chat_id": str(row[0]), "chat_type": str(row[1] or "")}
    # Fallback: agent:<namespace>:<platform>:<chat_type>:<chat_id>[:<user_id>]
    parts = str(session_id).split(":")
    if len(parts) >= 5 and parts[0] == "agent":
        return {"chat_id": parts[4], "chat_type": parts[3]}
    return None


def _cron_jobs():
    try:
        import cron.jobs as jobs  # noqa: PLC0415

        return jobs
    except (ImportError, AttributeError) as exc:
        raise HostAPIUnavailable("cron.jobs") from exc


def sync_beat_delivery() -> bool:
    """Keep the beat job on local delivery: one agent run fans out to every chat."""
    try:
        jobs = _cron_jobs()
    except HostAPIUnavailable:
        logger.warning("hermes-lover: host cron.jobs is unavailable; beat delivery is unchanged")
        return False

    changed = False
    with jobs.use_cron_store(_home()):
        for job in jobs.load_jobs():
            if job.get("name") != BEAT_JOB_NAME:
                continue
            if job.get("deliver") != ["local"]:
                jobs.update_job(job["id"], {"deliver": ["local"]})
                changed = True
    return changed


def track_chat(chat: dict[str, str]) -> bool:
    """Auto-subscribe every QQ chat the bot talks in (DM and group) to the beat job.

    Operator-authored deliver entries are preserved; this only appends chats that
    actually spoke to the bot, so a new group joins the proactive rotation without
    the operator editing config.
    """
    chat_id = str(chat.get("chat_id") or "")
    if not chat_id:
        return False
    try:
        with _targets_lock():
            targets = _load_targets_strict()
            if any(str(item.get("chat_id")) == chat_id for item in targets):
                return False
            targets.append({
                "chat_id": chat_id,
                "chat_type": str(chat.get("chat_type") or ""),
                "label": str(chat.get("label") or ""),
                "added_at": time.time(),
            })
            _save_targets(targets)
            sync_beat_delivery()
            return True
    except HostAPIUnavailable:
        logger.warning("hermes-lover: host plugin storage is unavailable; subscriptions are off")
        return False


def _beat_job_name(chat_id: str) -> str:
    return BEAT_JOB_PREFIX + hashlib.sha1(chat_id.encode("utf-8")).hexdigest()[:8]


def _write_chat_monitor(chat_id: str) -> str:
    """Write a per-chat wrapper so each beat job keeps its own slot state."""
    scripts_dir = _home() / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    name = _beat_job_name(chat_id) + ".py"
    wrapper = (
        "import os, runpy\n"
        "from pathlib import Path\n"
        f"os.environ[\"HERMES_LOVER_BEAT_KEY\"] = {chat_id!r}\n"
        "runpy.run_path(str(Path(__file__).with_name(\"hermes-lover-beat.py\")), run_name=\"__main__\")\n"
    )
    (scripts_dir / name).write_text(wrapper, encoding="utf-8")
    return name


def ensure_chat_job(chat: dict[str, str]) -> bool:
    """Give each talking chat its own beat job so every conversation gets its own line."""
    chat_id = str(chat.get("chat_id") or "")
    # Only real QQ chats get their own beat job; local CLI/Studio sessions have
    # chat ids like "local-<hash>" and must never receive proactive messages.
    if not chat_id or chat_id.startswith("local-"):
        return False
    try:
        jobs = _cron_jobs()
    except HostAPIUnavailable:
        logger.warning("hermes-lover: host cron.jobs is unavailable; no per-chat beat job")
        return False

    name = _beat_job_name(chat_id)
    with jobs.use_cron_store(_home()):
        jobs_list = jobs.load_jobs()
        if any(job.get("name") == name for job in jobs_list):
            return False
        enabled = any(
            bool(job.get("enabled")) and (job.get("name") or "").startswith(BEAT_JOB_NAME)
            for job in jobs_list
        )
        jobs.create_job(
            CHAT_BEAT_PROMPT.format(chat_type=str(chat.get("chat_type") or "chat"), chat_id=chat_id),
            BEAT_SCHEDULE,
            name=name,
            deliver=f"qqbot:{chat_id}",
            monitor_script=_write_chat_monitor(chat_id),
            paused=not enabled,
            paused_reason=None if enabled else "安装时默认停用，启用方法见 hermes-lover-guide 最后一步",
        )
    return True


def subscribe(chat: dict[str, str], label: str = "") -> tuple[str, int]:
    """Add ``chat`` (idempotent). Returns ``(state, target_count)``."""
    chat_id = str(chat.get("chat_id") or "")
    if not chat_id:
        return "missing", len(load_targets())
    try:
        with _targets_lock():
            targets = _load_targets_strict()
            if any(item["chat_id"] == chat_id for item in targets):
                return "present", len(targets)
            targets.append(
                {
                    "chat_id": chat_id,
                    "chat_type": str(chat.get("chat_type") or ""),
                    "label": label,
                    "added_at": time.time(),
                }
            )
            _save_targets(targets)
            sync_beat_delivery()
            return "added", len(targets)
    except HostAPIUnavailable:
        logger.warning("hermes-lover: host plugin storage is unavailable; subscriptions are off")
        return "unavailable", 0


def unsubscribe(chat_id: str) -> tuple[str, int]:
    """Remove ``chat_id`` (idempotent). Returns ``(state, target_count)``."""
    try:
        with _targets_lock():
            targets = _load_targets_strict()
            kept = [item for item in targets if item["chat_id"] != chat_id]
            if len(kept) == len(targets):
                return "absent", len(targets)
            _save_targets(kept)
            sync_beat_delivery()
            return "removed", len(kept)
    except HostAPIUnavailable:
        logger.warning("hermes-lover: host plugin storage is unavailable; subscriptions are off")
        return "unavailable", 0


def handle_message(text: str, session_id: str, platform: str = "") -> str | None:
    """React to one incoming message. Returns a context note for the model, or None."""
    if platform != "qqbot":
        return None
    action = classify(text)
    if action is None:
        return None
    chat = resolve_chat(session_id)
    if chat is None:
        return None
    if action == "add":
        state, count = subscribe(chat, label=str(session_id))
        if state == "unavailable":
            return None
        if state == "present":
            return "这个会话已经在主动消息投递列表里了（共 %d 个），不用重复加。" % count
        return "已把这个会话加入主动消息投递（共 %d 个）。" % count
    state, count = unsubscribe(chat["chat_id"])
    if state == "unavailable":
        return None
    if state == "absent":
        return "这个会话本来就不在主动消息列表里（当前 %d 个）。" % count
    return "已把这个会话从主动消息投递里移除（还剩 %d 个）。" % count


def set_beats_enabled(enabled: bool) -> int:
    """Enable or pause every hermes-lover beat job; returns how many changed."""
    try:
        jobs = _cron_jobs()
    except HostAPIUnavailable:
        logger.warning("hermes-lover: host cron.jobs is unavailable; beat jobs are unchanged")
        return 0

    changed = 0
    with jobs.use_cron_store(_home()):
        for job in jobs.load_jobs():
            name = job.get("name") or ""
            if name != BEAT_JOB_NAME and not name.startswith(BEAT_JOB_PREFIX):
                continue
            if bool(job.get("enabled")) == enabled:
                continue
            if enabled:
                jobs.resume_job(job["id"])
            else:
                jobs.pause_job(job["id"], "paused by the operator")
            changed += 1
    return changed
