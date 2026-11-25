"""Asynchronous, polite website crawler focused on business contact pages."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from . import security
from .email_extractor import (
    ExtractedEmail,
    extract_emails_from_html,
    extract_emails_with_stats,
    extract_page_metadata,
)

ProgressCallback = Callable[[dict], Awaitable[None] | None]
SiteCallback = Callable[["SiteCrawlResult"], Awaitable[None] | None]

_MULTI_LABEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
    "com.au", "net.au", "org.au", "co.nz", "net.nz", "org.nz",
    "co.in", "net.in", "org.in", "co.za", "com.br", "com.mx", "com.sg",
    "com.tr", "com.cn", "com.hk", "com.tw", "co.jp", "co.kr", "com.ar",
    "com.co", "com.pe", "com.pk", "com.my", "co.id", "co.th", "com.vn",
    "co.il", "com.sa", "com.qa", "com.eg", "com.ng", "co.ke", "com.ph",
    "co.ae", "com.ua", "com.pl", "co.at", "co.ch",
}

_PRIORITY_RULES: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("contact", re.compile(
        r"contact|kontakt|contacto|contactez|get-in-touch|getintouch|reach-us|"
        r"reachout|talk-to-us|enquir|inquir"
    ), 100),
    ("about", re.compile(
        r"about|who-we-are|whoweare|our-story|ourstory|company|impressum|imprint|"
        r"quienes-somos|uber-uns"
    ), 80),
    ("team", re.compile(
        r"team|leadership|people|management|staff|meet-the|our-people|directors|"
        r"board-of"
    ), 75),
    ("support", re.compile(r"support|help|faq|customer-service|customer-care|service"), 60),
    ("sales", re.compile(r"sales|partner|procurement|wholesale|reseller"), 55),
    ("careers", re.compile(r"career|jobs|vacanc|join-us|work-with-us|hiring"), 40),
    ("press", re.compile(r"press|media|newsroom|news"), 30),
)

_SKIP_LINK_RE = re.compile(
    r"login|signin|sign-in|register|account|cart|checkout|basket|password|"
    r"privacy|terms|cookie|unsubscribe|wp-admin|wp-login|feed|rss|sitemap|"
    r"\.(?:pdf|jpg|jpeg|png|gif|svg|webp|zip|mp4|mp3|css|js)$"
)


class CrawlCancelled(Exception):
    """Raised when the user cancels an extraction job."""


@dataclass(slots=True)
class CrawlConfig:
    max_pages_per_domain: int = 6
    max_concurrent_sites: int = 5
    per_domain_concurrency: int = 2
    timeout: float = 15.0
    max_response_bytes: int = security.MAX_RESPONSE_BYTES
    respect_robots: bool = True
    rate_limit_seconds: float = 0.75
    retries: int = 2


@dataclass(slots=True)
class CrawlTarget:
    url: str
    domain: str
    title: str = ""
    source: str = ""
    score: float = 0.0


@dataclass(slots=True)
class SiteCrawlResult:
    domain: str
    start_url: str
    company: str = ""
    country: str = "Unknown"
    pages_crawled: int = 0
    pages_failed: int = 0
    emails: list[ExtractedEmail] = field(default_factory=list)
    raw_email_count: int = 0
    errors: list[str] = field(default_factory=list)
    blocked_by_robots: bool = False
    duration_ms: int = 0

    @property
    def success(self) -> bool:
        return self.pages_crawled > 0

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "start_url": self.start_url,
            "company": self.company,
            "country": self.country,
            "pages_crawled": self.pages_crawled,
            "pages_failed": self.pages_failed,
            "emails": [email.to_dict() for email in self.emails],
            "raw_email_count": self.raw_email_count,
            "errors": self.errors,
            "blocked_by_robots": self.blocked_by_robots,
            "duration_ms": self.duration_ms,
            "success": self.success,
        }


def registrable_domain(hostname: str) -> str:
    host = (hostname or "").lower().strip(".").removeprefix("www.")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    suffix = ".".join(labels[-2:])
    if suffix in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return suffix


def _score_link(path: str, text: str) -> int:
    haystack = f"{path} {text}".lower()
    if _SKIP_LINK_RE.search(haystack):
        return 0
    best = 0
    for _, pattern, weight in _PRIORITY_RULES:
        if pattern.search(haystack):
            best = max(best, weight)
    return best


@dataclass(slots=True)
class _DiscoveredLink:
    url: str
    score: int


class Crawler:
    def __init__(
        self,
        client: httpx.AsyncClient,
        config: CrawlConfig | None = None,
        *,
        cancel_event: asyncio.Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.client = client
        self.config = config or CrawlConfig()
        self.cancel_event = cancel_event or asyncio.Event()
        self.progress = progress
        self._rate = security.DomainRateLimiter(self.config.rate_limit_seconds)
        self._domain_semaphores: dict[str, asyncio.Semaphore] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    async def _emit(self, payload: dict) -> None:
        if self.progress is None:
            return
        result = self.progress(payload)
        if asyncio.iscoroutine(result):
            await result

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise CrawlCancelled()

    def _domain_semaphore(self, domain: str) -> asyncio.Semaphore:
        sem = self._domain_semaphores.get(domain)
        if sem is None:
            sem = asyncio.Semaphore(self.config.per_domain_concurrency)
            self._domain_semaphores[domain] = sem
        return sem

    async def _fetch(self, url: str, *, content_types: tuple[str, ...] | None = None) -> security.FetchResult:
        host = urlsplit(url).hostname or ""
        await self._rate.wait(host)
        last_error: security.FetchError | None = None
        for attempt in range(self.config.retries + 1):
            self._check_cancelled()
            try:
                return await security.safe_get(
                    self.client,
                    url,
                    timeout=self.config.timeout,
                    max_bytes=self.config.max_response_bytes,
                    allowed_content_types=content_types
                    or ("text/html", "application/xhtml+xml"),
                )
            except security.FetchError as exc:
                last_error = exc
                retryable = exc.status_code is None or exc.status_code == 429 or exc.status_code >= 500
                if not retryable or attempt >= self.config.retries:
                    break
                await asyncio.sleep(0.8 * (attempt + 1))
        assert last_error is not None
        raise last_error

    async def _robots_for(self, url: str) -> RobotFileParser | None:
        if not self.config.respect_robots:
            return None
        parts = urlsplit(url)
        host = parts.hostname or ""
        if host in self._robots:
            return self._robots[host]
        lock = self._robots_locks.setdefault(host, asyncio.Lock())
        async with lock:
            if host in self._robots:
                return self._robots[host]
            robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
            parser: RobotFileParser | None = RobotFileParser()
            try:
                result = await self._fetch(
                    robots_url,
                    content_types=("text/plain", "text/html", "application/xhtml+xml"),
                )
                parser.parse(result.text.splitlines())
            except security.FetchError as exc:
                if exc.status_code in (401, 403):
                    parser = RobotFileParser()
                    parser.parse(["User-agent: *", "Disallow: /"])
                else:
                    parser = None
            self._robots[host] = parser
            return parser

    async def _allowed(self, url: str) -> bool:
        parser = await self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(security.USER_AGENT, url)

    def _discover_links(self, html: str, page_url: str, domain: str) -> list[_DiscoveredLink]:
        soup = BeautifulSoup(html, "lxml")
        found: dict[str, int] = {}
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href", "")).strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
                continue
            absolute = urljoin(page_url, href)
            parts = urlsplit(absolute)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                continue
            if registrable_domain(parts.hostname) != domain:
                continue
            path = parts.path or "/"
            text = anchor.get_text(" ", strip=True)[:120]
            score = _score_link(path, text)
            if score <= 0:
                continue
            clean = urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
            found[clean] = max(found.get(clean, 0), score)
        return [
            _DiscoveredLink(url=url, score=score)
            for url, score in sorted(found.items(), key=lambda item: (-item[1], item[0]))
        ]

    async def _crawl_page(
        self,
        url: str,
        domain: str,
        *,
        company: str | None = None,
        country: str | None = None,
    ) -> tuple[security.FetchResult, list[ExtractedEmail], str, str]:
        async with self._domain_semaphore(domain):
            result = await self._fetch(url)
        page_company, page_country = extract_page_metadata(result.text, result.url, domain)
        emails, raw_count = extract_emails_with_stats(
            result.text,
            result.url,
            website_domain=domain,
            company=company or page_company,
            country=country or page_country,
        )
        return result, emails, page_company, page_country, raw_count

    async def crawl_site(self, target: CrawlTarget) -> SiteCrawlResult:
        started = time.monotonic()
        domain = target.domain or registrable_domain(urlsplit(target.url).hostname or "")
        site = SiteCrawlResult(domain=domain, start_url=target.url)
        self._check_cancelled()
        await self._emit({
            "phase": "crawling",
            "message": f"Crawling {domain}",
            "current_domain": domain,
        })

        start_url = target.url
        try:
            if not await self._allowed(start_url):
                site.blocked_by_robots = True
                site.errors.append("Blocked by robots.txt")
                site.duration_ms = int((time.monotonic() - started) * 1000)
                return site
            try:
                home = await self._fetch(start_url)
            except security.FetchError:
                if start_url.startswith("https://"):
                    alternate = "http://" + start_url[len("https://"):]
                    if await self._allowed(alternate):
                        home = await self._fetch(alternate)
                        start_url = alternate
                        site.start_url = alternate
                    else:
                        raise
                else:
                    raise
        except CrawlCancelled:
            raise
        except (security.FetchError, security.UnsafeURLError) as exc:
            site.pages_failed = 1
            site.errors.append(str(exc)[:200])
            site.duration_ms = int((time.monotonic() - started) * 1000)
            return site

        site.pages_crawled = 1
        home_company, home_country = extract_page_metadata(home.text, home.url, domain)
        site.company = home_company
        site.country = home_country
        home_emails, home_raw = extract_emails_with_stats(
            home.text,
            home.url,
            website_domain=domain,
            company=home_company,
            country=home_country,
        )
        site.emails = list(home_emails)
        site.raw_email_count = home_raw
        await self._emit({
            "phase": "crawling",
            "message": f"Found {len(site.emails)} email(s) on {domain}",
            "current_domain": domain,
            "site_pages": site.pages_crawled,
            "site_emails": len(site.emails),
        })

        links = self._discover_links(home.text, home.url, domain)
        limit = max(0, self.config.max_pages_per_domain - 1)
        selected = links[:limit]

        page_results: list[tuple[int, security.FetchResult, list[ExtractedEmail], str, str, int]] = []
        semaphore = asyncio.Semaphore(self.config.per_domain_concurrency)

        async def fetch_link(link: _DiscoveredLink) -> None:
            async with semaphore:
                self._check_cancelled()
                try:
                    if not await self._allowed(link.url):
                        return
                    result, emails, company, country, raw_count = await self._crawl_page(
                        link.url, domain, company=site.company, country=site.country
                    )
                    page_results.append((link.score, result, emails, company, country, raw_count))
                    await self._emit({
                        "phase": "crawling",
                        "message": f"Crawled {domain}{urlsplit(result.url).path or '/'}",
                        "current_domain": domain,
                        "site_pages": site.pages_crawled + len(page_results),
                        "site_emails": len(site.emails) + sum(len(item[2]) for item in page_results),
                    })
                except CrawlCancelled:
                    raise
                except (security.FetchError, security.UnsafeURLError) as exc:
                    site.pages_failed += 1
                    if len(site.errors) < 20:
                        site.errors.append(f"{link.url}: {str(exc)[:160]}")

        if selected:
            results = await asyncio.gather(
                *(fetch_link(link) for link in selected), return_exceptions=True
            )
            for outcome in results:
                if isinstance(outcome, CrawlCancelled):
                    raise outcome
                if isinstance(outcome, Exception):
                    site.errors.append(str(outcome)[:160])

        page_results.sort(key=lambda item: -item[0])
        by_email: dict[str, tuple[int, ExtractedEmail]] = {
            email.email: (0, email) for email in site.emails
        }
        for score, result, emails, company, country, raw_count in page_results:
            site.pages_crawled += 1
            site.raw_email_count += raw_count
            if company and not site.company:
                site.company = company
            if country and site.country == "Unknown":
                site.country = country
            for email in emails:
                email.company = site.company or email.company
                email.country = email.country if email.country != "Unknown" else site.country
                existing = by_email.get(email.email)
                if existing is None or score > existing[0]:
                    by_email[email.email] = (score, email)

        site.emails = sorted(
            (email for _, email in by_email.values()), key=lambda email: email.email
        )
        site.duration_ms = int((time.monotonic() - started) * 1000)
        await self._emit({
            "phase": "crawling",
            "message": f"Finished {domain}: {len(site.emails)} email(s)",
            "current_domain": domain,
            "site_pages": site.pages_crawled,
            "site_emails": len(site.emails),
        })
        return site

    async def crawl_many(
        self,
        targets: list[CrawlTarget],
        *,
        should_stop: Callable[[], bool] | None = None,
        on_site: SiteCallback | None = None,
        max_workers: int | None = None,
    ) -> list[SiteCrawlResult]:
        """Crawl many sites with bounded concurrency and optional early stop."""
        if not targets:
            return []
        queue: asyncio.Queue[CrawlTarget] = asyncio.Queue()
        for target in targets:
            queue.put_nowait(target)

        results: list[SiteCrawlResult] = []
        stop = asyncio.Event()
        worker_count = min(max_workers or self.config.max_concurrent_sites, len(targets))

        async def worker() -> None:
            while not stop.is_set():
                if self.cancel_event.is_set():
                    return
                try:
                    target = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    site = await self.crawl_site(target)
                except CrawlCancelled:
                    return
                except Exception as exc:  # defensive: one bad site must not kill the job
                    site = SiteCrawlResult(
                        domain=target.domain,
                        start_url=target.url,
                        pages_failed=1,
                        errors=[str(exc)[:200]],
                    )
                results.append(site)
                if on_site is not None:
                    outcome = on_site(site)
                    if asyncio.iscoroutine(outcome):
                        await outcome
                if should_stop is not None and should_stop():
                    stop.set()
                    return
                queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        await asyncio.gather(*workers, return_exceptions=True)
        return results
