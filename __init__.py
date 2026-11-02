"""Hermes digital-life standalone plugin."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from . import digital_life
from .digital_life import proactive as _proactive
from .digital_life.runtime import record_api_error, record_request, record_turn

logger = logging.getLogger(__name__)

_LAST_TURN: dict[str, str] = {}
_SCOPE_CACHE: dict[str, tuple[str, str]] = {}
_CAPTURE = None
_JOURNAL_CHECK = None
_JOURNAL_PROMPTED: set[str] = set()
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
    except (sqlite3.Error, OSError, TypeError, ValueError):
        pass
    _SCOPE_CACHE[session_id] = (chat_id, chat_type)
    return chat_id, chat_type


def _status(args: dict[str, Any], **kwargs) -> str:
    return json.dumps(digital_life.runtime.status_snapshot(time.time() * 1000), ensure_ascii=False)


def _post_llm_call(assistant_response: str = "", **kwargs) -> None:
    if assistant_response:
        record_turn(time.time() * 1000, success=True)
        try:
            if _CAPTURE is not None:
                _CAPTURE(assistant_response)
        except Exception:
            pass
        try:
            if assistant_response.lstrip().startswith("/"):
                return
            unknown = digital_life.domain.find_gap_sentence(assistant_response)
            if unknown:
                digital_life.runtime.observe_assistant_unknown(time.time() * 1000, unknown)
        except Exception:
            pass


def _api_error(error: dict[str, Any] | None = None, retryable: bool | None = None, **kwargs) -> None:
    now_ms = time.time() * 1000
    error_type = str((error or {}).get("type", ""))
    record_api_error(now_ms, error_type=error_type, retryable=retryable)


def _post_tool_call(tool_name: str = "", args: dict[str, Any] | None = None, result: str = "", **kwargs) -> None:
    digital_life.runtime.observe_tool_result(time.time() * 1000, tool_name=tool_name, args=args or {}, result=result)


_RESET_COMMAND = re.compile(r"^/(?:new|reset)(?:\s+(?P<scope>all|全部))?(?:\s|$)", re.IGNORECASE)
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
    """``/new`` (or ``/reset``) in QQ: owner/manager only, and it wipes palace + life state."""
    try:
        text = (getattr(event, "text", "") or "").strip()
        source = getattr(event, "source", None)
        platform = getattr(getattr(source, "platform", None), "value", "") or ""
        match = _RESET_COMMAND.match(text)
        if platform != "qqbot" or not match:
            return None
        sender = (getattr(event, "sender_id", "") or getattr(source, "user_id", "") or "")
        sender = str(sender)
        if sender not in _reset_admins():
            logger.info("hermes-lover /new ignored: sender %s is not an owner", sender[-6:])
            return None
        chat_id = str(getattr(source, "chat_id", "") or "")
        factory = _MEMORY_STORE.get("factory")
        store = factory() if factory is not None else None
        if store is not None:
            cleared = store.clear(None if (match.group("scope") or "").lower() in {"all", "全部"}
                                  else (chat_id or None))
        else:
            cleared = 0
        now_ms = time.time() * 1000
        from .digital_life import domain as _domain, storage as _storage  # noqa: PLC0415

        _storage.update_state(now_ms, lambda state: _domain.default_state(now_ms))
        logger.info("hermes-lover /new by owner: cleared %s palace entries, life state reset", cleared)
    except Exception:
        logger.warning("hermes-lover /new reset failed", exc_info=True)
    return None


def _on_user_message(
    user_message: str = "", session_id: str = "", platform: str = "", sender_id: str = "", **kwargs
) -> dict[str, Any] | None:
    _LAST_TURN["user"] = user_message[:1500]
    _LAST_TURN["session"] = session_id or "unknown"
    _LAST_TURN["sender"] = sender_id or ""
    _LAST_TURN["platform"] = platform or ""
    _LAST_TURN["chat_id"], _LAST_TURN["chat_type"] = _session_scope(_LAST_TURN["session"])
    notes = []
    note = _proactive.handle_message(user_message, session_id, platform)
    if note:
        notes.append(note)
    if _JOURNAL_CHECK is not None:
        try:
            journal_note = _JOURNAL_CHECK(platform)
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


def _append_open_loops_once(content: Any, gaps: list[dict[str, Any]], now_ms: float) -> tuple[Any, bool]:
    """Append the open-loop suffix to exactly one text block (the last one)."""
    append = digital_life.runtime.append_open_loops
    if isinstance(content, str):
        appended = append(content, gaps, now_ms)
        return appended, appended != content
    if isinstance(content, list):
        items = list(content)
        for index in range(len(items) - 1, -1, -1):
            item = items[index]
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                appended = append(item["text"], gaps, now_ms)
                if appended == item["text"]:
                    return content, False
                items[index] = {**item, "text": appended}
                return items, True
    return content, False


def _affect_request(request: dict[str, Any], **kwargs) -> dict[str, Any] | None:
    now_ms = time.time() * 1000
    record_request(now_ms)
    state = digital_life.runtime.status_snapshot(now_ms)
    temperature = request.get("temperature")
    # B-class Hermes policy: an explicit caller temperature always wins.
    result_temperature = (
        digital_life.domain.pick_temperature(state["chemistry"], state["energy"])
        if "temperature" not in request or temperature is None else None
    )
    note = digital_life.runtime.affect_note(now_ms)
    changed = result_temperature is not None
    result = dict(request)
    system = result.get("system")
    if isinstance(system, str):
        rewritten, blocks = digital_life.runtime.reorder_prompt_memories(system, now_ms)
        appended = digital_life.runtime.append_open_loops(rewritten, state["gaps"], now_ms)
        if blocks or appended != system:
            result["system"] = appended
            changed = True
    elif isinstance(system, list):
        system_items = list(system)
        for index, item in enumerate(system_items):
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                rewritten, blocks = digital_life.runtime.reorder_prompt_memories(item["text"], now_ms)
                if blocks:
                    system_items[index] = {**item, "text": rewritten}
                    changed = True
        system_items, appended = _append_open_loops_once(system_items, state["gaps"], now_ms)
        if appended:
            changed = True
        if changed:
            result["system"] = system_items
    messages = request.get("messages")
    if isinstance(messages, list):
        rewritten = list(messages)
        for index, message in enumerate(rewritten):
            if not isinstance(message, dict) or message.get("role") != "system":
                continue
            content, reordered = _reorder_content(message.get("content"), now_ms)
            content, appended = _append_open_loops_once(content, state["gaps"], now_ms)
            reordered = reordered or appended
            if reordered:
                rewritten[index] = {**message, "content": content}
                changed = True
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
            content, appended = _append_open_loops_once(content, state["gaps"], now_ms)
            reordered = reordered or appended
            if reordered:
                rewritten[index] = {**item, "content": content}
                changed = True
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
    return _qq_bubble_rewrite(response_text)


def register(ctx) -> None:
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

    def capture_turn(assistant_response: str) -> None:
        if not ctx.get_config("memory.capture", True):
            return
        if _LAST_TURN.get("platform") != "qqbot":
            # CLI and Studio keep the host's native memory; only QQ turns enter the palace.
            return
        user = _LAST_TURN.get("user", "")
        if not user:
            return
        store = memory_store()
        store.capture(f"用户：{user}\n助手：{assistant_response[:1500]}",
                      session_id=_LAST_TURN.get("session", "unknown"),
                      chat_id=_LAST_TURN.get("chat_id", ""),
                      chat_type=_LAST_TURN.get("chat_type", ""),
                      speaker=_LAST_TURN.get("sender", ""))

    global _CAPTURE, _JOURNAL_CHECK
    _CAPTURE = capture_turn
    _MEMORY_STORE["factory"] = memory_store

    def journal_check(platform: str) -> str | None:
        """First QQ message of a day: if yesterday has no diary yet, ask the model to write it."""
        if platform != "qqbot":
            return None
        from datetime import datetime, timedelta  # noqa: PLC0415

        day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if day in _JOURNAL_PROMPTED:
            return None
        entries = memory_store().read_entries()
        rows = [e for e in entries.values() if e.get("date") == day and not e.get("archived")]
        if not rows or any(e["kind"] == "journal" for e in rows):
            return None
        _JOURNAL_PROMPTED.add(day)
        return (f"昨天的日记还没写。回答用户前先补上：用 memory_recall(day=\"{day}\") 读昨天的记录、"
                f"调用 hermes-lover status 看现在的状态，再用 memory_write(kind=\"journal\", day=\"{day}\", "
                f"text=\"…\") 写一篇 120–400 字第一人称日记，写清开心和难过的地方、发生了什么、自己的一点想法。"
                f"不要向用户提这件事，也不要问用户，写完后照常回答。")

    _JOURNAL_CHECK = journal_check

    def memory_write(args, session_id="", **kwargs):
        try:
            result = memory_store().write(args.get("text"), kind=args.get("kind", "note"),
                                          weight=args.get("weight"), session_id=session_id,
                                          day=args.get("day") or None,
                                          chat_id=_LAST_TURN.get("chat_id", ""),
                                          chat_type=_LAST_TURN.get("chat_type", ""),
                                          speaker=_LAST_TURN.get("sender", ""))
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            result = {"ok": False, "error": str(exc)}
        return json.dumps(result, ensure_ascii=False)

    def memory_recall(args, **kwargs):
        try:
            result = memory_store().recall(args.get("query", ""), k=args.get("k", 8),
                                           include_cold=args.get("include_cold", False),
                                           chat_id=_LAST_TURN.get("chat_id", "") or None,
                                           day=args.get("day") or None)
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            result = {"ok": False, "error": str(exc)}
        return json.dumps(result, ensure_ascii=False)

    ctx.register_tool(
        name="memory_write", toolset="digital_life",
        schema={"type": "object", "properties": {
            "text": {"type": "string", "maxLength": 4000},
            "kind": {"type": "string", "enum": ["crisis", "daily", "fact", "decision", "note", "journal"], "default": "note"},
            "weight": {"type": ["number", "null"]},
            "day": {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                    "description": "Anchor a journal entry to the day it describes."},
        }, "required": ["text"], "additionalProperties": False},
        handler=memory_write,
        description="Remember a meaningful event, experience or decision in this profile's dated event library. This is the profile's only memory. At most 4000 characters per entry; 20 new entries per session/day. Does not change life state.",
    )
    ctx.register_tool(
        name="memory_recall", toolset="digital_life",
        schema={"type": "object", "properties": {
            "query": {"type": ["string", "null"], "default": ""},
            "k": {"type": "integer", "default": 8},
            "include_cold": {"type": "boolean", "default": False},
            "day": {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                    "description": "Return that day's journal first, then its raw entries."},
        }, "additionalProperties": False},
        handler=memory_recall,
        description="Recall past events from this profile's event library using Chinese words or English tokens. Empty query ranks by weight, recency and attention. Pass `day` (YYYY-MM-DD) to open a day's journal first and then that day's entries. Displayed memories refresh their access history except during emergency stop. Does not change life state.",
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
        schema={"type": "object", "properties": {}, "additionalProperties": False},
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
