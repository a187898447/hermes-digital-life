"""MiniMax media calls (video, image, speech, speech-to-text) for one chat at a time.

The model never touches the network here: it writes a prompt or names an audio file,
and this module turns that into an mp4/jpeg/mp3/text. Video and long-form speech are
asynchronous on MiniMax's side; images and transcripts finish inside the call.
"""

from __future__ import annotations

import base64
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


class MiniMaxError(RuntimeError):
    pass


# Older call sites and tests know this module by the video model's name.
HailuoError = MiniMaxError


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
        raise MiniMaxError("no MiniMax key is configured (MINIMAX_CN_API_KEY)")
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
        raise MiniMaxError(f"MiniMax HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise MiniMaxError(f"MiniMax is unreachable: {exc}") from exc
    try:
        parsed = json.loads(body or b"{}")
    except ValueError as exc:
        raise MiniMaxError("MiniMax returned something that is not JSON") from exc
    base = parsed.get("base_resp") or {}
    if isinstance(base, dict) and base.get("status_code") not in (None, 0):
        raise MiniMaxError(f"MiniMax error {base.get('status_code')}: {base.get('status_msg')}")
    return parsed if isinstance(parsed, dict) else {}


def create(prompt: str, *, duration: int = 6, resolution: str = "768P") -> tuple[str, str]:
    """Submit one text-to-video task and return its task id."""
    text = " ".join(str(prompt or "").split())
    if not text:
        raise MiniMaxError("the prompt is empty")
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
        raise MiniMaxError("no MiniMax key is configured (MINIMAX_CN_API_KEY)")
    last: Exception | None = None
    for key in available:
        try:
            data = _request("/v1/video_generation", key=key, payload=payload)
        except MiniMaxError as exc:
            last = exc
            continue
        task_id = str(data.get("task_id") or "").strip()
        if task_id:
            return task_id, key
        last = MiniMaxError("MiniMax did not return a task id")
    raise last or MiniMaxError("MiniMax did not return a task id")


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
                raise MiniMaxError("the task succeeded without a file id")
            return state["file_id"]
        if last in ("Fail", "Failed"):
            raise MiniMaxError("MiniMax could not generate this video")
        sleep(POLL_SECONDS)
    raise MiniMaxError(f"still {last or 'queued'} after {TIMEOUT_SECONDS}s")


def download(file_id: str, *, key: str = "", name: str = "") -> Path:
    data = _request(f"/v1/files/retrieve?file_id={urllib.request.quote(file_id)}", key=key)
    url = str(((data.get("file") or {}).get("download_url")) or "").strip()
    if not url:
        raise MiniMaxError("MiniMax returned no download url")
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=180) as response:
        body = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(body) > MAX_DOWNLOAD_BYTES:
        raise MiniMaxError("the generated video is larger than 200 MB")
    filename = name or f"hailuo-{file_id[:12]}.mp4"
    return outbox.write_bytes(filename, body)


IMAGE_MODEL = os.environ.get("HERMES_LOVER_IMAGE_MODEL", "image-01")
IMAGE_RATIOS = ("1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9")
IMAGE_MAX_BYTES = 20 * 1024 * 1024
ASR_MODEL = os.environ.get("HERMES_LOVER_ASR_MODEL", "asr-1.0")
ASR_MAX_BYTES = 50 * 1024 * 1024
ASR_MAX_SECONDS = 500
AUDIO_SUFFIXES = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".aiff", ".alac")


TTS_MODEL = os.environ.get("HERMES_LOVER_TTS_MODEL", "speech-2.8-hd")
TTS_VOICES = ("female-shaonv", "female-yujie", "female-chengshu", "female-tianmei",
              "male-qn-qingse", "male-qn-jingying", "audiobook_male_1", "audiobook_female_1")
TTS_MAX_CHARS = 5000


def create_speech(text: str, *, voice: str = "female-shaonv", speed: float = 1.0,
                  timeout: float = TIMEOUT_SECONDS, sleep=time.sleep) -> Path:
    """Speak one piece of text into an mp3 in the outbox (async task + poll)."""
    body = " ".join(str(text or "").split())
    if not body:
        raise MiniMaxError("nothing to say")
    if len(body) > TTS_MAX_CHARS:
        raise MiniMaxError(f"that is {len(body)} characters; one clip at most {TTS_MAX_CHARS}")
    speed = min(2.0, max(0.5, float(speed or 1.0)))
    payload = {
        "model": TTS_MODEL,
        "text": body,
        "language_boost": "auto",
        "voice_setting": {"voice_id": voice or "female-shaonv", "speed": speed,
                          "vol": 1, "pitch": 0},
        "audio_setting": {"audio_sample_rate": 32000, "bitrate": 128000,
                          "format": "mp3", "channel": 1},
    }
    if not keys():
        raise MiniMaxError("no MiniMax key is configured (MINIMAX_CN_API_KEY)")
    last_error: Exception | None = None
    key = ""
    for key in keys():
        try:
            created = _request("/v1/t2a_async_v2", key=key, payload=payload, timeout=60.0)
            break
        except MiniMaxError as exc:
            last_error = exc
    else:
        raise last_error or MiniMaxError("no key could start a speech task")
    task_id = str((created.get("task_id") or (created.get("data") or {}).get("task_id") or "")).strip()
    if not task_id:
        raise MiniMaxError("MiniMax returned no task id")
    deadline = time.time() + max(60.0, float(timeout))
    file_id = ""
    while time.time() < deadline:
        answer = _request(f"/v1/query/t2a_async_query_v2?task_id={urllib.request.quote(task_id)}",
                          key=key, timeout=60.0)
        status = str(answer.get("status") or "").strip().lower()
        if status == "success":
            file_id = str(answer.get("file_id") or "").strip()
            break
        if status in ("failed", "expired"):
            raise MiniMaxError(f"the speech task {status}")
        sleep(POLL_SECONDS)
    if not file_id:
        raise MiniMaxError("the speech task did not finish in time")
    return _download_file(file_id, key=key)


