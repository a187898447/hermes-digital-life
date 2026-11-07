"""Hand the user one finished video, without giving the model the filesystem.

A generator (or the operator) drops finished clips into the outbox folder; the agent
only names one of those files. Everything here is about keeping that sentence true:
no absolute paths, no ``..``, no symlinks leading out, no other file types, and a
size cap the chat platform will actually accept.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .library import _data_dir

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus"}
MEDIA_SUFFIXES = VIDEO_SUFFIXES | IMAGE_SUFFIXES | AUDIO_SUFFIXES
MAX_MB = int(os.environ.get("HERMES_LOVER_VIDEO_MAX_MB", "30"))
MAX_BYTES = MAX_MB * 1024 * 1024


def root_dir() -> Path:
    """The one folder the agent may send from (created on first use)."""
    raw = os.environ.get("HERMES_LOVER_OUTBOX")
    root = Path(raw).expanduser() if raw else _data_dir() / "outbox"
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve(name: str) -> Path:
    """Absolute path of ``name`` inside the outbox, or ``ValueError`` saying why not."""
    raw = str(name or "").strip().strip("\"'")
    if not raw:
        raise ValueError("which video? give the file name inside the outbox folder")
    parts = Path(raw).parts
    if raw.startswith(("~", "/")) or os.path.isabs(raw) or ".." in parts:
        raise ValueError("give only the file name; media is sent from the outbox folder")
    root = root_dir().resolve()
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError("that path leads outside the outbox folder") from None
    if not candidate.is_file():
        raise ValueError(f"no such file: {raw} (put it in {root} first)")
    if candidate.suffix.lower() not in MEDIA_SUFFIXES:
        raise ValueError("only videos, images and audio can be sent")
    size = candidate.stat().st_size
    if size > MAX_BYTES:
        raise ValueError(f"the video is {size / 1024 / 1024:.1f} MB; QQ takes at most {MAX_MB} MB")
    return candidate


def write_bytes(name: str, data: bytes) -> Path:
    """Save a generated clip into the outbox atomically (for future generators)."""
    root = root_dir()
    target = (root / Path(str(name)).name).resolve()
    if target.suffix.lower() not in MEDIA_SUFFIXES:
        raise ValueError("only videos, images and audio can be stored")
    with tempfile.NamedTemporaryFile(dir=root, suffix=target.suffix, delete=False) as handle:
        handle.write(data)
        temp = Path(handle.name)
    os.replace(temp, target)
    return target
