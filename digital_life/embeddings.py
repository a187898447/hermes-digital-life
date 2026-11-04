"""Optional text embeddings for hybrid recall.

Every path here is best-effort: a missing package, a missing key, an offline
machine or a slow endpoint must leave memory recall on plain FTS5. Nothing in
this module raises to the caller.
"""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_API_MODEL = "text-embedding-3-small"
_UNSET = object()
_EMBEDDER = _UNSET


def normalize(vector):
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm or not math.isfinite(norm):
        return None
    return [value / norm for value in vector]


class LocalEmbedder:
    """sentence-transformers running fully offline; the model must already be cached."""

    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        self.model = model
        self._encoder = SentenceTransformer(model, local_files_only=True)

    def __repr__(self) -> str:
        return f"LocalEmbedder({self.model})"

    def embed(self, texts):
        try:
            vectors = self._encoder.encode(list(texts), normalize_embeddings=True,
                                           batch_size=16).tolist()
        except Exception:
            logger.warning("Local embedding failed; memory recall stays on FTS", exc_info=True)
            return None
        return vectors if len(vectors) == len(texts) else None


class ApiEmbedder:
    """OpenAI-compatible /embeddings endpoint from the host profile .env."""

    def __init__(self, url: str, key: str, model: str, timeout: float = 2.0):
        self.url = url
        self.key = key
        self.model = model
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"ApiEmbedder({self.model})"

    def embed(self, texts):
        body = json.dumps({"model": self.model, "input": list(texts)}).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            rows = sorted(payload["data"], key=lambda item: item.get("index", 0))
            vectors = [[float(value) for value in row["embedding"]] for row in rows]
        except Exception:
            logger.warning("Embedding API unavailable; memory recall stays on FTS", exc_info=True)
            return None
        return vectors if len(vectors) == len(texts) else None


def _read_env(paths):
    values = {}
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            values.setdefault(name.strip(), value.strip().strip("'\""))
    return values


def _profile_env():
    home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    paths = []
    profile = os.environ.get("HERMES_PROFILE", "")
    try:
        active = (home / "active_profile").read_text(encoding="utf-8").strip()
    except OSError:
        active = ""
    for name in (profile, active, "default"):
        if name:
            paths.append(home / "profiles" / name / ".env")
    paths.append(home / ".env")
    return _read_env(paths)


def _join_url(base: str) -> str:
    base = base.rstrip("/")
    return base if base.endswith("/embeddings") else base + "/embeddings"


def _build():
    model = os.environ.get("HERMES_LOVER_EMBED_MODEL", DEFAULT_MODEL)
    try:
        logger.info("Trying local embedding model %s", model)
        return LocalEmbedder(model)
    except Exception:
        logger.info("Local embedding model unavailable; trying the host embedding API")

    env = _profile_env()

    def setting(*names):
        for name in names:
            value = os.environ.get(name) or env.get(name)
            if value:
                return value
        return ""

    url = setting("EMBEDDING_API_URL")
    base = setting("EMBEDDING_API_BASE", "OPENAI_BASE_URL")
    key = setting("EMBEDDING_API_KEY", "OPENAI_API_KEY")
    model = setting("EMBEDDING_MODEL", "OPENAI_EMBEDDING_MODEL") or DEFAULT_API_MODEL
    if url or (base and key):
        try:
            return ApiEmbedder(url or _join_url(base), key, model)
        except Exception:
            logger.warning("Embedding API configuration failed; memory recall stays on FTS",
                           exc_info=True)
    return None


def default_embedder():
    """Return one process-wide embedder, or None when the environment has none."""
    global _EMBEDDER
    if _EMBEDDER is _UNSET:
        try:
            _EMBEDDER = _build()
        except Exception:
            logger.warning("Embedding setup failed; memory recall stays on FTS", exc_info=True)
            _EMBEDDER = None
    return _EMBEDDER
