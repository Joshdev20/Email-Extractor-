from __future__ import annotations

import asyncio

import httpx
import pytest

from backend.crawler import (
    CrawlCancelled,
    CrawlConfig,
    CrawlTarget,
    Crawler,
    registrable_domain,
)
from tests.conftest import read_fixture

PAGES = {
    "/": "home.html",
    "/contact-us": "contact.html",
    "/about-us": "about.html",
    "/our-team": "team.html",
    "/careers": "no_emails.html",
}


def handler_for(disallow_robots: bool = False, slow: bool = False, robots_error: bool = False):
    async def handler(request: httpx.Request) -> httpx.Response:
        if slow:
            await asyncio.sleep(0.05)
        host = request.url.host
        path = request.url.path
        if path == "/robots.txt":
            if robots_error:
                return httpx.Response(500, text="error")
            text = read_fixture("disallow_robots.txt") if disallow_robots else "User-agent: *\nAllow: /\n"
            return httpx.Response(200, text=text, headers={"content-type": "text/plain"})
        if host == "example.org":
            if path == "/":
                return httpx.Response(
                    200,
                    text='<html><body><a href="mailto:hello@acme.org">Email</a><p>sales@acme.org</p></body></html>',
                    headers={"content-type": "text/html"},
                )
            return httpx.Response(404, text="not found")
        if host == "example.com":
            fixture = PAGES.get(path)
            if fixture:
                return httpx.Response(200, text=read_fixture(fixture), headers={"content-type": "text/html"})
            return httpx.Response(404, text="not found")
        return httpx.Response(404, text="not found")

    return handler


async def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


@pytest.mark.asyncio
async def test_crawl_site_extracts_deduplicates_and_attributes_sources():
    async with await make_client(handler_for()) as client:
        crawler = Crawler(client, CrawlConfig(rate_limit_seconds=0, max_pages_per_domain=6))
        site = await crawler.crawl_site(CrawlTarget(url="https://example.com/", domain="example.com"))

    emails = {email.email: email for email in site.emails}
    assert site.company == "Northwind Property Group"
    assert site.country == "GB"
    assert site.pages_crawled == 5
    assert site.pages_failed == 0
    assert site.raw_email_count > len(site.emails)
    assert len(emails) == 10
    assert emails["contact@northwind.co.uk"].page_type == "contact"
    assert emails["contact@northwind.co.uk"].source_url == "https://example.com/contact-us"
    assert emails["careers@northwind.co.uk"].source_url == "https://example.com/contact-us"
    assert emails["press@northwind.co.uk"].page_type == "about"
    assert emails["jane.doe@northwind.co.uk"].page_type == "team"
    assert emails["info@northwind.co.uk"].category == "general"
    assert emails["careers@northwind.co.uk"].category == "careers"
    assert emails["support@northwind.co.uk"].category == "support"


@pytest.mark.asyncio
async def test_crawl_respects_max_pages_and_prioritises_contact():
    async with await make_client(handler_for()) as client:
        crawler = Crawler(client, CrawlConfig(rate_limit_seconds=0, max_pages_per_domain=2))
        site = await crawler.crawl_site(CrawlTarget(url="https://example.com/", domain="example.com"))

    assert site.pages_crawled == 2
    assert any(email.source_url.endswith("/contact-us") for email in site.emails)


@pytest.mark.asyncio
async def test_robots_disallow_blocks_the_site():
    async with await make_client(handler_for(disallow_robots=True)) as client:
        crawler = Crawler(client, CrawlConfig(rate_limit_seconds=0))
        site = await crawler.crawl_site(CrawlTarget(url="https://example.com/", domain="example.com"))

    assert site.blocked_by_robots is True
    assert site.pages_crawled == 0
    assert site.emails == []
    assert site.success is False


@pytest.mark.asyncio
async def test_robots_fetch_error_fails_open():
    async with await make_client(handler_for(robots_error=True)) as client:
        crawler = Crawler(client, CrawlConfig(rate_limit_seconds=0, retries=0))
        site = await crawler.crawl_site(CrawlTarget(url="https://example.com/", domain="example.com"))

    assert site.pages_crawled >= 1
    assert site.emails


@pytest.mark.asyncio
async def test_redirect_to_private_address_fails_the_site():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    async with await make_client(handler) as client:
        crawler = Crawler(client, CrawlConfig(rate_limit_seconds=0))
        site = await crawler.crawl_site(CrawlTarget(url="https://example.com/", domain="example.com"))

    assert site.pages_crawled == 0
    assert site.pages_failed == 1
    assert site.errors


@pytest.mark.asyncio
async def test_crawl_site_honours_cancellation_before_start():
    event = asyncio.Event()
    event.set()
    async with await make_client(handler_for()) as client:
        crawler = Crawler(client, CrawlConfig(rate_limit_seconds=0), cancel_event=event)
        with pytest.raises(CrawlCancelled):
            await crawler.crawl_site(CrawlTarget(url="https://example.com/", domain="example.com"))


@pytest.mark.asyncio
async def test_crawl_many_stops_at_target_email_count():
    targets = [
        CrawlTarget(url="https://example.com/", domain="example.com"),
        CrawlTarget(url="https://example.org/", domain="example.org"),
    ]
    completed = []
    async with await make_client(handler_for()) as client:
        crawler = Crawler(client, CrawlConfig(rate_limit_seconds=0, max_concurrent_sites=1))
        sites = await crawler.crawl_many(
            targets,
            max_workers=1,
            should_stop=lambda: sum(len(site.emails) for site in completed) >= 1,
            on_site=lambda site: completed.append(site),
        )
    assert len(sites) == 1
    assert sites[0].domain == "example.com"


