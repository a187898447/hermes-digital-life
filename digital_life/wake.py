"""Opt-in self-wake queue (PRD M11.4-M11.6, M11.8).

A wake is a nudge the plugin gives itself when an open loop has gone
unattended: the bubble the user sees carries only ``...``, while the loop
summary and the behaviour commitment travel in the ``digital-life:wake``
block of the next system prompt.

Four locks, ported from DLC, keep this from becoming noise:

1. the same loop re-woken inside one second is dropped without any record;
2. before delivery the state is re-read from disk and the queued id must
   still exist there;
3. closing a loop deletes its queued wake (``gaps.retire_wake``);
4. a wake attempted while an agent turn is running is dropped, never queued.

Everything here is pure state manipulation plus one injected bubble; the
host injector itself lives in ``__init__.py`` so this module stays testable.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from . import gaps as gap_pool
from .domain import float_or

logger = logging.getLogger(__name__)

WAKE_TEXT = "..."
WAKE_DEDUPE_MS = 1_000.0
WAKE_COOLDOWN_MS = 30 * 60_000.0
# A turn that never reports completion must not block waking forever.
WAKE_RUNNING_TTL_MS = 15 * 60_000.0
WAKE_MIN_TENSION = 80.0
WAKE_ECHO_MINUTES = 10.0
WAKE_PENALTY = 10.0
WAKE_SECTION_LIMIT = 400
# Wake state is per chat: one group's pending nudge must not silence another's.
WAKE_SCHEMA = 2
MAX_CHATS = 32

_RUNNING_AT = 0.0


def set_agent_running(running: bool) -> None:
    """Hooks mark a live turn so a wake cannot be queued mid-turn (lock 4)."""
    global _RUNNING_AT
    _RUNNING_AT = time.time() * 1000 if running else 0.0


def agent_running() -> bool:
    """True only while a turn is plausibly live; a lost post-hook expires."""
    return _RUNNING_AT > 0 and time.time() * 1000 - _RUNNING_AT < WAKE_RUNNING_TTL_MS


def new_wake_state() -> dict[str, Any]:
    return {"schema": WAKE_SCHEMA, "chats": {}}


def new_chat_wake() -> dict[str, Any]:
    return {"queue": [], "last_wake_at": 0.0, "last_gap_id": "", "active": None}


def _clean_slot(raw: Any) -> dict[str, Any]:
    slot = new_chat_wake()
    if not isinstance(raw, dict):
        return slot
    queue = raw.get("queue")
    if isinstance(queue, list):
        for item in queue[-8:]:
            if not isinstance(item, dict):
                continue
            wake_id, gap_id = item.get("id"), item.get("gap_id")
            if not isinstance(wake_id, str) or not wake_id or not isinstance(gap_id, str) or not gap_id:
                continue
            slot["queue"].append({
                "id": wake_id[:80], "gap_id": gap_id[:80],
                "created_at": max(0.0, float_or(item.get("created_at"), 0.0)),
            })
    slot["last_wake_at"] = max(0.0, float_or(raw.get("last_wake_at"), 0.0))
    last_gap = raw.get("last_gap_id")
    slot["last_gap_id"] = last_gap[:80] if isinstance(last_gap, str) else ""
    active = raw.get("active")
    if isinstance(active, dict) and isinstance(active.get("gap_id"), str) and active.get("gap_id"):
        slot["active"] = {
            "wake_id": str(active.get("wake_id") or "")[:80],
            "gap_id": active["gap_id"][:80],
            "delivered_at": max(0.0, float_or(active.get("delivered_at"), 0.0)),
            "manual": active.get("manual") is True,
        }
    return slot


def normalize_wake(raw: Any) -> dict[str, Any]:
    """Never raises; keeps at most MAX_CHATS chats, each cleaned like before.

    The pre-schema-2 layout kept one global slot, which has no chat owner; it is
    dropped rather than guessed at, because a wake aimed at the wrong chat is noise.
    """
    if not isinstance(raw, dict):
        return new_wake_state()
    chats = raw.get("chats")
    if not isinstance(chats, dict):
        if any(key in raw for key in ("queue", "active", "last_wake_at")):
            logger.info("hermes-lover: dropping pre-schema-2 wake state that has no chat owner")
        return new_wake_state()
    state = new_wake_state()
    for chat_id, slot in chats.items():
        key = str(chat_id)[:80]
        if key and len(state["chats"]) < MAX_CHATS:
            state["chats"][key] = _clean_slot(slot)
    return state


def _wake_dict(state: dict[str, Any]) -> dict[str, Any]:
    wake = state.get("wake")
    if isinstance(wake, dict) and isinstance(wake.get("chats"), dict):
        return wake
    wake = new_wake_state()
    state["wake"] = wake
    return wake


def _slot(state: dict[str, Any], chat_id: str, *, create: bool = False) -> dict[str, Any] | None:
    chat_id = str(chat_id or "")
    if not chat_id:
        raise ValueError("a wake must belong to exactly one chat")
    wake = _wake_dict(state)
    slot = wake["chats"].get(chat_id)
    if slot is None and create:
        if len(wake["chats"]) >= MAX_CHATS:
            stalest = min(wake["chats"], key=lambda key: float_or(wake["chats"][key].get("last_wake_at"), 0.0))
            wake["chats"].pop(stalest, None)
        slot = wake["chats"][chat_id] = new_chat_wake()
    return slot if isinstance(slot, dict) else None


def _wake_id(now_ms: float, chat_id: str, queue_len: int) -> str:
    suffix = re.sub(r"[^0-9A-Za-z]", "", str(chat_id))[-8:] or "chat"
    return f"wake-{int(now_ms)}-{queue_len}-{suffix}"


def _open_gap(gaps: list[dict[str, Any]], gap_id: str) -> dict[str, Any] | None:
    for gap in gaps:
        if isinstance(gap, dict) and gap.get("id") == gap_id and gap.get("status") == "open":
            return gap
    return None


def plan_wake(state: dict[str, Any], now_ms: float, *, chat_id: str,
              min_tension: float = WAKE_MIN_TENSION,
              cooldown_ms: float = WAKE_COOLDOWN_MS) -> dict[str, Any] | None:
    """Queue the most pressing loop, or return ``None`` when any lock holds.

    The 1-second same-loop repeat is dropped before anything is written, so
    a flapping caller leaves no trace (lock 1).
    """
    if agent_running():
        return None
    wake = _slot(state, chat_id, create=True)
    if wake is None:
        return None
    if wake.get("active") or wake.get("queue"):
        return None
    since_last = now_ms - float_or(wake.get("last_wake_at"), 0.0)
    if since_last < WAKE_DEDUPE_MS:
        return None
    if since_last < max(0.0, float_or(cooldown_ms, WAKE_COOLDOWN_MS)):
        return None
    if not isinstance(state.get("gaps"), list):
        return None
    if gap_pool.suppressed_by_echo(state, now_ms, minutes=WAKE_ECHO_MINUTES):
        return None
    best = None
    for gap in gap_pool.open_gaps(state["gaps"]):
        if gap_pool.sibling_of_recent_close(state["gaps"], gap, now_ms):
            continue
        tension = gap_pool.gap_tension(gap, now_ms)
        if tension < min_tension:
            continue
        if best is None or tension > gap_pool.gap_tension(best, now_ms):
            best = gap
    if best is None:
        return None
    item = {"id": _wake_id(now_ms, chat_id, len(wake["queue"])), "gap_id": str(best["id"]),
            "created_at": float(now_ms)}
    wake["queue"].append(item)
    return item


def verify_queued(state: dict[str, Any], wake_id: str) -> bool:
    """Lock 2: the queued id must still be on disk and its loop still open."""
    wake = state.get("wake") if isinstance(state, dict) else None
    if not isinstance(wake, dict) or not isinstance(wake.get("chats"), dict):
        return False
    for slot in wake["chats"].values():
        for item in (slot or {}).get("queue") or ():
            if item.get("id") == wake_id:
                return _open_gap(state.get("gaps") or [], str(item.get("gap_id"))) is not None
    return False


def cancel(state: dict[str, Any], wake_id: str) -> bool:
    wake = state.get("wake")
    if not isinstance(wake, dict) or not isinstance(wake.get("chats"), dict):
        return False
    dropped = False
    for slot in wake["chats"].values():
        if not isinstance(slot, dict):
            continue
        queue = slot.get("queue") or ()
        kept = [item for item in queue if item.get("id") != wake_id]
        if len(kept) != len(queue):
            slot["queue"] = kept
            dropped = True
    return dropped


def mark_delivered(state: dict[str, Any], wake_id: str, now_ms: float) -> bool:
    """Move a queued wake to ``active`` after the host accepted the bubble."""
    wake = _wake_dict(state)
    for slot in wake["chats"].values():
        if not isinstance(slot, dict):
            continue
        for item in list(slot.get("queue") or ()):
            if item.get("id") != wake_id:
                continue
            slot["queue"] = [other for other in slot["queue"] if other.get("id") != wake_id]
            slot["active"] = {"wake_id": wake_id, "gap_id": str(item.get("gap_id")),
                              "delivered_at": float(now_ms), "manual": False}
            slot["last_wake_at"] = float(now_ms)
            slot["last_gap_id"] = str(item.get("gap_id"))
            return True
    return False


def force_active(state: dict[str, Any], now_ms: float, *, chat_id: str,
                 gap_id: str | None = None) -> dict[str, Any] | None:
    """Operator/CLI trigger: mark a loop as woken without a bubble."""
    wake = _slot(state, chat_id, create=True)
    if wake is None:
        return None
    gaps = state.get("gaps") or []
    chosen = _open_gap(gaps, gap_id) if gap_id else None
    if chosen is None:
        open_items = gap_pool.open_gaps(gaps)
        if not open_items:
            return None
        chosen = max(open_items, key=lambda gap: gap_pool.gap_tension(gap, now_ms))
    wake["active"] = {"wake_id": f"wake-manual-{int(now_ms)}", "gap_id": str(chosen["id"]),
                      "delivered_at": float(now_ms), "manual": True}
    wake["last_wake_at"] = float(now_ms)
    wake["last_gap_id"] = str(chosen["id"])
    return wake["active"]


def reconcile(state: dict[str, Any], now_ms: float, *, chat_id: str) -> str:
    """Behaviour commitment: resolved loop clears; a still-open loop presses harder."""
    wake = _slot(state, chat_id)
    if wake is None or not wake.get("active"):
        return ""
    active = wake["active"]
    gap = next((item for item in state.get("gaps") or ()
                if isinstance(item, dict) and item.get("id") == active.get("gap_id")), None)
    if gap is None:
        wake["active"] = None
        return "closed"
    if gap.get("status") == "resolved" or gap_pool.gap_tension(gap, now_ms) <= 0.0:
        wake["active"] = None
        return "done"
    gap["relief"] = float_or(gap.get("relief"), 0.0) - WAKE_PENALTY
    wake["active"] = None
    return "unmet"


def section_text(state: dict[str, Any], now_ms: float, *, chat_id: str) -> str:
    """The ``digital-life:wake`` block; empty when nothing is active."""
    wake = _slot(state, chat_id)
    if wake is None or not wake.get("active"):
        return ""
    active = wake["active"]
    gap = next((item for item in state.get("gaps") or ()
                if isinstance(item, dict) and item.get("id") == active.get("gap_id")), None)
    if gap is None:
        return ""
    tension = int(round(gap_pool.gap_tension(gap, now_ms)))
    summary = str(gap.get("summary") or "").strip()
    age_minutes = max(0, int((now_ms - float_or(gap.get("created_at"), now_ms)) / 60_000.0))
    text = (
        "[digital-life:wake]\n"
        f"You woke yourself about an open loop: {summary} (tension {tension}, open {age_minutes}m).\n"
        "Before using any other tool, state a one-line plan for closing it, then act. "
        "If you answer it, its tension clears; if you leave it, it presses harder."
    )
    return text[:WAKE_SECTION_LIMIT]


def summary(state: dict[str, Any], now_ms: float) -> dict[str, Any]:
    wake = state.get("wake") if isinstance(state, dict) else {}
    wake = wake if isinstance(wake, dict) else {}
    chats = wake.get("chats") if isinstance(wake.get("chats"), dict) else {}
    active = [(chat_id, slot.get("active")) for chat_id, slot in chats.items()
              if isinstance(slot, dict) and isinstance(slot.get("active"), dict)]
    return {
        "chats": len(chats),
        "queued": sum(len(slot.get("queue") or ()) for slot in chats.values() if isinstance(slot, dict)),
        "active_gap": active[0][1].get("gap_id") if len(active) == 1 else None,
        "active_chats": [chat_id for chat_id, _ in active],
        "last_wake_at": max((float_or(slot.get("last_wake_at"), 0.0) for slot in chats.values()
                             if isinstance(slot, dict)), default=0.0),
    }
