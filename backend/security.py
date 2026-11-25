"""SSRF-safe URL validation, safe HTTP fetching and per-domain rate limiting.

Every outbound request made by the crawler goes through :func:`safe_get`, which
validates the initial URL, every redirect hop, the resolved IP addresses and the
response size before any content is handed to the parser.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlsplit

import httpx

USER_AGENT = "LeadScopeBot/1.0 (+business email discovery; respects robots.txt)"

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {None, 80, 443}
REDIRECT_CODES = {301, 302, 303, 307, 308}
DNS_CACHE_TTL = 300.0

BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "instance-data.ec2.internal",
    "kubernetes.default",
    "kubernetes.default.svc",
}

_BLOCKED_CIDRS = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "255.255.255.255/32",
    "::/128",
    "::1/128",
    "64:ff9b::/96",
    "100::/64",
    "2001:db8::/32",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
)
BLOCKED_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in _BLOCKED_CIDRS)
BLOCKED_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("192.0.0.192"),
}

_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TLD_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})$")
_DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})$"
)


class UnsafeURLError(ValueError):
    """Raised when a URL targets a blocked scheme, host or address."""


class FetchError(Exception):
    """Raised when a page cannot be fetched safely."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class FetchResult:
    url: str
    status_code: int
    text: str
    content_type: str
    redirects: int


_dns_cache: dict[str, tuple[float, tuple[str, ...]]] = {}
_dns_lock = asyncio.Lock()


def _is_blocked_ip(address: ipaddress._BaseAddress) -> bool:
    if address in BLOCKED_IPS:
        return True
    for network in BLOCKED_NETWORKS:
        if address.version == network.version and address in network:
            return True
    return False


def _check_hostname_literal(hostname: str) -> None:
    host = hostname.strip().lower().rstrip(".")
    if not host:
        raise UnsafeURLError("URL is missing a hostname")
    if host in BLOCKED_HOSTNAMES:
        raise UnsafeURLError("This host is not allowed")
    if host.endswith((".localhost", ".local", ".internal", ".home.arpa")):
        raise UnsafeURLError("Private or local hostnames are not allowed")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return
    if _is_blocked_ip(address):
        raise UnsafeURLError("Private, loopback or metadata addresses are not allowed")


async def _resolve_host(hostname: str, port: int) -> tuple[str, ...]:
    key = f"{hostname}:{port}"
    now = time.monotonic()
    async with _dns_lock:
        cached = _dns_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except (socket.gaierror, OSError) as exc:
        raise UnsafeURLError("Hostname could not be resolved") from exc

    addresses = tuple(sorted({info[4][0] for info in infos}))
    if not addresses:
        raise UnsafeURLError("Hostname could not be resolved")
    for raw in addresses:
        if _is_blocked_ip(ipaddress.ip_address(raw)):
            raise UnsafeURLError("Hostname resolves to a private or blocked address")

    async with _dns_lock:
        _dns_cache[key] = (now + DNS_CACHE_TTL, addresses)
    return addresses


def _validate_structure(url: str) -> tuple[str, int]:
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("URL is required")
    if len(url) > 2048:
        raise UnsafeURLError("URL is too long")
    if any(ch in url for ch in ("\n", "\r", "\x00", "\t")):
        raise UnsafeURLError("URL contains invalid characters")

    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError("Only http and https URLs are allowed")
    if parts.username or parts.password:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError("Invalid port in URL") from exc
    if port not in ALLOWED_PORTS:
        raise UnsafeURLError("Only standard web ports are allowed")
    hostname = parts.hostname
    if not hostname:
        raise UnsafeURLError("URL is missing a hostname")
    _check_hostname_literal(hostname)

    try:
        ascii_host = hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise UnsafeURLError("Invalid hostname") from exc

    try:
        ipaddress.ip_address(ascii_host.strip("[]"))
    except ValueError:
        if not _DOMAIN_RE.match(ascii_host):
            raise UnsafeURLError("Invalid domain name")
    return ascii_host, port or (443 if scheme == "https" else 80)


async def validate_url(url: str) -> str:
    """Validate a URL for outbound fetching and return a normalised form."""
    hostname, port = _validate_structure(url)
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    addresses = await _resolve_host(hostname, port)
    for raw_address in addresses:
        if _is_blocked_ip(ipaddress.ip_address(raw_address)):
            raise UnsafeURLError("Hostname resolves to a private or blocked address")
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if parts.port and parts.port not in (80, 443):
        netloc = f"{netloc}:{parts.port}"
    path = parts.path or "/"
    return f"{scheme}://{netloc}{path}" + (f"?{parts.query}" if parts.query else "")


