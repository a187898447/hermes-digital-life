#!/usr/bin/env python3
"""Read-only QQ Zone bridge: holds one QQ account's cookies, serves feed over localhost.

Usage:
  python3 qzone-bridge.py login          # scan a QR once, write cookies
  python3 qzone-bridge.py serve [--port]  # serve GET /health and GET /feed
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.cookiejar
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
COOKIE_PATH = Path(os.environ.get("QZONE_COOKIE_FILE") or (HOME / "qzone-cookies.json"))
PORT = int(os.environ.get("QZONE_BRIDGE_PORT") or 8765)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

QR_PNG = HOME / "qzone-qr.png"

# The bridge listens on loopback, but loopback is not an authentication boundary: any
# local process (or a DNS-rebinding page) could read a private timeline. Every request
# therefore carries a token that only this user can read.
TOKEN_NAME = "qzone-bridge.token"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_HOSTS = ("qpic.cn", "qlogo.cn", "myqcloud.com")


def token_path() -> Path:
    return Path(os.environ.get("QZONE_BRIDGE_TOKEN_FILE")
                or (HOME / "plugin-data" / "hermes-lover" / TOKEN_NAME))


def ensure_token() -> str:
    path = token_path()
    try:
        existing = path.read_text("utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    return token


def authorized(given: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(str(given or ""), str(expected))


def image_url_ok(url: str) -> bool:
    """Only QQ image CDNs, only https, only addresses the public internet routes."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if not any(host == suffix or host.endswith("." + suffix) for suffix in IMAGE_HOSTS):
        return False
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, parsed.port or 443)}
    except OSError:
        return False
    if not addresses:
        return False
    for raw in addresses:
        try:
            if not ipaddress.ip_address(raw).is_global:
                return False
        except ValueError:
            return False
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


def fetch_image(url: str) -> bytes | None:
    if not image_url_ok(url):
        return None
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    opener_ = urllib.request.build_opener(_NoRedirect)
    try:
        with opener_.open(request, timeout=20) as response:
            raw = response.read(MAX_IMAGE_BYTES + 1)
    except (OSError, ValueError):
        return None
    return None if len(raw) > MAX_IMAGE_BYTES else raw


def hash33(text: str) -> int:
    value = 0
    for ch in text:
        value = ((value << 5) + value) + ord(ch)
        value &= 0x7FFFFFFF
    return value


def uin_of(cookies: dict) -> str:
    raw = cookies.get("uin") or cookies.get("pt2gguin") or ""
    return re.sub(r"\D", "", raw).lstrip("0")


def gtk(skey: str) -> int:
    value = 5381
    for ch in skey:
        value = ((value << 5) + value) + ord(ch)
        value &= 0x7FFFFFFF
    return value


def opener(cookies: dict[str, str]) -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    host = "qzone.qq.com"
    for name, value in cookies.items():
        jar.set_cookie(http.cookiejar.Cookie(
            0, name, value, None, False, host, False, False, "/", True, False, None,
            False, None, None, {}))
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def login(open_qr) -> int:
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA), ("Referer", "https://qzone.qq.com/")]
    stamp = int(time.time() * 1000)
    url = ("https://ssl.ptlogin2.qq.com/ptqrshow?appid=549000912&e=2&l=M&s=3&d=72&v=4"
           f"&t={stamp}&daid=5&pt_3rd_aid=0")
    try:
        data = op.open(url, timeout=20).read()
    except OSError as exc:
        print(f"读取二维码失败：{exc}")
        return 2
    QR_PNG.write_bytes(data)
    qrsig = next((c.value for c in cj if c.name == "qrsig"), "")
    if not qrsig:
        print("没有拿到 qrsig")
        return 2
    print(f"二维码已保存：{QR_PNG}")
    open_qr(str(QR_PNG))
    token = hash33(qrsig)
    poll = ("https://ssl.ptlogin2.qq.com/ptqrlogin?u1=https%3A%2F%2Fqzone.qq.com"
            f"&ptqrtoken={token}&ptredirect=1&h=1&t=1&g=1&from_ui=1&ptlang=2052"
            f"&action=0-0-{stamp}&js_ver=22071515&js_type=1&login_sig=&pt_uistyle=40"
            "&aid=549000912&daid=5&pt_3rd_aid=0&has_onekey=1")
    for _ in range(120):
        time.sleep(2)
        try:
            body = op.open(poll, timeout=20).read().decode("utf-8", "replace")
        except OSError:
            continue
        code = re.search(r"ptuiCB\('(\d+)'", body)
        if not code:
            continue
        if code.group(1) == "0":
            check = re.search(r"'(https?://[^']*check_sig[^']+)'", body)
            if check:
                try:
                    op.open(check.group(1).replace("\\x26", "&").replace("\\/", "/"), timeout=20).read(64)
                except OSError:
                    pass
            uin = uin_of({c.name: c.value for c in cj})
            if uin:
                try:
                    op.open(f"https://user.qzone.qq.com/{uin}", timeout=20).read(64)
                except OSError:
                    pass
            store = {c.name: c.value for c in cj}
            COOKIE_PATH.write_text(json.dumps(store, ensure_ascii=False))
            os.chmod(COOKIE_PATH, 0o600)
            print(f"登录成功，cookie 已写入 {COOKIE_PATH}")
            return 0
        if code.group(1) not in {"66", "67", "65"}:
            print(f"登录失败：{body[:200]}")
            return 2
    print("等待扫码超时")
    return 2