def _download_file(file_id: str, *, key: str = "") -> Path:
    url = f"{API}/v1/files/retrieve_content?file_id={urllib.request.quote(file_id)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=180) as response:
        body = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(body) > MAX_DOWNLOAD_BYTES:
        raise MiniMaxError("the generated audio is larger than 200 MB")
    return outbox.write_bytes(f"speech-{int(time.time())}-{file_id[-6:]}.mp3", body)


def create_image(prompt: str, *, aspect_ratio: str = "1:1", reference: str = "") -> Path:
    """Text-to-image (or image-to-image with one reference URL); returns the saved jpeg."""
    text = " ".join(str(prompt or "").split())
    if not text:
        raise MiniMaxError("the prompt is empty")
    if aspect_ratio not in IMAGE_RATIOS:
        aspect_ratio = "1:1"
    payload: dict = {
        "model": IMAGE_MODEL,
        "prompt": text[:1500],
        "aspect_ratio": aspect_ratio,
        "response_format": "base64",
        "n": 1,
    }
    if reference.strip():
        payload["subject_reference"] = [{"type": "character", "image_file": reference.strip()}]
    available = keys()
    if not available:
        raise MiniMaxError("no MiniMax key is configured (MINIMAX_CN_API_KEY)")
    last: Exception | None = None
    for key in available:
        try:
            data = _request("/v1/image_generation", key=key, payload=payload, timeout=180)
        except MiniMaxError as exc:
            last = exc
            continue
        images = ((data.get("data") or {}).get("image_base64")) or []
        if not isinstance(images, list) or not images:
            last = MiniMaxError("MiniMax returned no image")
            continue
        try:
            raw = base64.b64decode(images[0])
        except (ValueError, TypeError) as exc:
            last = MiniMaxError(f"MiniMax returned an unreadable image: {exc}")
            continue
        if len(raw) > IMAGE_MAX_BYTES:
            last = MiniMaxError("the generated image is larger than 20 MB")
            continue
        return outbox.write_bytes(f"image-{int(time.time())}-{abs(hash(text)) % 10000:04d}.jpeg", raw)
    raise last or MiniMaxError("MiniMax returned no image")


def _multipart(fields: dict[str, str], *, filename: str, payload: bytes, mime: str) -> tuple[bytes, str]:
    boundary = f"----hermeslover{int(time.time() * 1000):x}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    chunks.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n".encode())
    chunks.append(payload)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _mime_for(path: Path) -> str:
    suffix = path.suffix.lower()
    return {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
            ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg",
            ".opus": "audio/ogg", ".aiff": "audio/aiff", ".alac": "audio/mp4"}.get(suffix, "application/octet-stream")


def transcribe(path: Path, *, language: str = "") -> dict:
    """Speech-to-text for one audio file; returns {"text", "duration", "model"}."""
    target = Path(path)
    if not target.is_file():
        raise MiniMaxError(f"no such audio file: {target.name}")
    if target.suffix.lower() not in AUDIO_SUFFIXES:
        raise MiniMaxError("only wav/mp3/m4a/aac/flac/ogg/opus/aiff audio can be transcribed")
    size = target.stat().st_size
    if size > ASR_MAX_BYTES:
        raise MiniMaxError("the audio is larger than the 50 MB limit")
    body, content_type = _multipart({"model": ASR_MODEL}, filename=target.name,
                                    payload=target.read_bytes(), mime=_mime_for(target))
    available = keys()
    if not available:
        raise MiniMaxError("no MiniMax key is configured (MINIMAX_CN_API_KEY)")
    last: Exception | None = None
    for key in available:
        request = urllib.request.Request(f"{API}/v1/speech_to_text", data=body, method="POST")
        request.add_header("Authorization", f"Bearer {key}")
        request.add_header("Content-Type", content_type)
        if language.strip():
            request.add_header("language", language.strip())
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                parsed = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            last = MiniMaxError(f"MiniMax HTTP {exc.code}: {detail}")
            continue
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = MiniMaxError(f"MiniMax is unreachable: {exc}")
            continue
        base = parsed.get("base_resp") or {}
        if isinstance(base, dict) and base.get("status_code") not in (None, 0):
            last = MiniMaxError(f"MiniMax error {base.get('status_code')}: {base.get('status_msg')}")
            continue
        text = str(parsed.get("text") or "").strip()
        if not text:
            last = MiniMaxError("MiniMax heard nothing in that audio")
            continue
        return {"text": text, "duration": parsed.get("duration") or 0, "model": ASR_MODEL}
    raise last or MiniMaxError("MiniMax could not transcribe that audio")
