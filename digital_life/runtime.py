"""Life-state orchestration with read-only model affordances."""

from __future__ import annotations

from typing import Any

from .domain import decay_chemistry, float_or, reorder_formatted_memory_blocks, settle_rest


def settle_state(now_ms: float) -> dict[str, Any]:
    def mutate(state):
        if state["emergency_stop"]:
            return state
        elapsed = max(0.0, now_ms - state["last_settled_at"])
        if elapsed <= 0:
            return state
        for gap in state["gaps"]:
            if gap.get("status") == "open":
                gap["tension"] = _gap_tension(gap, now_ms)
        high_gap = any(g.get("status") == "open" and g.get("tension", 0) >= 70 for g in state["gaps"])
        state["chemistry"] = decay_chemistry(
            state["chemistry"], elapsed, state["temperament"],
            energy=state["energy"], high_tension_gap=high_gap,
        )
        rest = settle_rest(state["energy"], 100.0, elapsed, now_ms, False)
        state["energy"] = rest["energy"]
        if rest["sleep_ms"]:
            for gap in state["gaps"]:
                if gap.get("status") == "open" and gap.get("created_at", now_ms) <= now_ms - elapsed:
                    gap["paused_ms"] = float_or(gap.get("paused_ms"), 0.0) + rest["sleep_ms"]
                    gap["tension"] = _gap_tension(gap, now_ms)
        state["last_settled_at"] = now_ms
        return state

    from .storage import update_state
    return update_state(now_ms, mutate)


def status_snapshot(now_ms: float) -> dict[str, Any]:
    state = settle_state(now_ms)
    return {
        "chemistry": dict(state["chemistry"]),
        "energy": state["energy"],
        "temperament": dict(state["temperament"]),
        "open_gaps": sum(g.get("status") == "open" for g in state["gaps"]),
        "emergency_stop": state["emergency_stop"],
        "updated_at": state["last_settled_at"],
        "gaps": sorted(
            (dict(gap, tension=_gap_tension(gap, now_ms)) for gap in state["gaps"]),
            key=lambda gap: gap["tension"], reverse=True,
        ),
    }


def record_request(now_ms: float) -> None:
    """Charge the observed provider request; retry attempts are independently charged."""
    def mutate(state):
        if state["emergency_stop"]:
            return state
        state = settle_before_transition(state, now_ms)
        state["energy"] = max(0.0, state["energy"] - 0.05 * state["temperament"]["metabolism"])
        state["last_interaction_at"] = now_ms
        state["last_settled_at"] = now_ms
        return state
    from .storage import update_state
    update_state(now_ms, mutate)


def record_turn(now_ms: float, *, success: bool) -> None:
    """Reward an observed completed turn using the adopted default rule."""
    if not success:
        return
    def mutate(state):
        if state["emergency_stop"]:
            return state
        state = settle_before_transition(state, now_ms)
        state["chemistry"]["dopamine"] = min(100.0, state["chemistry"]["dopamine"] + 5.0)
        state["last_interaction_at"] = now_ms
        state["last_settled_at"] = now_ms
        return state
    from .storage import update_state
    update_state(now_ms, mutate)


def record_api_error(now_ms: float, *, error_type: str, retryable: bool | None) -> None:
    """Apply the fixed stress response only to transient errors observed by the host."""
    if retryable is False:
        return
    error = error_type.lower()
    if not any(tag in error for tag in ("timeout", "connection", "network", "http", "connecterror", "requesterror", "networkerror")):
        return
    def mutate(state):
        if state["emergency_stop"]:
            return state
        state = settle_before_transition(state, now_ms)
        sensitivity = state["temperament"]["sensitivity"]
        state["chemistry"]["cortisol"] = min(100.0, state["chemistry"]["cortisol"] + 20.0 * sensitivity)
        _open_or_reactivate_gap(state, f"Observed transient provider error: {error[:120]}", "api_request_error", now_ms)
        state["last_settled_at"] = now_ms
        return state
    from .storage import update_state
    update_state(now_ms, mutate)