def load_cookies() -> dict[str, str]:
    try:
        data = json.loads(COOKIE_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


IMAGE_DIR = HOME / "qzone-images"
SEEN_PATH = HOME / "qzone-seen.json"
SEEN_CAP = 400
IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)


def load_seen() -> list[str]:
    try:
        data = json.loads(SEEN_PATH.read_text())
    except (OSError, ValueError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def save_seen(ids: list[str]) -> None:
    SEEN_PATH.write_text(json.dumps(ids[-SEEN_CAP:], ensure_ascii=False))


def _download_images(op, msg: dict, limit: int) -> list[str]:
    urls = []
    for match in IMG_RE.finditer(msg.get("content") or ""):
        urls.append(match.group(1).replace("&amp;", "&"))
    for pic in msg.get("pic") or []:
        if not isinstance(pic, dict):
            continue
        url = pic.get("url1") or pic.get("url2") or pic.get("pic_id") or pic.get("url")
        if url:
            urls.append(str(url).replace("http://", "https://", 1))
    paths = []
    for url in urls:
        if len(paths) >= limit:
            break
        if not url.startswith(("http://", "https://")):
            continue
        raw = fetch_image(url)
        if raw is None or len(raw) < 512:
            continue
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        suffix = ".png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
        name = hashlib.sha256(url.encode()).hexdigest()[:32] + suffix
        path = IMAGE_DIR / name
        if not path.exists():
            path.write_bytes(raw)
        paths.append(str(path))
    return paths


def _fetch_page(op, uin: str, cookies: dict, pos: int, num: int, with_images: bool):
    params = urllib.parse.urlencode({
        "uin": uin, "ftype": 0, "sort": 0, "pos": max(0, pos), "num": max(1, min(num, 40)),
        "replynum": 0, "g_tk": gtk(cookies.get("p_skey") or cookies["skey"]), "code": 0, "format": "jsonp",
        "callback": "_preloadCallback",
    })
    url = f"https://h5.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6?{params}"
    raw = op.open(url, timeout=20).read().decode("utf-8", "replace")
    match = re.search(r"^\s*_preloadCallback\((\{.*\})\)\s*;?\s*$", raw, re.S)
    if not match:
        raise ValueError("unexpected feed payload")
    payload = json.loads(match.group(1))
    items = []
    for msg in payload.get("msglist") or []:
        item = {
            "name": msg.get("name") or "",
            "time": msg.get("createTime") or "",
            "text": (msg.get("content") or "").strip()[:500],
            "comments": int(msg.get("cmtnum") or 0),
            "likes": int(msg.get("likenum") or 0),
            "_id": str(msg.get("tid") or ""),
        }
        if with_images:
            item["images"] = _download_images(op, msg, 2)
        items.append(item)
    return items, int(payload.get("total") or 0)


def fetch_feed(count: int = 5, with_images: bool = False, include_seen: bool = False, pos: int = 0) -> dict:
    cookies = load_cookies()
    if "skey" not in cookies or "uin" not in cookies:
        return {"ok": False, "error": "not logged in", "hint": "run: qzone-bridge.py login"}
    uin = uin_of(cookies)
    op = opener(cookies)
    op.addheaders = [("User-Agent", UA), ("Referer", f"https://user.qzone.qq.com/{uin}")]
    want = max(1, min(count, 10))
    seen = set(load_seen())
    items: list[dict] = []
    page = max(0, pos)
    total = 0
    pages = 0
    try:
        while len(items) < want and pages < 3:
            chunk, total = _fetch_page(op, uin, cookies, page, want, with_images)
            pages += 1
            if not chunk:
                break
            for item in chunk:
                if include_seen or item["_id"] not in seen:
                    items.append(item)
            page += len(chunk)
            if page >= total:
                break
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"feed request failed: {exc}"}
    fresh_ids = [item["_id"] for item in items if item["_id"]]
    if fresh_ids:
        save_seen(load_seen() + fresh_ids)
    fresh = items[:want]
    for item in items:
        item.pop("_id", None)
    return {"ok": True, "items": fresh, "total": total, "pos": page,
            "has_more": page < total, "scanned_pages": pages}


class Handler(BaseHTTPRequestHandler):
    token = ""

    def log_message(self, *_args):  # keep the bridge quiet
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - http.server API
        if not authorized(self.headers.get("X-Bridge-Token"), self.token):
            return self._json({"ok": False, "error": "unauthorized"}, 401)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            return self._json({"ok": True, "logged_in": bool(load_cookies())})
        if parsed.path == "/feed":
            q = urllib.parse.parse_qs(parsed.query)
            images = (q.get("images") or ["0"])[0] in ("1", "true", "yes")
            all_posts = (q.get("seen") or ["0"])[0] in ("1", "true", "yes")
            limit = max(1, min(int((q.get("count") or ["5"])[0]), 10))
            pos = max(0, int((q.get("pos") or ["0"])[0]))
            return self._json(fetch_feed(limit, images, all_posts, pos))
        self._json({"ok": False, "error": "not found"}, 404)


def serve(port: int) -> int:
    Handler.token = ensure_token()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"qzone-bridge listening on http://127.0.0.1:{port}")
    server.serve_forever()
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["login", "serve"])
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--open", default="open")
    args = parser.parse_args(argv)
    if args.command == "login":
        def _open(path: str) -> None:
            if sys.platform == "darwin" and args.open:
                os.system(f"{args.open} {json.dumps(path)}")
        return login(_open)
    return serve(args.port)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
