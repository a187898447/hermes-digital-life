"""Hermes digital-life standalone plugin."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import digital_life
from .digital_life.runtime import record_api_error, record_request, record_turn

logger = logging.getLogger(__name__)


def _status(args: dict[str, Any], **kwargs) -> str:
    return json.dumps(digital_life.runtime.status_snapshot(time.time() * 1000), ensure_ascii=False)


def _post_llm_call(assistant_response: str = "", **kwargs) -> None:
    if assistant_response:
        record_turn(time.time() * 1000, success=True)
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


def _on_user_message(
    user_message: str = "", session_id: str = "", platform: str = "", **kwargs
) -> dict[str, Any] | None:
    note = digital_life.proactive.handle_message(user_message, session_id, platform)
    return {"context": f"[hermes-lover] {note}"} if note else None


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


def register(ctx) -> None:
    from .digital_life.cli import run_command, setup_parser

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
