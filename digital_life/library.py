"""Read a text source in pages so the agent can study it and note what it learned.

The source is copied into the profile's data dir once (so later reads do not depend on
the original file), and every read returns one bounded page plus the cursor for the next
one. Nothing here decides what matters: the agent reads, then writes its own
``memory_write(kind="learned" / "note")`` entries.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import logging
from pathlib import Path

from . import netguard

logger = logging.getLogger("hermes-lover.library")

TEXT_SUFFIXES = {".txt", ".md", ".markdown"}
MAX_BYTES = 2_000_000
MAX_CHARS = 6000
PARAGRAPH = "\n"


def _data_dir() -> Path:
    """Host-provided data dir, with a plain fallback for CLI and tests.

    The URL path never needs the host plugin storage; the local path should not fail
    just because the process was started outside the gateway.
    """
    try:
        from plugins.plugin_storage import plugin_data_dir  # noqa: PLC0415

        return Path(plugin_data_dir("hermes-lover"))
    except (ImportError, RuntimeError, OSError):
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        return Path(home) / "plugin-data" / "hermes-lover"


def root() -> Path:
    path = _data_dir() / "library"
    path.mkdir(parents=True, exist_ok=True)
    return path


def inbox_root() -> Path:
    """Where the operator drops files for the agent to read.

    The chat can only ingest text from here: the model is talked to by strangers, so
    "read this path" must not reach the rest of the host filesystem.
    """
    path = _data_dir() / "inbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _local_path(source: str) -> Path:
    """Resolve a local source inside the inbox; refuse anything outside it."""
    root = inbox_root().resolve()
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = root / path
    if path.is_symlink():
        raise ValueError(f"不能读符号链接：{source}")
    try:
        real = path.resolve()
    except OSError:
        raise ValueError(f"not a readable file: {source}") from None
    try:
        real.relative_to(root)
    except ValueError:
        raise ValueError(
            f"只能读收件目录里的文件：先把文件放进 {root}，再用文件名调用。"
            "收件目录以外的路径属于宿主系统，聊天里不能读。") from None
    if not real.is_file():
        raise ValueError(f"not a readable file: {source}")
    return real


def _slug(title: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in title)[:40].strip("-")
    return cleaned or "source"


def _fetch(url: str, timeout: float = 20.0) -> bytes:
    body, _final, truncated = netguard.fetch_public(url, max_bytes=MAX_BYTES, timeout=timeout)
    if truncated:
        raise ValueError(f"source is larger than {MAX_BYTES // 1000} KB; split it first")
    return body


_GH_BLOB = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$")
_GH_TREE = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/?(.*)$")
_GH_REPO = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/?$")


_RAW_MIRRORS = (
    "https://raw.githubusercontent.com/{o}/{r}/{ref}/{p}",
    "https://cdn.jsdelivr.net/gh/{o}/{r}@{ref}/{p}",
)


def _raw(o: str, r: str, ref: str, p: str, fetcher) -> bytes:
    """raw.githubusercontent is unreachable on some networks; try mirrors, then the API."""
    last: Exception | None = None
    for tpl in _RAW_MIRRORS:
        try:
            return (fetcher or _fetch)(tpl.format(o=o, r=r, ref=ref, p=p))
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            last = exc
    try:
        api = f"https://api.github.com/repos/{o}/{r}/contents/{p}?ref={ref}"
        import base64 as _b64
        payload = json.loads((fetcher or _fetch)(api).decode("utf-8"))
        if isinstance(payload, dict) and payload.get("content"):
            return _b64.b64decode(payload["content"])
    except Exception as exc:  # noqa: BLE001
        last = exc
    raise ValueError(f"cannot fetch {o}/{r}/{p}: {last}")


def _github(source: str, fetcher) -> tuple[str, bytes] | None:
    """Turn a GitHub page URL into raw text (a file, a README, or a directory listing)."""
    blob = _GH_BLOB.match(source)
    if blob:
        owner, repo, ref, path = blob.groups()
        return f"{repo}/{path}", _raw(owner, repo, ref, path, fetcher)
    tree = _GH_TREE.match(source)
    if tree:
        owner, repo, ref, path = tree.groups()
        api = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}"
        payload = json.loads((fetcher or _fetch)(api).decode("utf-8"))
        if isinstance(payload, dict):
            payload = [payload]
        lines = [f"{repo}/{path or ''} 目录（{len(payload)} 项）："]
        for item in payload:
            if item.get("type") == "file" and item.get("name", "").lower().endswith(
                    (".md", ".txt", ".py", ".js", ".ts", ".json", ".yaml", ".yml")):
                lines.append(f"- {item['name']} ({item.get('size', 0)} B)")
        if len(lines) == 1:
            raise ValueError("no readable text files in that directory")
        return f"{repo}/{path or 'root'} listing", "\n".join(lines).encode("utf-8")
    repo = _GH_REPO.match(source)
    if repo:
        owner, name = repo.groups()
        body = b""
        for readme in ("README.md", "README.txt", "README"):
            for branch in ("main", "master"):
                try:
                    body = _raw(owner, name, branch, readme, fetcher)
                    break
                except Exception:
                    continue
            if body:
                break
        listing = _github_listing(owner, name, "", fetcher)
        if not body and not listing:
            raise ValueError("repo has no README and no readable files")
        text = body.decode("utf-8", "replace") if body else ""
        if listing:
            text += "\n\n---\n仓库里的文本文件（要读某个文件，把它的 blob 链接再交给 read_source）：\n" + listing
        return f"{name} (repo)", text.encode("utf-8")
    return None


def _github_listing(owner: str, repo: str, path: str, fetcher) -> str:
    """Names and sizes of readable text files at one directory level (best effort)."""
    try:
        api = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
        payload = json.loads((fetcher or _fetch)(api).decode("utf-8"))
    except Exception:  # noqa: BLE001 - the listing is a bonus, not a requirement
        return ""
    if isinstance(payload, dict):
        payload = [payload]
    lines = []
    for item in payload if isinstance(payload, list) else []:
        name, kind = item.get("name", ""), item.get("type", "")
        if kind == "file" and name.lower().endswith(
                (".md", ".txt", ".json", ".csv", ".py", ".js", ".ts", ".yaml", ".yml")):
            lines.append(f"- {name} ({item.get('size', 0)} B)")
        elif kind == "dir" and len(lines) < 12:
            lines.append(f"- {name}/")
    return "\n".join(lines)


def ingest(source: str, *, fetcher=None) -> dict:
    """Copy a text file or URL into the library; returns its metadata.

    ``fetcher`` is a test seam: passing one means the caller owns URL validation.
    """
    source = str(source or "").strip()
    if not source:
        raise ValueError("source is required")
    if "://" in source and not source.startswith(("http://", "https://")):
        raise ValueError("only http(s) links can be read")
    if source.startswith(("http://", "https://")):
        github = _github(source, fetcher)
        if github is not None:
            title, raw = github
        else:
            raw = (fetcher or _fetch)(source)
            title = source.rsplit("/", 1)[-1] or "web-page"
    else:
        path = _local_path(source)
        if path.suffix.lower() not in TEXT_SUFFIXES:
            raise ValueError(f"unsupported file type: {path.suffix or '(none)'} — only txt/md")
        raw = path.read_bytes()
        title = path.name
    if len(raw) > MAX_BYTES:
        raise ValueError(f"source is larger than {MAX_BYTES // 1000} KB; split it first")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("source is empty")
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    book = root() / f"{_slug(title)}-{digest}.txt"
    meta_path = book.with_suffix(".json")
    if not book.exists():
        book.write_text(text, "utf-8")
    pages = max(1, -(-len(text) // MAX_CHARS))
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
    except (OSError, ValueError):
        meta = {}
    meta.update({"title": title, "source": source, "file": book.name, "chars": len(text),
                 "pages": pages, "digest": digest})
    meta.setdefault("position", 0)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
    return meta


def _pages(text: str, width: int) -> list[str]:
    """Split on paragraph boundaries, packing up to ``width`` chars per page."""
    pages: list[str] = []
    current = ""
    for paragraph in text.split(PARAGRAPH):
        while len(paragraph) > width:
            head, paragraph = paragraph[:width], paragraph[width:]
            if current:
                pages.append(current)
                current = ""
            pages.append(head)
        if len(current) + len(paragraph) + 1 > width and current:
            pages.append(current)
            current = ""
        current = f"{current}{PARAGRAPH}{paragraph}" if current else paragraph
    if current or not pages:
        pages.append(current)
    return pages


def read(meta: dict, part: int | None = None, max_chars: int = MAX_CHARS) -> dict:
    """Return the next page of a source and remember where reading stopped.

    ``part`` omitted resumes at the stored position, so the agent can simply keep
    calling this while it still has room in the turn instead of asking the user.
    """
    width = max(500, min(int(max_chars), MAX_CHARS))
    text = (root() / meta["file"]).read_text("utf-8")
    pages = _pages(text, width)
    if part is None:
        part = int(meta.get("position") or 0)
    part = max(0, min(int(part), len(pages) - 1))
    page = pages[part]
    meta["position"] = min(part + 1, len(pages) - 1)
    try:
        (root() / (Path(meta["file"]).stem + ".json")).write_text(
            json.dumps(meta, ensure_ascii=False), "utf-8")
    except OSError:
        pass
    done = part + 1 >= len(pages)
    return {"ok": True, "title": meta.get("title", ""), "part": part, "pages": len(pages),
            "chars": len(text), "page_chars": len(page), "text": page,
            "next_part": None if done else part + 1, "finished": done,
            "progress": f"{part + 1}/{len(pages)}",
            "hint": ("读完了。把真正会用的说法用 memory_write 记成 kind=learned——条数按内容定，"
                     "可能一条没有，也可能很多；每条要短，并写清什么场合用。另外写一条 note 概括来源。"
                     if done else
                     "还没读完：本轮还有工具额度就接着读 next_part；读到真正有意思的说法就随手记 learned，"
                     "别一口气吞完整本。")}


def books() -> list[dict]:
    items = []
    for meta_path in sorted(root().glob("*.json")):
        try:
            items.append(json.loads(meta_path.read_text("utf-8")))
        except (OSError, ValueError):
            continue
    return items