@pytest.mark.asyncio
async def test_crawl_many_cancellation_between_sites():
    event = asyncio.Event()
    targets = [
        CrawlTarget(url="https://example.com/", domain="example.com"),
        CrawlTarget(url="https://example.org/", domain="example.org"),
    ]

    async def on_site(site):
        event.set()

    async with await make_client(handler_for()) as client:
        crawler = Crawler(client, CrawlConfig(rate_limit_seconds=0), cancel_event=event)
        sites = await crawler.crawl_many(targets, max_workers=1, on_site=on_site)
    assert len(sites) == 1


@pytest.mark.asyncio
async def test_crawl_many_isolates_failures():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n", headers={"content-type": "text/plain"})
        return httpx.Response(500, text="boom")

    targets = [
        CrawlTarget(url="https://example.com/", domain="example.com"),
        CrawlTarget(url="https://example.org/", domain="example.org"),
    ]
    async with await make_client(handler) as client:
        crawler = Crawler(client, CrawlConfig(rate_limit_seconds=0, retries=0))
        sites = await crawler.crawl_many(targets, max_workers=2)
    assert len(sites) == 2
    assert all(site.pages_crawled == 0 for site in sites)


def test_registrable_domain_handles_multi_label_suffixes():
    assert registrable_domain("www.example.co.uk") == "example.co.uk"
    assert registrable_domain("careers.example.com") == "example.com"
    assert registrable_domain("example.org") == "example.org"
    assert registrable_domain("sub.example.com.au") == "example.com.au"


@pytest.mark.asyncio
async def test_crawl_follows_links_from_contact_pages_for_depth():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n", headers={"content-type": "text/plain"})
        pages = {
            "/": '<a href="/contact-us">Contact</a>',
            "/contact-us": '<a href="/our-team">Team</a><p>hello@acme.org</p>',
            "/our-team": '<p>team@acme.org</p>',
        }
        body = pages.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    async with await make_client(handler) as client:
        crawler = Crawler(client, CrawlConfig(rate_limit_seconds=0, max_pages_per_domain=3))
        site = await crawler.crawl_site(CrawlTarget(url="https://example.com/", domain="example.com"))

    assert site.pages_crawled == 3
    assert {email.email for email in site.emails} == {"hello@acme.org", "team@acme.org"}
    team = next(email for email in site.emails if email.email == "team@acme.org")
    assert team.source_url == "https://example.com/our-team"


@pytest.mark.asyncio
async def test_crawl_reports_authentication_wall_without_bypassing_it():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n", headers={"content-type": "text/plain"})
        return httpx.Response(401, text="login required")

    async with await make_client(handler) as client:
        crawler = Crawler(client, CrawlConfig(rate_limit_seconds=0, retries=0))
        site = await crawler.crawl_site(CrawlTarget(url="https://example.com/", domain="example.com"))

    assert site.auth_required == 1
    assert site.pages_crawled == 0
    assert any("Authentication required" in error for error in site.errors)


@pytest.mark.asyncio
async def test_crawl_prioritises_buyer_and_seller_pages():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n", headers={"content-type": "text/plain"})
        pages = {
            "/": (
                '<a href="/about-us">About</a>'
                '<a href="/buyers">Buyers</a>'
                '<a href="/sellers">Sellers</a>'
            ),
            "/buyers": '<p>buyer@acme.org</p>',
            "/sellers": '<p>seller@acme.org</p>',
            "/about-us": '<p>about@acme.org</p>',
        }
        body = pages.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    async with await make_client(handler) as client:
        crawler = Crawler(client, CrawlConfig(rate_limit_seconds=0, max_pages_per_domain=3))
        site = await crawler.crawl_site(CrawlTarget(url="https://example.com/", domain="example.com"))

    assert {email.email for email in site.emails} == {"buyer@acme.org", "seller@acme.org"}
    assert all(email.source_url in {"https://example.com/buyers", "https://example.com/sellers"} for email in site.emails)


@pytest.mark.asyncio
async def test_crawler_sends_scoped_auth_to_authenticated_pages():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("cookie")))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n", headers={"content-type": "text/plain"})
        return httpx.Response(
            200,
            text='<p>sales@acme.org</p>',
            headers={"content-type": "text/html"},
        )

    async with await make_client(handler) as client:
        crawler = Crawler(
            client,
            CrawlConfig(
                rate_limit_seconds=0,
                max_pages_per_domain=1,
                auth_headers={"example.com": {"Cookie": "session=secret"}},
            ),
        )
        site = await crawler.crawl_site(CrawlTarget(url="https://example.com/private", domain="example.com"))

    assert site.emails[0].email == "sales@acme.org"
    assert ("/private", "session=secret") in seen


@pytest.mark.asyncio
async def test_crawl_uses_robots_sitemap_for_hidden_contact_pages():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml\n",
                headers={"content-type": "text/plain"},
            )
        if request.url.path == "/sitemap.xml":
            return httpx.Response(
                200,
                text=(
                    '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    "<url><loc>https://example.com/contact-us</loc></url>"
                    "</urlset>"
                ),
                headers={"content-type": "application/xml"},
            )
        if request.url.path == "/contact-us":
            return httpx.Response(
                200,
                text="<p>sales@acme.org</p>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(200, text="<html>home</html>", headers={"content-type": "text/html"})

    async with await make_client(handler) as client:
        crawler = Crawler(client, CrawlConfig(rate_limit_seconds=0, max_pages_per_domain=2))
        site = await crawler.crawl_site(CrawlTarget(url="https://example.com/", domain="example.com"))

    assert site.pages_crawled == 2
    assert [(email.email, email.source_url) for email in site.emails] == [
        ("sales@acme.org", "https://example.com/contact-us")
    ]
