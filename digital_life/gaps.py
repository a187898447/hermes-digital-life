"""Gap pool (执念池) lifecycle: propose, match, revive, close, and echo suppression.

Adapted from digital-life-core's ``gaps`` semantics in ``index.ts`` plus ``epiphany.ts``.
Every function is a pure operation on the ``state["gaps"]`` list; persistence and
host observation stay in ``runtime``. Open gaps are never deleted: closing keeps the id
and records ``closed_by``/``closed_at`` so a later synonym revives the original row
instead of creating a resolved/open twin (PRD M8.5). Resolved gaps older than
``GAP_RESOLVED_TTL_DAYS`` expire; open gaps only raise a warning past the soft cap.
"""

from __future__ import annotations

from typing import Any

import logging

from .chemistry import float_or

logger = logging.getLogger(__name__)

GAP_TENSION_BASE = 80.0
GAP_TENSION_PER_HOUR = 5.0
GAP_SIMILARITY = 0.6
GAP_RESOLVED_TTL_DAYS = 30
# An open loop that nobody closes is eventually nagging, but a chat going quiet
# overnight is not abandonment: 12h retired loops before the other side woke up.
# Three quiet days is the point where bringing it up again would be nagging.
FADE_AFTER_HOURS = 72.0
GAP_OPEN_WARN = 1000
EPIPHANY_ECHO_MINUTES = 10.0
SIBLING_WINDOW_MINUTES = 30.0
MINUTE_MS = 60_000.0


def normalize_gap_text(text: str) -> str:
    import re
    value = str(text or "").lower()
    value = re.sub(r"我不知道|不知道|不晓得|不确定|不清楚|没法回答|无法回答|无法确定|找不到|查不到|没有找到|说不准|不好说", "", value)
    return re.sub(r"[\s，。！？、；：\"''（）()《》【】\[\]…·,.!?;:'\"()<>~@#$%^&+=/\\|]", "", value)


def gap_similarity(left: str, right: str) -> float:
    """Substring containment first, then CJK bigram Jaccard; tolerant of short text."""
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


def gap_tension(gap: dict[str, Any], now_ms: float) -> float:
    """Tension grows with age (5/hour) and is reduced only by relief or closure."""
    if not isinstance(gap, dict) or gap.get("status") == "resolved":
        return 0.0
    elapsed = max(0.0, now_ms - float_or(gap.get("created_at"), now_ms) - float_or(gap.get("paused_ms"), 0.0))
    return max(0.0, min(100.0, GAP_TENSION_BASE + GAP_TENSION_PER_HOUR * (elapsed / 3_600_000)
                            - float_or(gap.get("relief"), 0.0)))


