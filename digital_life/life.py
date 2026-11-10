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
KINDS = ("read", "think", "note", "practice", "rest")
AGENDA_SIZE = {"疲惫": 2, "低落": 2, "烦躁": 2, "来劲": 5, "平常": 4}
MINUTES = {"read": 25, "think": 15, "note": 10, "practice": 15,
           "write": 20, "nap": 15, "rest": 20}
# Reading and drawing share one window: at most one of them per five hours, chosen
# by which has waited longer. Notes and thoughts are lighter but still not back to
# back, so the day is not a treadmill of traces.
LONG_KINDS = ("read", "practice")
SHORT_KINDS = ("think", "note", "write", "nap")
LONG_GAP_MINUTES = 300
SHORT_GAP_MINUTES = 120
PULSES = {
    "read": {"dopamine": 4.0, "serotonin": 1.0},
    "think": {"dopamine": 2.0},
    "note": {"dopamine": 3.0},
    "practice": {"dopamine": 3.0, "serotonin": 1.0},
    "write": {"dopamine": 3.0, "serotonin": 2.0},
    "nap": {"cortisol": -5.0, "serotonin": 2.0},
    "rest": {"cortisol": -4.0, "serotonin": 2.0},
    "dropped": {"cortisol": 3.0},
}

_WRITER: Callable[..., Any] | None = None


def set_writer(writer: Callable[..., Any] | None) -> None:
    """Install the memory sink; it receives (text, chat_id) so a trace stays scoped."""
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


_THINK_TITLE = "想想没说完的事"


