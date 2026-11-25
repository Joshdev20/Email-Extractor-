from __future__ import annotations

import httpx
import pytest

from backend.security import (
    FetchError,
    UnsafeURLError,
    normalize_domain_input,
    safe_get,
    validate_url,
)


@pytest.mark.asyncio
async def test_safe_get_returns_html():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><body>hello</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await safe_get(client, "https://example.com/")
    assert "hello" in result.text
    assert result.status_code == 200


@pytest.mark.asyncio
async def test_safe_get_follows_public_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "https://example.org/final"})
        return httpx.Response(200, text="<html>final</html>", headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await safe_get(client, "https://example.com/")
    assert result.url == "https://example.org/final"
    assert "final" in result.text


@pytest.mark.asyncio
async def test_safe_get_blocks_redirect_to_private_address():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnsafeURLError):
            await safe_get(client, "https://example.com/")


@pytest.mark.asyncio
async def test_safe_get_rejects_large_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 500, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError, match="maximum allowed size"):
            await safe_get(client, "https://example.com/", max_bytes=100)


@pytest.mark.asyncio
async def test_safe_get_rejects_unsupported_content_type():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError, match="Unsupported content type"):
            await safe_get(client, "https://example.com/")


@pytest.mark.asyncio
async def test_safe_get_reports_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError) as info:
            await safe_get(client, "https://example.com/")
    assert info.value.status_code == 404


@pytest.mark.asyncio
async def test_validate_url_blocks_hostname_resolving_to_private(monkeypatch):
    async def private_dns(hostname: str, port: int):
        return ("10.0.0.7",)

    monkeypatch.setattr("backend.security._resolve_host", private_dns)
    with pytest.raises(UnsafeURLError):
        await validate_url("https://evil.example/")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/",
        "http://[::1]/",
        "http://user:pass@example.com/",
        "https://example.com:8080/",
        "ftp://example.com/",
        "javascript:alert(1)",
    ],
)
async def test_validate_url_blocks_unsafe_targets(url):
    with pytest.raises(UnsafeURLError):
        await validate_url(url)


@pytest.mark.asyncio
async def test_validate_url_allows_public_host():
    assert await validate_url("https://example.com/path?q=1") == "https://example.com/path?q=1"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Example.com", "example.com"),
        ("https://www.Example.co.uk/contact", "www.example.co.uk"),
        ("  acme.com  ", "acme.com"),
        ("sub.domain.example.org", "sub.domain.example.org"),
    ],
)
def test_normalize_domain_input_valid(value, expected):
    assert normalize_domain_input(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "localhost",
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "metadata.google.internal",
        "ftp://example.com",
        "http://user:pass@example.com",
        "https://example.com:8080/",
        "not-a-domain",
        "exa_mple.com",
        "",
    ],
)
def test_normalize_domain_input_invalid(value):
    with pytest.raises(ValueError):
        normalize_domain_input(value)
