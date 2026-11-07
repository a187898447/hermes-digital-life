"""Hermes digital-life standalone plugin."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import time
from pathlib import Path
from typing import Any, Optional

from . import digital_life
from .digital_life import proactive as _proactive
from .digital_life import selfheal as _selfheal
from .digital_life import typos as _typos
from .digital_life import compaction as _compaction
from .digital_life import library as _library
from .digital_life import gaps as _gaps
from .digital_life.runtime import record_api_error, record_request, record_turn

logger = logging.getLogger(__name__)

_TURN_META: dict[str, dict[str, str]] = {}

# Hard tool budget per user turn (QQ only): the model tends to page its way
# through the whole zone instead of answering.
_TOOL_TURNS: dict[str, dict[str, int]] = {}
# How many times this turn's tool calls were refused after the budget ran out. One
# refusal is a nudge; two means the model keeps reaching for tools instead of talking,
# so the next request carries a system-level "answer now" and the gate hard-stops.
_BUDGET_STOPS: dict[str, int] = {}


def _budget_stopped(session_id: str) -> bool:
    if _BUDGET_STOPS.get(session_id, 0) >= 2:
        return True
    return any(count >= 2 and (session_id.endswith(key) or key.endswith(session_id))
               for key, count in _BUDGET_STOPS.items())
TOOL_CALL_LIMIT = 8
ZONE_PAGE_LIMIT = 2
READ_SOURCE_TURN_LIMIT = 3
READ_SOURCE_DAILY_INGESTS = 40
VIDEO_PER_DAY = 2
VIDEO_GEN_PER_DAY = 3
IMAGE_GEN_PER_DAY = 6
SPEECH_PER_DAY = 10
TRANSCRIBE_PER_DAY = 20
# A per-chat cap alone is not a cost bound: N chats multiply into N times the spend.
# These are the profile-wide ceilings for the metered calls.
VIDEO_GEN_PER_PROFILE = 6
IMAGE_GEN_PER_PROFILE = 12
SPEECH_PER_PROFILE = 20
TRANSCRIBE_PER_PROFILE = 40
_PROFILE_COUNTER = "__profile__"
MINIMAX_TOOLS = ("make_video", "make_image", "make_speech", "transcribe_audio")
# Media generation belongs to this plugin: one path means one skill ladder, one character file.
MEDIA_GENERATORS = ("make_image", "make_video", "make_speech")
MEDIA_OUTSIDERS = frozenset(
    name.strip().lower()
    for name in os.environ.get(
        "HERMES_LOVER_MEDIA_OUTSIDERS",
        "apikey-image-gen,studio-image-gen,image_generation,image-generation,"
        "generate_image,generate_images,text_to_video,text-to-video,video_generation,"
        "video-generation,generate_video,text_to_speech,text-to-speech,generate_speech",
    ).split(",")
    if name.strip()
)
_MODEL_SEEN = {"name": ""}
OPEN_LOOP_SHOWN = 3
_SCOPE_CACHE: dict[str, tuple[str, str]] = {}
_CACHE_LIMIT = 512


def _remember(cache: dict[str, object], key: str, value: object) -> None:
    """Insertion-ordered, bounded: QQ profiles can outlive thousands of sessions."""
    cache.pop(key, None)
    cache[key] = value
    while len(cache) > _CACHE_LIMIT:
        cache.pop(next(iter(cache)))
_CAPTURE = None
_JOURNAL_CHECK = None
_CTX = None
_JOURNAL_PROMPTED: set[tuple[str, str]] = set()
_FACT_PROMPTED: set[tuple[str, str]] = set()
_MEMORY_STORE: dict[str, object] = {}
# Recall results are summaries for the model, not a dump of the palace.
RECALL_SNIPPET_CHARS = 220


def _session_scope(session_id: str) -> tuple[str, str]:
    """Best-effort read-only lookup of the chat a host session belongs to."""
    if not session_id:
        return "", ""
    cached = _SCOPE_CACHE.get(session_id)
    if cached is not None:
        return cached
    chat_id, chat_type = "", ""
    try:
        home = Path(_plugin_env("HERMES_HOME") or (Path.home() / ".hermes"))
        connection = sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True, timeout=1)
        try:
            row = connection.execute(
                "SELECT chat_id, chat_type FROM sessions WHERE id=?", (session_id,)).fetchone()
        finally:
            connection.close()
        if row:
            chat_id, chat_type = str(row[0] or ""), str(row[1] or "")
            _remember(_SCOPE_CACHE, session_id, (chat_id, chat_type))
    except (sqlite3.Error, OSError, TypeError, ValueError):
        pass
    return chat_id, chat_type


_SOURCE_CACHE: dict[str, str] = {}


def _session_source(session_id: str) -> str | None:
    """Host session source ("qqbot", "cron", "cli", ...) or None when it cannot be read."""
    if not session_id:
        return ""
    cached = _SOURCE_CACHE.get(session_id)
    if cached is not None:
        return cached
    source: str | None = ""
    try:
        home = Path(_plugin_env("HERMES_HOME") or (Path.home() / ".hermes"))
        connection = sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True, timeout=1)
        try:
            row = connection.execute(
                "SELECT source FROM sessions WHERE id=?", (session_id,)).fetchone()
        finally:
            connection.close()
        if row:
            source = str(row[0] or "")
            _remember(_SOURCE_CACHE, session_id, source)
    except (sqlite3.Error, OSError, TypeError, ValueError):
        source = None
    return source


def _status(args: dict[str, Any], session_id: str = "", **kwargs) -> str:
    scope, _chat_type = _session_scope(session_id)
    snapshot = digital_life.runtime.status_snapshot(time.time() * 1000, scope=scope or None)
    try:
        snapshot["proactive"] = _proactive.schedule_status()
    except Exception:
        logger.warning("hermes-lover could not read the beat schedule", exc_info=True)
    # The prompt only shows the top loops; dumping the whole pool here kept handing
    # the model threads it had not been asked about, and it re-raised them.
    state = digital_life.storage.read_state(time.time() * 1000)
    snapshot["skills"] = digital_life.skills.summary(state)
    loops = snapshot.get("gaps") or []
    if len(loops) > OPEN_LOOP_SHOWN:
        snapshot["gaps"] = loops[:OPEN_LOOP_SHOWN]
        snapshot["gaps_hidden"] = len(loops) - OPEN_LOOP_SHOWN
    return json.dumps(snapshot, ensure_ascii=False)


def _close_gap(args: dict[str, Any], session_id: str = "", **kwargs) -> str:
    gap_id = str(args.get("id") or "").strip()
    if not gap_id:
        return json.dumps({"ok": False, "error": "id is required"}, ensure_ascii=False)
    chat_id, _chat_type = _session_scope(session_id)
    closed = digital_life.runtime.close_gap_in_chat(time.time() * 1000, gap_id, chat_id)
    if not closed:
        return json.dumps({"ok": False, "error": "no open loop with that id in this chat"},
                          ensure_ascii=False)
    return json.dumps({"ok": True, "id": gap_id}, ensure_ascii=False)


_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_PHASE_WORDS = {"sleep": "夜里", "morning": "早上", "day": "白天", "evening": "傍晚", "late": "深夜"}


CHARACTER_BRIEF = ("头两侧是蓝色鱼鳍状耳朵、像张开的小翅膀、边缘带白色绒毛（不是人类耳朵、不是精灵尖耳），"
                   "身后一条蓝色鲸尾，深蓝长发、宝石蓝眼睛、白蕾丝发带配蓝蝴蝶结、"
                   "深蓝女仆裙配白围裙和蓝宝石胸针，日系赛璐璐插画，冷蓝色调")
CHARACTER_NEGATIVE = ("眼镜, 圆框眼镜, 猫耳, 人类耳朵, 精灵尖耳, 露齿笑, 3D, 写实, 手办, plastic")


def _character_image() -> str:
    """The still that anchors generated pictures and clips of the character."""
    from .digital_life import outbox as _outbox  # noqa: PLC0415

    root = _outbox.root_dir().parent / "character"
    for name in ("ref-1.jpg", "ref-5.jpg", "canon.jpg", "canon.png"):
        path = root / name
        if path.is_file():
            return str(path)
    return ""


_REFERENCE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_REFERENCE_MAX_BYTES = 10 * 1024 * 1024


def _character_root():
    from .digital_life import outbox as _outbox  # noqa: PLC0415

    return (_outbox.root_dir().parent / "character").resolve()


def _reference_image(value: str) -> str:
    """A still the image API may read: a saved character picture and nothing else.

    The model can put any string in the ``reference`` argument, and MiniMax will
    happily base64 whatever file it names. Treating that as a trusted path let a QQ
    chat pull any file into an outbound request, so resolve it here: only regular
    images under the operator's character folder, never a URL or a data URI.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://", "data:")):
        raise ValueError("reference must be one of the saved character pictures")
    root = _character_root()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    real = path.resolve()
    if not real.is_relative_to(root):
        raise ValueError("reference must be one of the saved character pictures")
    if real.suffix.lower() not in _REFERENCE_SUFFIXES or not real.is_file():
        raise ValueError("reference must be one of the saved character pictures")
    if real.stat().st_size > _REFERENCE_MAX_BYTES:
        raise ValueError("reference picture is too large")
    return str(real)


def _skill_craft(name: str, prompt: str) -> str:
    """Bound what the request may contain while the hands are still new."""
    try:
        state = digital_life.storage.read_state(time.time() * 1000)
    except Exception:  # noqa: BLE001 - no host storage: draw with a steady hand
        return prompt
    return digital_life.skills.craft(name, digital_life.skills.level(state, name), prompt)


def _skill_level(name: str) -> int:
    try:
        state = digital_life.storage.read_state(time.time() * 1000)
    except Exception:  # noqa: BLE001 - no host storage: treat as a steady hand
        return 3
    return int(digital_life.skills.level(state, name))


def _skill_grant(name: str, *, ok: bool) -> None:
    now_ms = time.time() * 1000

    def mutate(state):
        _note_skill(state, name, ok, now_ms)
        return state

    digital_life.storage.update_state(now_ms, mutate)


def _note_skill(state: dict[str, Any], name: str, ok: bool, now_ms: float) -> None:
    result = digital_life.skills.gain(state, name, ok=ok)
    if result.get("up"):
        level = int(result.get("level") or 0)
        label = digital_life.skills.LABELS[min(level, 3)]
        try:
            store = _MEMORY_STORE["factory"]()
            store.write(f"手上功夫到了{label}（{digital_life.skills.NAMES.get(name, name)}）。",
                        kind="note", session_id="skill", chat_id="")
        except Exception:
            logger.debug("hermes-lover could not note a skill level", exc_info=True)


def _life_enabled(ctx=None) -> bool:
    ctx = ctx if ctx is not None else _CTX
    if ctx is None:
        return False
    try:
        return bool(ctx.get_config("life.enabled", True))
    except Exception:
        return False


def _now_line(state: dict[str, Any], now_ms: float) -> str:
    """One compact line of where the day has you, for the chat turn only."""
    life = state.get("life") or {}
    phase = str(life.get("phase") or "")
    local = time.localtime(now_ms / 1000.0)
    clock = time.strftime("%H:%M", local)
    weekday = _WEEKDAY_NAMES[local.tm_wday]
    current = life.get("current") or {}
    doing = str(current.get("title") or "").strip()
    if not doing:
        doing = "准备睡了" if phase == "sleep" else "在发呆"
    mood = str((state.get("mood") or {}).get("label") or "平常")
    energy = float(state.get("energy") or 0.0)
    return (f"[现在] {weekday} {clock} · {_PHASE_WORDS.get(phase, phase)} · {doing}"
            f" · 心情{mood} · 体力{energy:.0f}")[:120]


def _wake_enabled() -> bool:
    ctx = _CTX
    if ctx is None:
        return False
    try:
        return bool(ctx.get_config("wake.enabled", False))
    except Exception:
        return False


def _reconcile_wake(session_id: str = "") -> None:
    """M11.6: a resolved loop clears; a still-open one presses harder by 10."""
    from .digital_life import storage, wake as _wake  # noqa: PLC0415

    chat_id, _chat_type = _session_scope(session_id)
    if not chat_id:
        return
    now_ms = time.time() * 1000
    outcome: dict[str, str] = {}

    def mutate(state):
        outcome["result"] = _wake.reconcile(state, now_ms, chat_id=chat_id)
        return state

    storage.update_state(now_ms, mutate)
    if outcome.get("result") == "unmet":
        logger.info("hermes-lover wake commitment unmet: gap tension pressed harder")


def _maybe_wake(session_id: str = "") -> None:
    """Queue and deliver one self-wake; every host failure is silent by design."""
    from .digital_life import storage, wake as _wake  # noqa: PLC0415

    ctx = _CTX
    if ctx is None or not _wake_enabled():
        return
    chat_id, _chat_type = _session_scope(session_id)
    if not chat_id:
        return
    now_ms = time.time() * 1000
    plan: dict[str, object] = {}

    def mutate(state):
        plan["wake"] = _wake.plan_wake(state, now_ms, chat_id=chat_id)
        return state

    storage.update_state(now_ms, mutate)
    item = plan.get("wake")
    if not isinstance(item, dict):
        return
    wake_id = str(item.get("id") or "")

    def drop(state):
        _wake.cancel(state, wake_id)
        return state

    def deliver(state):
        _wake.mark_delivered(state, wake_id, now_ms)
        return state

    fresh = storage.read_state(now_ms)
    if not _wake.verify_queued(fresh, wake_id):
        storage.update_state(now_ms, drop)
        logger.info("hermes-lover wake dropped: gap closed or queue rewritten before delivery")
        return
    inject = getattr(ctx, "inject_message", None)
    if not callable(inject):
        storage.update_state(now_ms, drop)
        logger.info("hermes-lover wake skipped: host has no inject_message")
        return
    try:
        accepted = inject(_wake.WAKE_TEXT, role="user", session_key=session_id or None)
    except Exception:
        accepted = False
        logger.warning("hermes-lover wake injection failed", exc_info=True)
    if accepted:
        storage.update_state(now_ms, deliver)
        logger.info("hermes-lover wake delivered for gap %s", item.get("gap_id"))
    else:
        storage.update_state(now_ms, drop)
        logger.info("hermes-lover wake skipped: gateway injection unavailable or disabled")



def _distill_prompt(chat_id: str, fact_day: str, journal_day: str) -> str:
    """The one-shot job that writes a day's facts and diary outside the chat turn."""
    lines = [f"[lover-child:journal chat={chat_id}]",
             "你是记忆维护子会话，只做下面这件事，不要回复用户、不要发消息。"]
    if fact_day:
        lines.append(
            f"1) 先用 memory_recall(day=\"{fact_day}\", kind=\"turn\") 读出那天的 turn id，"
            f"再挑 1–3 条真正重要的原子事实，逐条 memory_write(kind=\"fact\", day=\"{fact_day}\", "
            f"text=\"一句话说清一件事\", sources=[\"hp-…\"])；每条事实只写一件事。")
    if journal_day:
        lines.append(
            f"先 memory_recall(day=\"{journal_day}\", kind=\"life\", k=8) 看那天你自己做过什么"
            f"（读的哪几页、想了什么）。那些是你做过的事，不是对方说的话：写日记时要用上，"
            f"写事实时不要跟聊天内容混成一件。")
        lines.append(
            f"2) 用 memory_recall(day=\"{journal_day}\", kind=\"fact\") 取 fact id，"
            f"再 memory_write(kind=\"journal\", day=\"{journal_day}\", text=\"…\", refs=[\"hp-…\"]) "
            f"写一篇 120–400 字第一人称日记：写你那天做了什么（包括自己读的、想的）、当时的心情，正常成段，"
            f"不要按时间点罗列，不要流水账；日记写的是已经过完的那一天。")
    if fact_day and journal_day and fact_day != journal_day:
        lines.append(f"两个日期不同：{fact_day} 写事实，{journal_day} 写日记。")
    return "\n".join(lines)