def normalize_domain_input(value: str) -> str:
    """Normalise a user-supplied domain or URL to a hostname (no DNS lookup)."""
    if not isinstance(value, str):
        raise ValueError("Domain must be a string")
    raw = value.strip()
    if not raw:
        raise ValueError("Domain is required")
    if len(raw) > 512:
        raise ValueError("Domain is too long")
    if any(ch in raw for ch in ("\n", "\r", "\x00", "\t", " ")):
        raise ValueError("Domain contains invalid characters")

    candidate = raw if "://" in raw else f"https://{raw}"
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError("Only http and https URLs are allowed")
    if parts.username or parts.password:
        raise ValueError("URLs with embedded credentials are not allowed")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid port in URL") from exc
    if port not in ALLOWED_PORTS:
        raise ValueError("Only standard web ports are allowed")
    hostname = parts.hostname
    if not hostname:
        raise ValueError("Invalid domain")

    host = hostname.lower().rstrip(".")
    _check_hostname_literal(host)
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Invalid domain name") from exc

    try:
        address = ipaddress.ip_address(ascii_host.strip("[]"))
    except ValueError:
        labels = ascii_host.split(".")
        if len(labels) < 2 or not all(_LABEL_RE.match(label) for label in labels):
            raise ValueError("Invalid domain name")
        if not _TLD_RE.match(labels[-1]):
            raise ValueError("Invalid top-level domain")
    else:
        if _is_blocked_ip(address):
            raise ValueError("Private, loopback or metadata addresses are not allowed")
    return ascii_host


def normalise_url_for_fetch(value: str) -> str:
    """Convert a user-supplied website value into a fetchable http(s) URL."""
    raw = value.strip()
    if not raw:
        raise ValueError("URL is required")
    if "://" not in raw:
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError("Only http and https URLs are allowed")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid port in URL") from exc
    if port not in ALLOWED_PORTS:
        raise ValueError("Only standard web ports are allowed")
    return raw


def _safe_text(value: str | None) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value or "")


async def safe_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float = 15.0,
    max_bytes: int = MAX_RESPONSE_BYTES,
    allowed_content_types: Iterable[str] = ("text/html", "application/xhtml+xml"),
    max_redirects: int = MAX_REDIRECTS,
) -> FetchResult:
    """Fetch a public web page with SSRF, redirect, size and content checks."""
    current = await validate_url(url)
    allowed = {item.lower() for item in allowed_content_types}
    redirects = 0

    for _ in range(max_redirects + 1):
        try:
            async with client.stream(
                "GET",
                current,
                follow_redirects=False,
                timeout=timeout,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                    "Accept-Language": "en;q=0.9",
                },
            ) as response:
                if response.status_code in REDIRECT_CODES:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("Redirect without a destination")
                    redirects += 1
                    if redirects > max_redirects:
                        raise FetchError("Too many redirects")
                    current = await validate_url(urljoin(current, location))
                    continue

                if response.status_code >= 400:
                    raise FetchError(
                        f"Website returned HTTP {response.status_code}",
                        status_code=response.status_code,
                    )

                content_type = (
                    response.headers.get("content-type", "").split(";")[0].strip().lower()
                )
                if content_type and content_type not in allowed:
                    raise FetchError(f"Unsupported content type: {content_type}")

                length = response.headers.get("content-length")
                if length:
                    try:
                        if int(length) > max_bytes:
                            raise FetchError("Response exceeds the maximum allowed size")
                    except ValueError:
                        pass

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError("Response exceeds the maximum allowed size")
                    chunks.append(chunk)

                raw = b"".join(chunks)
                encoding = response.charset_encoding or "utf-8"
                try:
                    text = raw.decode(encoding, errors="replace")
                except LookupError:
                    text = raw.decode("utf-8", errors="replace")

                return FetchResult(
                    url=current,
                    status_code=response.status_code,
                    text=_safe_text(text),
                    content_type=content_type,
                    redirects=redirects,
                )
        except httpx.TimeoutException as exc:
            raise FetchError("Website request timed out") from exc
        except httpx.TransportError as exc:
            raise FetchError(f"Connection failed ({exc.__class__.__name__})") from exc

    raise FetchError("Too many redirects")


class DomainRateLimiter:
    """Ensures a minimum delay between requests to the same domain."""

    def __init__(self, min_interval: float = 0.75) -> None:
        self.min_interval = max(0.0, min_interval)
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, domain: str) -> None:
        if self.min_interval <= 0:
            return
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            last = self._last.get(domain, 0.0)
            delay = self.min_interval - (now - last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[domain] = time.monotonic()
