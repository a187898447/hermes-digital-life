import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "qzone-bridge.py"


def load_bridge():
    spec = importlib.util.spec_from_file_location("qzone_bridge_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge()

    def test_tokens_must_match_exactly(self):
        self.assertTrue(self.bridge.authorized("abc", "abc"))
        self.assertFalse(self.bridge.authorized("abc", "abd"))
        self.assertFalse(self.bridge.authorized("", "abc"))
        self.assertFalse(self.bridge.authorized(None, "abc"))
        self.assertFalse(self.bridge.authorized("abc", ""))

    def test_ensure_token_writes_a_private_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token"
            with patch.dict(os.environ, {"QZONE_BRIDGE_TOKEN_FILE": str(path)}):
                token = self.bridge.ensure_token()
                self.assertEqual(len(token), 64)
                self.assertEqual(path.read_text(), token)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(self.bridge.ensure_token(), token)


class ImageGuardTests(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge()

    def _resolve_to(self, ip):
        return lambda *args, **kwargs: [(2, 1, 6, "", (ip, 443))]

    def test_only_qq_image_hosts_over_https(self):
        public = self._resolve_to("1.2.3.4")
        with patch.object(self.bridge.socket, "getaddrinfo", public):
            self.assertTrue(self.bridge.image_url_ok("https://a.qpic.cn/x.jpg"))
            self.assertTrue(self.bridge.image_url_ok("https://qlogo.cn/x.png"))
            self.assertFalse(self.bridge.image_url_ok("http://a.qpic.cn/x.jpg"))
            self.assertFalse(self.bridge.image_url_ok("https://evil.example/x.jpg"))
            self.assertFalse(self.bridge.image_url_ok("https://qpic.cn.evil.example/x.jpg"))

    def test_private_addresses_are_refused_even_for_allowed_hosts(self):
        for ip in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1"):
            with patch.object(self.bridge.socket, "getaddrinfo", self._resolve_to(ip)):
                self.assertFalse(self.bridge.image_url_ok("https://a.qpic.cn/x.jpg"), ip)

    def test_oversized_or_disallowed_images_are_not_fetched(self):
        self.assertLessEqual(self.bridge.MAX_IMAGE_BYTES, 5 * 1024 * 1024)
        with patch.object(self.bridge, "image_url_ok", return_value=False):
            self.assertIsNone(self.bridge.fetch_image("https://a.qpic.cn/x.jpg"))


class HandlerAuthTests(unittest.TestCase):
    def _serve(self):
        bridge = load_bridge()
        bridge.Handler.token = "secret"
        server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return server, bridge

    def test_requests_without_the_token_are_rejected(self):
        server, bridge = self._serve()
        url = f"http://127.0.0.1:{server.server_port}/health"
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(url, timeout=3)
        self.assertEqual(raised.exception.code, 401)
        request = urllib.request.Request(url, headers={"X-Bridge-Token": "wrong"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 401)

    def test_requests_with_the_token_are_served(self):
        server, bridge = self._serve()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/health",
            headers={"X-Bridge-Token": "secret"})
        payload = json.loads(urllib.request.urlopen(request, timeout=3).read())
        self.assertTrue(payload["ok"])
