"""Life-state orchestration with read-only model affordances."""

from __future__ import annotations

import time
from typing import Any

from . import affect
from . import events
from . import gaps as gap_pool
from . import life as life_module
from .chemical_pool import ChemicalPool
from .domain import clamp, decay_chemistry, float_or, reorder_formatted_memory_blocks, settle_rest

POOL = ChemicalPool()

# One proactive message may be left unanswered; after this many in a row the
# plugin stops sending until the person speaks, instead of turning into spam.
OUTREACH_REST_AT = 3
# Silent waiting itself erodes serotonin (about 7 points an hour); a reply ends
# the drain and gives a little back. The floor keeps the mood low, never absurd.
WAITING_DRAIN_PER_MINUTE = 0.12
WAITING_SEROTONIN_FLOOR = 20.0


def settle_state(now_ms: float) -> dict[str, Any]:
    def mutate(state):
        if state["emergency_stop"]:
            return None
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
        _drain_waiting_silence(state, elapsed)
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


def _drain_waiting_silence(state: dict[str, Any], elapsed_ms: float) -> None:
    """A nudge that nobody answered costs mood while it sits there unanswered."""
    book = state.get("outreach")
    if not isinstance(book, dict) or elapsed_ms <= 0:
        return
    waiting = any(isinstance(entry, dict) and int(float_or(entry.get("unanswered"), 0.0)) > 0
                  for entry in book.values())
    if not waiting:
        return
    chemistry = state.get("chemistry")
    if not isinstance(chemistry, dict):
        return
    current = float_or(chemistry.get("serotonin"), 50.0)
    chemistry["serotonin"] = max(
        WAITING_SEROTONIN_FLOOR, current - WAITING_DRAIN_PER_MINUTE * (elapsed_ms / 60_000.0)
    )


def _gap_visible(gap: dict[str, Any], scope: str | None) -> bool:
    """A gap may only be shown to the conversation that raised it (or to every one)."""
    return str(gap.get("chat", "") or "") in ("", scope or "")


def status_snapshot(now_ms: float, scope: str | None = None) -> dict[str, Any]:
    state = settle_state(now_ms)
    visible = [gap for gap in state["gaps"] if _gap_visible(gap, scope)]
    return {
        "chemistry": dict(state["chemistry"]),
        "energy": state["energy"],
        "temperament": dict(state["temperament"]),
        "open_gaps": sum(g.get("status") == "open" for g in visible),
        "emergency_stop": state["emergency_stop"],
        "wake": state.get("wake") or {"schema": 2, "chats": {}},
        "affect_history": [dict(item) for item in state.get("affect_history", [])],
        "affect": {
            "mood": affect.label(state),
            "valence": affect.valence(state),
            "arousal": affect.arousal(state),
            "deltas": affect.deltas(state),
            "hint": affect.hint(state),
        },
        "mood": {
            "label": affect.label(state),
            "since": float_or((state.get("mood") or {}).get("since"), now_ms),
            "minutes": affect.mood_since(state, now_ms),
            "valence": affect.valence(state),
        },
        "life": life_module.summary(state, now_ms),
        "updated_at": state["last_settled_at"],
        "gaps": sorted(
            (dict(gap, tension=_gap_tension(gap, now_ms)) for gap in visible),
            key=lambda gap: gap["tension"], reverse=True,
        ),
    }


def record_request(now_ms: float) -> None:
    """Charge the observed provider request; retry attempts are independently charged."""
    def mutate(state):
        if state["emergency_stop"]:
            return None
        state = settle_before_transition(state, now_ms)
        POOL.apply("request", state)
        state["last_interaction_at"] = now_ms
        state["last_settled_at"] = now_ms
        return state
    from .storage import update_state
    update_state(now_ms, mutate)


def outreach_unanswered(now_ms: float, chat: str) -> int:
    """How many proactive messages in ``chat`` are still waiting for a reply."""
    chat = str(chat or "")
    if not chat:
        return 0
    try:
        from .storage import read_state
        entry = (read_state(now_ms).get("outreach") or {}).get(chat)
    except Exception:  # noqa: BLE001 - a missing state read must not block a send
        return 0
    return int(float_or(entry.get("unanswered"), 0.0)) if isinstance(entry, dict) else 0


