import socket
import unittest
import urllib.request
from unittest import mock

from digital_life import netguard

PUBLIC = "93.184.216.34"


def _resolves_to(ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET

    def fake(host, port, *args, **kwargs):
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]

    return fake


class ValidateTests(unittest.TestCase):
    def test_public_https_is_allowed(self):
        with mock.patch.object(netguard.socket, "getaddrinfo", _resolves_to(PUBLIC)):
            self.assertEqual(netguard.validate("https://example.com/post"), "https://example.com/post")

    def test_public_host_on_8080_is_allowed(self):
        with mock.patch.object(netguard.socket, "getaddrinfo", _resolves_to(PUBLIC)):
            netguard.validate("http://example.com:8080/feed")

    def test_loopback_private_and_link_local_are_refused(self):
        for url in ("http://127.0.0.1:8765/feed",
                    "http://10.1.2.3/",
                    "http://192.168.0.2/",
                    "http://169.254.169.254/latest/meta-data",
                    "http://[::1]/",
                    "http://0.0.0.0/"):
            with self.subTest(url=url), self.assertRaises(netguard.BlockedURL):
                netguard.validate(url)

    def test_hostname_resolving_to_private_is_refused(self):
        with mock.patch.object(netguard.socket, "getaddrinfo", _resolves_to("127.0.0.1")):
            with self.assertRaisesRegex(netguard.BlockedURL, "non-public"):
                netguard.validate("http://looks-public.example/")

    def test_userinfo_is_refused(self):
        with mock.patch.object(netguard.socket, "getaddrinfo", _resolves_to(PUBLIC)):
            with self.assertRaises(netguard.BlockedURL):
                netguard.validate("https://user:pass@example.com/")

    def test_only_http_ports_are_allowed(self):
        for url in ("https://example.com:2222/", "http://example.com:6379/"):
            with self.subTest(url=url), self.assertRaises(netguard.BlockedURL):
                netguard.validate(url)

    def test_non_http_schemes_are_refused(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "data:text/plain,hi"):
            with self.subTest(url=url), self.assertRaises(netguard.BlockedURL):
                netguard.validate(url)

    def test_empty_url_is_refused(self):
        with self.assertRaises(netguard.BlockedURL):
            netguard.validate("   ")


class RedirectTests(unittest.TestCase):
    def test_redirect_to_private_address_is_refused(self):
        guard = netguard._GuardedRedirect()
        request = urllib.request.Request("https://example.com/")
        with self.assertRaises(netguard.BlockedURL):
            guard.redirect_request(request, None, 302, "Found", {}, "http://127.0.0.1/")

    def test_redirect_hops_are_capped(self):
        guard = netguard._GuardedRedirect(max_redirects=1)
        request = urllib.request.Request("https://example.com/")
        with mock.patch.object(netguard, "validate", lambda url: url), \
                mock.patch.object(urllib.request.HTTPRedirectHandler, "redirect_request", lambda *a: None):
            guard.redirect_request(request, None, 302, "", {}, "https://a.example/")
            with self.assertRaisesRegex(netguard.BlockedURL, "too many redirects"):
                guard.redirect_request(request, None, 302, "", {}, "https://b.example/")


class _Response:
    def __init__(self, body):
        self.body = body

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def geturl(self):
        return "https://example.com/"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FetchTests(unittest.TestCase):
    def test_body_is_capped_and_truncation_reported(self):
        opener = mock.MagicMock()
        opener.open.return_value = _Response(b"abcdef")
        with mock.patch.object(netguard.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(netguard, "validate", lambda url: url):
            body, final, truncated = netguard.fetch_public("https://example.com/", max_bytes=4)
        self.assertEqual(body, b"abcd")
        self.assertEqual(final, "https://example.com/")
        self.assertTrue(truncated)

    def test_small_body_is_not_truncated(self):
        opener = mock.MagicMock()
        opener.open.return_value = _Response(b"ab")
        with mock.patch.object(netguard.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(netguard, "validate", lambda url: url):
            body, _final, truncated = netguard.fetch_public("https://example.com/", max_bytes=4)
        self.assertEqual(body, b"ab")
        self.assertFalse(truncated)


if __name__ == "__main__":
    unittest.main()
