"""Profile event memory. Daily logs are authoritative; SQLite is a disposable index."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import struct
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from . import embeddings
from .domain import anchor_key

logger = logging.getLogger(__name__)
RRF_K = 60
VECTOR_TOP = 20
DAY_MS = 86_400_000
DEFAULT_WEIGHTS = {"crisis": 2.0, "daily": 1.0, "fact": 1.2, "decision": 1.5, "note": 0.8, "turn": 0.4,
                   "journal": 2.5, "summary": 1.4}
_WRITE_LOCK = threading.RLock()
_WORDS = re.compile(r"[\u3400-\u9fff\U00020000-\U0002fa1f]+|[a-zA-Z0-9]+")
_ID = re.compile(r"hp-(\d{8})-(\d{4,})\Z")


def tokens(text: str) -> list[str]:
    result = []
    for match in _WORDS.finditer(text):
        word = match.group()
        if word.isascii():
            result.append(word.lower())
        else:
            result.extend(word[i:i + 2] for i in range(len(word) - 1))
    return result


# A-class memory-v2 semantics: seven-day effective weight, reciprocal recency and attention.
def decay(weight: float, last_access_at: float, now_ms: float) -> float:
    return weight * 0.5 ** (max(0, now_ms - last_access_at) / DAY_MS / 7)


def attention(cortisol: float) -> dict[str, float]:
    crisis, daily = (3.0, 0.1) if cortisol >= 80 else (1.8, 0.4) if cortisol >= 55 else (1.0, 1.0)
    return {"crisis": crisis, "daily": daily, "fact": 1.0, "decision": 1.0, "note": 1.0, "turn": 0.5,
            "journal": 1.2, "summary": 1.0}


def relevance(bm25: float) -> float:
    # B-class adaptation: FTS5's more-negative BM25 becomes a bounded positive strength.
    strength = max(0.0, -bm25)
    return strength / (1 + strength)


ANCHOR_BOOST = 1.5


def anchor_affinity(entry: dict, query: str) -> bool:
    """A query that names a recorded anchor pulls that memory forward."""
    if not query:
        return False
    lowered = query.lower()
    return any(key and key in lowered for key in entry.get("anchors") or ())


def score(entry: dict, now_ms: float, rel: float, cortisol: float, empty_query: bool,
          query: str = "") -> float:
    effective = decay(entry["weight"], entry["last_access_at"], now_ms)
    recency = 1 / (1 + max(0, now_ms - entry["created_at"]) / DAY_MS / 30)
    multiplier = attention(cortisol)[entry["kind"]]
    if empty_query:
        # B-class policy: no-query recall can still reflect current attention.
        bias = min(1, max(0, (multiplier - 0.1) / 2.9))
        return 0.55 * min(1.5, effective) + 0.25 * recency + 0.20 * bias
    total = 0.40 * rel + 0.20 * min(1.5, effective) + 0.15 * recency + 0.25 * rel * multiplier
    return total * ANCHOR_BOOST if anchor_affinity(entry, query) else total


def anchor_keys(text: str) -> list[str]:
    """Anchors are the stable opening lines of an exchange, not the whole blob."""
    keys: list[str] = []
    for line in text.splitlines():
        key = anchor_key(line)
        if len(key) < 2 or key in keys:
            continue
        keys.append(key)
        if len(keys) == 3:
            break
    return keys


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _now(value):
    return int(time.time() * 1000 if value is None else value)


class MemoryStore:
    def __init__(self, root: Path, *, connect=None, life_state=None, writes_enabled=None,
                 embedder=None):
        self.root = Path(root)
        self.directory = self.root / "memory"
        self.index = self.root / "memory-index.db"
        self.connect = connect or (lambda: sqlite3.connect(self.index))
        self.life_state = life_state or (lambda: {"emergency_stop": False, "chemistry": {"cortisol": 20}})
        self.writes_enabled = writes_enabled or (lambda: True)
        # None means "whatever this machine offers"; False disables vectors for tests/ops.
        self.embedder = embeddings.default_embedder() if embedder is None else embedder

    def _state(self):
        return self.life_state()

    def _require_running(self):
        if self._state().get("emergency_stop"):
            raise ValueError("Emergency stop is active; memory is read-only")

    @contextmanager
    def _locked(self):
        with _WRITE_LOCK:
            self._require_running()
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / ".lock").open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    self._require_running()
                    yield
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _paths(self):
        return sorted(p for p in self.directory.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/*.jsonl")
                      if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.jsonl", p.name))

    def _replay(self):
        entries = {}
        digest = hashlib.sha256()
        for path in self._paths():
            data = path.read_bytes()
            digest.update(str(path.relative_to(self.directory)).encode())
            digest.update(data)
            for line in data.splitlines(keepends=True):
                if not line.endswith(b"\n"):
                    logger.warning("Ignoring incomplete event tail in %s", path)
                    continue
                try:
                    event = json.loads(line)
                    self._fold(entries, event)
                except (ValueError, TypeError, KeyError, OverflowError):
                    logger.warning("Ignoring invalid memory event in %s", path)
        return entries, digest.hexdigest()

    @staticmethod
    def _fold(entries, event):
        if not isinstance(event, dict) or event.get("version", 1) != 1:
            raise ValueError("invalid event version")
        identifier = event["id"]
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise ValueError("invalid id")
        op = event["op"]
        if op == "add":
            if identifier in entries:
                raise ValueError("duplicate id")
            if event["kind"] not in DEFAULT_WEIGHTS or not isinstance(event["text"], str) or not event["text"].strip():
                raise ValueError("invalid content")
            for key in ("weight", "cortisol", "created_at", "last_access_at"):
                if not _number(event[key]): raise ValueError("invalid number")
            if not 0.2 <= event["weight"] <= 3 or not 0 <= event["cortisol"] <= 100:
                raise ValueError("invalid weight/cortisol")
            if type(event["hits"]) is not int or event["hits"] < 0 or not isinstance(event["source_session_id"], str):
                raise ValueError("invalid access metadata")
            entries[identifier] = {k: event[k] for k in (
                "id", "kind", "text", "weight", "cortisol", "created_at", "last_access_at", "hits", "source_session_id")}
            for field in ("chat_id", "chat_type", "speaker"):
                value = event.get(field, "")
                if not isinstance(value, str):
                    raise ValueError("invalid scope")
                entries[identifier][field] = value
            anchors = event.get("anchors", [])
            if not isinstance(anchors, list) or not all(isinstance(key, str) for key in anchors):
                raise ValueError("invalid anchors")
            entries[identifier]["anchors"] = anchors[:3]
            for field in ("sources", "refs"):
                value = event.get(field, [])
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ValueError("invalid references")
                entries[identifier][field] = value[:20]
            entries[identifier]["compacted_into"] = ""
            date = identifier.split("-")[1]
            entries[identifier].update(date=f"{date[:4]}-{date[4:6]}-{date[6:]}", archived=False)
        elif op == "touch":
            row = entries[identifier]
            if not _number(event["last_access_at"]) or type(event["hits"]) is not int or event["hits"] < row["hits"]:
                raise ValueError("invalid touch")
            changes = {"last_access_at": event["last_access_at"], "hits": event["hits"]}
            if "weight" in event:
                if not _number(event["weight"]) or not 0.2 <= event["weight"] <= 3:
                    raise ValueError("invalid weight")
                changes["weight"] = event["weight"]
            row.update(changes)
        elif op == "forget":
            if not _number(event["archived_at"]): raise ValueError("invalid archive time")
            entries[identifier].update(archived=True, archived_at=event["archived_at"])
        elif op == "compact":
            # Cold entries keep their text but point at the summary that now represents them.
            target = event["into"]
            if not isinstance(target, str) or not _ID.fullmatch(target) or target not in entries:
                raise ValueError("invalid summary id")
            entries[identifier]["compacted_into"] = target
        else:
            raise ValueError("unknown operation")

    def read_entries(self):
        """Read authority without touching files, index or life state."""
        return self._replay()[0]

    @staticmethod
    def _populate(db, entries):
        db.execute("DROP TABLE IF EXISTS entries_fts")
        db.execute("DROP TABLE IF EXISTS entries")
        db.execute("DROP TABLE IF EXISTS cache_meta")
        db.execute("CREATE TABLE entries(id TEXT PRIMARY KEY, text TEXT, archived INTEGER, compacted_into TEXT, payload TEXT)")
        try:
            db.execute("CREATE VIRTUAL TABLE entries_fts USING fts5(id UNINDEXED, grams, tokenize='unicode61')")
        except sqlite3.OperationalError as exc:
            raise RuntimeError("SQLite FTS5 is required for event memory") from exc
        db.execute("CREATE TABLE cache_meta(digest TEXT)")
        # Vectors survive a rebuild by id; entries are immutable, so only missing ids are embedded.
        db.execute("CREATE TABLE IF NOT EXISTS vectors(id TEXT PRIMARY KEY, dim INTEGER NOT NULL, blob BLOB NOT NULL)")
        for row in entries.values():
            db.execute("INSERT INTO entries VALUES(?,?,?,?,?)", (
                row["id"], row["text"], int(row["archived"]), row.get("compacted_into") or "",
                json.dumps(row, ensure_ascii=False)))
            db.execute("INSERT INTO entries_fts VALUES(?,?)", (row["id"], " ".join(tokens(row["text"]))))

    def _ensure_vectors(self, db, entries):
        """Embed entries that have no vector yet. A failure only costs hybrid recall."""
        if not self.embedder:
            return 0
        written = 0
        try:
            db.execute("CREATE TABLE IF NOT EXISTS vectors(id TEXT PRIMARY KEY, dim INTEGER NOT NULL, blob BLOB NOT NULL)")
            have = {row[0] for row in db.execute("SELECT id FROM vectors")}
            missing = [entry for identifier, entry in entries.items() if identifier not in have]
            for start in range(0, len(missing), 16):
                batch = missing[start:start + 16]
                vectors = self.embedder.embed([entry["text"] for entry in batch])
                if not vectors or len(vectors) != len(batch):
                    return written
                for entry, vector in zip(batch, vectors):
                    if not isinstance(vector, (list, tuple)) or not vector:
                        continue
                    values = [float(value) for value in vector]
                    if not all(math.isfinite(value) for value in values):
                        continue
                    unit = embeddings.normalize(values)
                    if unit is None:
                        continue
                    db.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?)",
                               (entry["id"], len(unit), struct.pack(f"<{len(unit)}f", *unit)))
                    written += 1
        except Exception:
            logger.warning("Vector indexing failed; memory recall falls back to FTS", exc_info=True)
        return written

    def _save_index(self, entries, digest):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            self._populate(db, entries)
            self._ensure_vectors(db, entries)
            db.execute("INSERT INTO cache_meta VALUES(?)", (digest,))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _sync(self, entries, digest, force=False):
        if self.index.exists() and not force:
            try:
                with closing(sqlite3.connect(self.index.as_uri() + "?mode=ro", uri=True)) as db:
                    if db.execute("SELECT digest FROM cache_meta").fetchone() == (digest,):
                        return
            except sqlite3.DatabaseError:
                pass
        try:
            self._save_index(entries, digest)
        except sqlite3.DatabaseError as exc:
            # Only an unreadable database is discarded; transaction/schema failures are propagated.
            if "not a database" not in str(exc) and "malformed" not in str(exc):
                raise
            self.index.unlink(missing_ok=True)
            self._save_index(entries, digest)

    def rebuild_index(self):
        with self._locked():
            entries, digest = self._replay()
            self._sync(entries, digest, force=True)
            return {"ok": True, "total": len(entries)}

    @staticmethod
    def _write_all(fd, data):
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0: raise OSError("memory log short write")
            view = view[written:]

    def _append(self, events, now_ms):
        date = datetime.fromtimestamp(now_ms / 1000)
        path = self.directory / date.strftime("%Y/%m/%Y-%m-%d.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            data = path.read_bytes()
            if data and not data.endswith(b"\n"):
                boundary = data.rfind(b"\n") + 1
                tail = data[boundary:]
                try:
                    json.loads(tail)
                except (ValueError, UnicodeDecodeError):
                    recovery = path.with_name(path.name + f".recovery-{time.time_ns()}")
                    with recovery.open("xb") as backup:
                        backup.write(tail)
                        backup.flush()
                        os.fsync(backup.fileno())
                    with path.open("r+b") as log:
                        log.truncate(boundary)
                        log.flush()
                        os.fsync(log.fileno())
                else:
                    with path.open("ab") as log:
                        log.write(b"\n")
                        log.flush()
                        os.fsync(log.fileno())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            for event in events:
                data = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
                self._write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _ids(value, label):
        """Validate a reference list before filtering it against the replayed entries."""
        if value is None:
            return []
        if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{label} must be a list of strings")
        return [item for item in value if item][:20]

    @staticmethod
    def _known(ids, entries):
        return [identifier for identifier in ids if _ID.fullmatch(identifier) and identifier in entries]

    def get(self, identifier):
        """Read one entry by id, including archived entries; never touches access history."""
        if not isinstance(identifier, str):
            raise ValueError("id must be a string")
        return self.read_entries().get(identifier)

    def write(self, text, kind="note", weight=None, *, session_id="unknown", now_ms=None,
              chat_id="", chat_type="", speaker="", day=None, sources=None, refs=None):
        if not isinstance(text, str) or not text.strip(): raise ValueError("空的记不住")
        date_override = None
        if day is not None:
            if not isinstance(day, str) or not re.match(r"\d{4}-\d{2}-\d{2}\Z", day):
                raise ValueError("day must be YYYY-MM-DD")
            date_override = day
        text = text.strip()
        if len(text) > 4000: raise ValueError("超过4000字符，请拆成几条")
        if kind not in DEFAULT_WEIGHTS: raise ValueError("Unsupported memory kind")
        if weight is not None and not _number(weight): raise ValueError("weight must be a finite number")
        sources = self._ids(sources, "sources")
        refs = self._ids(refs, "refs")
        weight = DEFAULT_WEIGHTS[kind] if weight is None else min(3.0, max(0.2, weight))
        enabled = self.writes_enabled()
        if type(enabled) is not bool: raise ValueError("memory.writes_enabled must be boolean")
        if not enabled: raise ValueError("Memory writes are disabled")
        now_ms = _now(now_ms)
        date = date_override or datetime.fromtimestamp(now_ms / 1000).strftime("%Y-%m-%d")
        session_id = session_id or "unknown"
        with self._locked():
            # shortcut: replay validates all files per operation; add incremental checkpoints for large archives.
            self._append([], now_ms)
            entries, digest = self._replay()
            try:
                self._sync(entries, digest)
            except (sqlite3.Error, RuntimeError, OSError):
                logger.warning("Memory index unavailable before write; event log remains authoritative", exc_info=True)
            # Facts cite the turns they were distilled from; diaries cite their facts.
            traced_sources = self._known(sources, entries) if kind == "fact" else []
            traced_refs = self._known(refs, entries) if kind == "journal" else []
            duplicate = next((e for e in entries.values()
                              if e["date"] == date and e["text"] == text
                              and e.get("chat_id", "") == (chat_id or "")), None)
            if duplicate:
                event = {"op": "touch", "id": duplicate["id"], "last_access_at": now_ms,
                         "hits": duplicate["hits"], "weight": min(3.0, duplicate["weight"] + 0.2)}
            else:
                today = [e for e in entries.values() if e["date"] == date]
                if sum(e["source_session_id"] == session_id for e in today) >= 20:
                    raise ValueError("Daily session memory quota (20) reached")
                seq = max((int(e["id"].rsplit("-", 1)[1]) for e in today), default=0) + 1
                event = {"op": "add", "id": f"hp-{date.replace('-', '')}-{seq:04d}", "text": text,
                         "kind": kind, "weight": weight, "cortisol": self._state().get("chemistry", {}).get("cortisol", 20),
                         "created_at": now_ms, "last_access_at": now_ms, "hits": 0, "source_session_id": session_id,
                         "chat_id": str(chat_id or ""), "chat_type": str(chat_type or ""), "speaker": str(speaker or ""),
                         "sources": traced_sources, "refs": traced_refs}
            self._require_running()
            self._append([event], now_ms)
            indexed = True
            try:
                entries, digest = self._replay()
                self._save_index(entries, digest)
            except (sqlite3.Error, RuntimeError, OSError):
                logger.warning("Memory event persisted but index update failed", exc_info=True)
                indexed = False
            return {"ok": True, "id": event["id"], "date": date, "duplicate": bool(duplicate),
                    "created_at": duplicate["created_at"] if duplicate else now_ms, "indexed": indexed}


    def capture(self, text, *, session_id="unknown", now_ms=None,
                chat_id="", chat_type="", speaker=""):
        """Append one raw exchange as a day-sharded turn entry (no model, no quota)."""
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "empty"}
        text = text.strip()[:4000]
        enabled = self.writes_enabled()
        if type(enabled) is not bool:
            return {"ok": False, "error": "memory.writes_enabled must be boolean"}
        if not enabled:
            return {"ok": False, "error": "Memory writes are disabled"}
        now_ms = _now(now_ms)
        date = datetime.fromtimestamp(now_ms / 1000).strftime("%Y-%m-%d")
        session_id = session_id or "unknown"
        with self._locked():
            self._append([], now_ms)
            entries, digest = self._replay()
            try:
                self._sync(entries, digest)
            except (sqlite3.Error, RuntimeError, OSError):
                logger.warning("Memory index unavailable before capture", exc_info=True)
            today = [e for e in entries.values() if e["date"] == date]
            seq = max((int(e["id"].rsplit("-", 1)[1]) for e in today), default=0) + 1
            event = {"op": "add", "id": f"hp-{date.replace('-', '')}-{seq:04d}", "text": text,
                     "kind": "turn", "weight": 0.4,
                     "cortisol": self._state().get("chemistry", {}).get("cortisol", 20),
                     "created_at": now_ms, "last_access_at": now_ms, "hits": 0,
                     "source_session_id": session_id,
                     "chat_id": str(chat_id or ""), "chat_type": str(chat_type or ""), "speaker": str(speaker or ""),
                     "anchors": anchor_keys(text)}
            self._require_running()
            self._append([event], now_ms)
            try:
                entries, digest = self._replay()
                self._save_index(entries, digest)
            except (sqlite3.Error, RuntimeError, OSError):
                logger.warning("Memory event persisted but index update failed", exc_info=True)
            return {"ok": True, "id": event["id"], "date": date}

    def clear(self, chat_id=None, *, now_ms=None):
        """Archive every live entry (optionally only one chat). Returns how many were archived."""
        stamp = time.time() * 1000 if now_ms is None else now_ms
        with self._locked():
            self._require_running()
            entries = self.read_entries()
            targets = [e for e in entries.values()
                       if not e.get("archived") and (chat_id is None or e.get("chat_id", "") == chat_id)]
            if not targets:
                return 0
            self._append([{"version": 1, "id": e["id"], "op": "forget", "archived_at": stamp}
                          for e in targets], stamp)
            try:
                entries, digest = self._replay()
                self._sync(entries, digest)
            except (sqlite3.Error, RuntimeError, OSError):
                logger.warning("Memory index unavailable after clear", exc_info=True)
            return len(targets)

    @staticmethod
    def _summary_text(rows, date, chat_id):
        """A deterministic digest: date, chat, a taste of each entry, and the ids it covers."""
        head = f"{date} {chat_id or '私聊'} 共 {len(rows)} 条："
        ids = [row["id"] for row in rows]
        tail = ""
        for count in range(len(ids), 0, -1):
            suffix = f"（来源 {' '.join(ids[:count])}"
            if count < len(ids):
                suffix += f" 等 {len(ids)} 条"
            suffix += "）"
            if len(head) + len(suffix) <= 200:
                tail = suffix
                break
        if not tail:
            head = f"{date} 共 {len(rows)} 条："
            tail = f"（来源 {ids[0]}）"[:(200 - len(head))]
        room = max(1, 200 - len(head) - len(tail))
        body = "；".join(row["text"].replace("\n", " ") for row in rows)
        if len(body) > room:
            body = body[:room - 1] + "…"
        return (head + body + tail)[:200]

    def compact(self, days=30, *, now_ms=None):
        """Fold archived entries untouched for `days` into one cold summary per (date, chat).

        Summaries are archived too, so normal recall stays unchanged and include_cold shows
        the summary instead of expanding the originals, which remain readable by id.
        """
        if type(days) is not int or not 1 <= days <= 3650:
            raise ValueError("days must be an integer between 1 and 3650")
        now_ms = _now(now_ms)
        cutoff = now_ms - days * DAY_MS
        with self._locked():
            self._require_running()
            self._append([], now_ms)
            entries, digest = self._replay()
            try:
                self._sync(entries, digest)
            except (sqlite3.Error, RuntimeError, OSError):
                logger.warning("Memory index unavailable before compact", exc_info=True)
            groups: dict[tuple[str, str], list[dict]] = {}
            for entry in entries.values():
                if entry.get("kind") == "summary" or not entry.get("archived"):
                    continue
                if entry.get("compacted_into") or entry.get("last_access_at", 0) >= cutoff:
                    continue
                groups.setdefault((entry["date"], entry.get("chat_id", "")), []).append(entry)
            if not groups:
                return {"ok": True, "groups": 0, "summaries": 0, "compacted": 0}
            seqs: dict[str, int] = {}
            events = []
            for date, chat_id in sorted(groups):
                rows = sorted(groups[(date, chat_id)], key=lambda row: (row["created_at"], row["id"]))
                seq = seqs.get(date)
                if seq is None:
                    seq = max((int(e["id"].rsplit("-", 1)[1]) for e in entries.values() if e["date"] == date),
                              default=0) + 1
                seqs[date] = seq + 1
                summary_id = f"hp-{date.replace('-', '')}-{seq:04d}"
                ids = [row["id"] for row in rows]
                events.append({
                    "op": "add", "id": summary_id, "text": self._summary_text(rows, date, chat_id),
                    "kind": "summary", "weight": DEFAULT_WEIGHTS["summary"],
                    "cortisol": self._state().get("chemistry", {}).get("cortisol", 20),
                    "created_at": now_ms, "last_access_at": now_ms, "hits": 0,
                    "source_session_id": "memory-compact", "chat_id": chat_id,
                    "chat_type": rows[0].get("chat_type", ""), "speaker": "",
                    "sources": ids[:20], "refs": []})
                events.append({"op": "forget", "id": summary_id, "archived_at": now_ms})
                events.extend({"op": "compact", "id": identifier, "into": summary_id, "at": now_ms}
                              for identifier in ids)
            self._append(events, now_ms)
            try:
                entries, digest = self._replay()
                self._save_index(entries, digest)
            except (sqlite3.Error, RuntimeError, OSError):
                logger.warning("Memory summaries persisted but index update failed", exc_info=True)
            return {"ok": True, "groups": len(groups), "summaries": len(groups),
                    "compacted": sum(len(rows) for rows in groups.values())}

    @staticmethod
    def _query(db, query, include_cold):
        # Cold recall shows summaries and never expands the entries they already cover.
        condition = "e.archived=1 AND e.compacted_into=''" if include_cold else "e.archived=0"
        if not query:
            rows = db.execute(f"SELECT e.payload, 0 FROM entries e WHERE {condition}").fetchall()
            return [(json.loads(row), 0.0) for row, _ in rows]
        grams = tokens(query)
        if grams:
            expression = " AND ".join('"' + gram.replace('"', '""') + '"' for gram in grams)
            rows = db.execute(
                f"SELECT e.payload, bm25(entries_fts) FROM entries_fts JOIN entries e ON e.id=entries_fts.id WHERE entries_fts MATCH ? AND {condition}",
                (expression,)).fetchall()
            return [(json.loads(row), relevance(rank)) for row, rank in rows]
        literal = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = db.execute(f"SELECT e.payload FROM entries e WHERE {condition} AND e.text LIKE ? ESCAPE '\\'", (f"%{literal}%",)).fetchall()
        return [(json.loads(row), 1.0) for row, in rows]

    def _query_vector(self, query):
        if not self.embedder:
            return None
        try:
            vectors = self.embedder.embed([query])
        except Exception:
            logger.warning("Query embedding failed; memory recall stays on FTS", exc_info=True)
            return None
        if not vectors or not vectors[0]:
            return None
        try:
            values = [float(value) for value in vectors[0]]
        except (TypeError, ValueError):
            return None
        if not values or not all(math.isfinite(value) for value in values):
            return None
        return embeddings.normalize(values)

    def _vector_hits(self, db, entries, query_vector, include_cold, chat_id, day, kind):
        """Cosine top-20 from the persisted vectors, filtered like the FTS path."""
        rows = db.execute("SELECT id, dim, blob FROM vectors").fetchall()
        hits = []
        for identifier, dim, blob in rows:
            if dim != len(query_vector):
                raise ValueError("embedding dimension changed")
            if blob is None or len(blob) != 4 * dim:
                raise ValueError("corrupt embedding")
            entry = entries.get(identifier)
            if entry is None or (not include_cold and entry.get("archived")):
                continue
            if include_cold and entry.get("compacted_into"):
                continue
            if chat_id and entry.get("chat_id", "") not in ("", chat_id):
                continue
            if day and entry.get("date") != day:
                continue
            if kind and entry.get("kind") != kind:
                continue
            vector = struct.unpack(f"<{dim}f", blob)
            hits.append((sum(a * b for a, b in zip(query_vector, vector)), identifier))
        hits.sort(key=lambda item: (-item[0], item[1]))
        return [identifier for _, identifier in hits[:VECTOR_TOP]]

    def _index_vectors(self, entries):
        """Backfill vectors for entries written before P1 or after a reset."""
        if not self.embedder:
            return
        try:
            with closing(sqlite3.connect(self.index.as_uri() + "?mode=ro", uri=True)) as db:
                if db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] >= len(entries):
                    return
        except sqlite3.Error:
            pass
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_vectors(db, entries)
            db.commit()
        except BaseException:
            db.rollback()
            logger.warning("Vector backfill failed; memory recall stays on FTS", exc_info=True)
        finally:
            db.close()

    def _drop_vectors(self):
        db = self.connect()
        try:
            db.execute("DROP TABLE IF EXISTS vectors")
            db.commit()
        except sqlite3.Error:
            logger.warning("Could not clear the broken vector index", exc_info=True)
        finally:
            db.close()

    def _retrieve(self, entries, query, k, include_cold, now_ms, cortisol, disk=False, chat_id=None,
                  day=None, kind=None, hybrid=False):
        # In-memory FTS also provides identical retrieval when stop forbids repairing the disk cache.
        connection = sqlite3.connect(self.index.as_uri() + "?mode=ro", uri=True) if disk else sqlite3.connect(":memory:")
        mode, broken, vector_ids = "fts", False, []
        with closing(connection) as db:
            if not disk:
                self._populate(db, entries)
            candidates = self._query(db, query, include_cold)
            if hybrid and query and disk:
                query_vector = self._query_vector(query)
                if query_vector:
                    try:
                        vector_ids = self._vector_hits(db, entries, query_vector, include_cold,
                                                       chat_id, day, kind)
                        mode = "hybrid"
                    except (sqlite3.Error, ValueError, TypeError, struct.error) as exc:
                        logger.warning("Vector index unusable; clearing it and using FTS: %s", exc)
                        broken = True
            if chat_id:
                # A conversation only sees its own exchanges plus globally written notes.
                candidates = [(row, rel) for row, rel in candidates
                              if row.get("chat_id", "") in ("", chat_id)]
            if day:
                # Day lookups open the journal first, then that day's raw entries.
                candidates = [(row, rel) for row, rel in candidates if row.get("date") == day]
            if kind:
                candidates = [(row, rel) for row, rel in candidates if row.get("kind") == kind]
        fts_rows = [(row, rel, score(row, now_ms, rel, cortisol, not query, query))
                    for row, rel in candidates]
        fts_rows.sort(key=lambda item: (-item[2], -item[0]["created_at"], item[0]["id"]))
        if mode == "hybrid":
            # Reciprocal-rank fusion keeps one strong signal from either side near the top.
            rrf = {}
            for position, (row, _rel, _row_score) in enumerate(fts_rows[:VECTOR_TOP], 1):
                rrf[row["id"]] = (rrf.get(row["id"], 0.0) or 0.0) + 1.0 / (RRF_K + position)
            for position, identifier in enumerate(vector_ids, 1):
                rrf[identifier] = (rrf.get(identifier, 0.0) or 0.0) + 1.0 / (RRF_K + position)
            fused = {row["id"]: (row, row_score) for row, _rel, row_score in fts_rows}
            for identifier in vector_ids:
                if identifier not in fused:
                    row = entries[identifier]
                    fused[identifier] = (row, score(row, now_ms, 0.0, cortisol, False, query))
            ordered = sorted(fused.items(), key=lambda item: (
                day is not None and item[1][0]["kind"] != "journal",
                -(rrf.get(item[0]) or 0.0), -item[1][1],
                -item[1][0]["created_at"], item[0]))
            ranked = [pair for _identifier, pair in ordered]
        else:
            ranked = [(row, row_score) for row, _rel, row_score in fts_rows]
            ranked.sort(key=lambda item: (day is not None and item[0]["kind"] != "journal",
                                          -item[1], -item[0]["created_at"], item[0]["id"]))
        shown = []
        for row, row_score in ranked:
            refs = list(row.get("refs") or ())
            sources = list(row.get("sources") or ())
            if row.get("kind") in ("journal", "summary") and not sources:
                # A diary or summary traces downward through the facts it cites.
                sources = refs
            shown.append(dict(row, sources=sources, refs=refs, score=row_score,
                              effective_weight=decay(row["weight"], row["last_access_at"], now_ms)))
        scoped = [e for e in entries.values() if not chat_id or e.get("chat_id", "") in ("", chat_id)]
        return {"total": sum(not e["archived"] for e in scoped),
                "cold_total": sum(e["archived"] for e in scoped), "matched": len(shown),
                "returned": min(k, len(shown)), "shown": shown[:k], "attention": attention(cortisol),
                "mode": mode, "_vector_broken": broken}

    def recall(self, query="", k=8, include_cold=False, *, now_ms=None, chat_id=None, day=None,
               cortisol=None, kind=None, hybrid=True):
        if query is None: query = ""
        if not isinstance(query, str): raise ValueError("query must be a string or null")
        if day is not None:
            if not isinstance(day, str) or not re.match(r"\d{4}-\d{2}-\d{2}\Z", day):
                raise ValueError("day must be YYYY-MM-DD")
        if kind is not None and kind not in DEFAULT_WEIGHTS:
            raise ValueError("Unsupported memory kind")
        if type(k) is not int: raise ValueError("k must be an integer")
        if type(include_cold) is not bool: raise ValueError("include_cold must be boolean")
        if type(hybrid) is not bool: raise ValueError("hybrid must be boolean")
        query, k, now_ms = query.strip(), min(20, max(1, k)), _now(now_ms)
        state = self._state()
        if cortisol is None:
            cortisol = state.get("chemistry", {}).get("cortisol", 20)
        if state.get("emergency_stop"):
            result = self._retrieve(self.read_entries(), query, k, include_cold, now_ms, cortisol,
                                    chat_id=chat_id, day=day, kind=kind)
            result.pop("_vector_broken", None)
            return result
        with self._locked():
            entries, digest = self._replay()
            self._sync(entries, digest)
            if hybrid:
                self._index_vectors(entries)
            result = self._retrieve(entries, query, k, include_cold, now_ms, cortisol, disk=True,
                                    chat_id=chat_id, day=day, kind=kind, hybrid=hybrid)
            if result.pop("_vector_broken", False):
                self._drop_vectors()
            events = [{"op": "touch", "id": row["id"], "hits": row["hits"] + 1, "last_access_at": now_ms}
                      for row in result["shown"]]
            if events:
                self._require_running()
                self._append(events, now_ms)
                for row, event in zip(result["shown"], events):
                    row.update(hits=event["hits"], last_access_at=now_ms)
                entries, digest = self._replay()
                try:
                    self._save_index(entries, digest)
                except sqlite3.Error:
                    logger.warning("Recall touches persisted but index update failed", exc_info=True)
            return result