def record_outreach(now_ms: float, chat: str) -> bool:
    """Count one proactive message to ``chat``.

    Returns ``False`` once the chat has had :data:`OUTREACH_REST_AT` unanswered
    nudges in a row: the caller must not send again until the person speaks.
    Every nudge before that presses serotonin down by its stage
    (``outreach_unanswered_1``, ``_2``, ``_3``): the first costs a little, the
    third costs a lot, because the earlier ones are still hanging unanswered.
    """
    chat = str(chat or "").strip()
    outcome = {"ok": False}

    def mutate(state):
        if state["emergency_stop"] or not chat:
            return None
        state = settle_before_transition(state, now_ms)
        book = state.setdefault("outreach", {})
        entry = book.get(chat) if isinstance(book.get(chat), dict) else {}
        unanswered = int(float_or(entry.get("unanswered"), 0.0))
        if unanswered >= OUTREACH_REST_AT:
            return state
        stage = min(unanswered + 1, OUTREACH_REST_AT)
        POOL.apply(f"outreach_unanswered_{stage}", state)
        book[chat] = {"unanswered": unanswered + 1, "last_at": float(now_ms)}
        state["last_settled_at"] = now_ms
        outcome["ok"] = True
        return state

    from .storage import update_state
    update_state(now_ms, mutate)
    return outcome["ok"]


def record_reply(now_ms: float, chat: str) -> bool:
    """The person spoke in ``chat``: end the wait and give a little serotonin back."""
    chat = str(chat or "").strip()
    outcome = {"heard": False}

    def mutate(state):
        if not chat:
            return None
        state = settle_before_transition(state, now_ms)
        book = state.get("outreach")
        entry = book.pop(chat, None) if isinstance(book, dict) else None
        if isinstance(entry, dict) and int(float_or(entry.get("unanswered"), 0.0)) > 0:
            POOL.apply("reconnected", state)
            outcome["heard"] = True
        if isinstance(book, dict) and not book:
            state.pop("outreach", None)
        state["last_settled_at"] = now_ms
        return state

    from .storage import update_state
    update_state(now_ms, mutate)
    return outcome["heard"]


def record_turn(now_ms: float, *, success: bool, user_message: str = "") -> None:
    """Reward an observed completed turn and fold in what the user just did."""
    if not success:
        return
    category = events.classify(user_message)
    def mutate(state):
        if state["emergency_stop"]:
            return None
        state = settle_before_transition(state, now_ms)
        affect.record(state, now_ms)
        POOL.apply("turn_complete", state)
        _apply_event_impulse(state, category, now_ms)
        state["last_interaction_at"] = now_ms
        state["last_settled_at"] = now_ms
        return state
    from .storage import update_state
    update_state(now_ms, mutate)


def _apply_event_impulse(state: dict[str, Any], category: str | None, now_ms: float) -> None:
    """Apply at most one capped, cooldown-guarded chemistry nudge for this turn.

    Chemistry is profile-level, so the cooldown is per category per profile rather
    than per chat: two groups praising in the same 10 minutes still count once.
    """
    if not category:
        return
    book = state.setdefault("affect_events", {"day": "", "total": 0.0, "cooldowns": {}})
    day = time.strftime("%Y-%m-%d", time.localtime(now_ms / 1000))
    if book.get("day") != day:
        book["day"] = day
        book["total"] = 0.0
        book["cooldowns"] = {}
    cooldowns = book.setdefault("cooldowns", {})
    last = float(cooldowns.get(category, 0.0))
    if last and now_ms - last < events.COOLDOWN_MS:
        return
    remaining = max(0.0, events.DAILY_CAP - float(book.get("total", 0.0)))
    deltas = events.scaled_deltas(category, remaining)
    if not deltas:
        return
    chemicals = state["chemistry"]
    sensitivity = float_or((state.get("temperament") or {}).get("sensitivity"), 1.0)
    for key, value in deltas.items():
        current = float(chemicals.get(key, 50.0))
        chemicals[key] = POOL.clamp_chemical(key, current + value * sensitivity)
    cooldowns[category] = float(now_ms)
    book["total"] = float(book.get("total", 0.0)) + sum(abs(value) for value in deltas.values())


