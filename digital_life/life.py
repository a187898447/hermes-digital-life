"""The life layer: a small daily agenda the runtime works through on its own clock.

Everything here is grounded in material the process can actually read — a page
from the library, an open loop, what was read a moment ago. A finished activity
leaves a trace; a missing source drops the activity instead of inventing one.
The only model call is the optional note pass, budgeted per day.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable, Iterable

from . import library
from .chemistry import float_or

SCHEMA = 1
MAX_AGENDA = 8
MAX_TRACES = 20
LIFE_PULSE_CAP = 20.0
QUIET_HOURS = (0, 6)
KINDS = ("read", "think", "note", "rest")
AGENDA_SIZE = {"疲惫": 2, "低落": 2, "烦躁": 2, "来劲": 5, "平常": 4}
MINUTES = {"read": 25, "think": 15, "note": 10, "rest": 20}
PULSES = {
    "read": {"dopamine": 4.0, "serotonin": 1.0},
    "think": {"dopamine": 2.0},
    "note": {"dopamine": 3.0},
    "rest": {"cortisol": -4.0, "serotonin": 2.0},
    "dropped": {"cortisol": 3.0},
}

_WRITER: Callable[[str], Any] | None = None


def set_writer(writer: Callable[[str], Any] | None) -> None:
    """Install the memory writer used for traces (the plugin owns the store)."""
    global _WRITER
    _WRITER = writer


def new_life_state(now_ms: float = 0.0) -> dict[str, Any]:
    return {"schema": SCHEMA, "day": "", "agenda": [], "current": None, "traces": [],
            "pulse": {"day": "", "total": 0.0}, "last_tick_at": 0.0}


def normalize_life(raw: Any) -> dict[str, Any]:
    state = new_life_state()
    if not isinstance(raw, dict):
        return state
    state["day"] = str(raw.get("day") or "")[:10]
    agenda = raw.get("agenda")
    if isinstance(agenda, list):
        state["agenda"] = [item for item in agenda[-MAX_AGENDA:] if _valid_item(item)]
    current = raw.get("current")
    state["current"] = str(current) if isinstance(current, str) and current else None
    if state["current"] and not any(item["id"] == state["current"] for item in state["agenda"]):
        state["current"] = None
    traces = raw.get("traces")
    if isinstance(traces, list):
        state["traces"] = [item for item in traces[-MAX_TRACES:] if _valid_trace(item)]
    pulse = raw.get("pulse") if isinstance(raw.get("pulse"), dict) else {}
    state["pulse"] = {"day": str(pulse.get("day") or "")[:10],
                      "total": max(0.0, float_or(pulse.get("total"), 0.0))}
    state["last_tick_at"] = max(0.0, float_or(raw.get("last_tick_at"), 0.0))
    return state


def _valid_item(item: Any) -> bool:
    return (isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("kind") in KINDS
            and isinstance(item.get("title"), str) and item.get("status") in ("pending", "running", "done", "dropped"))


def _valid_trace(item: Any) -> bool:
    return isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip() != ""


def local_day(now_ms: float) -> str:
    return datetime.fromtimestamp(now_ms / 1000.0).strftime("%Y-%m-%d")


def phase(now_ms: float, quiet_hours: tuple[int, int] = QUIET_HOURS) -> str:
    hour = datetime.fromtimestamp(now_ms / 1000.0).hour
    start, end = quiet_hours
    if start <= hour < end:
        return "sleep"
    if hour < 9:
        return "morning"
    if hour < 18:
        return "day"
    if hour < 23:
        return "evening"
    return "late"


def agenda_size(mood_label: str) -> int:
    return AGENDA_SIZE.get(str(mood_label or ""), AGENDA_SIZE["平常"])


def build_agenda(now_ms: float, *, mood_label: str = "平常", books: Iterable[dict] | None = None,
                 gaps: Iterable[dict] | None = None) -> list[dict[str, Any]]:
    """Deterministic agenda for one local day: no model call, same input same plan."""
    size = max(1, min(MAX_AGENDA, agenda_size(mood_label)))
    shelf = [book for book in (books or []) if isinstance(book, dict) and book.get("file")]
    open_gaps = [gap for gap in (gaps or []) if isinstance(gap, dict) and gap.get("status") == "open"]
    rest = 0 if phase(now_ms) == "sleep" else 1
    plan: list[tuple[str, str, Any]] = []
    if shelf:
        plan.append(("read", f"读《{str(shelf[0].get('title') or '那本书')[:20]}》", shelf[0]))
    if open_gaps:
        gap = open_gaps[0]
        plan.append(("think", f"想想「{str(gap.get('summary') or '')[:18]}」", gap))
    plan.append(("note", "记一笔", None))
    if rest:
        plan.append(("rest", "歇一会儿", None))
    while len(plan) < size:
        kind = "rest" if rest and len(plan) % 2 == 0 else "note"
        plan.append((kind, "歇一会儿" if kind == "rest" else "记一笔", None))
    stamp = int(now_ms)
    return [{"id": f"act-{stamp}-{index}", "kind": kind, "title": title, "minutes": MINUTES[kind],
             "status": "pending", "started_at": 0.0, "done_at": 0.0, "source": _source(kind, extra)}
            for index, (kind, title, extra) in enumerate(plan[:size])]


def _source(kind: str, extra: Any) -> dict[str, Any]:
    if kind == "read" and isinstance(extra, dict):
        return {"type": "library", "file": str(extra.get("file") or ""), "part": int(extra.get("position") or 0)}
    if kind == "think" and isinstance(extra, dict):
        return {"type": "gap", "id": str(extra.get("id") or "")}
    return {}


def current(state: dict[str, Any]) -> dict[str, Any] | None:
    life = state.get("life") or {}
    ident = life.get("current")
    for item in life.get("agenda") or []:
        if item.get("id") == ident:
            return item
    return None


def step(state: dict[str, Any], now_ms: float) -> dict[str, Any]:
    """Advance one agenda item. Mutates ``state["life"]``; returns what happened."""
    life = state.setdefault("life", new_life_state())
    today = local_day(now_ms)
    started: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    pulses: dict[str, float] = {}

    if life.get("day") != today:
        life["day"] = today
        life["agenda"] = build_agenda(now_ms, mood_label=_mood_label(state),
                                      books=_books(), gaps=state.get("gaps"))
        life["current"] = None

    running = current(state)
    if running is not None and _due(running, now_ms):
        trace = _finish(state, running, now_ms)
        running["done_at"] = now_ms
        life["current"] = None
        if running["status"] == "dropped":
            dropped.append(running)
            for name, amount in PULSES["dropped"].items():
                pulses[name] = pulses.get(name, 0.0) + amount
        else:
            finished.append(running)
            if trace:
                traces.append(trace)
            for name, amount in PULSES.get(running["kind"], {}).items():
                pulses[name] = pulses.get(name, 0.0) + amount

    if life.get("current") is None and phase(now_ms) != "sleep":
        nxt = next((item for item in life["agenda"] if item["status"] == "pending"), None)
        if nxt is not None:
            nxt["status"] = "running"
            nxt["started_at"] = now_ms
            life["current"] = nxt["id"]
            started.append(nxt)

    if traces:
        life["traces"] = (life.get("traces") or [])[-(MAX_TRACES - len(traces)):] + traces
    return {"started": started, "finished": finished, "dropped": dropped,
            "traces": traces, "pulses": pulses}


def _due(item: dict[str, Any], now_ms: float) -> bool:
    started = float_or(item.get("started_at"), 0.0)
    return started > 0 and now_ms - started >= MINUTES.get(item["kind"], 15) * 60_000


def _mood_label(state: dict[str, Any]) -> str:
    from . import affect
    return affect.label(state)


def _books() -> list[dict]:
    try:
        return library.books()
    except (OSError, ValueError):
        return []


def _finish(state: dict[str, Any], item: dict[str, Any], now_ms: float) -> dict[str, Any] | None:
    """Settle one running item; sets its status and returns the trace when there is one."""
    kind = item["kind"]
    source = item.get("source") or {}
    if kind == "read":
        trace = _finish_read(item, source, now_ms)
    elif kind == "think":
        trace = _finish_think(state, item, source, now_ms)
    elif kind == "note":
        trace = _finish_note(state, now_ms)
    else:
        trace = None  # rest leaves no trace
    item["status"] = "done" if (trace is not None or kind == "rest") else "dropped"
    return trace


def _finish_read(item: dict[str, Any], source: dict[str, Any], now_ms: float) -> dict[str, Any] | None:
    name = source.get("file") or ""
    meta = next((book for book in _books() if book.get("file") == name), None)
    if meta is None:
        return None
    try:
        result = library.read(dict(meta), part=int(source.get("part") or 0), max_chars=1200)
    except (OSError, ValueError, KeyError):
        return None
    text = str(result.get("text") or "")
    excerpt = " ".join(text.split())[:90]
    progress = result.get("progress") or ""
    headline = f"读完《{item['title'][1:-1]}》" if result.get("finished") else f"读了《{item['title'][1:-1]}》{progress}"
    trace_text = f"{headline}。{excerpt}" if excerpt else headline
    return {"at": now_ms, "kind": "read", "title": item["title"], "text": trace_text,
            "source": {"type": "library", "file": name, "part": result.get("part")},
            "share": bool(result.get("finished"))}


def _finish_think(state: dict[str, Any], item: dict[str, Any], source: dict[str, Any],
                  now_ms: float) -> dict[str, Any] | None:
    gap_id = source.get("id") or ""
    gap = next((g for g in state.get("gaps") or [] if g.get("id") == gap_id), None)
    if gap is None:
        return None
    summary = str(gap.get("summary") or "").strip()
    if not summary:
        return None
    return {"at": now_ms, "kind": "think", "title": item["title"],
            "text": f"又琢磨了一下「{summary[:60]}」。", "source": {"type": "gap", "id": gap_id},
            "share": False}


def _finish_note(state: dict[str, Any], now_ms: float) -> dict[str, Any] | None:
    recent = [trace for trace in (state.get("life") or {}).get("traces") or [] if trace.get("kind") != "note"][-2:]
    if not recent:
        return None
    parts = [str(trace.get("title") or "") for trace in recent if trace.get("title")]
    text = "今天" + "，".join(parts) + "。" if parts else ""
    if not text:
        return None
    return {"at": now_ms, "kind": "note", "title": "记一笔", "text": text[:120],
            "source": {"type": "traces"}, "share": False}


def apply_pulses(state: dict[str, Any], pulses: dict[str, float], now_ms: float) -> dict[str, float]:
    """Scale one batch into the daily pulse budget, then deliver it in place."""
    from .chemical_pool import ChemicalPool

    if not pulses:
        return {}
    life = state.setdefault("life", new_life_state())
    today = local_day(now_ms)
    pulse = life.get("pulse") if isinstance(life.get("pulse"), dict) else {"day": "", "total": 0.0}
    if pulse.get("day") != today:
        pulse = {"day": today, "total": 0.0}
    magnitude = sum(abs(value) for value in pulses.values())
    remaining = max(0.0, LIFE_PULSE_CAP - float_or(pulse.get("total"), 0.0))
    if magnitude <= 0 or remaining <= 0:
        life["pulse"] = pulse
        return {}
    factor = min(1.0, remaining / magnitude)
    pool = ChemicalPool()
    applied = {}
    for name, amount in pulses.items():
        value = pool.inflict(state, name, amount * factor)
        if value is not None:
            applied[name] = amount * factor
    pulse["total"] = float_or(pulse.get("total"), 0.0) + magnitude * factor
    life["pulse"] = pulse
    return applied


def summary(state: dict[str, Any], now_ms: float) -> dict[str, Any]:
    life = state.get("life") or new_life_state()
    item = current(state)
    agenda = life.get("agenda") or []
    done = sum(1 for entry in agenda if entry.get("status") == "done")
    remaining = sum(int(entry.get("minutes") or 0) for entry in agenda if entry.get("status") == "pending")
    recent = [{"title": trace.get("title"), "at": trace.get("at"), "kind": trace.get("kind")}
              for trace in (life.get("traces") or [])[-3:]]
    return {"phase": phase(now_ms), "day": life.get("day") or local_day(now_ms),
            "current": {"title": item.get("title"), "kind": item.get("kind"),
                        "started_at": item.get("started_at")} if item else None,
            "done_today": done, "planned_today": len(agenda), "remaining_minutes": remaining,
            "recent": recent, "last_tick_at": float_or(life.get("last_tick_at"), 0.0)}


def remember(trace: dict[str, Any]) -> None:
    """Hand one trace to the memory store, if the plugin installed a writer."""
    if _WRITER is None:
        return
    text = str(trace.get("text") or "").strip()
    if not text:
        return
    try:
        _WRITER(text)
    except Exception:  # noqa: BLE001 - a trace must never break the tick
        import logging
        logging.getLogger(__name__).debug("life trace was not written to memory", exc_info=True)