def build_agenda(now_ms: float, *, mood_label: str = "平常", books: Iterable[dict] | None = None,
                 gaps: Iterable[dict] | None = None,
                 skills_state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Deterministic agenda for one local day: no model call, same input same plan."""
    size = max(1, min(MAX_AGENDA, agenda_size(mood_label)))
    book = _next_book(books)
    open_gaps = [gap for gap in (gaps or []) if isinstance(gap, dict) and gap.get("status") == "open"]
    rest = 0 if phase(now_ms) == "sleep" else 1
    plan: list[tuple[str, str, Any]] = []
    if book is not None:
        plan.append(("read", f"读《{str(book.get('title') or '那本书')[:20]}》", book))
    if open_gaps:
        # The life is one stream for every chat, so the title must not replay what
        # another conversation said; the gap itself stays the private source.
        plan.append(("think", _THINK_TITLE, open_gaps[0]))
    plan.append(("note", "记一笔", None))
    practice = _practice_target(skills_state)
    if practice is not None:
        plan.append(("practice", f"练一练{practice['label']}", practice))
    plan.append(("write", "写几行字", None))
    if rest:
        plan.append(("nap", "打个盹", None))
        plan.append(("rest", "歇一会儿", None))
    while len(plan) < size:
        kind = "rest" if rest and len(plan) % 2 == 0 else "note"
        plan.append((kind, "歇一会儿" if kind == "rest" else "记一笔", None))
    stamp = int(now_ms)
    return [{"id": f"act-{stamp}-{index}", "kind": kind, "title": title, "minutes": MINUTES[kind],
             "status": "pending", "started_at": 0.0, "done_at": 0.0, "source": _source(kind, extra)}
            for index, (kind, title, extra) in enumerate(plan[:size])]


def _next_book(books: Iterable[dict] | None) -> dict[str, Any] | None:
    """Pick one shelf book, preferring the ones that are not finished yet."""
    shelf = [book for book in (books or []) if isinstance(book, dict) and book.get("file")]
    if not shelf:
        return None
    unfinished = [book for book in shelf
                  if int(book.get("offset") or 0) < int(book.get("chars") or 0)]
    return (unfinished or shelf)[0]


def _source(kind: str, extra: Any) -> dict[str, Any]:
    if kind == "read" and isinstance(extra, dict):
        return {"type": "library", "file": str(extra.get("file") or "")}
    if kind == "think" and isinstance(extra, dict):
        return {"type": "gap", "id": str(extra.get("id") or "")}
    if kind == "practice" and isinstance(extra, dict):
        return {"type": "skill", "name": str(extra.get("name") or "")}
    return {}


def _practice_target(skills_state: dict[str, Any] | None) -> dict[str, str] | None:
    """The weakest media skill still short of 熟练, if any is left to train."""
    from . import skills as skills_module  # noqa: PLC0415 - only needed when planning

    state = skills_state if isinstance(skills_state, dict) else None
    if state is None:
        return None
    try:
        name = skills_module.weakest(state, ["make_image", "make_video"])
    except ValueError:
        return None
    if skills_module.level(state, name) >= 3:
        return None
    return {"name": name, "label": skills_module.NAMES.get(name, name)}


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
    for item in life.get("agenda") or []:  # old state may carry a summary in a think title
        if isinstance(item, dict) and item.get("kind") == "think":
            item["title"] = _THINK_TITLE
    today = local_day(now_ms)
    started: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    pulses: dict[str, float] = {}

    if life.get("day") != today:
        life["day"] = today
        life["agenda"] = build_agenda(now_ms, mood_label=_mood_label(state),
                                      books=_books(), gaps=state.get("gaps"),
                                      skills_state=state.get("skills"))
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
        allowed = _allowed_kinds(life, now_ms)
        nxt = next((item for item in life["agenda"]
                    if item["status"] == "pending" and item["kind"] in allowed), None)
        if nxt is None:
            # The day's plan is a starting point, not a quota: when it runs out the
            # body picks up whatever it has material for next — inside the same limits.
            nxt = _next_activity(state, now_ms, allowed)
            if nxt is not None:
                life["agenda"] = _trim_agenda([*(life.get("agenda") or []), nxt])
        if nxt is not None:
            nxt["status"] = "running"
            nxt["started_at"] = now_ms
            life["current"] = nxt["id"]
            if nxt["kind"] in LONG_KINDS:
                life["long_at"] = now_ms
            elif nxt["kind"] in SHORT_KINDS:
                life["short_at"] = now_ms
            started.append(nxt)

    if traces:
        life["traces"] = (life.get("traces") or [])[-(MAX_TRACES - len(traces)):] + traces
    return {"started": started, "finished": finished, "dropped": dropped,
            "traces": traces, "pulses": pulses}


_ACTIVITY_ORDER = ("read", "practice", "write", "think", "note", "nap")


def _trim_agenda(agenda: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the open items and only the tail of the finished ones."""
    if len(agenda) <= 12:
        return agenda
    open_items = [item for item in agenda if item.get("status") != "done"]
    done = [item for item in agenda if item.get("status") == "done"]
    return [*(done[-(max(0, 12 - len(open_items))):]), *open_items]


def _allowed_kinds(life: dict[str, Any], now_ms: float) -> set[str]:
    allowed = {"rest"}
    if now_ms - float_or(life.get("long_at"), 0.0) >= LONG_GAP_MINUTES * 60_000:
        allowed.update(LONG_KINDS)
    if now_ms - float_or(life.get("short_at"), 0.0) >= SHORT_GAP_MINUTES * 60_000:
        allowed.update(SHORT_KINDS)
    return allowed


def _next_activity(state: dict[str, Any], now_ms: float,
                   allowed: set[str] | None = None) -> dict[str, Any]:
    """Choose what to do next from real material, preferring what was done longest ago."""
    life = state.get("life") or {}
    last: dict[str, int] = {}
    for index, trace in enumerate(life.get("traces") or []):
        kind = str((trace or {}).get("kind") or "")
        if kind:
            last[kind] = index
    options: list[tuple[str, str, Any]] = []
    book = _next_book(_books())
    if book is not None:
        options.append(("read", f"读《{str(book.get('title') or '那本书')[:20]}》", book))
    practice = _practice_target(state.get("skills"))
    if practice is not None:
        options.append(("practice", f"练一练{practice['label']}", practice))
    gaps = [gap for gap in (state.get("gaps") or [])
            if isinstance(gap, dict) and gap.get("status") == "open"]
    if gaps:
        options.append(("think", _THINK_TITLE, gaps[0]))
    options.append(("write", "写几行字", None))
    options.append(("note", "记一笔", None))
    if phase(now_ms) != "sleep" and float_or(state.get("energy"), 60.0) < 85.0:
        options.append(("nap", "打个盹", None))
    if allowed is not None:
        options = [option for option in options if option[0] in allowed]
    if not options:
        return None
    kind, title, extra = min(
        options,
        key=lambda option: (last.get(option[0], -1), _ACTIVITY_ORDER.index(option[0])),
    )
    stamp = int(now_ms)
    return {"id": f"act-{stamp}-next", "kind": kind, "title": title,
            "minutes": MINUTES[kind], "status": "pending", "started_at": 0.0,
            "done_at": 0.0, "source": _source(kind, extra)}


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
    elif kind == "practice":
        trace = _finish_practice(item, source, now_ms)
    elif kind == "write":
        trace = _finish_write(state, now_ms)
    elif kind == "nap":
        trace = _finish_nap(state, now_ms)
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
        # Omit ``part``: the library resumes at the stored position, so reading in the
        # day and reading through read_source stay on the same bookmark.
        result = library.read(dict(meta), max_chars=library.MAX_CHARS)
    except (OSError, ValueError, KeyError):
        return None
    text = str(result.get("text") or "")
    excerpt = " ".join(text.split())[:90]
    progress = result.get("progress") or ""
    headline = f"读完《{item['title'][1:-1]}》" if result.get("finished") else f"读了《{item['title'][1:-1]}》{progress}"
    trace_text = f"{headline}。{excerpt}" if excerpt else headline
    return {"at": now_ms, "kind": "read", "title": item["title"], "text": trace_text,
            "source": {"type": "library", "file": name, "part": result.get("part")},
            "chat": str(meta.get("ingested_by") or ""), "share": False}


def _finish_think(state: dict[str, Any], item: dict[str, Any], source: dict[str, Any],
                  now_ms: float) -> dict[str, Any] | None:
    gap_id = source.get("id") or ""
    gap = next((g for g in state.get("gaps") or [] if g.get("id") == gap_id), None)
    if gap is None:
        return None
    if not str(gap.get("summary") or "").strip():
        return None
    return {"at": now_ms, "kind": "think", "title": item["title"],
            "text": "又琢磨了一会儿之前没说完的事。",
            "source": {"type": "gap", "id": gap_id},
            "chat": str(gap.get("chat") or ""), "share": False}


def _finish_practice(item: dict[str, Any], source: dict[str, Any], now_ms: float) -> dict[str, Any] | None:
    from . import skills as skills_module  # noqa: PLC0415

    name = str(source.get("name") or "")
    if name not in skills_module.NAMES:
        return None
    label = skills_module.NAMES[name]
    return {"at": now_ms, "kind": "practice", "title": item["title"],
            "text": f"练了一会儿{label}，手比上一次稳一点。", "source": {"type": "skill", "name": name},
            "share": False}


def _note_title(title: str) -> str:
    """Generalize a diary line that would otherwise carry a book title."""
    return "读了一会儿书" if title.startswith("读《") else title


def _finish_write(state: dict[str, Any], now_ms: float) -> dict[str, Any] | None:
    """Writing is a longer pass over the same material: a line or two, not a summary."""
    recent = [trace for trace in (state.get("life") or {}).get("traces") or []
              if trace.get("kind") not in ("note", "write")][-3:]
    parts = [_note_title(str(trace.get("title") or "")) for trace in recent if trace.get("title")]
    text = "写了几行：今天" + "，".join(parts) + "。" if parts else "写了几行，又划掉了。"
    return {"at": now_ms, "kind": "write", "title": "写了几行字", "text": text[:160],
            "source": {"type": "traces"}, "share": False}


def _finish_nap(state: dict[str, Any], now_ms: float) -> dict[str, Any] | None:
    """A short daytime sleep: the one activity that gives energy back on its own."""
    state["energy"] = min(100.0, float_or(state.get("energy"), 60.0) + 8.0)
    return {"at": now_ms, "kind": "nap", "title": "打了个盹",
            "text": "打了个盹，醒来手是暖的。", "source": {}, "share": False}


def _finish_note(state: dict[str, Any], now_ms: float) -> dict[str, Any] | None:
    recent = [trace for trace in (state.get("life") or {}).get("traces") or [] if trace.get("kind") != "note"][-2:]
    if not recent:
        return None
    parts = [_note_title(str(trace.get("title") or "")) for trace in recent if trace.get("title")]
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
        _WRITER(text, str(trace.get("chat") or ""))
    except Exception:  # noqa: BLE001 - a trace must never break the tick
        import logging
        logging.getLogger(__name__).debug("life trace was not written to memory", exc_info=True)
