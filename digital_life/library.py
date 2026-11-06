"""Read a text source in pages so the agent can study it and note what it learned.

The source is copied into the profile's data dir once (so later reads do not depend on
the original file), and every read returns one bounded page plus the cursor for the next
one. Nothing here decides what matters: the agent reads, then writes its own
``memory_write(kind="learned" / "note")`` entries.
"""

from __future__ import annotations

import hashlib
import json
import re
import logging
import urllib.request
from pathlib import Path

logger = logging.getLogger("hermes-lover.library")

TEXT_SUFFIXES = {".txt", ".md", ".markdown"}
MAX_BYTES = 2_000_000
MAX_CHARS = 6000
PARAGRAPH = "\n"


def root() -> Path:
    from plugins.plugin_storage import plugin_data_dir  # noqa: PLC0415

    path = Path(plugin_data_dir("hermes-lover")) / "library"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(title: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in title)[:40].strip("-")
    return cleaned or "source"


def _fetch(url: str, timeout: float = 20.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "hermes-lover/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_BYTES + 1)


_GH_BLOB = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$")
_GH_TREE = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/?(.*)$")
_GH_REPO = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/?$")


def _github(source: str, fetcher) -> tuple[str, bytes] | None:
    """Turn a GitHub page URL into raw text (a file, a README, or a directory listing)."""
    blob = _GH_BLOB.match(source)
    if blob:
        owner, repo, ref, path = blob.groups()
        raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
        return f"{repo}/{path}", (fetcher or _fetch)(raw)
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
        for readme in ("README.md", "README.txt", "README"):
            for branch in ("main", "master"):
                raw = f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{readme}"
                try:
                    return f"{name}/{readme}", (fetcher or _fetch)(raw)
                except Exception:
                    continue
        raise ValueError("repo has no README on main/master")
    return None


def ingest(source: str, *, fetcher=None) -> dict:
    """Copy a text file or URL into the library; returns its metadata."""
    source = str(source or "").strip()
    if not source:
        raise ValueError("source is required")
    if source.startswith(("http://", "https://")):
        github = _github(source, fetcher)
        if github is not None:
            title, raw = github
        else:
            raw = (fetcher or _fetch)(source)
            title = source.rsplit("/", 1)[-1] or "web-page"
    else:
        path = Path(source).expanduser()
        if not path.is_file():
            raise ValueError(f"not a readable file: {source}")
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
