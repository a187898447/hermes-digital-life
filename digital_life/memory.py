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
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
DAY_MS = 86_400_000
DEFAULT_WEIGHTS = {"crisis": 2.0, "daily": 1.0, "fact": 1.2, "decision": 1.5, "note": 0.8}
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
    return {"crisis": crisis, "daily": daily, "fact": 1.0, "decision": 1.0, "note": 1.0}


def relevance(bm25: float) -> float:
    # B-class adaptation: FTS5's more-negative BM25 becomes a bounded positive strength.
    strength = max(0.0, -bm25)
    return strength / (1 + strength)


def score(entry: dict, now_ms: float, rel: float, cortisol: float, empty_query: bool) -> float:
    effective = decay(entry["weight"], entry["last_access_at"], now_ms)
    recency = 1 / (1 + max(0, now_ms - entry["created_at"]) / DAY_MS / 30)
    multiplier = attention(cortisol)[entry["kind"]]
    if empty_query:
        # B-class policy: no-query recall can still reflect current attention.
        bias = min(1, max(0, (multiplier - 0.1) / 2.9))
        return 0.55 * min(1.5, effective) + 0.25 * recency + 0.20 * bias
    return 0.40 * rel + 0.20 * min(1.5, effective) + 0.15 * recency + 0.25 * rel * multiplier


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _now(value):
    return int(time.time() * 1000 if value is None else value)


class MemoryStore:
    def __init__(self, root: Path, *, connect=None, life_state=None, writes_enabled=None):
        self.root = Path(root)
        self.directory = self.root / "memory"
        self.index = self.root / "memory-index.db"
        self.connect = connect or (lambda: sqlite3.connect(self.index))
        self.life_state = life_state or (lambda: {"emergency_stop": False, "chemistry": {"cortisol": 20}})
        self.writes_enabled = writes_enabled or (lambda: True)

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
        db.execute("CREATE TABLE entries(id TEXT PRIMARY KEY, text TEXT, archived INTEGER, payload TEXT)")
        try:
            db.execute("CREATE VIRTUAL TABLE entries_fts USING fts5(id UNINDEXED, grams, tokenize='unicode61')")
        except sqlite3.OperationalError as exc:
            raise RuntimeError("SQLite FTS5 is required for event memory") from exc
        db.execute("CREATE TABLE cache_meta(digest TEXT)")
        for row in entries.values():
            db.execute("INSERT INTO entries VALUES(?,?,?,?)", (
                row["id"], row["text"], int(row["archived"]), json.dumps(row, ensure_ascii=False)))
            db.execute("INSERT INTO entries_fts VALUES(?,?)", (row["id"], " ".join(tokens(row["text"]))))

    def _save_index(self, entries, digest):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            self._populate(db, entries)
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

    def write(self, text, kind="note", weight=None, *, session_id="unknown", now_ms=None):
        if not isinstance(text, str) or not text.strip(): raise ValueError("空的记不住")
        text = text.strip()
        if len(text) > 4000: raise ValueError("超过4000字符，请拆成几条")
        if kind not in DEFAULT_WEIGHTS: raise ValueError("Unsupported memory kind")
        if weight is not None and not _number(weight): raise ValueError("weight must be a finite number")
        weight = DEFAULT_WEIGHTS[kind] if weight is None else min(3.0, max(0.2, weight))
        enabled = self.writes_enabled()
        if type(enabled) is not bool: raise ValueError("memory.writes_enabled must be boolean")
        if not enabled: raise ValueError("Memory writes are disabled")
        now_ms = _now(now_ms)
        date = datetime.fromtimestamp(now_ms / 1000).strftime("%Y-%m-%d")
        session_id = session_id or "unknown"
        with self._locked():
            # shortcut: replay validates all files per operation; add incremental checkpoints for large archives.
            self._append([], now_ms)
            entries, digest = self._replay()
            try:
                self._sync(entries, digest)
            except (sqlite3.Error, RuntimeError, OSError):
                logger.warning("Memory index unavailable before write; event log remains authoritative", exc_info=True)
            duplicate = next((e for e in entries.values() if e["date"] == date and e["text"] == text), None)
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
                         "created_at": now_ms, "last_access_at": now_ms, "hits": 0, "source_session_id": session_id}
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

    @staticmethod
    def _query(db, query, include_cold):
        condition = "1" if include_cold else "e.archived=0"
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

    def _retrieve(self, entries, query, k, include_cold, now_ms, cortisol, disk=False):
        # In-memory FTS also provides identical retrieval when stop forbids repairing the disk cache.
        connection = sqlite3.connect(self.index.as_uri() + "?mode=ro", uri=True) if disk else sqlite3.connect(":memory:")
        with closing(connection) as db:
            if not disk:
                self._populate(db, entries)
            candidates = self._query(db, query, include_cold)
        ranked = [dict(row, score=score(row, now_ms, rel, cortisol, not query),
                       effective_weight=decay(row["weight"], row["last_access_at"], now_ms)) for row, rel in candidates]
        ranked.sort(key=lambda row: (-row["score"], -row["created_at"], row["id"]))
        return {"total": sum(not e["archived"] for e in entries.values()),
                "cold_total": sum(e["archived"] for e in entries.values()), "matched": len(ranked),
                "returned": min(k, len(ranked)), "shown": ranked[:k], "attention": attention(cortisol)}

    def recall(self, query="", k=8, include_cold=False, *, now_ms=None):
        if query is None: query = ""
        if not isinstance(query, str): raise ValueError("query must be a string or null")
        if type(k) is not int: raise ValueError("k must be an integer")
        if type(include_cold) is not bool: raise ValueError("include_cold must be boolean")
        query, k, now_ms = query.strip(), min(20, max(1, k)), _now(now_ms)
        state = self._state()
        cortisol = state.get("chemistry", {}).get("cortisol", 20)
        if state.get("emergency_stop"):
            return self._retrieve(self.read_entries(), query, k, include_cold, now_ms, cortisol)
        with self._locked():
            entries, digest = self._replay()
            self._sync(entries, digest)
            result = self._retrieve(entries, query, k, include_cold, now_ms, cortisol, disk=True)
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
