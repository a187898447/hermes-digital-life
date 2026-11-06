"""MiniMax Hailuo text-to-video, wrapped for one bad idea at a time.

The model never touches the network here: it writes a prompt, and this module turns
that into one mp4 in the outbox. Generation is asynchronous on MiniMax's side, so the
tool call returns a task id immediately and a worker thread finishes the job, drops the
file in the outbox, and sends it to the chat that asked.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import outbox

API = os.environ.get("MINIMAX_VIDEO_API", "https://api.minimax.cn").rstrip("/")
MODEL = os.environ.get("HERMES_LOVER_VIDEO_MODEL", "MiniMax-Hailuo-2.3")
SUPPORTED_DURATIONS = (6, 10)
SUPPORTED_RESOLUTIONS = ("768P", "1080P")
POLL_SECONDS = 10
TIMEOUT_SECONDS = int(os.environ.get("HERMES_LOVER_VIDEO_TIMEOUT", "900"))
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024


class HailuoError(RuntimeError):
    pass


KEY_ENVS = ("MINIMAX_CN_API_KEY", "MINIMAX_CN_2_API_KEY", "MINIMAX_H3_API_KEY", "MINIMAX_API_KEY")


def keys() -> list[str]:
    """Every configured key, in the order we should spend them."""
    seen: list[str] = []
    for name in KEY_ENVS:
        value = (os.environ.get(name) or "").strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def api_key() -> str:
    found = keys()
    if not found:
        raise HailuoError("no MiniMax key is configured (MINIMAX_CN_API_KEY)")
    return found[0]


def _request(path: str, *, key: str = "", payload: dict | None = None,
             timeout: float = 60.0) -> dict:
    url = f"{API}{path}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    request.add_header("Authorization", f"Bearer {key or api_key()}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        raise HailuoError(f"MiniMax HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise HailuoError(f"MiniMax is unreachable: {exc}") from exc
    try:
        parsed = json.loads(body or b"{}")
    except ValueError as exc:
        raise HailuoError("MiniMax returned something that is not JSON") from exc
    base = parsed.get("base_resp") or {}
    if isinstance(base, dict) and base.get("status_code") not in (None, 0):
        raise HailuoError(f"MiniMax error {base.get('status_code')}: {base.get('status_msg')}")
    return parsed if isinstance(parsed, dict) else {}


def create(prompt: str, *, duration: int = 6, resolution: str = "768P") -> tuple[str, str]:
    """Submit one text-to-video task and return its task id."""
    text = " ".join(str(prompt or "").split())
    if not text:
        raise HailuoError("the prompt is empty")
    if duration not in SUPPORTED_DURATIONS:
        duration = 6
    if resolution not in SUPPORTED_RESOLUTIONS:
        resolution = "768P"
    payload = {
        "model": MODEL,
        "prompt": text[:1500],
        "duration": duration,
        "resolution": resolution,
        "prompt_optimizer": True,
    }
    available = keys()
    if not available:
        raise HailuoError("no MiniMax key is configured (MINIMAX_CN_API_KEY)")
    last: Exception | None = None
    for key in available:
        try:
            data = _request("/v1/video_generation", key=key, payload=payload)
        except HailuoError as exc:
            last = exc
            continue
        task_id = str(data.get("task_id") or "").strip()
        if task_id:
            return task_id, key
        last = HailuoError("MiniMax did not return a task id")
    raise last or HailuoError("MiniMax did not return a task id")


def status(task_id: str, *, key: str = "") -> dict:
    data = _request(f"/v1/query/video_generation?task_id={urllib.request.quote(task_id)}", key=key)
    return {"status": str(data.get("status") or ""), "file_id": str(data.get("file_id") or "")}


def wait(task_id: str, *, key: str = "", timeout: float | None = None,
         sleep=time.sleep) -> str:
    """Block until the task succeeds; return its file id."""
    deadline = time.time() + (timeout if timeout is not None else TIMEOUT_SECONDS)
    last = ""
    while time.time() < deadline:
        state = status(task_id, key=key)
        last = state["status"] or last
        if last == "Success":
            if not state["file_id"]:
                raise HailuoError("the task succeeded without a file id")
            return state["file_id"]
        if last in ("Fail", "Failed"):
            raise HailuoError("MiniMax could not generate this video")
        sleep(POLL_SECONDS)
    raise HailuoError(f"still {last or 'queued'} after {TIMEOUT_SECONDS}s")


def download(file_id: str, *, key: str = "", name: str = "") -> Path:
    data = _request(f"/v1/files/retrieve?file_id={urllib.request.quote(file_id)}", key=key)
    url = str(((data.get("file") or {}).get("download_url")) or "").strip()
    if not url:
        raise HailuoError("MiniMax returned no download url")
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=180) as response:
        body = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(body) > MAX_DOWNLOAD_BYTES:
        raise HailuoError("the generated video is larger than 200 MB")
    filename = name or f"hailuo-{file_id[:12]}.mp4"
    return outbox.write_bytes(filename, body)
