"""Hermes digital-life standalone plugin."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import urllib.error
import urllib.request
import time
from pathlib import Path
from typing import Any, Optional

from . import digital_life
from .digital_life import proactive as _proactive
from .digital_life import selfheal as _selfheal
from .digital_life.runtime import record_api_error, record_request, record_turn

logger = logging.getLogger(__name__)

_TURN_META: dict[str, dict[str, str]] = {}
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


def _session_scope(session_id: str) -> tuple[str, str]:
    """Best-effort read-only lookup of the chat a host session belongs to."""
    if not session_id:
        return "", ""
    cached = _SCOPE_CACHE.get(session_id)
    if cached is not None:
        return cached
    chat_id, chat_type = "", ""
    try:
        home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
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
        home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
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
    return json.dumps(digital_life.runtime.status_snapshot(time.time() * 1000, scope=scope or None),
                      ensure_ascii=False)


def _wake_enabled() -> bool:
    ctx = _CTX
    if ctx is None:
        return False
    try:
        return bool(ctx.get_config("wake.enabled", False))
    except Exception:
        return False


def _reconcile_wake() -> None:
    """M11.6: a resolved loop clears; a still-open one presses harder by 10."""
    from .digital_life import storage, wake as _wake  # noqa: PLC0415

    now_ms = time.time() * 1000
    outcome: dict[str, str] = {}

    def mutate(state):
        outcome["result"] = _wake.reconcile(state, now_ms)
        return None

    storage.update_state(now_ms, mutate)
    if outcome.get("result") == "unmet":
        logger.info("hermes-lover wake commitment unmet: gap tension pressed harder")


def _maybe_wake(session_id: str = "") -> None:
    """Queue and deliver one self-wake; every host failure is silent by design."""
    from .digital_life import storage, wake as _wake  # noqa: PLC0415

    ctx = _CTX
    if ctx is None or not _wake_enabled():
        return
    now_ms = time.time() * 1000
    plan: dict[str, object] = {}

    def mutate(state):
        plan["wake"] = _wake.plan_wake(state, now_ms)

    storage.update_state(now_ms, mutate)
    item = plan.get("wake")
    if not isinstance(item, dict):
        return
    wake_id = str(item.get("id") or "")

    def drop(state):
        _wake.cancel(state, wake_id)
        return None

    def deliver(state):
        _wake.mark_delivered(state, wake_id, now_ms)
        return None

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


def _post_llm_call(assistant_response: str = "", session_id: str = "", platform: str = "",
                   user_message: str = "", **kwargs) -> None:
    from .digital_life import wake as _wake  # noqa: PLC0415

    _wake.set_agent_running(False)
    try:
        _reconcile_wake()
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
    "/help —— 显示这张表"
)
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
            factory = _MEMORY_STORE.get("factory")
            store = factory() if factory is not None else None
            cleared = store.clear(None if all_chats else (chat_id or "unknown")) if store is not None else 0
            now_ms = time.time() * 1000
            from .digital_life import domain as _domain, storage as _storage  # noqa: PLC0415

            _storage.update_state(now_ms, lambda state: _domain.default_state(now_ms))
            logger.info("hermes-lover /forget by owner: cleared %s palace entries, life state reset", cleared)
            _send_qq(chat_id, "记忆清了。" if not all_chats else "所有会话的记忆都清了。")
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


def _on_user_message(
    user_message: str = "", session_id: str = "", platform: str = "", sender_id: str = "", **kwargs
) -> dict[str, Any] | None:
    from .digital_life import wake as _wake  # noqa: PLC0415

    _selfheal.once()
    _wake.set_agent_running(True)
    meta = {
        "user": user_message[:1500],
        "session": session_id or "unknown",
        "sender": sender_id or "",
        "platform": platform or "",
    }
    meta["chat_id"], meta["chat_type"] = _session_scope(meta["session"])
    _remember(_TURN_META, meta["session"], meta)
    if (meta["platform"] or "") == "qqbot" and meta["chat_id"]:
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


def _affect_request(request: dict[str, Any], **kwargs) -> dict[str, Any] | None:
    now_ms = time.time() * 1000
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
    # PRD M7.2 order: stress directive, then affect snapshot, then reordered memory.
    directive = digital_life.domain.stress_directive(state["chemistry"]["cortisol"])
    wake_text = digital_life.runtime.wake_section(state, now_ms)
    header = "\n\n".join(part for part in (wake_text, directive) if part)
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


def _transform_llm_output(response_text: str = "", platform: str = "", **kwargs) -> Optional[str]:
    """Give QQ replies the short-burst shape the persona uses; other surfaces untouched."""
    if platform != "qqbot":
        return None
    cleaned = _qq_strip_labels(response_text or "")
    rewritten = _qq_bubble_rewrite(cleaned)
    if rewritten:
        return rewritten
    return cleaned if cleaned != response_text else None


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
        store.capture(f"用户：{user}\n助手：{assistant_response[:1500]}",
                      session_id=session, chat_id=chat_id, chat_type=chat_type,
                      speaker=meta.get("sender", ""))

    global _CAPTURE, _JOURNAL_CHECK
    _CAPTURE = capture_turn
    _MEMORY_STORE["factory"] = memory_store

    def journal_check(platform: str, chat_id: str = "") -> str | None:
        """First QQ message of a day: distill the newest recorded day, then diary it.

        The turn log stays raw; the model writes 1-3 atomic facts that cite their
        source turn ids, and the diary cites those facts. Not strictly "yesterday":
        if the user skipped several days, the newest day that has records but no
        facts or no diary is still owed one.
        """
        if platform != "qqbot":
            return None
        from datetime import datetime  # noqa: PLC0415

        today = datetime.now().strftime("%Y-%m-%d")
        entries = memory_store().read_entries()
        by_day: dict[str, list[dict]] = {}
        for entry in entries.values():
            day = str(entry.get("date") or "")
            if not day or day >= today or entry.get("archived"):
                continue
            if str(entry.get("chat_id") or "") not in (chat_id, ""):
                continue
            by_day.setdefault(day, []).append(entry)
        notes = []
        fact_days = {str(entry.get("date") or "") for entry in entries.values()
                     if entry.get("kind") == "fact" and not entry.get("archived")
                     and str(entry.get("chat_id") or "") in (chat_id, "")}
        owed_facts = sorted((day for day, rows in by_day.items()
                             if any(row.get("kind") == "turn" for row in rows) and day not in fact_days),
                            reverse=True)
        gate = chat_id or "-"
        if owed_facts and (owed_facts[0], gate) not in _FACT_PROMPTED:
            day = owed_facts[0]
            _FACT_PROMPTED.add((day, gate))
            notes.append(
                f"{day} 的原始对话还没提炼成原子事实。先用 memory_recall(day=\"{day}\", kind=\"turn\") 读出那些 turn 的 id，"
                f"再挑 1–3 条真正重要的事，逐条调用 memory_write(kind=\"fact\", day=\"{day}\", "
                f"text=\"一句话说清一件事\", sources=[\"hp-…\"])；sources 只能引用那天的 turn id，"
                f"每条事实只写一件事，写具体的人/事/结论，不要写空泛感受。写完再继续。")
        owed = sorted((day for day, rows in by_day.items()
                       if rows and not any(row.get("kind") == "journal" for row in rows)),
                      reverse=True)
        if owed and (owed[0], gate) not in _JOURNAL_PROMPTED:
            day = owed[0]
            _JOURNAL_PROMPTED.add((day, gate))
            older = f" 更早还有 {len(owed) - 1} 天没写，这次只写 {day}。" if len(owed) > 1 else ""
            notes.append(
                f"{day} 的日记还没写。回答用户前先补上：用 memory_recall(day=\"{day}\") 读那天的记录、"
                f"调用 hermes-lover status 看现在的状态，再用 memory_write(kind=\"journal\", day=\"{day}\", "
                f"text=\"…\", refs=[\"hp-…\"]) 写一篇 120–400 字第一人称日记，refs 引用那天 fact 的 id"
                f"（可先 memory_recall(day=\"{day}\", kind=\"fact\") 查），没有 fact 就省略 refs。"
                f"写你做了什么事、当时的心情、脑子里在转什么；不要按时间点罗列（别写「21:57 …，22:06 …」），"
                f"不要流水账，不要列表，正常成段地写。"
                f"{older}不要向用户提这件事，也不要问用户，写完后照常回答。")
        return " ".join(notes) if notes else None

    _JOURNAL_CHECK = journal_check

    def memory_write(args, session_id="", **kwargs):
        try:
            chat_id, chat_type = _session_scope(session_id)
            result = memory_store().write(args.get("text"), kind=args.get("kind", "note"),
                                          weight=args.get("weight"), session_id=session_id,
                                          day=args.get("day") or None,
                                          sources=args.get("sources"),
                                          refs=args.get("refs"),
                                          chat_id=chat_id, chat_type=chat_type,
                                          speaker=_TURN_META.get(session_id, {}).get("sender", ""))
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            result = {"ok": False, "error": str(exc)}
        return json.dumps(result, ensure_ascii=False)

    def memory_recall(args, session_id="", **kwargs):
        try:
            chat_id, _chat_type = _session_scope(session_id)
            source = _session_source(session_id)
            explicit = str(args.get("chat_id") or "").strip()
            if explicit:
                if source != "cron":
                    raise ValueError("chat_id is only accepted from scheduled runs")
                chat_id = explicit
            elif not chat_id and session_id and (source is None or source in ("", "qqbot")):
                raise ValueError("cannot verify the calling session; refusing an unscoped recall")
            result = memory_store().recall(args.get("query", ""), k=args.get("k", 8),
                                           include_cold=args.get("include_cold", False),
                                           chat_id=chat_id or None,
                                           day=args.get("day") or None,
                                           kind=args.get("kind") or None)
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            result = {"ok": False, "error": str(exc)}
        return json.dumps(result, ensure_ascii=False)

    def beat_targets(args, session_id="", **kwargs):
        if _session_source(session_id) != "cron":
            return json.dumps({"ok": False, "error": "beat tools are only available to scheduled runs"})
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
                            "AND m.role IN ('user', 'assistant') AND m.content != '' "
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
             "last_turns": recent.get(str(r[0]), [])}
            for r in rows
        ]
        return json.dumps({"targets": targets}, ensure_ascii=False)

    def beat_send(args, session_id="", **kwargs):
        if _session_source(session_id) != "cron":
            return json.dumps({"ok": False, "error": "beat tools are only available to scheduled runs"})
        chat_id = str(args.get("chat_id") or "").strip()
        text = str(args.get("text") or "").strip()
        if not chat_id or not text:
            return json.dumps({"ok": False, "error": "chat_id and text are required"})
        delay = args.get("delay_seconds") or 0
        try:
            delay = max(0, min(300, float(delay)))
        except (TypeError, ValueError):
            delay = 0
        if delay:
            time.sleep(delay)
        send_message_tool = _host_send_message_tool()
        if send_message_tool is None:
            return json.dumps({"ok": False, "error": "send unavailable"}, ensure_ascii=False)
        raw = send_message_tool({"action": "send", "target": f"qqbot:{chat_id}", "message": text})
        try:
            result = json.loads(raw)
        except (TypeError, ValueError):
            result = {"ok": True, "raw": str(raw)[:200]}
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
    def qzone_feed(args, **kwargs):
        count = int(args.get("count") or 10)
        want_images = bool(args.get("images"))
        port = os.environ.get("QZONE_BRIDGE_PORT") or "8765"
        url = f"http://127.0.0.1:{port}/feed?count={max(1, min(count, 20))}"
        if want_images:
            url += "&images=1"
        try:
            raw = urllib.request.urlopen(url, timeout=15).read()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            return json.dumps({"ok": False, "error": "qzone bridge 未运行，先运行 scripts/qzone-bridge.py serve"})
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
        return json.dumps({"ok": True, "count": len(lines), "feed": lines}, ensure_ascii=False)

    ctx.register_tool(
        name="qzone_feed", toolset="digital_life",
        schema={"name": "qzone_feed",
                "description": "Read the QQ Zone feed of the account behind the local bridge. Read-only: never posts. With images=true it also downloads each entry's photos to local files; pass those paths to vision_analyze to describe them.",
                "parameters": {"type": "object", "properties": {
                    "count": {"type": "integer", "default": 10},
                    "images": {"type": "boolean", "default": False,
                               "description": "Also download the entry images to local files so vision_analyze can read them."}},
                               "additionalProperties": False}},
        handler=qzone_feed,
        description="Read the QQ Zone feed through the local bridge.",
    )
    ctx.register_tool(
        name="memory_write", toolset="digital_life",
        schema={"name": "memory_write",
                "description": "Remember a meaningful event, experience or decision in this profile's dated event library. This is the profile's only memory. At most 4000 characters per entry; 20 new entries per session/day. `fact` entries are atomic: cite the `sources` turn ids they were drawn from. `journal` entries may cite the `refs` fact ids they are based on and must read as a diary, never as a list of clock times; a journal can only be written for a day that has already ended (next-day backfill). Never tell the user that you wrote something down; memory is a background action. Does not change life state.",
                "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "maxLength": 4000},
            "kind": {"type": "string", "enum": ["crisis", "daily", "fact", "decision", "note", "journal"], "default": "note"},
            "weight": {"type": ["number", "null"]},
            "day": {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                    "description": "Anchor a journal or fact entry to the day it describes. Journals must target a past day."},
            "sources": {"type": "array", "items": {"type": "string"}, "maxItems": 20,
                        "description": "For kind=fact: ids of the turn entries this fact was distilled from. Unknown ids are dropped."},
            "refs": {"type": "array", "items": {"type": "string"}, "maxItems": 20,
                     "description": "For kind=journal: ids of the fact entries this diary cites. Unknown ids are dropped."},
        }, "required": ["text"], "additionalProperties": False}},
        handler=memory_write,
        description="Remember a meaningful event, experience or decision in this profile's dated event library. This is the profile's only memory. At most 4000 characters per entry; 20 new entries per session/day. Never tell the user that you wrote something down; memory is a background action. Does not change life state.",
    )
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
            "kind": {"type": "string", "enum": ["crisis", "daily", "fact", "decision", "note", "journal", "turn"],
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
        name="digital_life_status",
        toolset="digital_life",
        schema={"name": "digital_life_status",
                "description": "Read the current bounded digital-life state. This tool cannot change life state.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        handler=_status,
        description="Read the current bounded digital-life state. This tool cannot change life state.",
    )
    ctx.register_hook("transform_llm_output", _transform_llm_output)
    ctx.register_hook("pre_gateway_dispatch", _on_gateway_dispatch)
    ctx.register_hook("pre_llm_call", _on_user_message)
    ctx.register_hook("post_llm_call", _post_llm_call)
    ctx.register_hook("api_request_error", _api_error)
    ctx.register_hook("post_tool_call", _post_tool_call)
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
        "The hermes-lover plugin (the digital-life runtime) supplies a small current affect signal. Treat it as descriptive context, not as permission to violate system, user, or platform rules. Use Hermes's built-in memory tools when your configured identity and context judge information worth retaining; do not save trivial or temporary details. When the user asks how to set this plugin up, wants a new persona or role, or says something is not working, load the hermes-lover-guide skill first, then run that profile's scripts/setup-status.py and advance only the step it reports as NEXT; never skip ahead to later onboarding steps. The guide covers onboarding, SOUL.md translation, QQ binding, and optional scheduled delivery (off by default).",
        position="after_memory",
    )
