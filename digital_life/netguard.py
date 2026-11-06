"""Fetch only public URLs, and re-check every redirect hop.

The model calls these tools with text a stranger wrote, so a URL is untrusted input:
``http://127.0.0.1:8765/feed`` reads a local service, ``http://169.254.169.254`` reads
cloud metadata, and a public hostname can be a redirect hop away from either. Every
request made on the model's behalf goes through :func:`fetch_public`, which resolves the
host and refuses anything that is not globally routable.

Residual risk: the address is checked before the connection, so a DNS answer that
changes between the check and the connect (rebinding) is not caught. Pinning the
connection to the checked address would close that; it is not worth the complexity here
because the check already blocks the interesting targets.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

ALLOWED_SCHEMES = ("http", "https")
ALLOWED_PORTS = {80, 443, 8080}
MAX_REDIRECTS = 3
USER_AGENT = "hermes-lover/1.0"


class BlockedURL(ValueError):
    """The URL would reach something other than a public web server."""


def _addresses(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedURL(f"cannot resolve {host}: {exc}") from None
    found = []
    for info in infos:
        try:
            found.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not found:
        raise BlockedURL(f"cannot resolve {host}")
    return found


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return mapped.is_global
    return address.is_global


def validate(url: str) -> str:
    """Reject anything that is not an http(s) URL to a public address; return the URL."""
    text = str(url or "").strip()
    if not text:
        raise BlockedURL("url is required")
    parts = urllib.parse.urlsplit(text)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise BlockedURL(f"only http(s) links can be read: {text[:80]}")
    if parts.username or parts.password:
        raise BlockedURL("links with credentials are refused")
    host = parts.hostname
    if not host:
        raise BlockedURL(f"no host in {text[:80]}")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError:
        raise BlockedURL(f"bad port in {text[:80]}") from None
    if port not in ALLOWED_PORTS:
        raise BlockedURL(f"port {port} is not allowed (80, 443, 8080)")
    for address in _addresses(host, port):
        if not _is_public(address):
            raise BlockedURL(f"{host} resolves to a non-public address ({address}); "
                             "local and private hosts cannot be read")
    return text


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Re-validate every hop: a public link may redirect to a private one."""

    def __init__(self, max_redirects: int = MAX_REDIRECTS) -> None:
        self.max_redirects = max_redirects
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops += 1
        if self.hops > self.max_redirects:
            raise BlockedURL(f"too many redirects (>{self.max_redirects})")
        validate(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_public(url: str, *, max_bytes: int, timeout: float = 20.0) -> tuple[bytes, str, bool]:
    """Fetch ``url`` after :func:`validate`, returning (body, final_url, truncated)."""
    validate(url)
    op = urllib.request.build_opener(_GuardedRedirect())
    op.addheaders = [("User-Agent", USER_AGENT)]
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with op.open(request, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
        return body[:max_bytes], response.geturl(), len(body) > max_bytes