def observe_tool_result(now_ms: float, *, tool_name: str, args: dict[str, Any], result: str) -> None:
    """Open/reopen a gap on observed search failure and close it on observed success."""
    if tool_name not in {"web_search", "web_fetch", "search", "grep", "glob"}:
        return
    empty_markers = ("no results", "no matches", "no results found", "0 results", "empty result")
    try:
        import json
        decoded = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        decoded = result
    failed = isinstance(decoded, dict) and bool(decoded.get("error"))
    rendered = str(result or "").lower()
    empty = not rendered.strip() or any(marker in rendered for marker in empty_markers)
    if isinstance(decoded, list):
        empty = empty or not decoded
    elif isinstance(decoded, dict):
        for key in ("results", "items", "matches"):
            if key in decoded and isinstance(decoded[key], list) and not decoded[key]:
                empty = True
                break
    query = str(args.get("query") or args.get("pattern") or args.get("url") or tool_name).strip()[:160]
    if not failed and not empty:
        successful_query = str(args.get("query") or args.get("pattern") or args.get("url") or "").strip()
        if not successful_query:
            return
        def close_observed(state):
            if state["emergency_stop"]:
                return state
            state = settle_before_transition(state, now_ms)
            targets = [gap for gap in state["gaps"] if gap.get("status") == "open"
                       and (gap.get("query") == successful_query or _gap_similarity(
                           _normalize_gap_text(successful_query),
                           _normalize_gap_text(str(gap.get("summary", ""))),
                       ) >= 0.6)]
            target_summaries = [str(gap.get("summary", "")) for gap in targets]
            for gap in state["gaps"]:
                if gap.get("status") != "open":
                    continue
                if any(_gap_similarity(
                    _normalize_gap_text(str(gap.get("summary", ""))),
                    _normalize_gap_text(summary),
                ) >= 0.6 for summary in target_summaries):
                    gap["status"] = "resolved"
                    gap["tension"] = 0.0
                    gap["relief"] = 0.0
                    gap["resolved_at"] = now_ms
                    gap["closed_by"] = "observed_search_success"
            state["last_interaction_at"] = now_ms
            state["last_settled_at"] = now_ms
            return state
        from .storage import update_state
        update_state(now_ms, close_observed)
        return
    summary = f"Observed {tool_name} {'failure' if failed else 'empty result'}: {query}"

    def mutate(state):
        if state["emergency_stop"]:
            return state
        state = settle_before_transition(state, now_ms)
        _open_or_reactivate_gap(state, summary, f"tool:{tool_name}", now_ms, query=query)
        state["last_interaction_at"] = now_ms
        state["last_settled_at"] = now_ms
        return state

    from .storage import update_state
    update_state(now_ms, mutate)


def _open_or_reactivate_gap(state: dict[str, Any], summary: str, source: str, now_ms: float,
                            *, query: str | None = None) -> None:
    norm = _normalize_gap_text(summary)
    for gap in state["gaps"]:
        existing = _normalize_gap_text(str(gap.get("summary", "")))
        similarity = _gap_similarity(norm, existing)
        if similarity < 0.6:
            continue
        if gap.get("status") == "resolved":
            gap["status"] = "open"
            gap["resolved_at"] = None
            gap.pop("closed_by", None)
            gap["relief"] = max(0.0, float_or(gap.get("relief"), 0.0) - 10.0)
        gap["source"] = source
        if query:
            gap["query"] = query
        gap["tension"] = _gap_tension(gap, now_ms)
        return
    state["gaps"].append({
        "id": f"gap-{int(now_ms)}-{len(state['gaps'])}",
        "summary": summary[:200],
        "status": "open",
        "created_at": now_ms,
        "source": source,
        **({"query": query} if query else {}),
        "relief": 0.0,
        "tension": 80.0,
    })
    state["gaps"] = state["gaps"][-200:]


def _normalize_gap_text(text: str) -> str:
    import re
    value = text.lower()
    value = re.sub(r"我不知道|不知道|不晓得|不确定|不清楚|没法回答|无法回答|无法确定|找不到|查不到|没有找到|说不准|不好说", "", value)
    return re.sub(r"[\s，。！？、；：\"''（）()《》【】\[\]…·,.!?;:'\"()<>~@#$%^&+=/\\|]", "", value)