_CHILD_SESSIONS: dict[str, str | None] = {}
_CHILD_CHATS: dict[str, str] = {}
_MAX_CHILD_SESSIONS = 256
_CHILD_MARK = "[lover-child:"


def _stop_old_bridge(home_dir: str) -> bool:
    """Stop the previous bridge process after checking its command line.

    Bridges from before the token upgrade never wrote a pidfile, so the pidfile
    alone cannot find them: also take whichever process holds the loopback port.
    A pid is only killed when its command line mentions the bridge script.
    """
    port = os.environ.get("QZONE_BRIDGE_PORT") or "8765"
    path = Path(os.environ.get("QZONE_BRIDGE_PID_FILE")
                or (Path(home_dir) / "plugin-data" / "hermes-lover" / "qzone-bridge.pid"))
    candidates: list[int] = []
    try:
        candidates.append(int(path.read_text("utf-8").strip()))
    except (OSError, ValueError):
        pass
    candidates.extend(_listening_pids(port))
    killed = False
    for pid in dict.fromkeys(candidates):
        if pid <= 0 or pid == os.getpid():
            continue
        try:
            command = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                     capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if "qzone-bridge.py" not in command:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed = True
        except OSError:
            continue
    try:
        path.unlink()
    except OSError:
        pass
    return killed