def open_gaps(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [gap for gap in gaps if isinstance(gap, dict) and gap.get("status") == "open"]


def match_gap(text: str, gaps: list[dict[str, Any]], *, threshold: float = GAP_SIMILARITY) -> dict[str, Any] | None:
    """Best open gap matching ``text``; ties break toward the oldest (longest held) gap."""
    norm = normalize_gap_text(text)
    if not norm:
        return None
    best: tuple[float, float, dict[str, Any]] | None = None
    for gap in open_gaps(gaps):
        score = gap_similarity(norm, normalize_gap_text(str(gap.get("summary", ""))))
        if score < threshold:
            continue
        key = (score, -float_or(gap.get("created_at"), 0.0), gap)
        if best is None or key[:2] > best[:2]:
            best = key
    return best[2] if best else None


def answered_gaps(gaps: list[dict[str, Any]], text: str,
                  *, threshold: float = GAP_SIMILARITY) -> list[dict[str, Any]]:
    """Open gaps whose summary appears in (or closely matches) a tool result.

    Kept separate from :func:`match_gap` because a result document is much longer
    than a gap summary: containment, not Jaccard, is the right test there.
    """
    norm = normalize_gap_text(text)
    if not norm:
        return []
    hits = []
    for gap in open_gaps(gaps):
        own = normalize_gap_text(str(gap.get("summary", "")))
        if own and (own in norm or gap_similarity(own, norm) >= threshold):
            hits.append(gap)
    return hits


def propose_gap(state: dict[str, Any], summary: str, source: str, now_ms: float, *,
                query: str | None = None, chat: str = "") -> dict[str, Any]:
    """Open a gap, or revive/reinforce an existing one (same id, no twin rows)."""
    gaps = state.setdefault("gaps", [])
    if not isinstance(gaps, list):
        gaps = state["gaps"] = []
    norm = normalize_gap_text(summary)
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        if gap_similarity(norm, normalize_gap_text(str(gap.get("summary", "")))) < GAP_SIMILARITY:
            continue
        # A gap raised in one conversation must not be adopted by another one.
        if str(gap.get("chat") or "") != str(chat or ""):
            continue
        if gap.get("status") == "resolved":
            gap["revived_from"] = gap.pop("closed_by", None)
            gap["revive_count"] = int(float_or(gap.get("revive_count"), 0.0)) + 1
            gap["resolved_at"] = None
            gap["closed_at"] = None
            gap["status"] = "open"
            gap["relief"] = max(0.0, float_or(gap.get("relief"), 0.0) - 10.0)
        gap["source"] = source
        if query:
            gap["query"] = query
        gap["tension"] = gap_tension(gap, now_ms)
        return gap
    gap = {
        "id": f"gap-{int(now_ms)}-{len(gaps)}",
        "summary": str(summary)[:200],
        "status": "open",
        "created_at": now_ms,
        "source": source,
        "chat": chat,
        "relief": 0.0,
        "tension": GAP_TENSION_BASE,
    }
    if query:
        gap["query"] = query
    gaps.append(gap)
    prune_gaps(state, now_ms)
    return gap


def prune_gaps(state: dict[str, Any], now_ms: float) -> int:
    """Expire old resolved gaps; never drop an open one. Returns how many left."""
    gaps = state.get("gaps")
    if not isinstance(gaps, list):
        return 0
    ttl = GAP_RESOLVED_TTL_DAYS * 24 * 60.0 * MINUTE_MS
    kept = []
    expired = 0
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        if gap.get("status") == "open":
            kept.append(gap)
            continue
        closed_at = float_or(gap.get("closed_at"), float_or(gap.get("resolved_at"), 0.0))
        if closed_at and now_ms - closed_at > ttl:
            expired += 1
            continue
        kept.append(gap)
    state["gaps"] = kept
    open_count = sum(1 for gap in kept if gap.get("status") == "open")
    if open_count > GAP_OPEN_WARN:
        logger.warning("hermes-lover gap pool has %s open loops (soft cap %s)", open_count, GAP_OPEN_WARN)
    return len(kept)


def close_gap(state: dict[str, Any], *, by: str, now_ms: float, match_text: str | None = None,
              gap_id: str | None = None, chat: str | None = None,
              threshold: float = GAP_SIMILARITY) -> list[dict[str, Any]]:
    """Close ``gap_id`` exactly, or the open gaps matching ``match_text`` in ``chat``.

    ``chat`` restricts the match to that conversation plus gaps that have no chat;
    ``None`` keeps the old profile-wide behaviour for operator commands.
    """
    if match_text is None and gap_id is None:
        return []

    def in_chat(gap: dict[str, Any]) -> bool:
        return chat is None or str(gap.get("chat") or "") in (chat, "")

    targets: list[dict[str, Any]] = []
    norm = normalize_gap_text(match_text or "")
    for gap in open_gaps(state.get("gaps") or []):
        if gap_id is not None:
            if gap.get("id") == gap_id and in_chat(gap):
                targets.append(gap)
            continue
        if (str(gap.get("query") or "") == str(match_text)
                or gap_similarity(norm, normalize_gap_text(str(gap.get("summary", "")))) >= threshold):
            targets.append(gap)
    if not targets:
        return []
    summaries = [normalize_gap_text(str(gap.get("summary", ""))) for gap in targets]
    closed: list[dict[str, Any]] = []
    for gap in open_gaps(state.get("gaps") or []):
        own = normalize_gap_text(str(gap.get("summary", "")))
        if gap not in targets and not any(gap_similarity(own, summary) >= threshold for summary in summaries):
            continue
        if not in_chat(gap):
            continue
        gap["status"] = "resolved"
        gap["tension"] = 0.0
        gap["relief"] = 0.0
        gap["resolved_at"] = now_ms
        gap["closed_at"] = now_ms
        gap["closed_by"] = by
        closed.append(gap)
        retire_wake(state, str(gap.get("id") or ""))
    return closed


ANSWER_GRACE_MS = 60_000


def close_answered(state: dict[str, Any], now_ms: float, *, chat: str) -> list[str]:
    """A loop the assistant raised is answered once the user has replied.

    The next message in the same chat is the reply, whether or not it addresses the
    question directly: asking twice is worse than letting a half-answered loop go.
    """
    if not chat:
        return []
    closed: list[str] = []
    for gap in open_gaps(state.get("gaps") or []):
        if str(gap.get("chat") or "") != str(chat):
            continue
        if gap.get("source") != "assistant_unknown_claim":
            continue
        if now_ms - float_or(gap.get("created_at"), now_ms) < ANSWER_GRACE_MS:
            continue
        hit = close_gap(state, by="answered", now_ms=now_ms, gap_id=str(gap.get("id") or ""), chat=chat)
        closed.extend(str(item.get("id") or "") for item in hit)
    if closed:
        logger.info("hermes-lover closed %d answered loop(s) in %s", len(closed), chat)
    return closed


def fade_stale(state: dict[str, Any], now_ms: float, *, chat: str | None = None) -> list[str]:
    """Retire open loops that have been carried past their shelf life."""
    cutoff = now_ms - FADE_AFTER_HOURS * 3_600_000
    closed: list[str] = []
    for gap in open_gaps(state.get("gaps") or []):
        if chat is not None and str(gap.get("chat") or "") not in (chat, ""):
            continue
        if float_or(gap.get("created_at"), now_ms) > cutoff:
            continue
        gap["status"] = "resolved"
        gap["closed_by"] = "faded"
        gap["closed_at"] = now_ms
        gap["resolved_at"] = now_ms
        retire_wake(state, str(gap.get("id") or ""))
        closed.append(str(gap.get("id") or ""))
    if closed:
        logger.info("hermes-lover faded %d stale open loop(s)", len(closed))
    return closed


def sibling_of_recent_close(gaps: list[dict[str, Any]], gap: dict[str, Any], now_ms: float,
                            *, window_minutes: float = SIBLING_WINDOW_MINUTES) -> dict[str, Any] | None:
    """M8.4 gate: is this open gap a missed synonym of a just-closed one?"""
    own = normalize_gap_text(str(gap.get("summary", "")))
    window = window_minutes * MINUTE_MS
    for candidate in gaps:
        if not isinstance(candidate, dict) or candidate.get("status") != "resolved":
            continue
        closed_at = float_or(candidate.get("closed_at"), float_or(candidate.get("resolved_at"), 0.0))
        if not closed_at or now_ms - closed_at > window:
            continue
        if gap_similarity(own, normalize_gap_text(str(candidate.get("summary", "")))) >= GAP_SIMILARITY:
            return candidate
    return None


def suppressed_by_echo(state: dict[str, Any], now_ms: float,
                       *, minutes: float = EPIPHANY_ECHO_MINUTES) -> bool:
    """M11.8: a wake right after an epiphany would ask her to solve what she just solved."""
    last = float_or(state.get("last_epiphany_at"), 0.0)
    return bool(last) and now_ms - last < minutes * MINUTE_MS


def note_epiphany(state: dict[str, Any], now_ms: float) -> None:
    state["last_epiphany_at"] = now_ms


def retire_wake(state: dict[str, Any], gap_id: str) -> None:
    """M11.4 lock 3: physically drop queued wake entries when their gap closes."""
    wake = state.get("wake")
    if not isinstance(wake, dict) or not isinstance(wake.get("chats"), dict):
        return
    for slot in wake["chats"].values():
        if not isinstance(slot, dict):
            continue
        queue = slot.get("queue")
        if isinstance(queue, list):
            slot["queue"] = [item for item in queue
                             if not isinstance(item, dict) or item.get("gap_id") != gap_id]
        active = slot.get("active")
        if isinstance(active, dict) and active.get("gap_id") == gap_id:
            slot["active"] = None
