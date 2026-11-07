"""Hand the user one finished video, without giving the model the filesystem.

A generator (or the operator) drops finished clips into the outbox folder; the agent
only names one of those files. Everything here is about keeping that sentence true:
no absolute paths, no ``..``, no symlinks leading out, no other file types, and a
size cap the chat platform will actually accept.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path

from .library import _data_dir

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus"}
MEDIA_SUFFIXES = VIDEO_SUFFIXES | IMAGE_SUFFIXES | AUDIO_SUFFIXES
MAX_MB = int(os.environ.get("HERMES_LOVER_VIDEO_MAX_MB", "30"))
MAX_BYTES = MAX_MB * 1024 * 1024
# Generated media is a cache, not an archive: keep it long enough to re-send, then drop it.
KEEP_DAYS = int(os.environ.get("HERMES_LOVER_OUTBOX_KEEP_DAYS", "30"))


def root_dir() -> Path:
    """The operator's shared folder (created on first use)."""
    raw = os.environ.get("HERMES_LOVER_OUTBOX")
    root = Path(raw).expanduser() if raw else _data_dir() / "outbox"
    root.mkdir(parents=True, exist_ok=True)
    return root


def chat_dir(chat_id: str) -> Path:
    """The folder one conversation's generated media lives in."""
    root = root_dir()
    key = hashlib.sha1(str(chat_id or "").encode("utf-8")).hexdigest()[:16]
    path = root / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve(name: str, chat_id: str = "") -> Path:
    """Absolute path of ``name`` this chat may send, or ``ValueError`` saying why not.

    A chat sees its own generated media plus files the operator deliberately drops
    in the shared outbox root; another chat's folder is not reachable.
    """
    raw = str(name or "").strip().strip("\"'")
    if not raw:
        raise ValueError("which video? give the file name inside the outbox folder")
    parts = Path(raw).parts
    if raw.startswith(("~", "/")) or os.path.isabs(raw) or ".." in parts:
        raise ValueError("give only the file name; media is sent from the outbox folder")
    if len(parts) != 1:  # one flat name: no subfolders, so no chat's folder is reachable
        raise ValueError("give only the file name; media is sent from the outbox folder")
    root = root_dir().resolve()
    bases = [chat_dir(chat_id).resolve(), root] if chat_id else [root]
    for base in bases:
        candidate = (base / raw).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            raise ValueError("that path leads outside the outbox folder") from None
        if not candidate.is_file():
            continue
        if candidate.suffix.lower() not in MEDIA_SUFFIXES:
            raise ValueError("only videos, images and audio can be sent")
        size = candidate.stat().st_size
        if size > MAX_BYTES:
            raise ValueError(f"the video is {size / 1024 / 1024:.1f} MB; QQ takes at most {MAX_MB} MB")
        return candidate
    raise ValueError(f"no such file: {raw} (put it in {root} first)")


def _sweep(folder: Path, now: float) -> None:
    """Delete this folder's generated media older than KEEP_DAYS; never touch symlinks."""
    if KEEP_DAYS <= 0:
        return
    cutoff = now - KEEP_DAYS * 86400
    try:
        entries = list(folder.iterdir())
    except OSError:
        return
    for item in entries:
        try:
            if item.is_symlink() or not item.is_file():
                continue
            if item.suffix.lower() not in MEDIA_SUFFIXES:
                continue
            if item.stat().st_mtime < cutoff:
                item.unlink()
        except OSError:
            continue


def write_bytes(name: str, data: bytes, chat_id: str = "") -> Path:
    """Save generated media atomically in the folder of the chat that asked for it."""
    folder = chat_dir(chat_id) if chat_id else root_dir()
    target = folder / Path(str(name)).name
    if target.suffix.lower() not in MEDIA_SUFFIXES:
        raise ValueError("only videos, images and audio can be stored")
    # A same-uid attacker can pre-place a symlink at the target name; writing through it
    # would land outside the outbox. Refuse the link instead of resolving it.
    if target.is_symlink():
        raise ValueError("refusing to write through a symlink in the outbox")
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, suffix=target.suffix, delete=False) as handle:
            handle.write(data)
            temp = Path(handle.name)
        os.replace(temp, target)
        temp = None
    finally:
        if temp is not None:
            try:
                temp.unlink()
            except OSError:
                pass
    if chat_id:
        _sweep(folder, time.time())
    return target.resolve()