def _gap_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    if len(short) >= 4 and short in long:
        return len(short) / len(long)
    def bigrams(value: str) -> set[str]:
        if len(value) <= 2:
            return {value}
        return {value[index:index + 2] for index in range(len(value) - 1)}
    first, second = bigrams(left), bigrams(right)
    union = first | second
    return len(first & second) / len(union) if union else 0.0


def _gap_tension(gap: dict[str, Any], now_ms: float) -> float:
    if gap.get("status") == "resolved":
        return 0.0
    elapsed = max(0.0, now_ms - float_or(gap.get("created_at"), now_ms) - float_or(gap.get("paused_ms"), 0.0))
    elapsed_hours = elapsed / 3_600_000
    return max(0.0, min(100.0, 80.0 + 5.0 * elapsed_hours - float_or(gap.get("relief"), 0.0)))


def settle_before_transition(state: dict[str, Any], now_ms: float) -> dict[str, Any]:
    elapsed = max(0.0, now_ms - state["last_settled_at"])
    if elapsed:
        high_gap = any(g.get("status") == "open" and g.get("tension", 0) >= 70 for g in state["gaps"])
        state["chemistry"] = decay_chemistry(
            state["chemistry"], elapsed, state["temperament"],
            energy=state["energy"], high_tension_gap=high_gap,
        )
        state["energy"] = settle_rest(state["energy"], 100.0, elapsed, now_ms, False)["energy"]
    return state


def affect_note(now_ms: float) -> str:
    state = status_snapshot(now_ms)
    dopamine = state["chemistry"]["dopamine"]
    cortisol = state["chemistry"]["cortisol"]
    serotonin = state["chemistry"]["serotonin"]
    energy = state["energy"]
    if state["emergency_stop"]:
        directive = "Emergency stop is active. Do not initiate activity or change life state."
    elif cortisol >= 80:
        directive = "Current stress is high: keep attention on immediate concerns; be concise and calm. Other retrieved memories still exist; do not imply they were lost."
    elif energy < 20:
        directive = "Energy is very low: respond briefly and avoid initiating extra activity."
    elif serotonin < 35:
        directive = "Mood stability is low: use a restrained, patient tone."
    elif dopamine >= 70:
        directive = "Engagement is elevated: a warmer, more animated tone is appropriate."
    else:
        directive = "Use the configured identity and current conversation; do not infer emotion beyond these signals."
    return (
        "[Digital-life affect snapshot; state is advisory and does not override system policy]\n"
        f"dopamine={dopamine:.1f}, cortisol={cortisol:.1f}, serotonin={serotonin:.1f}, energy={energy:.1f}. "
        f"{directive}"
    )


def reorder_prompt_memories(prompt: str, now_ms: float) -> tuple[str, int]:
    state = status_snapshot(now_ms)
    return reorder_formatted_memory_blocks(
        prompt,
        state["chemistry"]["cortisol"],
        state["chemistry"]["dopamine"],
    )


def stop_state(now_ms: float, stopped: bool) -> None:
    from .storage import update_state
    def mutate(state):
        state["emergency_stop"] = stopped
        if not stopped:
            state["energy"] = 100.0
        state["last_settled_at"] = now_ms
        return state
    update_state(now_ms, mutate)


def resolve_gap(now_ms: float, gap_id: str) -> bool:
    """Operator-requested gap closure; no arbitrary numeric mutation is accepted."""
    resolved = False
    def mutate(state):
        nonlocal resolved
        if state["emergency_stop"]:
            return None
        for gap in state["gaps"]:
            if gap.get("id") == gap_id and gap.get("status") == "open":
                gap["status"] = "resolved"
                gap["tension"] = 0.0
                gap["relief"] = 0.0
                gap["resolved_at"] = now_ms
                gap["closed_by"] = "operator"
                resolved = True
        state["last_settled_at"] = now_ms
        return state
    from .storage import update_state
    update_state(now_ms, mutate)
    return resolved