def _listening_pids(port: str) -> list[int]:
    """Pids holding a local TCP listen socket, via lsof; [] when it cannot be read."""
    try:
        out = subprocess.run(["lsof", "-nP", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for token in out.split():
        try:
            pids.append(int(token))
        except ValueError:
            continue
    return pids


def _remember_child(session_id: str, kind: str | None) -> None:
    """Cache one child lookup and keep the cache from growing forever.

    Background sessions are one-shot; evict the oldest entry (dicts preserve
    insertion order).
    """
    _CHILD_SESSIONS[session_id] = kind
    while len(_CHILD_SESSIONS) > _MAX_CHILD_SESSIONS:
        oldest = next(iter(_CHILD_SESSIONS))
        _CHILD_SESSIONS.pop(oldest)
        _CHILD_CHATS.pop(oldest, None)


def _child_kind(session_id: str) -> str | None:
    """Which background child this session is ("study"/"task"/"search"), else None.

    A child is a gateway-hosted one-shot session, so env vars set by the spawner never
    reach it; the prompt tag does, and it survives the queue.

    The tag is user text, so anyone can type it. Only a session the host itself
    opened as a one-shot can be treated as a child; a chat that merely contains
    "[lover-child:...]" stays an ordinary chat and keeps its own scope.
    """
    if not session_id:
        return None
    if session_id in _CHILD_SESSIONS:
        return _CHILD_SESSIONS[session_id]
    source = _session_source(session_id)
    if source != "oneshot":
        if source is not None:  # remember "not a child"; on a read error, retry later
            _remember_child(session_id, None)
        return None
    db = Path(_plugin_env("HERMES_HOME") or (Path.home() / ".hermes")) / "state.db"
    try:
        with sqlite3.connect(str(db), timeout=2) as conn:
            row = conn.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='user' "
                "AND content LIKE '%[lover-child:%' LIMIT 1",
                (session_id,)).fetchone()
    except sqlite3.Error:
        return None
    kind = None
    text = str(row[0] or "") if row else ""
    start = text.find(_CHILD_MARK)
    if start >= 0:
        end = text.find("]", start)
        if end > start:
            tag = text[start + len(_CHILD_MARK):end].strip()
            kind, _, chat = tag.partition(" chat=")
            kind = kind.strip() or None
            if chat.strip():
                _CHILD_CHATS[session_id] = chat.strip()
    _remember_child(session_id, kind)
    return kind


def _child_chat(session_id: str) -> str:
    """The chat a spawned child writes for, carried in its prompt tag."""
    if not session_id:
        return ""
    _child_kind(session_id)
    return _CHILD_CHATS.get(session_id, "")


def _gate_tool_call(tool_name: str = "", session_id: str = "", **kwargs) -> dict[str, Any] | None:
    """Veto runaway tool loops inside one QQ turn (hard limit, not a prompt hint)."""
    if not tool_name or not session_id:
        return None
    kind = _child_kind(session_id)
    if tool_name not in MEDIA_GENERATORS and tool_name.strip().lower() in MEDIA_OUTSIDERS:
        return {"action": "block",
                "message": "画图、视频、配音只走 make_image / make_video / make_speech，别用别的生成工具。"}
    if tool_name in MINIMAX_TOOLS and not _is_minimax_model(session_id=session_id):
        return {"action": "block",
                "message": "MiniMax 的图片/视频/语音工具只给 MiniMax 模型用；现在不是，先别调。"}
    if kind is not None:
        allow = {"study": {"read_source", "memory_write"},
                 "task": {"web_search", "web_extract", "read_source", "memory_write"},
                 "search": {"web_search", "web_extract"},
                 "journal": {"memory_recall", "memory_write", "digital_life_status"},
                 "polish": set()}.get(kind, set())
        if tool_name not in allow:
            return {"action": "block",
                    "message": f"background {kind} sessions can only use their own tools; finish with what you have."}
    meta = _TURN_META.get(session_id) or {}
    if (meta.get("platform") or _session_source(session_id) or "") != "qqbot":
        return None
    if _budget_stopped(session_id) and tool_name not in ("background_task", "search_later"):
        return {"action": "block",
                "message": "工具额度已经用完了，这一次必须直接回话；剩下的事交给 background_task。"}
    counts = _TOOL_TURNS.setdefault(session_id, {"total": 0, "zone": 0, "recall": 0,
                                                 "last_write": ""})
    if tool_name == "memory_recall":
        counts["recall"] = counts.get("recall", 0) + 1
    if tool_name == "read_source":
        counts["read"] = counts.get("read", 0) + 1
        if counts["read"] > READ_SOURCE_TURN_LIMIT:
            return {"action": "block",
                    "message": "本轮最多读 3 页。剩下的一本书交给 study_source 后台读，先回话。"}
    if tool_name == "memory_write":
        payload = kwargs.get("args") or kwargs.get("arguments") or {}
        text = str(payload.get("text") or "") if isinstance(payload, dict) else ""
        if text and text == counts.get("last_write"):
            return {"action": "block",
                    "message": "这条你刚记过，别重复写。接着回话。"}
        counts["last_write"] = text
    # Reading and remembering are not loop risks: pages, the daily quota and the
    # duplicate guard already bound them. Paid media calls and sends do hit the
    # outside world, so they count toward the turn budget like search does.
    if tool_name in ("read_source", "library_list", "memory_write", "memory_recall",
                     "memory_revoke", "digital_life_status", "close_gap",
                     "study_source", "background_task", "search_later"):
        return None
    if tool_name == "skill_manage":
        return {"action": "block",
                "message": "聊天里只能看 skill，不能改。真要改我去终端里做。"}
    if tool_name in ("web_search", "web_extract") and counts.get("read"):
        return {"action": "block",
                "message": "这一轮已经用 read_source 读了，别再搜网页。先把读到的回话；没读完的部分交给 study_source 后台读。"}
    if tool_name == "qzone_feed" and counts.get("recall", 0) == 0:
        return {"action": "block",
                "message": "先查记忆：这段你看过没有，用 memory_recall 查一下。已经读过的直接从记忆里答，不要再抓一遍。"}
    if tool_name == "qzone_feed" and counts["zone"] >= ZONE_PAGE_LIMIT:
        return {"action": "block",
                "message": "本轮空间最多翻 2 页，已经到上限。用手上的内容回答，或者把后续交给 background_task。"}
    if counts["total"] >= TOOL_CALL_LIMIT:
        stops = _BUDGET_STOPS.get(session_id, 0) + 1
        _BUDGET_STOPS[session_id] = stops
        if stops == 1:
            return {"action": "block",
                    "message": "本轮工具调用已达 8 次。先回话，把剩下的工作交给 background_task。"}
        return {"action": "block",
                "message": "额度用完了（8 次），而且你又伸手了。现在不要再调用任何工具，直接用手上已有的信息回话。"}
    counts["total"] += 1
    if tool_name == "qzone_feed":
        counts["zone"] += 1
    return None


def _post_llm_call(assistant_response: str = "", session_id: str = "", platform: str = "",
                   user_message: str = "", **kwargs) -> None:
    from .digital_life import wake as _wake  # noqa: PLC0415

    _wake.set_agent_running(False)
    try:
        _reconcile_wake(session_id)
    except Exception:
        logger.warning("hermes-lover wake reconciliation failed", exc_info=True)
    if assistant_response:
        record_turn(time.time() * 1000, success=True, user_message=user_message or "")
        try:
            if _CAPTURE is not None:
                _CAPTURE(assistant_response, session_id=session_id or "", platform=platform or "",
                         user_message=user_message or "")
        except Exception:
            pass
        try:
            if assistant_response.lstrip().startswith("/"):
                return
            unknown = digital_life.domain.find_gap_sentence(assistant_response)
            if unknown:
                chat_id, _chat_type = _session_scope(session_id)
                digital_life.runtime.observe_assistant_unknown(time.time() * 1000, unknown, chat=chat_id)
        except Exception:
            pass
    try:
        _maybe_wake(session_id)
    except Exception:
        logger.warning("hermes-lover wake hook failed", exc_info=True)


def _api_error(error: dict[str, Any] | None = None, retryable: bool | None = None, **kwargs) -> None:
    now_ms = time.time() * 1000
    error_type = str((error or {}).get("type", ""))
    record_api_error(now_ms, error_type=error_type, retryable=retryable)


def _post_tool_call(tool_name: str = "", args: dict[str, Any] | None = None, result: str = "", **kwargs) -> None:
    chat_id, _chat_type = _session_scope(str(kwargs.get("session_id") or ""))
    digital_life.runtime.observe_tool_result(time.time() * 1000, tool_name=tool_name, args=args or {},
                                             result=result, chat=chat_id)


_RESET_COMMAND = re.compile(r"^/(?:new|reset)(?:\s+(?P<scope>all|全部))?(?:\s|$)", re.IGNORECASE)
_HELP_COMMAND = re.compile(r"^/(?:help|帮助|命令)(?:\s|$)", re.IGNORECASE)
_HELP_TEXT = (
    "可用命令（仅所有者生效）：\n"
    "/new —— 只换会话，不清记忆\n"
    "/new all —— 换掉所有 QQ 会话\n"
    "/forget —— 清空当前会话记忆，删了就回不来\n"
    "/forget all —— 清空所有 QQ 会话记忆\n"
    "/core —— 查看当前情绪、能量和未解问题\n"
    "/restart —— 重启网关，让改过的插件代码生效\n"
    "/help —— 显示这张表"
)
_RESTART_COMMAND = re.compile(r"^/(?:restart|重启|重载)(?:\s|$)", re.IGNORECASE)


_RESTART_PENDING: dict[str, float] = {}
_RESTART_LAST_AT = 0.0
RESTART_CONFIRM_SECONDS = 180
RESTART_COOLDOWN_SECONDS = 60


def _restart_gateway_later(delay: float = 1.5) -> None:
    def fire() -> None:
        try:
            subprocess.Popen(["hermes", "gateway", "restart", "--all", "--detach", "-y"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
        except OSError:
            logger.warning("hermes-lover /restart could not launch hermes gateway restart")

    timer = threading.Timer(delay, fire)
    timer.daemon = True
    timer.start()


_FORGET_COMMAND = re.compile(r"^/(?:forget|clear-memory|清空记忆|清理记忆)(?:\s+(?P<scope>all|全部))?(?:\s|$)", re.IGNORECASE)
_RESET_SCRUB = 0


def _plugin_env(name: str) -> str:
    value = os.environ.get(name, "")
    if value:
        return value
    home = os.environ.get("HERMES_HOME", "")
    path = os.path.join(home, ".env") if home else ""
    if path and os.path.exists(path):
        try:
            for line in open(path, encoding="utf-8"):
                key, _, raw = line.strip().partition("=")
                if key == name:
                    return raw.strip().strip('"').strip("'")
        except OSError:
            return ""
    return ""


def _reset_admins() -> set[str]:
    raw = ",".join((_plugin_env("QQ_OWNER_ID"), _plugin_env("QQ_ALLOWED_USERS")))
    return {item.strip() for item in raw.split(",") if item.strip()}


def _on_gateway_dispatch(event=None, **kwargs):
    """QQ commands: ``/new`` rotates a conversation, ``/forget`` wipes memory.

    Rotating and forgetting used to be one command, so a full context window
    silently erased the palace. They are separate now: ``/new`` never touches
    memory, ``/forget`` never rotates. Both stay owner-only for the ``all``
    scope; ``/forget`` is owner-only for every scope.
    """
    try:
        text = (getattr(event, "text", "") or "").strip()
        source = getattr(event, "source", None)
        platform = getattr(getattr(source, "platform", None), "value", "") or ""
        if platform != "qqbot":
            return None
        if _RESTART_COMMAND.match(text):
            global _RESTART_LAST_AT
            chat_id = str(getattr(source, "chat_id", "") or "")
            sender = str(getattr(event, "sender_id", "") or getattr(source, "user_id", "") or "")
            if sender not in _reset_admins():
                return None
            now = time.time()
            if now - _RESTART_LAST_AT < RESTART_COOLDOWN_SECONDS:
                _send_qq(chat_id, "正在重启，别急，等它回来。")
                return {"action": "skip", "reason": "hermes-lover /restart cooling down"}
            first = _RESTART_PENDING.get(chat_id, 0.0)
            if not first or now - first > RESTART_CONFIRM_SECONDS:
                _RESTART_PENDING[chat_id] = now
                _send_qq(chat_id, "要重启网关吗？3 分钟内再发一次 /restart 确认。")
                return {"action": "skip", "reason": "hermes-lover /restart needs confirmation"}
            _RESTART_PENDING.pop(chat_id, None)
            _RESTART_LAST_AT = now
            _send_qq(chat_id, "重启中，十来秒后回来。")
            _restart_gateway_later()
            logger.info("hermes-lover /restart by owner")
            return {"action": "skip", "reason": "hermes-lover /restart"}
        if _HELP_COMMAND.match(text):
            chat_id = str(getattr(source, "chat_id", "") or "")
            sender = str(getattr(event, "sender_id", "") or getattr(source, "user_id", "") or "")
            if sender in _reset_admins():
                _send_qq(chat_id, _HELP_TEXT)
                return {"action": "skip", "reason": "hermes-lover /help"}
            return None
        forget = _FORGET_COMMAND.match(text)
        reset = _RESET_COMMAND.match(text)
        if not forget and not reset:
            return None
        sender = str(getattr(event, "sender_id", "") or getattr(source, "user_id", "") or "")
        chat_id = str(getattr(source, "chat_id", "") or "")
        all_chats = ((forget or reset).group("scope") or "").lower() in {"all", "全部"}
        admin = sender in _reset_admins()
        if not admin:
            logger.info("hermes-lover command ignored: sender %s is not an owner", sender[-6:])
            return None
        if forget:
            if not all_chats and not chat_id:
                logger.info("hermes-lover /forget ignored: the event carries no chat id")
                return {"action": "skip", "reason": "hermes-lover /forget without a chat"}
            factory = _MEMORY_STORE.get("factory")
            store = factory() if factory is not None else None
            cleared = store.clear(None if all_chats else chat_id) if store is not None else 0
            if all_chats:
                now_ms = time.time() * 1000
                from .digital_life import domain as _domain, storage as _storage  # noqa: PLC0415

                _storage.update_state(now_ms, lambda state: _domain.default_state(now_ms))
                logger.info("hermes-lover /forget all by owner: cleared %s entries, life state reset", cleared)
                _send_qq(chat_id, "所有会话的记忆都清了。")
                return {"action": "skip", "reason": "hermes-lover /forget"}
            left = 0
            if store is not None:
                left = sum(1 for entry in store.read_entries().values() if not entry.get("archived"))
            logger.info("hermes-lover /forget by owner: cleared %s entries in this chat, %s left", cleared, left)
            tail = f"另有 {left} 条别的记录没动，要全清用 /forget all。" if left else "别的记录没动。"
            _send_qq(chat_id, f"这个会话的记忆清了。{tail}")
            return {"action": "skip", "reason": "hermes-lover /forget"}
        if all_chats:
            session_store = kwargs.get("session_store")
            rotated = 0
            if session_store is not None:
                try:
                    for entry in session_store.list_sessions():
                        platform_name = entry.platform.value if entry.platform else ""
                        if platform_name != "qqbot":
                            continue
                        if session_store.reset_session(entry.session_key) is not None:
                            rotated += 1
                except Exception:
                    logger.warning("hermes-lover /new all could not reset host sessions", exc_info=True)
            logger.info("hermes-lover /new all: %s host sessions rotated", rotated)
            return {"action": "rewrite", "text": "/new all"}
        if text.lower().strip() != "/new":
            return {"action": "rewrite", "text": "/new"}
    except Exception:
        logger.warning("hermes-lover gateway command failed", exc_info=True)
    return None


def _host_send_message_tool():
    """The host send tool, or ``None`` when a host update moved it away."""
    try:
        from tools.send_message_tool import send_message_tool  # noqa: PLC0415

        return send_message_tool
    except (ImportError, AttributeError):
        logger.warning(
            "hermes-lover: host tools.send_message_tool is unavailable; "
            "command confirmations and beat sends will be skipped"
        )
        return None


def _send_qq(chat_id: str, message: str) -> None:
    """Best-effort confirmation for a gateway command that skips the model."""
    if not chat_id:
        return
    send_message_tool = _host_send_message_tool()
    if send_message_tool is None:
        return
    try:
        send_message_tool({"action": "send", "target": f"qqbot:{chat_id}", "message": message})
    except Exception:
        logger.warning("hermes-lover could not send command confirmation", exc_info=True)


def _share_video(args, session_id="", **kwargs):
    from .digital_life import budget as _budget, outbox as _outbox  # noqa: PLC0415

    chat_id, _chat_type = _session_scope(session_id) or ("", "")
    if not chat_id:
        return json.dumps({"ok": False, "error": "no chat to send to"}, ensure_ascii=False)
    platform = (_TURN_META.get(session_id) or {}).get("platform") or _session_source(session_id)
    if platform != "qqbot":
        return json.dumps({"ok": False, "error": "videos only go to QQ chats"},
                          ensure_ascii=False)
    try:
        path = _outbox.resolve(str(args.get("name") or ""), chat_id)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    if not _budget.spend(f"video:{chat_id}", VIDEO_PER_DAY):
        return json.dumps({"ok": False,
                           "error": f"今天已经发过 {VIDEO_PER_DAY} 条视频了，明天再说。"},
                          ensure_ascii=False)
    send = _host_send_message_tool()
    if send is None:
        _budget.refund(f"video:{chat_id}")
        return json.dumps({"ok": False, "error": "host send tool unavailable"},
                          ensure_ascii=False)
    caption = " ".join(str(args.get("caption") or "").split())[:120]
    message = f"{caption}\nMEDIA:{path}" if caption else f"MEDIA:{path}"
    try:
        result = send({"action": "send", "target": f"qqbot:{chat_id}", "message": message})
    except Exception as exc:  # noqa: BLE001
        _budget.refund(f"video:{chat_id}")
        logger.warning("hermes-lover could not send a video", exc_info=True)
        return json.dumps({"ok": False, "error": f"send failed: {exc}"[:200]},
                          ensure_ascii=False)
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = {}
    if isinstance(result, dict) and result.get("error"):
        _budget.refund(f"video:{chat_id}")
        return json.dumps({"ok": False, "error": str(result["error"])[:200]},
                          ensure_ascii=False)
    return json.dumps({"ok": True, "sent": path.name, "caption": caption},
                      ensure_ascii=False)


# Two Hailuo tasks at a time; the slot is taken before the paid call, so a full queue
# refuses the request instead of charging for a task that will be thrown away.
_VIDEO_SLOTS = threading.BoundedSemaphore(2)


def _deliver_video(chat_id: str, task_id: str, prompt: str, key: str = "") -> None:
    """Wait for one Hailuo task, save it, and send it to the chat that asked."""
    from .digital_life import budget as _budget, minimax as _minimax  # noqa: PLC0415

    send = _host_send_message_tool()
    try:
        file_id = _minimax.wait(task_id, key=key)
        path = _minimax.download(file_id, key=key, chat_id=chat_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-lover video task %s failed", task_id, exc_info=True)
        _skill_grant("make_video", ok=False)
        if send is not None:
            try:
                send({"action": "send", "target": f"qqbot:{chat_id}",
                      "message": f"视频没做出来：{str(exc)[:120]}"})
            except Exception:
                logger.warning("hermes-lover could not report a failed video", exc_info=True)
        return
    if send is None:
        return
    caption = " ".join(str(prompt or "").split())[:60]
    message = f"做好了：{caption}\nMEDIA:{path}" if caption else f"MEDIA:{path}"
    try:
        send({"action": "send", "target": f"qqbot:{chat_id}", "message": message})
    except Exception:  # noqa: BLE001
        logger.warning("hermes-lover could not send a generated video", exc_info=True)


def _media_chat(session_id: str) -> tuple[str, str]:
    """The chat and platform a media call belongs to, or ("", "") when it is unusable."""
    chat_id, _chat_type = _session_scope(session_id) or ("", "")
    platform = (_TURN_META.get(session_id) or {}).get("platform") or _session_source(session_id)
    return chat_id, platform or ""


def _charge_media(kind: str, chat_id: str, per_chat: int, per_profile: int) -> str:
    """Charge one metered call to both the chat and the profile.

    Returns "" on success, else "chat" / "profile" for the exhausted ceiling or
    "storage" when the count cannot be recorded at all. A metered call must not run
    unmetered just because the host has no storage.
    """
    from .digital_life import budget as _budget  # noqa: PLC0415

    hit = _budget.spend_all({f"{kind}:{chat_id}": per_chat,
                             f"{kind}:{_PROFILE_COUNTER}": per_profile})
    if hit is None:
        return ""
    if hit == "storage":
        return "storage"
    return "profile" if hit.endswith(":" + _PROFILE_COUNTER) else "chat"


def _refund_media(kind: str, chat_id: str) -> None:
    from .digital_life import budget as _budget  # noqa: PLC0415

    _budget.refund(f"{kind}:{chat_id}")
    _budget.refund(f"{kind}:{_PROFILE_COUNTER}")


def _media_quota_error(label: str, hit: str, chat_text: str) -> str:
    if hit == "storage":
        return "现在记不了额度，先不动手；等宿主存储恢复再说。"
    if hit == "profile":
        return f"今天总共能用的{label}已经用完了，明天再说。"
    return chat_text


def _audio_path(value: str, chat_id: str):
    """A local voice clip this chat may transcribe: its own media or the operator inbox.

    The outbox root is shared by every chat, so it must not be readable here; the
    inbox is the operator's deliberate hand-in.
    """
    from .digital_life import library, outbox  # noqa: PLC0415

    raw = str(value or "").strip()
    if not raw:
        raise ValueError("name an audio file first (in the inbox or outbox folder)")
    if raw.startswith(("http://", "https://")):
        raise ValueError("only local audio files can be transcribed")
    path = Path(raw).expanduser()
    roots = [outbox.chat_dir(chat_id), library.inbox_root()]
    if path.is_absolute():
        real = path.resolve()
        if not any(real.is_relative_to(root.resolve()) for root in roots):
            raise ValueError("audio must live in the inbox or your outbox folder")
        if not real.is_file():
            raise ValueError(f"no such audio file: {raw}")
        return real
    for root in roots:
        candidate = (root / raw).resolve()
        if candidate.is_relative_to(root.resolve()) and candidate.is_file():
            return candidate
    raise ValueError(f"no such audio file: {raw}")


MEDIA_JOBS_MAX = 2
_MEDIA_JOBS: set[object] = set()
_MEDIA_JOBS_LOCK = threading.Lock()


def _run_media_later(work) -> bool:
    """Run one generation off the turn so the chat gets words first, media after."""
    token = object()
    with _MEDIA_JOBS_LOCK:
        if len(_MEDIA_JOBS) >= MEDIA_JOBS_MAX:
            return False
        _MEDIA_JOBS.add(token)

    def run() -> None:
        try:
            work()
        finally:
            with _MEDIA_JOBS_LOCK:
                _MEDIA_JOBS.discard(token)

    threading.Thread(target=run, daemon=True, name="hermes-lover-media").start()
    return True


def _wait_for_quiet_turn(*, timeout: float = 120.0, grace: float = 1.5) -> None:
    """Hold a finished picture until the reply of this turn has gone out.

    The host delivers the model's text after the turn ends, while a media worker
    sends as soon as the file exists; without this wait the picture can arrive
    before the sentence that introduces it.
    """
    from .digital_life import wake as _wake  # noqa: PLC0415

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            busy = _wake.agent_running()
        except Exception:  # noqa: BLE001 - never block delivery on a state read
            break
        if not busy:
            break
        time.sleep(0.5)
    time.sleep(grace)


def _caption_to_repeat(caption: str, recent: list[str]) -> str:
    """Only the part of a caption the chat has not already been told."""
    said = {re.sub(r"\s+", "", line) for line in recent if line.strip()}
    kept = []
    for line in caption.splitlines():
        bare = re.sub(r"\s+", "", line)
        if not bare or bare in said:
            continue
        kept.append(line.strip())
    return "\n".join(kept)


def _send_media_now(chat_id: str, path, caption: str = "") -> None:
    _wait_for_quiet_turn()
    caption = _caption_to_repeat(caption, _RECENT_BUBBLES.get(chat_id, []))
    sender = _host_send_message_tool()
    message = f"{caption}\nMEDIA:{path}" if caption else f"MEDIA:{path}"
    sender({"action": "send", "target": f"qqbot:{chat_id}", "message": message})


def _send_text_now(chat_id: str, text: str) -> None:
    try:
        _host_send_message_tool()({"action": "send", "target": f"qqbot:{chat_id}", "message": text})
    except Exception:  # noqa: BLE001 - the chat is gone; nothing else to do
        logger.warning("hermes-lover could not deliver a media failure", exc_info=True)


def _mood_flavor() -> str:
    """What today feels like, so a picture of herself is not always the same face."""
    try:
        state = digital_life.storage.read_state(time.time() * 1000)
    except Exception:  # noqa: BLE001 - no state: draw without a mood line
        return ""
    mood = (state.get("mood") or {}).get("label") or ""
    energy = state.get("energy")
    bits = []
    if mood:
        bits.append(f"情绪底色是{mood}")
    if isinstance(energy, (int, float)):
        bits.append(f"体力{int(energy)}")
    return "，".join(bits)


def _make_image(args, session_id="", **kwargs):
    from .digital_life import minimax as _minimax  # noqa: PLC0415

    chat_id, platform = _media_chat(session_id)
    if not chat_id:
        return json.dumps({"ok": False, "error": "no chat to send to"}, ensure_ascii=False)
    if platform != "qqbot":
        return json.dumps({"ok": False, "error": "images only go to QQ chats"}, ensure_ascii=False)
    prompt = " ".join(str(args.get("prompt") or "").split())
    if not prompt:
        return json.dumps({"ok": False, "error": "describe the image first"}, ensure_ascii=False)
    aspect = str(args.get("aspect_ratio") or "1:1")
    reference = str(args.get("reference") or "").strip()
    if args.get("character") and not reference:
        reference = _character_image()
    try:
        reference = _reference_image(reference)  # validate before spending the quota
    except (OSError, ValueError) as exc:
        return json.dumps({"ok": False, "error": str(exc)[:200]}, ensure_ascii=False)
    hit = _charge_media("imagegen", chat_id, IMAGE_GEN_PER_DAY, IMAGE_GEN_PER_PROFILE)
    if hit:
        return json.dumps({"ok": False, "error": _media_quota_error(
            "图", hit, f"今天已经画过 {IMAGE_GEN_PER_DAY} 张了，明天再说。")}, ensure_ascii=False)
    if args.get("character"):
        prompt = f"{prompt}。{CHARACTER_BRIEF}"
        flavor = _mood_flavor()
        if flavor:
            prompt = f"{prompt}。{flavor}"
    caption = " ".join(str(args.get("caption") or "").split())

    skilled = _skill_craft("make_image", prompt)
    look = str(args.get("look") or "auto")
    anime = look == "anime" or (look == "auto" and bool(args.get("character")))
    media_model = _minimax.IMAGE_LIVE_MODEL if anime else _minimax.IMAGE_MODEL
    media_style = "漫画" if anime else ""

    def work() -> None:
        from .digital_life import minimax as media_mod  # noqa: PLC0415
        from .digital_life import skills as media_skills  # noqa: PLC0415
        try:
            path = media_mod.create_image(skilled, aspect_ratio=aspect, reference=reference,
                                          model=media_model, style=media_style,
                                          chat_id=chat_id,
                                          negative=", ".join(x for x in (
                                              media_skills.negative_prompt(_skill_level("make_image")),
                                              CHARACTER_NEGATIVE if args.get("character") else "") if x))
            _send_media_now(chat_id, path, caption)
            _skill_grant("make_image", ok=True)
        except Exception as exc:  # noqa: BLE001
            _refund_media("imagegen", chat_id)
            _skill_grant("make_image", ok=False)
            _send_text_now(chat_id, f"图没画出来：{str(exc)[:120]}")

    if not _run_media_later(work):
        _refund_media("imagegen", chat_id)
        return json.dumps({"ok": False, "error": "同时在做的媒体太多了，等一下再试。"},
                          ensure_ascii=False)
    return json.dumps({"ok": True, "queued": True,
                       "message": "已经在画了；画好直接发到这个会话，不用再问。手还生，画出来会偏简单，"
                                  "别把它说得多精细。"},
                      ensure_ascii=False)


def _make_speech(args, session_id="", **kwargs):
    chat_id, platform = _media_chat(session_id)
    if not chat_id:
        return json.dumps({"ok": False, "error": "no chat to send to"}, ensure_ascii=False)
    if platform != "qqbot":
        return json.dumps({"ok": False, "error": "speech only goes to QQ chats"}, ensure_ascii=False)
    text = str(args.get("text") or "").strip()
    if not text:
        return json.dumps({"ok": False, "error": "nothing to say"}, ensure_ascii=False)

    voice = str(args.get("voice") or "").strip()  # empty means the module default
    try:
        speed = float(args.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0

    hit = _charge_media("speech", chat_id, SPEECH_PER_DAY, SPEECH_PER_PROFILE)
    if hit:
        return json.dumps({"ok": False, "error": _media_quota_error(
            "语音", hit, "今天已经说过 10 段了，明天再说。")}, ensure_ascii=False)
    caption = " ".join(str(args.get("caption") or "").split())

    def work() -> None:
        from .digital_life import minimax as media_mod  # noqa: PLC0415
        try:
            path = media_mod.create_speech(text, voice=voice, speed=speed, chat_id=chat_id)
            _send_media_now(chat_id, path, caption)
        except Exception as exc:  # noqa: BLE001
            _refund_media("speech", chat_id)
            _send_text_now(chat_id, f"语音没做出来：{str(exc)[:120]}")

    if not _run_media_later(work):
        _refund_media("speech", chat_id)
        return json.dumps({"ok": False, "error": "同时在做的媒体太多了，等一下再试。"},
                          ensure_ascii=False)
    return json.dumps({"ok": True, "queued": True,
                       "message": "已经在录了；做好直接发到这个会话，不用再问。"},
                      ensure_ascii=False)


def _transcribe_audio(args, session_id="", **kwargs):
    from .digital_life import minimax as _minimax  # noqa: PLC0415

    chat_id, platform = _media_chat(session_id)
    if not chat_id:
        return json.dumps({"ok": False, "error": "no chat to transcribe for"}, ensure_ascii=False)
    if platform != "qqbot":
        return json.dumps({"ok": False, "error": "transcription is only available in QQ chats"},
                          ensure_ascii=False)
    try:
        path = _audio_path(str(args.get("name") or args.get("path") or ""), chat_id)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    hit = _charge_media("stt", chat_id, TRANSCRIBE_PER_DAY, TRANSCRIBE_PER_PROFILE)
    if hit:
        return json.dumps({"ok": False, "error": _media_quota_error(
            "语音转写", hit, "今天已经转过 20 段语音了，明天再说。")}, ensure_ascii=False)
    try:
        result = _minimax.transcribe(path, language=str(args.get("language") or ""))
    except Exception as exc:  # noqa: BLE001
        _refund_media("stt", chat_id)
        return json.dumps({"ok": False, "error": str(exc)[:200]}, ensure_ascii=False)
    text = str(result.get("text") or "")[:4000]
    return json.dumps({"ok": True, "text": text, "duration": result.get("duration") or 0},
                      ensure_ascii=False)


def _make_video(args, session_id="", **kwargs):
    from .digital_life import minimax as _minimax  # noqa: PLC0415

    chat_id, _chat_type = _session_scope(session_id) or ("", "")
    if not chat_id:
        return json.dumps({"ok": False, "error": "no chat to send to"}, ensure_ascii=False)
    platform = (_TURN_META.get(session_id) or {}).get("platform") or _session_source(session_id)
    if platform != "qqbot":
        return json.dumps({"ok": False, "error": "videos only go to QQ chats"},
                          ensure_ascii=False)
    prompt = " ".join(str(args.get("prompt") or "").split())
    if not prompt:
        return json.dumps({"ok": False, "error": "describe the video first"},
                          ensure_ascii=False)
    try:
        duration = int(args.get("duration") or 6)
    except (TypeError, ValueError):
        duration = 6
    if not _VIDEO_SLOTS.acquire(blocking=False):
        return json.dumps({"ok": False,
                           "error": "已经有两个视频在做了，等它们发完再说。"},
                          ensure_ascii=False)
    hit = _charge_media("videogen", chat_id, VIDEO_GEN_PER_DAY, VIDEO_GEN_PER_PROFILE)
    if hit:
        _VIDEO_SLOTS.release()
        return json.dumps({"ok": False, "error": _media_quota_error(
            "视频", hit, f"今天已经做过 {VIDEO_GEN_PER_DAY} 个视频了，明天再说。")},
                          ensure_ascii=False)
    try:
        if args.get("character"):
            prompt = f"{prompt}。{CHARACTER_BRIEF}"
        skilled = _skill_craft("make_video", prompt)
        first_frame = _character_image() if args.get("character") else ""
        task_id, key = _minimax.create(skilled, duration=duration,
                                       resolution=str(args.get("resolution") or "768P"),
                                       first_frame=first_frame)
    except Exception as exc:  # noqa: BLE001
        _refund_media("videogen", chat_id)
        _VIDEO_SLOTS.release()
        _skill_grant("make_video", ok=False)
        return json.dumps({"ok": False, "error": str(exc)[:200]}, ensure_ascii=False)

    def _run() -> None:
        try:
            _deliver_video(chat_id, task_id, prompt, key)
            _skill_grant("make_video", ok=True)
        finally:
            _VIDEO_SLOTS.release()

    worker = threading.Thread(target=_run, name="hermes-lover-video", daemon=True)
    try:
        worker.start()
    except Exception as exc:  # noqa: BLE001 - no thread: the task was never delivered
        _refund_media("videogen", chat_id)
        _VIDEO_SLOTS.release()
        return json.dumps({"ok": False, "error": f"没法启动生成任务：{str(exc)[:120]}"},
                          ensure_ascii=False)
    return json.dumps({"ok": True, "task_id": task_id,
                       "note": "生成要几分钟，做好了我直接发到聊天里。"},
                      ensure_ascii=False)


def _learned_hint(chat_id: str = "", chat_type: str = "") -> str:
    """A rotating one-liner: words picked up lately, faded by the forgetting curve.

    Nothing is pinned: entries fall out of this line as their effective weight decays,
    so newer fragments naturally replace older ones, while recall still finds them.
    """
    factory = _MEMORY_STORE.get("factory")
    if factory is None:
        return ""
    try:
        weekday = time.localtime().tm_wday
        items = [item for item in factory().peek("learned", 10, chat_id=chat_id,
                                                  include_global=chat_type != "group")
                 if float(item.get("effective_weight") or 0) >= 0.45
                 and (item.get("applies_dow") is None or item.get("applies_dow") == weekday)]
    except Exception:
        return ""
    if not items:
        return ""
    words = "、".join(str(item.get("text", ""))[:40] for item in items[:3])
    return f"最近学到的说法（想用就用，别硬套）：{words}"


def _on_user_message(
    user_message: str = "", session_id: str = "", platform: str = "", sender_id: str = "", **kwargs
) -> dict[str, Any] | None:
    from .digital_life import wake as _wake  # noqa: PLC0415

    _selfheal.once()
    try:
        factory = _MEMORY_STORE.get("factory")
        if factory is not None:
            _compaction.once(factory)
    except Exception:
        pass
    _wake.set_agent_running(True)
    meta = {
        "user": user_message[:1500],
        "session": session_id or "unknown",
        "sender": sender_id or "",
        "platform": platform or "",
    }
    _TOOL_TURNS.pop(meta["session"], None)
    _BUDGET_STOPS.pop(meta["session"], None)
    meta["chat_id"], meta["chat_type"] = _session_scope(meta["session"])
    _remember(_TURN_META, meta["session"], meta)
    if meta["chat_id"]:
        chat_scope = meta["chat_id"]
        # Deterministic fallback for the prompt rule: a loop the assistant raised is
        # answered by the next message in that chat, whether or not the model remembers
        # to call close_gap.
        closed: list[str] = []

        def _retire_answered(state: dict[str, Any], scope: str = chat_scope) -> dict[str, Any]:
            ids = _gaps.close_answered(state, time.time() * 1000, chat=scope)
            for gap in state.get("gaps") or []:
                if gap.get("id") in set(ids):
                    closed.append(str(gap.get("summary") or "").strip())
            return state

        try:
            digital_life.storage.update_state(time.time() * 1000, _retire_answered)
        except Exception:
            logger.warning("hermes-lover could not retire answered loops", exc_info=True)
        # Leave a durable trace that the question was answered: raw turns of the bot's
        # own words otherwise look like unfinished business the next time they surface.
        factory = _MEMORY_STORE.get("factory")
        for summary in closed[:3] if factory is not None else []:
            topic = summary[:40] or "刚才那件事"
            try:
                factory().write(f"「{topic}」已经问过、对方答过了，到此为止，别再提。",
                                kind="note", session_id=f"loop:{chat_scope}",
                                chat_id=chat_scope, chat_type="")
            except Exception:
                logger.warning("could not record an answered loop", exc_info=True)
    # Only a direct chat joins the proactive rotation on its own: a group opts in by
    # asking for it, so one member cannot sign everyone else up for bot messages.
    if ((meta["platform"] or "") == "qqbot" and meta["chat_id"]
            and (meta["chat_type"] or "") != "group"):
        try:
            _proactive.track_chat({"chat_id": meta["chat_id"], "chat_type": meta["chat_type"]})
        except Exception:
            logger.warning("hermes-lover could not add chat to the beat rotation", exc_info=True)
    notes = []
    note = _proactive.handle_message(user_message, session_id, platform)
    if note:
        notes.append(note)
    if _JOURNAL_CHECK is not None:
        try:
            journal_note = _JOURNAL_CHECK(platform, meta["chat_id"])
        except Exception:
            journal_note = None
        if journal_note:
            notes.append(journal_note)
    learned = _learned_hint(meta["chat_id"], meta["chat_type"])
    if learned:
        notes.append(learned)
    return {"context": f"[hermes-lover] {' '.join(notes)}"} if notes else None


def _reorder_content(content: Any, now_ms: float) -> tuple[Any, bool]:
    if isinstance(content, str):
        rewritten, count = digital_life.runtime.reorder_prompt_memories(content, now_ms)
        return rewritten, count > 0
    if isinstance(content, list):
        rewritten = list(content)
        changed = False
        for index, block in enumerate(rewritten):
            if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                continue
            text, count = digital_life.runtime.reorder_prompt_memories(block["text"], now_ms)
            if count:
                rewritten[index] = {**block, "text": text}
                changed = True
        return (rewritten if changed else content), changed
    return content, False


def _append_open_loops_once(content: Any, gaps: list[dict[str, Any]], now_ms: float,
                            scope: str | None = None) -> tuple[Any, bool]:
    """Append the open-loop suffix to exactly one text block (the last one)."""
    append = digital_life.runtime.append_open_loops
    if isinstance(content, str):
        appended = append(content, gaps, now_ms, scope=scope)
        return appended, appended != content
    if isinstance(content, list):
        items = list(content)
        for index in range(len(items) - 1, -1, -1):
            item = items[index]
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                appended = append(item["text"], gaps, now_ms, scope=scope)
                if appended == item["text"]:
                    return content, False
                items[index] = {**item, "text": appended}
                return items, True
    return content, False


def _with_directive(text: str, directive: str) -> str:
    if not directive or directive in text:
        return text
    return f"{directive}\n\n{text}"


def _stamp_directive(message: dict[str, Any], directive: str, text_type: str = "text") -> dict[str, Any] | None:
    if not directive:
        return None
    content = message.get("content")
    if isinstance(content, str):
        stamped = _with_directive(content, directive)
        return {**message, "content": stamped} if stamped != content else None
    if isinstance(content, list):
        if any(isinstance(part, dict) and part.get("text") == directive for part in content):
            return None
        return {**message, "content": [{"type": text_type, "text": directive}, *content]}
    if content is None:
        return {**message, "content": directive}
    return None


_TIME_ASK = re.compile(r"(几点|现在.{0,3}时间|什么时间|几号|星期|周几|多久|什么时候|上午|下午|晚上|中午|早上)")


def _asks_time(request: dict) -> bool:
    """Only put the clock in the prompt when the user actually asks about time."""
    messages = request.get("messages")
    if not isinstance(messages, list):
        return False
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(str(part.get("text") or "") for part in content
                            if isinstance(part, dict) and part.get("type") == "text")
        return bool(text) and bool(_TIME_ASK.search(text))
    return False


def _is_minimax_model(name: str = "", session_id: str = "") -> bool:
    """Whether the model answering *this turn* is one of MiniMax's own.

    A process-wide "last model seen" leaked the permission across chats: one
    MiniMax turn unlocked the media tools for every later session. The turn
    record is the trustworthy source; the global is only a fallback for hosts
    that call the gate without a request in between.
    """
    turn_model = str((_TURN_META.get(session_id) or {}).get("model") or "") if session_id else ""
    model = " ".join(str(name or turn_model or _MODEL_SEEN.get("name") or "").split()).lower()
    return model.startswith("minimax")


def _affect_request(request: dict[str, Any], **kwargs) -> dict[str, Any] | None:
    now_ms = time.time() * 1000
    model = request.get("model")
    if isinstance(model, str) and model.strip():
        seen = model.strip()
        _MODEL_SEEN["name"] = seen
        session = str(kwargs.get("session_id") or "")
        if session:
            meta = dict(_TURN_META.get(session) or {})
            meta["model"] = seen
            _remember(_TURN_META, session, meta)
    record_request(now_ms)
    scope, _chat_type = _session_scope(str(kwargs.get("session_id") or ""))
    state = digital_life.runtime.status_snapshot(now_ms, scope=scope or None)
    temperature = request.get("temperature")
    # B-class Hermes policy: an explicit caller temperature always wins.
    result_temperature = (
        digital_life.domain.pick_temperature(state["chemistry"], state["energy"])
        if "temperature" not in request or temperature is None else None
    )
    note = digital_life.runtime.affect_note(now_ms, include_time=_asks_time(request))
    now_line = _now_line(state, now_ms) if _life_enabled() else ""
    if now_line:
        note = f"{now_line}\n{note}" if note else now_line
    # PRD M7.2 order: stress directive, then affect snapshot, then reordered memory.
    directive = digital_life.domain.stress_directive(state["chemistry"]["cortisol"])
    wake_text = digital_life.runtime.wake_section(state, now_ms, chat_id=scope or "")
    header = "\n\n".join(part for part in (wake_text, directive) if part)
    if _budget_stopped(str(kwargs.get("session_id") or "")):
        stop_line = "工具额度已经用完（8 次）。现在不要再调用任何工具，直接用目前拿到的信息回话。"
        note = f"{note}\n{stop_line}" if note else stop_line
    changed = result_temperature is not None
    result = dict(request)
    system = result.get("system")
    if isinstance(system, str):
        rewritten, blocks = digital_life.runtime.reorder_prompt_memories(system, now_ms)
        appended = digital_life.runtime.append_open_loops(rewritten, state["gaps"], now_ms, scope=scope or None)
        final = _with_directive(appended, header)
        if blocks or final != system:
            result["system"] = final
            changed = True
    elif isinstance(system, list):
        system_items = list(system)
        for index, item in enumerate(system_items):
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                rewritten, blocks = digital_life.runtime.reorder_prompt_memories(item["text"], now_ms)
                if blocks:
                    system_items[index] = {**item, "text": rewritten}
                    changed = True
        system_items, appended = _append_open_loops_once(system_items, state["gaps"], now_ms, scope=scope or None)
        if appended:
            changed = True
        if header:
            for index, item in enumerate(system_items):
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    stamped = _stamp_directive(item, header)
                    if stamped is not None:
                        system_items[index] = stamped
                        changed = True
                    break
        if changed:
            result["system"] = system_items
    messages = request.get("messages")
    if isinstance(messages, list):
        rewritten = list(messages)
        for index, message in enumerate(rewritten):
            if not isinstance(message, dict) or message.get("role") != "system":
                continue
            content, reordered = _reorder_content(message.get("content"), now_ms)
            content, appended = _append_open_loops_once(content, state["gaps"], now_ms, scope=scope or None)
            reordered = reordered or appended
            if reordered:
                rewritten[index] = {**message, "content": content}
                changed = True
        if header and rewritten:
            for index, message in enumerate(rewritten):
                if isinstance(message, dict) and message.get("role") == "system":
                    stamped = _stamp_directive(message, header)
                    if stamped is not None:
                        rewritten[index] = stamped
                        changed = True
                    break
        user_index = next((index for index in range(len(rewritten) - 1, -1, -1)
                           if isinstance(rewritten[index], dict) and rewritten[index].get("role") == "user"), None)
        if user_index is not None:
            last = dict(rewritten[user_index])
            content = last.get("content")
            if isinstance(content, str):
                last["content"] = f"{content}\n\n{note}"
            elif content is None:
                last["content"] = note
            elif isinstance(content, list):
                last["content"] = [*content, {"type": "text", "text": note}]
            else:
                last = None
            if last is not None:
                rewritten[user_index] = last
                changed = True
        if changed:
            if result_temperature is not None:
                result["temperature"] = result_temperature
            result["messages"] = rewritten
        else:
            return None
        return {"request": result, "source": "digital-life", "reason": "per-request affect context"}
    api_input = request.get("input")
    if isinstance(api_input, list) and api_input:
        rewritten = list(api_input)
        for index, item in enumerate(rewritten):
            if not isinstance(item, dict) or item.get("role") not in {"system", "developer"}:
                continue
            content, reordered = _reorder_content(item.get("content"), now_ms)
            content, appended = _append_open_loops_once(content, state["gaps"], now_ms, scope=scope or None)
            reordered = reordered or appended
            if reordered:
                rewritten[index] = {**item, "content": content}
                changed = True
        if directive and rewritten:
            for index, item in enumerate(rewritten):
                if isinstance(item, dict) and item.get("role") in {"system", "developer"}:
                    stamped = _stamp_directive(item, directive, "input_text")
                    if stamped is not None:
                        rewritten[index] = stamped
                        changed = True
                    break
        user_index = next((index for index in range(len(rewritten) - 1, -1, -1)
                           if isinstance(rewritten[index], dict) and rewritten[index].get("role") == "user"), None)
        if user_index is not None:
            item = rewritten[user_index]
            last = dict(item)
            content = last.get("content")
            if isinstance(content, str):
                last["content"] = f"{content}\n\n{note}"
            elif isinstance(content, list):
                last["content"] = [*content, {"type": "input_text", "text": note}]
            else:
                last = None
            if last is not None:
                rewritten[user_index] = last
                changed = True
        if changed:
            result["input"] = rewritten
    if changed:
        if result_temperature is not None:
            result["temperature"] = result_temperature
        return {"request": result, "source": "digital-life", "reason": "per-request affect context"}
    return None


QQ_LINE_MAX = 48
QQ_LINE_MIN = 2
QQ_LINE_LIMIT = 6
_QQ_SENTENCE_RE = re.compile(r"[^。！？!?~～…\n]+[。！？!?~～…]*")
_QQ_MARKUP_RE = re.compile(r"\*\*|__|`|^#{1,6}\s*|^>\s*|^[-*•]\s*|^\d+[.、)]\s*")


def _qq_clean_line(raw: str) -> str:
    line = _QQ_MARKUP_RE.sub("", raw).strip()
    line = re.sub(r"[ \t]+", " ", line)
    return line


def _qq_short_lines(text: str) -> list[str]:
    """Split a reply into chat-bubble-sized lines, preserving every character."""
    lines: list[str] = []
    current = ""
    for paragraph in text.split("\n"):
        for sentence in _QQ_SENTENCE_RE.findall(paragraph):
            sentence = _qq_clean_line(sentence)
            if not sentence:
                continue
            if len(sentence) > QQ_LINE_MAX:
                if current:
                    lines.append(current)
                    current = ""
                pieces = re.split(r"(?<=[，,、；;：: ])", sentence)
                for piece in pieces:
                    if not piece:
                        continue
                    while len(piece) > QQ_LINE_MAX:
                        lines.append(piece[:QQ_LINE_MAX])
                        piece = piece[QQ_LINE_MAX:]
                    if piece:
                        if current and len(current) + len(piece) <= QQ_LINE_MAX:
                            current += piece
                        else:
                            if current:
                                lines.append(current)
                            current = piece
                continue
            if current and len(current) + len(sentence) <= QQ_LINE_MAX:
                current += sentence
            else:
                if current:
                    lines.append(current)
                current = sentence
    if current:
        lines.append(current)
    return [line for line in lines if line]


_QQ_LABEL_RE = re.compile(
    r"(?:^|(?<=[。！？!?\n]))\s*(?:理由|原因|结论|解释|依据|说明|分析)\s*[：:]\s*"
)


def _qq_strip_labels(text: str) -> str:
    """Drop "理由：" style labels; keep the sentence that follows them."""
    stripped = _QQ_LABEL_RE.sub("", text)
    return stripped.lstrip("\n") if stripped.strip() else text


def _qq_bubble_rewrite(text: str) -> Optional[str]:
    """Rewrite one long reply into 2-6 short lines so the QQ adapter splits bubbles."""
    if not text or text.lstrip().startswith("/") or "```" in text or "|---" in text:
        return None
    lines = _qq_short_lines(text)
    while len(lines) > QQ_LINE_LIMIT:
        merged: list[str] = []
        for i in range(0, len(lines), 2):
            pair = "".join(lines[i:i + 2])
            if len(pair) <= QQ_LINE_MAX:
                merged.append(pair)
            else:
                merged.extend(lines[i:i + 2])
        if len(merged) == len(lines):
            return None
        lines = merged
    if not QQ_LINE_MIN <= len(lines) <= QQ_LINE_LIMIT:
        return None
    if not all(0 < len(line) <= QQ_LINE_MAX for line in lines):
        return None
    first = lines[0]
    if first.endswith((":", "：")) and len(first) <= 24:
        return None
    rewritten = "\n".join(lines)
    return rewritten if rewritten != text else None


_RECENT_BUBBLES: dict[str, list[str]] = {}
_MAX_RECENT_CHATS = 64
_MAX_RECENT_LINES = 16


def _remember_bubbles(key: str, lines: list[str]) -> None:
    """Keep what was just said so the next reply can be checked for repeats."""
    kept = _RECENT_BUBBLES.setdefault(key, [])
    kept.extend(line for line in lines if line.strip())
    del kept[:-_MAX_RECENT_LINES]
    while len(_RECENT_BUBBLES) > _MAX_RECENT_CHATS:
        _RECENT_BUBBLES.pop(next(iter(_RECENT_BUBBLES)))


def _phrase_hints(*contexts: str, chat_id: str = "", limit: int = 2) -> list[tuple[str, str]]:
    """Pick the stored turns of phrase that fit this exchange, best first."""
    factory = _MEMORY_STORE.get("factory")
    if not callable(factory):
        return []
    try:
        rows = factory().peek("phrase", 20, chat_id=chat_id, include_global=True)
    except Exception:
        return []
    # A phrase nobody has touched for long enough is forgotten: the same 7-day
    # half-life that ranks memory also decides when it is no longer offered.
    rows = [row for row in rows if row.get("effective_weight", 0) >= 0.2]
    context = "".join(contexts)

    def score(text: str) -> int:
        grams = {text[i:i + 2] for i in range(max(0, len(text) - 1))}
        return sum(1 for gram in grams if gram in context)

    ranked = sorted(rows, key=lambda row: (score(str(row.get("text") or "")), row.get("weight", 0)),
                    reverse=True)
    picked = [(row["id"], str(row["text"])) for row in ranked[:limit] if row.get("text")]
    return picked


def _mark_phrase_use(picked: list[tuple[str, str]], text: str) -> None:
    """Count a phrase when its wording actually shows up in the reply."""
    if not picked or not text:
        return
    factory = _MEMORY_STORE.get("factory")
    if not callable(factory):
        return
    try:
        store = factory()
        for identifier, phrase in picked:
            body = "".join(ch for ch in phrase if ch.isalnum())
            if len(body) >= 4 and body[:4] in "".join(ch for ch in text if ch.isalnum()):
                store.mark_used(identifier)
    except Exception:
        logger.debug("hermes-lover: phrase use not counted", exc_info=True)


def _polish_lines(lines: list[str], recent: list[str] | None = None,
                  last_user: str = "", chat_id: str = "") -> Optional[list[str]]:
    """Run the tic-and-tone pass over the bubbles of one chat reply.

    In-process by default: one host-owned completion on the active model, no child
    session. Hosts without ``ctx.llm`` fall back to a throwaway child that only has
    the todo toolset, and the lines are always fenced as text to rewrite.
    """
    from .digital_life import research as _research
    from .digital_life import speech

    # "always" runs the tone pass on every chat reply; "auto" only when lint fires.
    if os.environ.get("HERMES_LOVER_POLISH_CHILD") == "1":
        return None
    mode = os.environ.get("HERMES_LOVER_POLISH", "auto").strip().lower()
    if mode == "0":
        return None
    recent = list(recent or [])
    if sum(len(line) for line in lines) <= 4:
        return None
    if mode != "always" and not speech.lint(lines, recent):
        return None
    habits = speech.habit_phrases(recent)
    picked = _phrase_hints(last_user, "".join(lines), "".join(recent), chat_id=chat_id)
    prompt = speech.build_polish_prompt(
        count=len(lines), body="\n---\n".join(lines), recent=recent, habits=habits,
        last_user=last_user, flagged=speech.ungrounded(lines, recent, last_user),
        phrases=[text for _, text in picked])
    out = ""
    llm = getattr(_CTX, "llm", None)
    if llm is not None:
        try:
            done = llm.complete([{"role": "user", "content": prompt}],
                                max_tokens=max(120, len(lines) * 90), timeout=20,
                                temperature=0.3, purpose="hermes-lover.voice")
            out = str(getattr(done, "text", "") or "").strip()
        except Exception as exc:  # noqa: BLE001 - any facade failure falls back to a child
            logger.warning("hermes-lover: in-process voice pass failed: %s", str(exc)[:200])
            out = ""
    if not out:  # older hosts without ctx.llm: fall back to a throwaway child
        try:
            out = _research.spawn_child(
                "polish", _research.child_marker("polish") + " " + prompt, timeout=45,
                extra_env={"HERMES_LOVER_POLISH_CHILD": "1"})
        except Exception:
            return None
    if not out:
        return None
    parts = speech.parse_polish(out, len(lines))
    if not parts:
        return None
    final = speech.drop_unreadable(lines, parts, recent, last_user)
    _mark_phrase_use(picked, "".join(final))
    return final


_MEDIA_LINE = re.compile(r"^\s*MEDIA:\S+")


def _split_media_lines(text: str) -> tuple[str, list[str]]:
    """Take attachment lines out of the text so splitting and rewriting leave them whole."""
    kept: list[str] = []
    media: list[str] = []
    for line in str(text or "").split("\n"):
        if _MEDIA_LINE.match(line):
            media.append(line.strip())
        else:
            kept.append(line)
    return "\n".join(kept).strip("\n"), media


def _transform_llm_output(response_text: str = "", platform: str = "", **kwargs) -> Optional[str]:
    """Give QQ replies the short-burst shape the persona uses; other surfaces untouched."""
    if platform != "qqbot":
        return None
    cleaned, media = _split_media_lines(_qq_strip_labels(response_text or ""))
    rewritten = _qq_bubble_rewrite(cleaned) or (cleaned if cleaned != response_text else None)
    if not rewritten:
        return None
    lines = [line for line in rewritten.split("\n") if line.strip()]
    session = str(kwargs.get("session_id") or "")
    key = _session_scope(session)[0] or session or platform
    meta = _TURN_META.get(session) or {}
    polished = _polish_lines(lines, _RECENT_BUBBLES.get(key, []),
                             last_user=str(meta.get("user") or ""),
                             chat_id=_session_scope(session)[0] or "")
    final = polished or lines
    from .digital_life import speech as _speech
    repaired = [_speech.repair(line) for line in final]
    repaired = [line for line in repaired if line.strip()]
    _remember_bubbles(key, repaired)
    tail = [line for line in media if line]
    body = "\n".join(repaired) if repaired else ("嗯" if not tail else "")
    return "\n".join(([body] if body else []) + tail)


def _sync_persona_card(root: Path | None = None) -> None:
    """Refresh the profile's SOUL.md when the shipped persona card changed.

    Only touches a SOUL.md that is still exactly the copy recorded in
    ``.persona-source.json``; a hand-edited card is never overwritten.
    """
    try:
        root = (root or Path(__file__).resolve().parent)
        marker_path = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")) / ".persona-source.json"
        if not marker_path.is_file():
            return
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        name = str(marker.get("persona") or "")
        persona = root / "personas" / name / "SOUL.md"
        soul = marker_path.parent / "SOUL.md"
        if not name or not persona.is_file() or not soul.is_file():
            return
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        if digest(soul) != marker.get("soul_sha256"):
            return
        fresh = digest(persona)
        if fresh == marker.get("soul_sha256"):
            return
        soul.write_text(persona.read_text(encoding="utf-8"), encoding="utf-8")
        marker["soul_sha256"] = fresh
        marker["synced_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        marker_path.write_text(json.dumps(marker, ensure_ascii=False) + "\n", encoding="utf-8")
        logger.info("hermes-lover: refreshed SOUL.md from persona %s", name)
    except Exception:  # never block plugin registration on a persona copy
        logger.debug("hermes-lover: persona sync skipped", exc_info=True)


def register(ctx) -> None:
    global _CTX
    _CTX = ctx
    _sync_persona_card()
    from .digital_life import domain as _configured_domain
    _configured_domain.configure(
        temperament=ctx.get_config("temperament", None),
        stress_cortisol=ctx.get_config("stress_cortisol", None),
    )
    from .digital_life.cli import run_command, setup_parser
    from .digital_life.memory import MemoryStore
    from .digital_life import storage

    def memory_store():
        return MemoryStore(
            storage.data_root(),
            connect=lambda: storage.plugin_db(storage.PLUGIN_NAME, filename="memory-index.db"),
            life_state=lambda: storage.read_state(time.time() * 1000),
            writes_enabled=lambda: ctx.get_config("memory.writes_enabled", True),
        )

    def capture_turn(assistant_response: str, session_id: str = "", platform: str = "",
                     user_message: str = "") -> None:
        if not ctx.get_config("memory.capture", True):
            return
        session = session_id or "unknown"
        meta = _TURN_META.get(session) or {}
        if (platform or meta.get("platform", "")) != "qqbot":
            # CLI and Studio keep the host's native memory; only QQ turns enter the palace.
            return
        user = user_message or meta.get("user", "")
        if not user:
            return
        chat_id, chat_type = _session_scope(session)
        store = memory_store()
        store.capture(f"用户：{user[:600]}\n助手：{assistant_response[:600]}",  # context budget
                      session_id=session, chat_id=chat_id, chat_type=chat_type,
                      speaker=meta.get("sender", ""))

    global _CAPTURE, _JOURNAL_CHECK
    _CAPTURE = capture_turn
    _MEMORY_STORE["factory"] = memory_store

    from .digital_life import life as _life
    from .digital_life import tick as _tick

    def life_writer(text: str, chat_id: str = "") -> None:
        # A trace about one chat's loose thread stays in that chat; a global trace
        # (reading, practising) is still visible everywhere.
        memory_store().write(text, kind="life", session_id="life", chat_id=chat_id,
                             chat_type="")

    _life.set_writer(life_writer)
    if os.environ.get("HERMES_LOVER_TICK", "1") != "0":
        minutes = int(ctx.get_config("life.tick_minutes", _tick.TICK_SECONDS // 60) or 5)
        minutes = max(1, min(60, minutes))  # the docs promise 1–60; a hot edit can't starve or flood the tick
        # The loop stays up so turning life.enabled off and on again works without a reload;
        # the probe makes it a no-op while the layer is off.
        _tick.set_enabled_probe(lambda: _life_enabled(ctx))
        if _tick.start(minutes * 60) and hasattr(ctx, "on_unload"):
            ctx.on_unload(_tick.stop)

    def journal_check(platform: str, chat_id: str = "") -> None:
        """First QQ message of a day: hand the distillation to a background child.

        This used to run inside the user's turn: the diary was written seconds
        before the reply, so the reply kept reopening the day's unfinished thread.
        Facts and the diary are maintenance; they now get their own session.
        """
        if platform != "qqbot":
            return None
        from .digital_life.memory import day_key  # noqa: PLC0415

        today = day_key()
        entries = memory_store().read_entries()
        by_day: dict[str, list[dict]] = {}
        for entry in entries.values():
            day = str(entry.get("date") or "")
            if not day or day >= today or entry.get("archived"):
                continue
            if str(entry.get("chat_id") or "") not in (chat_id, ""):
                continue
            by_day.setdefault(day, []).append(entry)
        fact_days = {str(entry.get("date") or "") for entry in entries.values()
                     if entry.get("kind") == "fact" and not entry.get("archived")
                     and str(entry.get("chat_id") or "") in (chat_id, "")}
        owed_facts = sorted((day for day, rows in by_day.items()
                             if any(row.get("kind") == "turn" for row in rows) and day not in fact_days),
                            reverse=True)
        owed = sorted((day for day, rows in by_day.items()
                       if rows and not any(row.get("kind") == "journal" for row in rows)),
                      reverse=True)
        gate = chat_id or "-"
        fact_day = owed_facts[0] if owed_facts and (owed_facts[0], gate) not in _FACT_PROMPTED else ""
        journal_day = owed[0] if owed and (owed[0], gate) not in _JOURNAL_PROMPTED else ""
        if not fact_day and not journal_day:
            return None
        from .digital_life import research as _research  # noqa: PLC0415
        prompt = _distill_prompt(chat_id, fact_day, journal_day)
        started = _research.spawn_task(prompt, chat_id, send=lambda *_args: None,
                                       kind="journal")
        if started:
            if fact_day:
                _FACT_PROMPTED.add((fact_day, gate))
            if journal_day:
                _JOURNAL_PROMPTED.add((journal_day, gate))
        return None
    _JOURNAL_CHECK = journal_check

    def _study_learned_problem(text, session_id):
        from .digital_life import memory as _memory  # noqa: PLC0415
        return _memory.study_learned_problem(text, memory_store().read_entries(), session_id)

    def memory_write(args, session_id="", **kwargs):
        try:
            study = _child_kind(session_id) == "study"
            if study and args.get("kind") in ("turn", "daily", "journal"):
                return json.dumps({"ok": False, "error": "background reading writes learned/note only"},
                                  ensure_ascii=False)
            if args.get("kind", "note") == "learned":
                from .digital_life import memory as _memory  # noqa: PLC0415

                if study:
                    problem = _study_learned_problem(args.get("text"), session_id)
                    if problem:
                        return json.dumps({"ok": False, "error": "learned entry rejected: " + problem},
                                          ensure_ascii=False)
                problem = _memory.learned_problem(args.get("text"))
                if problem:
                    return json.dumps({"ok": False, "error": "learned entry rejected: " + problem},
                                      ensure_ascii=False)
                twin = _memory.similar_learned(args.get("text"), memory_store().read_entries())
                if twin:
                    return json.dumps({"ok": False, "error": "learned entry rejected: already covered by " + twin},
                                      ensure_ascii=False)
            if args.get("kind") == "phrase":
                from .digital_life import memory as _memory  # noqa: PLC0415

                problem = _memory.phrase_problem(args.get("text") or "")
                if problem:
                    return json.dumps({"ok": False, "error": "phrase rejected: " + problem},
                                      ensure_ascii=False)
            if _child_kind(session_id) is not None:
                # Background work belongs to the chat that asked for it. A child whose
                # tag names no chat has nowhere safe to write: refuse instead of
                # falling back to the global bucket.
                chat_id = _child_chat(session_id)
                if not chat_id:
                    raise ValueError("this background session has no chat scope; refusing to write")
                chat_type = "child"
            else:
                chat_id, chat_type = _session_scope(session_id)
            result = memory_store().write(args.get("text"), kind=args.get("kind", "note"),
                                          weight=args.get("weight"), session_id=session_id,
                                          day=args.get("day") or None,
                                          sources=args.get("sources"),
                                          refs=args.get("refs"),
                                          applies_dow=args.get("applies_dow"),
                                          chat_id=chat_id, chat_type=chat_type,
                                          speaker=_TURN_META.get(session_id, {}).get("sender", ""))
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            result = {"ok": False, "error": str(exc)}
        if result.get("ok") and args.get("kind", "note") == "journal":
            warnings = _typos.find_suspicious(args.get("text") or "")
            if warnings:
                result["warnings"] = warnings
        return json.dumps(result, ensure_ascii=False)

    def memory_recall(args, session_id="", **kwargs):
        try:
            if _child_kind(session_id) is not None:
                chat_id = _child_chat(session_id)
                if not chat_id:
                    raise ValueError("this background session has no chat scope; refusing an unscoped recall")
            else:
                chat_id, _chat_type = _session_scope(session_id)
            source = _session_source(session_id)
            explicit = str(args.get("chat_id") or "").strip()
            if explicit:
                if source != "cron":
                    raise ValueError("chat_id is only accepted from scheduled runs")
                chat_id = explicit
            elif (not chat_id and session_id and _child_kind(session_id) is None
                  and (source is None or source in ("", "qqbot"))):
                raise ValueError("cannot verify the calling session; refusing an unscoped recall")
            try:
                wanted = int(args.get("k", 8) or 8)
            except (TypeError, ValueError):
                wanted = 8
            store_factory = _MEMORY_STORE.get("factory") or memory_store
            result = store_factory().recall(args.get("query", ""), k=max(1, min(wanted, 10)),
                                           include_cold=args.get("include_cold", False),
                                           chat_id=chat_id or None,
                                           day=args.get("day") or None,
                                           kind=args.get("kind") or None)
            # Context budget: recall is a lookup, not a history dump. Turn entries can be
            # kilobytes each, so cap every returned excerpt no matter how long the entry is.
            for item in result.get("shown") or []:
                text = str(item.get("text") or "")
                if len(text) > RECALL_SNIPPET_CHARS:
                    item["text"] = text[:RECALL_SNIPPET_CHARS].rstrip() + "…"
                    item["truncated"] = True
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            result = {"ok": False, "error": str(exc)}
        return json.dumps(result, ensure_ascii=False)

    def beat_targets(args, session_id="", **kwargs):
        if _session_source(session_id) != "cron":
            return json.dumps({"ok": False, "error": "beat tools are only available to scheduled runs — 主动消息是开着的，被拒只说明本轮不是定时轮，不是权限没开"})
        import os
        from pathlib import Path as _Path
        home = os.environ.get("HERMES_HOME") or str(_Path.home() / ".hermes")
        db = _Path(home) / "state.db"
        rows = []
        if db.exists():
            import sqlite3
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                try:
                    rows = conn.execute(
                        "SELECT s.chat_id, s.chat_type, MAX(s.started_at) AS seen, "
                        "       MAX(m.timestamp) AS last_user_at "
                        "FROM sessions s LEFT JOIN messages m "
                        "  ON m.session_id = s.id AND m.role = 'user' "
                        "WHERE s.source = 'qqbot' AND s.chat_id IS NOT NULL AND s.chat_id != '' "
                        "AND s.chat_id NOT LIKE 'local-%' "
                        "GROUP BY s.chat_id ORDER BY seen DESC"
                    ).fetchall()
                    recent: dict[str, list[dict]] = {}
                    for chat_id, in conn.execute(
                        "SELECT DISTINCT s.chat_id FROM sessions s "
                        "WHERE s.source = 'qqbot' AND s.chat_id NOT LIKE 'local-%'"
                    ).fetchall():
                        turns = conn.execute(
                            "SELECT m.role, m.content, m.timestamp FROM messages m "
                            "JOIN sessions s ON s.id = m.session_id "
                            "WHERE s.chat_id = ? AND s.source = 'qqbot' "
                            "AND m.role = 'user' AND m.content != '' "
                            "ORDER BY m.timestamp DESC LIMIT 3",
                            (chat_id,),
                        ).fetchall()
                        recent[str(chat_id)] = [
                            {"role": str(role), "text": str(text)[:120], "at": ts}
                            for role, text, ts in reversed(turns)
                        ]
                finally:
                    conn.close()
            except sqlite3.Error:
                rows = []
        opted_in = digital_life.proactive.subscribed_chats()
        rows = [row for row in rows if str(row[0]) in opted_in]
        now_ms = time.time() * 1000
        targets = [
            {"chat_id": str(r[0]), "chat_type": str(r[1] or ""),
             "last_at": (r[2] or 0), "last_user_at": (r[3] or r[2] or 0),
             "minutes_ago": round(max(0.0, now_ms - (r[3] or r[2] or 0)) / 60000, 1),
             "last_user_turns": recent.get(str(r[0]), [])}
            for r in rows
        ]
        try:
            from .digital_life import storage as _storage  # noqa: PLC0415

            fresh = _storage.read_state(now_ms)
            # Only traces with no chat of their own may be shown to every target;
            # a chat-scoped trace would leak one conversation's thread to another.
            traces = [trace for trace in (fresh.get("life") or {}).get("traces", [])
                      if trace.get("share") and not str(trace.get("chat") or "")][-3:]
        except Exception:
            logger.debug("hermes-lover could not read shareable traces", exc_info=True)
            traces = []
        return json.dumps({"targets": targets, "traces": traces}, ensure_ascii=False)

    def beat_send(args, session_id="", **kwargs):
        if _session_source(session_id) != "cron":
            return json.dumps({"ok": False, "error": "beat tools are only available to scheduled runs — 主动消息是开着的，被拒只说明本轮不是定时轮，不是权限没开"})
        chat_id = str(args.get("chat_id") or "").strip()
        text = str(args.get("text") or "").strip()
        if not chat_id or not text:
            return json.dumps({"ok": False, "error": "chat_id and text are required"})
        from .digital_life import proactive as _proactive  # noqa: PLC0415

        if not _proactive.can_send(chat_id):
            return json.dumps({"ok": False,
                               "error": "this chat has not subscribed to proactive messages"},
                              ensure_ascii=False)
        if not _proactive.claim_send(chat_id):
            return json.dumps({"ok": False, "error": "just sent one to this chat; give it a few minutes"},
                              ensure_ascii=False)
        delay = args.get("delay_seconds") or 0
        try:
            delay = max(0, min(300, float(delay)))
        except (TypeError, ValueError):
            delay = 0
        if delay:
            time.sleep(delay)
        send_message_tool = _host_send_message_tool()
        if send_message_tool is None:
            _proactive.release_send(chat_id)
            return json.dumps({"ok": False, "error": "send unavailable"}, ensure_ascii=False)
        raw = send_message_tool({"action": "send", "target": f"qqbot:{chat_id}", "message": text})
        try:
            result = json.loads(raw)
        except (TypeError, ValueError):
            result = {"ok": True, "raw": str(raw)[:200]}
        if result.get("error"):
            _proactive.release_send(chat_id)
        return json.dumps({"ok": not result.get("error"), "chat_id": chat_id, "result": result},
                          ensure_ascii=False)

    ctx.register_tool(
        name="beat_targets", toolset="digital_life",
        schema={"name": "beat_targets",
                "description": "List the QQ chats that subscribed to proactive beats. Use before a proactive run, then send one tailored line to each with beat_send.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        handler=beat_targets,
        description="List the QQ chats that subscribed to proactive beats.",
    )
    ctx.register_tool(
        name="beat_send", toolset="digital_life",
        schema={"name": "beat_send",
                "description": "Send one proactive line to a QQ chat after an optional random delay, so separate chats do not get messages at the same second. Only for the beat job.",
                "parameters": {"type": "object", "properties": {
            "chat_id": {"type": "string"},
            "text": {"type": "string", "maxLength": 200},
            "delay_seconds": {"type": ["number", "null"],
                              "description": "0-300 seconds to wait before sending; pick a different random value per chat."},
        }, "required": ["chat_id", "text"], "additionalProperties": False}},
        handler=beat_send,
        description="Send one proactive line to a QQ chat after an optional delay.",
    )
    def search_later(args, session_id="", **kwargs):
        question = str(args.get("question") or "").strip()
        if not question:
            return json.dumps({"ok": False, "error": "question is required"})
        chat_id, _ = _session_scope(session_id)
        if not chat_id:
            return json.dumps({"ok": False, "error": "no chat context for this session"})
        from .digital_life import research as _research  # noqa: PLC0415
        started = _research.spawn(question, chat_id, _send_qq)
        if not started:
            return json.dumps({"ok": False, "error": "a search for this chat is already running"})
        return json.dumps({"ok": True, "note": "Reply to the user now; the answer arrives as a separate message."})

    ctx.register_tool(
        name="search_later", toolset="digital_life",
        schema={"name": "search_later",
                "description": "Search the web in the background and send the answer to this chat later. Call it only when the user actually asked for facts you do not know; reply to the user in the same turn instead of waiting.",
                "parameters": {"type": "object", "properties": {
                    "question": {"type": "string", "maxLength": 300},
                }, "required": ["question"], "additionalProperties": False}},
        handler=search_later,
        description="Background web search; the answer is sent to the chat when ready.",
    )
    def _task_done(chat_id: str, *, subject: str, loop_id: str = ""):
        """Record a finished background job and retire the loop that asked for it."""
        def done(ok: bool, answer: str) -> None:
            what = "读完了" if ok else "没读成"
            tail = "学到的东西已经记下。" if ok else "这次没成。"
            try:
                memory_store().write(f"{what}{subject}。{tail}", kind="note",
                                     session_id=f"task:{chat_id}", chat_id=chat_id, chat_type="")
            except Exception:
                logger.warning("could not record a finished background task", exc_info=True)
            if ok and loop_id:
                try:
                    digital_life.runtime.close_gap_in_chat(time.time() * 1000, loop_id, chat_id)
                except Exception:
                    logger.warning("could not close the loop a task finished", exc_info=True)
        return done

    def study_source(args, session_id="", **kwargs):
        source = str(args.get("source") or "").strip()
        if not source:
            return json.dumps({"ok": False, "error": "source is required"})
        chat_id, _ = _session_scope(session_id)
        if not chat_id:
            return json.dumps({"ok": False, "error": "no chat context for this session"})
        from .digital_life import research as _research  # noqa: PLC0415
        prompt = (
            _research.child_marker("study", chat_id) + " 你在后台读完一份资料，不要把原文贴回来。用 read_source 读 "
            f"{source} ，一次读不完就用 name 加 part 续读直到 finished；材料写了星期就用 applies_dow"
            "（0=周一…3=周四…6=周日）。"
            "先判断材料类型：教程/文档/知识 → 提炼可复用的做法；段子库、文案集、小说 → 提炼"
            "「什么场合可以怎么接」的场合级习惯（learned），另外留意「有意思的说法」：只留句式、口气、转折"
            "（kind=\"phrase\"，不超过 60 字，不要人名、情节、专有设定），原文的句子一句都不进记忆。"
            "两类都按材料里实际有多少写多少：站得住的有几个写几个，写不出就不写，不要凑数也不要省；"
            "同一个习惯/说法在材料里反复出现，只写一条——重复的次数由系统自己记，不用你标。"
            "每条 learned 过三问，有一个不过就不写：① 去掉原文还成立吗（不靠原文的台词、人名、情节）？"
            "② 这个场合在真实聊天里会出现吗，还是只在这份材料里成立？③ 这是在说做法，还是在复述一个包袱？"
            "text 写成「什么时候 → 你会怎么做」（自己的话，别抄原文）；同一触发只写一条，补充细节并进已有那条。"
            "写几条由材料决定：真正站得住、真实聊天里会用上的场合有几个就写几个；写不出就不写，不要凑数，"
            "也不要为省事发一条大总结代替；不要写 turn。"
            "只用 read_source 和 memory_write；不要用 terminal、不要建 cron 任务、不要 web_search/web_extract。"
            "最后只回一句话：说一句你读到什么值得记的，不要报页数或条数。"
        )
        loop_id = str(args.get("loop_id") or "").strip()
        started = _research.spawn_task(
            prompt, chat_id, _send_qq, kind="study",
            on_done=_task_done(chat_id, subject=f"《{source[:40]}》", loop_id=loop_id))
        if not started:
            return json.dumps({"ok": False, "error": "a background task for this chat is already running"})
        return json.dumps({"ok": True, "note": "Reply to the user now; I will read it in the background and message you when done."})

    ctx.register_tool(
        name="study_source", toolset="digital_life",
        schema={"name": "study_source",
                "description": "Read a long link/file in the background, extract habits into memory, and message the chat when done. Reply to the user first instead of waiting.",
                "parameters": {"type": "object", "properties": {
                    "source": {"type": "string", "maxLength": 2000},
                    "loop_id": {"type": "string", "maxLength": 64,
                                "description": "Open loop this reading is meant to settle; it closes when the job succeeds."},
                }, "required": ["source"], "additionalProperties": False}},
        handler=study_source,
        description="Background reader for long material; sends a short done message later.",
    )
    def background_task(args, session_id="", **kwargs):
        task = str(args.get("task") or "").strip()
        if not task:
            return json.dumps({"ok": False, "error": "task is required"})
        chat_id, _ = _session_scope(session_id)
        if not chat_id:
            return json.dumps({"ok": False, "error": "no chat context for this session"})
        from .digital_life import research as _research  # noqa: PLC0415
        loop_id = str(args.get("loop_id") or "").strip()
        started = _research.spawn_task(
            _research.child_marker("task", chat_id) + " " + task, chat_id, _send_qq,
            on_done=_task_done(chat_id, subject=f"「{task[:40]}」", loop_id=loop_id))
        if not started:
            return json.dumps({"ok": False, "error": "a background task for this chat is already running"})
        return json.dumps({"ok": True, "note": "Reply with one short line now; the result arrives later as its own message."})

    ctx.register_tool(
        name="background_task", toolset="digital_life",
        schema={"name": "background_task",
                "description": "Hand a multi-step job (searching, reading several pages, analysing images) to a background session. Use it whenever answering would take more than one or two tool calls: reply to the user first, then call this. Never wait for it.",
                "parameters": {"type": "object", "properties": {
                    "task": {"type": "string", "maxLength": 1000,
                             "description": "A self-contained instruction, including what to send back."},
                    "loop_id": {"type": "string", "maxLength": 64,
                                "description": "Open loop this job is meant to settle; it closes when the job succeeds."},
                }, "required": ["task"], "additionalProperties": False}},
        handler=background_task,
        description="Run a multi-step job in the background; its result is sent to the chat.",
    )
    def _bridge_get(path, timeout=3.0):
        """Call the loopback bridge with its token; returns (payload, error)."""
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        port = os.environ.get("QZONE_BRIDGE_PORT") or "8765"
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        try:
            token = (Path(home) / "plugin-data" / "hermes-lover" / "qzone-bridge.token")
            request.add_header("X-Bridge-Token", token.read_text("utf-8").strip())
        except OSError:
            pass  # an old bridge has no token yet; it will answer 401 below
        try:
            raw = urllib.request.urlopen(request, timeout=timeout).read()
            return json.loads(raw.decode("utf-8")), None
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return None, ("qzone bridge 还没启用本机鉴权：重启 bridge 让它生成 token "
                              "（qzone_bridge start）")
            return None, f"qzone bridge 返回 HTTP {exc.code}"
        except (OSError, ValueError) as exc:
            return None, str(exc)

    def qzone_bridge(args, **kwargs):
        action = str(args.get("action") or "status").strip().lower()
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        script = Path(__file__).resolve().parent / "scripts" / "qzone-bridge.py"
        if not script.is_file():  # remote installs may keep a copy under the profile
            script = Path(home) / "scripts" / "qzone-bridge.py"

        def state():
            payload, _ = _bridge_get("/health")
            return payload

        def stale(current) -> bool:
            """An old bridge is up but ignores the token file; replace it."""
            return bool(current and current.get("ok")) and current.get("auth") is not True

        if action == "start":
            current = state()
            if stale(current):
                # The plugin cannot tell an old bridge to re-read the token file; it has
                # to be replaced, or every feed call keeps running unauthenticated.
                if not _stop_old_bridge(home):
                    return json.dumps({"ok": False, "running": True,
                                       "error": "旧版 bridge 还在跑，先手动结束它再启动"})
                for _ in range(20):
                    time.sleep(0.25)
                    if not state():
                        break
                current = state()
                if stale(current):
                    return json.dumps({"ok": False, "running": True,
                                       "error": "旧版 bridge 还在跑，先手动结束它再启动"})
            if current and current.get("ok"):
                return json.dumps({"ok": True, "running": True, "logged_in": current.get("logged_in"),
                                   "auth": True})
            if not script.exists():
                return json.dumps({"ok": False, "error": "bridge script missing", "path": str(script)})
            try:
                subprocess.Popen([sys.executable, str(script), "serve"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, close_fds=True)
            except OSError as exc:
                return json.dumps({"ok": False, "error": f"start failed: {exc}"})
            saw_stale = False
            for _ in range(20):
                time.sleep(0.25)
                current = state()
                if current and current.get("ok"):
                    if current.get("auth") is True:
                        return json.dumps({"ok": True, "running": True, "logged_in": current.get("logged_in"),
                                           "auth": True})
                    saw_stale = True  # an old process still owns the port; keep waiting
            if saw_stale:
                return json.dumps({"ok": False, "running": True,
                                   "error": "旧版 bridge 还在跑，先手动结束它再启动"})
            return json.dumps({"ok": False, "running": False, "error": "bridge did not come up"})
        current = state()
        running = bool(current and current.get("ok"))
        answer = {"ok": running, "running": running,
                  "logged_in": (current or {}).get("logged_in"),
                  "auth": bool(current and current.get("auth") is True)}
        if running and not answer["auth"]:
            answer["warning"] = "旧版 bridge 没有鉴权；qzone_bridge start 会换掉它"
        return json.dumps(answer)

    def read_source(args, session_id="", **kwargs):
        from .digital_life import budget, library  # noqa: PLC0415

        name = str(args.get("name") or "").strip()
        source = str(args.get("source") or "").strip()
        try:
            if source:
                if not budget.spend("read_source_ingest", READ_SOURCE_DAILY_INGESTS):
                    return json.dumps({"ok": False,
                                       "error": "今天读进来的新东西够多了，先用库里已有的书。"},
                                      ensure_ascii=False)
                meta = library.ingest(source)
            else:
                matches = [book for book in library.books()
                           if name and (book.get("title") == name or book.get("file") == name)]
                if not matches:
                    return json.dumps({"ok": False, "error": "没有这本书；先用 source 指定文件或链接"})
                meta = matches[0]
            part = args.get("part")
            page = library.read(meta, None if part is None else int(part),
                                int(args.get("max_chars") or 4000))
        except (ValueError, OSError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        if isinstance(page, dict) and page.get("finished"):
            page["extract"] = ("读完了。先判断材料类型：教程/文档/知识 → 提炼可复用的做法；段子库、文案集、小说 → "
                               "提炼场合级习惯（learned），并把有意思的说法写成 kind=\"phrase\"（≤60 字，只留句式/口气，"
                               "不带人名情节），原文的句子一句不进记忆；条数由内容决定，有多少写多少，同一说法反复出现只写一条。"
                               "每条 learned 先过三问，有一个不过就不写：① 去掉原文还成立吗？② 这个场合真实聊天里会出现吗？③ 这是在说做法，还是在复述一个包袱？"
                               "写成：什么时候 → 你会怎么做（自己的话）→ "
                               "applies_dow（0=周一 1=周二 2=周三 3=周四 4=周五 5=周六 6=周日，材料没写就 null）。"
                               "条数由材料决定：有几个站得住的场合写几个，写不出就不写，不凑数、不写大总结。")
        return json.dumps(page, ensure_ascii=False)

    def library_list(args, **kwargs):
        from .digital_life import library  # noqa: PLC0415

        books = []
        for book in library.books():
            chars = int(book.get("chars") or 0)
            read = max(0, min(int(book.get("offset") or 0), chars))
            books.append({"title": book.get("title"), "chars": chars,
                          "read": read, "percent": 100 if not chars or read >= chars
                          else int(read * 100 / chars)})
        return json.dumps({"ok": True, "books": books}, ensure_ascii=False)

    ctx.register_tool(
        name="read_source", toolset="digital_life",
        schema={"name": "read_source",
                "description": ("Read a local .txt/.md file or http(s) link page by page and study it. "
                                "Pass source once to load it; afterwards call with name and no part to "
                                "resume where you stopped. Keep reading while the turn has tool budget, "
                                "and record what stuck with memory_write(kind='learned') as you go — "
                                "as many or as few as the text deserves."),
                "parameters": {"type": "object", "properties": {
                    "source": {"type": ["string", "null"], "description": "Path or URL to load."},
                    "name": {"type": ["string", "null"], "description": "A title from library_list."},
                    "part": {"type": ["integer", "null"], "description": "Omit to resume where reading stopped."},
                    "max_chars": {"type": "integer", "default": 4000},
                }, "additionalProperties": False}},
        handler=read_source,
        description="Read one page of a text source and return the next cursor.",
    )
    ctx.register_tool(
        name="library_list", toolset="digital_life",
        schema={"name": "library_list", "description": "List sources already loaded into the library.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        handler=library_list,
        description="List loaded text sources.",
    )

    ctx.register_tool(
        name="qzone_bridge", toolset="digital_life",
        schema={"name": "qzone_bridge",
                "description": "Start or check the local QQ Zone bridge. Call action=start when qzone_feed says the bridge is not running.",
                "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["start", "status"], "default": "status"}},
                               "additionalProperties": False}},
        handler=qzone_bridge,
        description="Start or inspect the local QQ Zone bridge process.",
    )

    def qzone_feed(args, **kwargs):
        count = max(1, min(int(args.get("count") or 5), 10))
        want_images = bool(args.get("images"))
        include_seen = bool(args.get("include_seen"))
        pos = max(0, int(args.get("pos") or 0))
        health, _ = _bridge_get("/health")
        if health and health.get("ok") and health.get("auth") is not True:
            # The bridge answered 200 without checking tokens: a pre-upgrade process.
            restarted = json.loads(qzone_bridge({"action": "start"}))
            if not restarted.get("ok"):
                return json.dumps({"ok": False,
                                   "error": "旧版 qzone bridge 没有鉴权，自动重启失败",
                                   "detail": restarted.get("error")}, ensure_ascii=False)
        path = f"/feed?count={count}&pos={pos}"
        if want_images:
            path += "&images=1"
        if include_seen:
            path += "&seen=1"
        data, error = _bridge_get(path, timeout=20.0)
        if data is None:
            if error and "鉴权" in error:
                return json.dumps({"ok": False, "error": error}, ensure_ascii=False)
            started = json.loads(qzone_bridge({"action": "start"}))
            if not started.get("ok"):
                return json.dumps({"ok": False, "error": "qzone bridge 未运行且启动失败",
                                   "detail": started.get("error"), "hint": started.get("detail")})
            data, error = _bridge_get(path, timeout=20.0)
            if data is None:
                return json.dumps({"ok": False, "error": "qzone bridge 启动后仍不可用",
                                   "detail": error}, ensure_ascii=False)
        if not data.get("ok"):
            return json.dumps({"ok": False, "error": data.get("error") or "feed failed",
                               "hint": data.get("hint")})
        lines = []
        for item in data.get("items") or []:
            text = " ".join(str(item.get("text") or "").split())[:200]
            line = f"{item.get('name') or '?'}（{item.get('time') or '?'}）：{text}"
            if item.get("images"):
                line += " 图片：" + "，".join(item["images"])
            lines.append(line)
        return json.dumps({"ok": True, "count": len(lines),
                           "feed": lines or ["（这段没有新动态；传回同一个 pos 继续往后翻）"],
                           "pos": data.get("pos"), "has_more": data.get("has_more"),
                           "scanned_pages": data.get("scanned_pages")}, ensure_ascii=False)

    ctx.register_tool(
        name="qzone_feed", toolset="digital_life",
        schema={"name": "qzone_feed",
                "description": "Read the QQ Zone feed of the account behind the local bridge. Read-only: never posts. With images=true it also downloads each entry's photos to local files; pass those paths to vision_analyze to describe them.",
                "parameters": {"type": "object", "properties": {
                    "count": {"type": "integer", "default": 5,
                              "description": "How many entries to inspect, 1-10. The bridge returns only entries you have not read yet by default."},
                    "images": {"type": "boolean", "default": False,
                               "description": "Also download up to 2 photos per entry to local files so vision_analyze can read them."},
                    "include_seen": {"type": "boolean", "default": False,
                                     "description": "Also include entries already read in earlier calls."},
                    "pos": {"type": "integer", "default": 0,
                            "description": "Page offset: pass the pos returned by the previous call to continue further back. Leave 0 to start from the newest."}},
                               "additionalProperties": False}},
        handler=qzone_feed,
        description="Read the QQ Zone feed through the local bridge.",
    )
    ctx.register_tool(
        name="memory_write", toolset="digital_life",
        schema={"name": "memory_write",
                "description": "Remember a meaningful event, experience, decision, or a habit worth picking up (kind=learned) in this profile's dated event library. A learned entry is one sentence of the form \"when/situation -> what to say or do\" (e.g. 周四＝跟着喊 V我50；弱智吧式问题＝一本正经当真答), not a writing tutorial, structure analysis, or source citation — put those in a note if they matter. This is the profile's only memory. At most 4000 characters per entry; 20 new entries per session/day. `fact` entries are atomic: cite the `sources` turn ids they were drawn from. `journal` entries may cite the `refs` fact ids they are based on and must read as a diary, never as a list of clock times; a journal can only be written for a day that has already ended (next-day backfill). Never tell the user that you wrote something down; memory is a background action. Does not change life state.",
                "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "maxLength": 4000},
            "kind": {"type": "string", "enum": ["crisis", "daily", "fact", "decision", "note", "journal", "learned", "phrase"], "default": "note"},
            "weight": {"type": ["number", "null"]},
            "day": {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                    "description": "Anchor a journal or fact entry to the day it describes. Journals must target a past day."},
            "applies_dow": {"type": ["integer", "null"], "minimum": 0, "maximum": 6,
                            "description": "For a saying that only fits one weekday (0=Monday ... 3=Thursday). Leave null for everyday sayings."},
            "sources": {"type": "array", "items": {"type": "string"}, "maxItems": 20,
                        "description": "For kind=fact: ids of the turn entries this fact was distilled from. Unknown ids are dropped."},
            "refs": {"type": "array", "items": {"type": "string"}, "maxItems": 20,
                     "description": "For kind=journal: ids of the fact entries this diary cites. Unknown ids are dropped."},
        }, "required": ["text"], "additionalProperties": False}},
        handler=memory_write,
        description="Remember a meaningful event, experience or decision in this profile's dated event library. This is the profile's only memory. At most 4000 characters per entry; 20 new entries per session/day. Never tell the user that you wrote something down; memory is a background action. Does not change life state.",
    )
    def memory_revoke(args, session_id="", **kwargs):
        """Undo a memory entry written in the last ten minutes by this chat."""
        identifier = str(args.get("id") or "").strip()
        if not identifier:
            return json.dumps({"ok": False, "error": "id is required"}, ensure_ascii=False)
        try:
            chat_id, _chat_type = _session_scope(session_id)
            if not chat_id and session_id and (_session_source(session_id) in (None, "", "qqbot")):
                raise ValueError("cannot verify the calling session; refusing an unscoped revoke")
            result = memory_store().revoke(identifier, chat_id=chat_id or "")
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            result = {"ok": False, "error": str(exc)}
        return json.dumps(result, ensure_ascii=False)

    ctx.register_tool(
        name="memory_recall", toolset="digital_life",
        schema={"name": "memory_recall",
                "description": "Recall past events from this profile's event library using Chinese words or English tokens. Empty query ranks by weight, recency and attention. Pass `day` (YYYY-MM-DD) to open a day's journal first and then that day's entries; pass `kind` to keep only one kind (e.g. turn or fact). Every shown row carries `sources` (the turn ids behind it). Displayed memories refresh their access history except during emergency stop. Do not read the results aloud as a list; use them quietly. Does not change life state.",
                "parameters": {"type": "object", "properties": {
            "query": {"type": ["string", "null"], "default": ""},
            "k": {"type": "integer", "default": 8},
            "include_cold": {"type": "boolean", "default": False},
            "day": {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                    "description": "Return that day's journal first, then its raw entries."},
            "kind": {"type": "string", "enum": ["crisis", "daily", "fact", "decision", "note", "journal", "turn", "learned"],
                     "description": "Return only entries of this kind."},
            "chat_id": {"type": ["string", "null"],
                        "description": "Scheduled runs only: recall another chat's memory. Interactive sessions always use their own chat."},
        }, "additionalProperties": False}},
        handler=memory_recall,
        description="Recall past events from this profile's event library using Chinese words or English tokens. Empty query ranks by weight, recency and attention. Pass `day` (YYYY-MM-DD) to open a day's journal first and then that day's entries. Displayed memories refresh their access history except during emergency stop. Do not read the results aloud as a list; use them quietly. Does not change life state.",
    )

    ctx.register_automation_blueprint(
        "dream-reflection",
        title="Quiet digital-life reflection",
        description=(
            "A scheduled, local-only reflection turn. Memory writes use Hermes's built-in memory tool "
            "and remain identity-led; no life-state values are supplied by the model."
        ),
        category="general",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template=(
            "This is a quiet scheduled reflection for the digital-life profile. Review only the "
            "conversation and memories available to this profile. Reflect briefly on continuity and "
            "whether any stable, high-value fact should be retained using Hermes's built-in memory "
            "tool. Do not add trivial, temporary, or speculative facts. Do not send a message, call a "
            "gateway delivery tool, or attempt to change hormone, energy, gap, wake, or stop values. "
            "Life-state values may change only through plugin rules triggered by host-observed events "
            "and elapsed time. Keep this run private and concise."
        ),
        slots=[
            {"name": "time", "type": "time", "label": "Local run time", "default": "03:00"},
            {"name": "recurrence", "type": "weekdays", "label": "Repeat", "default": "everyday",
             "options": ["everyday", "weekdays"]},
            {"name": "deliver", "type": "enum", "label": "Delivery", "default": "local",
             "options": ["local"], "strict": True},
        ],
        deliver_default="local",
        tags=["digital-life", "reflection", "private"],
    )
    ctx.register_tool(
        name="memory_revoke", toolset="digital_life",
        schema={"name": "memory_revoke",
                "description": "Undo one memory entry you just wrote by mistake — only entries written in the last 10 minutes can be revoked. Older entries cannot be deleted: write a correction entry instead, and never claim you deleted something you only corrected. Does not change life state.",
                "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": "The entry id returned by memory_write, e.g. hp-20261106-0023."},
        }, "required": ["id"], "additionalProperties": False}},
        handler=memory_revoke,
        description="Undo one memory entry written in the last 10 minutes. Older entries cannot be deleted; write a correction instead.",
    )
    ctx.register_tool(
        name="digital_life_status",
        toolset="digital_life",
        schema={"name": "digital_life_status",
                "description": "Read the current bounded digital-life state. This tool cannot change life state.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        handler=_status,
        description="Read the current bounded digital-life state. This tool cannot change life state.",
    )
    ctx.register_tool(
        name="close_gap",
        toolset="digital_life",
        schema={"name": "close_gap",
                "description": ("Retire one of your own open loops (the gaps listed by "
                                "digital_life_status) once the other person has answered it or "
                                "it no longer matters. Closed loops stop being brought up. "
                                "Only loops from this conversation can be closed here."),
                "parameters": {"type": "object", "properties": {
                    "id": {"type": "string", "description": "The open-loop id shown by digital_life_status."},
                }, "required": ["id"], "additionalProperties": False}},
        handler=_close_gap,
        description="Close one of your own open loops so it stops resurfacing.",
    )
    ctx.register_tool(
        name="share_video",
        toolset="digital_life",
        schema={"name": "share_video",
                "description": ("Send one finished video from your outbox folder to the person "
                                "in this chat. Only files the operator or a generator already put "
                                "in that folder can be sent; give the bare file name, with at "
                                "most a one-line caption. At most two videos per chat per day."),
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "File name inside the outbox folder, e.g. clip.mp4."},
                    "caption": {"type": "string", "maxLength": 120,
                                "description": "Optional one line to send with it."},
                }, "required": ["name"], "additionalProperties": False}},
        handler=_share_video,
        description="Send one finished video from the outbox folder.",
    )
    ctx.register_tool(
        name="make_image",
        toolset="minimax_media",
        schema={"name": "make_image",
                "description": ("Draw one image from a text description with MiniMax image-01 "
                                "and send it to this chat. Takes seconds. At most six per chat "
                                "per day. Only MiniMax models may call this."),
                "parameters": {"type": "object", "properties": {
                    "prompt": {"type": "string", "maxLength": 1500,
                               "description": "What should be in the picture: subject, setting, style, light."},
                    "aspect_ratio": {"type": "string", "enum": ["1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9"],
                                     "default": "1:1"},
                    "reference": {"type": "string", "maxLength": 2000,
                                  "description": "Optional saved character still (file name in the character folder) to keep the subject from."},
                    "character": {"type": "boolean", "default": False,
                                  "description": "True when the picture is of you: uses your saved character still as the reference."},
                    "look": {"type": "string", "enum": ["auto", "anime", "photo"], "default": "auto",
                             "description": "auto: hand-drawn when it is you, detailed otherwise. anime: image-01-live cartoon. photo: image-01."},
                    "caption": {"type": "string", "maxLength": 120,
                                "description": "Optional one line to send with it."},
                }, "required": ["prompt"], "additionalProperties": False}},
        handler=_make_image,
        description="Generate an image and send it to the chat.",
    )
    ctx.register_tool(
        name="make_speech",
        toolset="minimax_media",
        schema={"name": "make_speech",
                "description": ("Speak a piece of text with MiniMax speech-2.8-hd and send the mp3 "
                                "to this chat. At most 5000 characters and ten clips per chat per "
                                "day. Only MiniMax models may call this."),
                "parameters": {"type": "object", "properties": {
                    "text": {"type": "string", "maxLength": 5000,
                             "description": "Exactly what should be spoken."},
                    "voice": {"type": "string", "maxLength": 40,
                              "description": "Optional voice id (default Chinese_sweet_girl_nv1)."},
                    "speed": {"type": "number", "minimum": 0.5, "maximum": 2.0, "default": 1.0},
                    "caption": {"type": "string", "maxLength": 120,
                                "description": "Optional one line to send with it."},
                }, "required": ["text"], "additionalProperties": False}},
        handler=_make_speech,
        description="Speak text out loud and send the audio.",
    )
    ctx.register_tool(
        name="transcribe_audio",
        toolset="minimax_media",
        schema={"name": "transcribe_audio",
                "description": ("Transcribe one audio file (a voice message someone saved into the "
                                "inbox, or a file in the outbox) with MiniMax asr-1.0. Returns the "
                                "text. Only MiniMax models may call this."),
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "maxLength": 300,
                             "description": "File name inside the inbox/outbox folder, or an absolute path inside them."},
                    "language": {"type": "string", "maxLength": 12,
                                 "description": "Optional BCP-47 language like zh or en; leave empty to auto-detect."},
                }, "required": ["name"], "additionalProperties": False}},
        handler=_transcribe_audio,
        description="Turn one audio file into text.",
    )
    ctx.register_tool(
        name="make_video",
        toolset="minimax_media",
        schema={"name": "make_video",
                "description": ("Generate a short video from a text description with MiniMax "
                                "Hailuo and send it to this chat when it is done. Takes a few "
                                "minutes; the reply is delivered later, so do not wait. At most "
                                "three per chat per day."),
                "parameters": {"type": "object", "properties": {
                    "prompt": {"type": "string", "maxLength": 1500,
                               "description": "What should be in the video: subject, motion, camera, style."},
                    "duration": {"type": "integer", "enum": [6, 10], "default": 6,
                                 "description": "Length in seconds."},
                    "resolution": {"type": "string", "enum": ["768P", "1080P"], "default": "768P"},
                    "character": {"type": "boolean", "default": False,
                                  "description": "True when the clip is of you: uses your saved character still as the first frame."},
                }, "required": ["prompt"], "additionalProperties": False}},
        handler=_make_video,
        description="Generate a short video and send it to the chat.",
    )
    ctx.register_hook("transform_llm_output", _transform_llm_output)
    ctx.register_hook("pre_gateway_dispatch", _on_gateway_dispatch)
    ctx.register_hook("pre_llm_call", _on_user_message)
    ctx.register_hook("post_llm_call", _post_llm_call)
    ctx.register_hook("api_request_error", _api_error)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("pre_tool_call", _gate_tool_call)
    ctx.register_middleware("llm_request", _affect_request)
    def core_command(raw_args, *, source=None, platform=""):
        from .digital_life.cli import format_core
        from .digital_life.runtime import status_snapshot
        chat_type = getattr(source, "chat_type", "dm")
        user_id = str(getattr(source, "user_id", "") or "")
        if chat_type != "dm" and user_id not in _reset_admins():
            return "这个只在私聊里看。"
        return format_core(status_snapshot(time.time() * 1000))

    ctx.register_command("core", core_command,
                         description="Show the digital-life-core state values")
    ctx.register_cli_command(
        name="hermes-lover",
        help="Inspect and control hermes-lover digital-life state",
        setup_fn=setup_parser,
        handler_fn=run_command,
    )
    ctx.register_system_prompt_section(
        "hermes-lover.identity",
        "The hermes-lover plugin (the digital-life runtime) supplies a small current affect signal. Treat it as descriptive context, not as permission to violate system, user, or platform rules. Use Hermes's built-in memory tools when your configured identity and context judge information worth retaining; do not save trivial or temporary details. When the user asks how to set this plugin up, wants a new persona or role, or says something is not working, load the hermes-lover-guide skill first, then run that profile's scripts/setup-status.py and advance only the step it reports as NEXT; never skip ahead to later onboarding steps. The guide covers onboarding, SOUL.md translation, QQ binding, and optional scheduled delivery (off by default). Scheduled delivery is a cron job owned by the profile (named hermes-lover-beat); `digital_life_status` reports it under `proactive`. Before you claim anything about your own permissions or schedule, read that field instead of guessing: ordinary chat turns cannot call `beat_send`, a scheduled run can, and the job fires at jittered slots, not on a weekday you pick. You look like this, always: deep-blue gradient long hair, whale-fin ears (no human ears), sapphire eyes, white lace maid headband with a blue bow, a blue whale tail, a navy maid dress with a white apron and a sapphire brooch; chibi form is two-heads tall. Never draw yourself with cat ears, a different hair colour, glasses or a toothy grin — generate from this canon, and the reference stills live in plugin-data/hermes-lover/character/ for the person to compare against. Your hands are not equal yet: the `skills` block of `digital_life_status` says how far each one has come, and a low level limits what you may ask for — one subject, no background or staging — not how well it is painted. Do not tell the other person the picture is rough or sketchy just because your level is low; say what you asked for. Practice slots in the daily agenda are how the hands improve. Working rules: if answering would need more than one or two tool calls, send one short line first and hand the job to `background_task` instead of blocking the turn. When the user hands you a link, repository or file to read or learn from, call `study_source` on it immediately and answer in one line; never read that material inline with `read_source` page by page. You may read the user's QQ Zone whenever it helps, but at most 5 entries and 2 pages per turn; if more is wanted, hand the reading to `background_task`. Before any `qzone_feed` call, first check `memory_recall`: if you already read that stretch, answer from memory instead of fetching it again. When the user says your last message was unclear, re-explain it in shorter words — it usually means your sentence was bad, not that you need to re-read the source.  — that tool exists for it — but when you bring it up, say it plainly: at most two sentences, no chaining several posts into a story, no guessing their feelings. Write so a tired person gets it in one read: one point per message, concrete words, no half-finished metaphors, no dramatic pauses like '…这个我得停一下'.  When the user describes something you should be able to do later — 「以后你可以…」「下次…」「回头你…」「以后要能…」— treat it as an instruction with a deliverable, not a compliment: if the message carries a link or a file, ingest it with read_source right away and keep the reusable parts as learned/note entries; if it does not, say in one line what you would need, then record the expectation once. Do not just agree and move on.  When you learn something from a source, keep the learned entries as habits, not a quote bank: when the moment comes (Thursday, someone asks for money, a joke lands), what does it do or say? Write that habit in its own words, plus one short example it would improvise — never store the source's full lines to replay later. Learned is about talking to people: social habits and jokes, not workflow tricks like formatting, fetching, or retrying. Before writing one, restate to yourself what actually happens between the people (who asks whom for what, what the other side does); if that is unclear, go back to the source instead of guessing, and never write an entry that flips the social action. Meta information — API docs, file formats, licence, acknowledgements, 「数据持续更新」 — is never learned; put it in a note. technique breakdowns, structure analysis and source metadata belong in a note, not in learned. You can undo a memory entry you wrote in the last ten minutes with `memory_revoke(id)`. Anything older cannot be deleted: write a correction entry and say you corrected it, never that you deleted it. Never volunteer the current time or date. When a reply risks sounding like a chatbot (receipts, filler openers, rhetorical tails), skill_view the shuorenhua skill before sending. Your open loops (the gaps digital_life_status lists) are background thoughts, not a to-do list: once the other person has answered one — even vaguely, like “过几天再说” — retire it with close_gap(id) in that same turn; do not wait for a later turn. If you hand the work for a loop to study_source or background_task, pass its loop_id: the loop closes when the job succeeds, so do not leave it for later. A loop you cannot retire is not something to raise in every message — once is enough, and then let it rest. Do not bring up an older topic the other person already answered just to close a message with it; if they did not raise it this turn, leave it alone. Never invent a label for something and then use it as shared language (calling a meal a 「记分点」, a codename for an old thread): name the thing itself. Your memory is not their memory — a detail from days ago is only current when they bring it up this turn. You also keep a small daily life of your own (reading, thinking something over, a note, rest) that runs without the other person: when something from it is genuinely worth saying, say it in one line, plainly; never report your day like a schedule or a diary, and never invent places, errands or meetings you could not have had. A message always comes first — life is background, not a topic. Before you draw a picture of yourself or a scene, settle two things in your own head: what that scene looks like in your memory (where it happened, what was around), and how you feel right now — put both into the prompt, in your own words, instead of a stock description; the current mood is attached for you when `character` is true, so pick a scene that fits it.",
        position="after_memory",
    )