def record_api_error(now_ms: float, *, error_type: str, retryable: bool | None) -> None:
    """Apply the fixed stress response only to transient errors observed by the host."""
    if retryable is False:
        return
    error = error_type.lower()
    if not any(tag in error for tag in ("timeout", "connection", "network", "http", "connecterror", "requesterror", "networkerror")):
        return
    def mutate(state):
        if state["emergency_stop"]:
            return None
        state = settle_before_transition(state, now_ms)
        POOL.apply("provider_error", state)
        _open_or_reactivate_gap(state, f"Observed transient provider error: {error[:120]}", "api_request_error", now_ms)
        state["last_settled_at"] = now_ms
        return state
    from .storage import update_state
    update_state(now_ms, mutate)


def observe_tool_result(now_ms: float, *, tool_name: str, args: dict[str, Any], result: str,
                        chat: str = "") -> None:
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
                return None
            state = settle_before_transition(state, now_ms)
            gap_pool.close_gap(state, by="observed_search_success", now_ms=now_ms,
                               match_text=successful_query, chat=chat)
            succeeded = state.get("tools_succeeded")
            if not isinstance(succeeded, list):
                succeeded = state["tools_succeeded"] = []
            if tool_name not in succeeded:
                # M8.3 epiphany: a capability that has never worked before now works.
                # Whatever open gap the result actually answers is closed with provenance.
                succeeded.append(tool_name)
                answered = gap_pool.answered_gaps(state.get("gaps") or [], successful_query + "\n" + rendered[:2000])
                closed_ids = [gap_pool.close_gap(state, by=f"epiphany:{tool_name}", now_ms=now_ms,
                                                 gap_id=gap["id"], chat=chat) for gap in answered]
                if any(closed_ids):
                    gap_pool.note_epiphany(state, now_ms)
            state["last_interaction_at"] = now_ms
            state["last_settled_at"] = now_ms
            return state
        from .storage import update_state
        update_state(now_ms, close_observed)
        return
    summary = f"Observed {tool_name} {'failure' if failed else 'empty result'}: {query}"

    def mutate(state):
        if state["emergency_stop"]:
            return None
        state = settle_before_transition(state, now_ms)
        _open_or_reactivate_gap(state, summary, f"tool:{tool_name}", now_ms, query=query, chat=chat)
        state["last_interaction_at"] = now_ms
        state["last_settled_at"] = now_ms
        return state

    from .storage import update_state
    update_state(now_ms, mutate)


def observe_assistant_unknown(now_ms: float, summary: str, chat: str = "") -> None:
    """Record an assistant uncertainty through the guarded internal gap transition."""
    from .storage import update_state

    def mutate(state):
        if state["emergency_stop"]:
            return None
        state = settle_before_transition(state, now_ms)
        _open_or_reactivate_gap(state, summary, "assistant_unknown_claim", now_ms, chat=chat)
        state["last_interaction_at"] = now_ms
        state["last_settled_at"] = now_ms
        return state

    update_state(now_ms, mutate)


def _open_or_reactivate_gap(state: dict[str, Any], summary: str, source: str, now_ms: float,
                            *, query: str | None = None, chat: str = "") -> None:
    gap_pool.propose_gap(state, summary, source, now_ms, query=query, chat=chat)


def _normalize_gap_text(text: str) -> str:
    return gap_pool.normalize_gap_text(text)


def _gap_similarity(left: str, right: str) -> float:
    return gap_pool.gap_similarity(left, right)


def _gap_tension(gap: dict[str, Any], now_ms: float) -> float:
    return gap_pool.gap_tension(gap, now_ms)


def settle_before_transition(state: dict[str, Any], now_ms: float) -> dict[str, Any]:
    elapsed = max(0.0, now_ms - state["last_settled_at"])
    if elapsed:
        high_gap = any(g.get("status") == "open" and g.get("tension", 0) >= 70 for g in state["gaps"])
        state["chemistry"] = decay_chemistry(
            state["chemistry"], elapsed, state["temperament"],
            energy=state["energy"], high_tension_gap=high_gap,
        )
        _drain_waiting_silence(state, elapsed)
        state["energy"] = settle_rest(state["energy"], 100.0, elapsed, now_ms, False)["energy"]
    return state


def wake_section(state: dict[str, Any], now_ms: float, chat_id: str = "") -> str:
    """The opt-in ``digital-life:wake`` block; empty when no wake is active."""
    if not chat_id:
        return ""
    try:
        from . import wake as wake_module
        return wake_module.section_text(state, now_ms, chat_id=chat_id)
    except Exception:  # noqa: BLE001 - wake context must never break a request
        return ""


def body_line() -> str:
    """One host-vitals line for the affect section; empty when telemetry is unavailable."""
    try:
        from .host_metrics import body_line as render
        return render()
    except Exception:  # noqa: BLE001 - the body line is optional context
        return ""


def affect_note(now_ms: float, *, include_time: bool = False) -> str:
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
    mood = affect.render(state, now_ms)
    stamp = time.localtime(now_ms / 1000.0)
    clock = time.strftime("%Y-%m-%d %H:%M", stamp) + "（周" + "一二三四五六日"[stamp.tm_wday] + "）"
    when = f"现在：{clock}。（不主动提时间，对方问起才用）\n" if include_time else ""
    note = f"{when}{mood}\n（以上是你此刻的身体状态；按它调整语气，但不覆盖安全与事实规则。\n这些数字只给你自己判断语气用，不要念出来、也不要拿它当谈资，也不要用「参数」「调试」「重置」这类工程说法；用感觉词说。）\n{directive}"
    body = body_line()
    return f"{note}\n{body}" if body else note


def reorder_prompt_memories(prompt: str, now_ms: float) -> tuple[str, int]:
    state = status_snapshot(now_ms)
    return reorder_formatted_memory_blocks(
        prompt,
        state["chemistry"]["cortisol"],
        state["chemistry"]["dopamine"],
    )


def append_open_loops(system_text: str, gaps: list[dict[str, Any]], now_ms: float,
                      scope: str | None = None) -> str:
    """Append a bounded summary of the highest-tension unresolved gaps."""
    # A-class PRD M8.6: expose unresolved loops; formatting/budget are the task's host adaptation.
    open_gaps = sorted(
        (gap for gap in gaps if isinstance(gap, dict) and gap.get("status") == "open"
         and _gap_visible(gap, scope)),
        key=lambda gap: float_or(gap.get("tension"), 0.0), reverse=True,
    )[:3]
    if not open_gaps:
        return system_text

    def age_label(gap: dict[str, Any]) -> str:
        age_ms = max(0.0, now_ms - float_or(gap.get("created_at"), now_ms))
        minutes = int(age_ms // 60_000)
        if minutes >= 24 * 60:
            return f"{minutes // (24 * 60)}d"
        if minutes >= 60:
            return f"{minutes // 60}h"
        return f"{minutes}m"

    entries = [
        f"tension={float_or(gap.get('tension'), 0.0):.0f}: "
        f"{str(gap.get('summary', '')).replace(chr(10), ' ').replace(chr(13), ' ')}"
        f"（{age_label(gap)}）"
        for gap in open_gaps
    ]
    while entries:
        suffix = "[open loops] 你心里还挂着的（不是待办，别每轮都提）：" + "；".join(entries)
        separator = "\n\n" if system_text else ""
        if len(separator + suffix) <= 400:
            return f"{system_text}{separator}{suffix}"
        if len(entries) == 1:
            return system_text + separator + suffix[:400 - len(separator)]
        entries.pop()
    return system_text


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
        if gap_pool.close_gap(state, by="operator", now_ms=now_ms, gap_id=gap_id):
            resolved = True
        state["last_settled_at"] = now_ms
        return state
    from .storage import update_state
    update_state(now_ms, mutate)
    return resolved


def close_gap_in_chat(now_ms: float, gap_id: str, chat: str) -> bool:
    """The assistant retiring one of its own loops, limited to that conversation."""
    closed = False

    def mutate(state):
        nonlocal closed
        if state["emergency_stop"]:
            return None
        state = settle_before_transition(state, now_ms)
        if gap_pool.close_gap(state, by="self", now_ms=now_ms, gap_id=gap_id,
                              chat=chat or ""):
            closed = True
        state["last_settled_at"] = now_ms
        return state

    from .storage import update_state
    update_state(now_ms, mutate)
    return closed
