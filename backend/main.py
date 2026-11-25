"""FastAPI application for the Business Email Extractor and Lead Finder.

No database and no server-side persistence: active jobs live in process memory,
are returned to the browser, and are discarded on a short TTL or restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from .crawler import (
    CrawlCancelled,
    CrawlConfig,
    CrawlTarget,
    Crawler,
    SiteCrawlResult,
    registrable_domain,
)
from .search_engine import (
    DiscoveryResult,
    GoogleConfig,
    QuerySpec,
    SearchConfigError,
    SearchError,
    SearchQuotaError,
    build_search_queries,
    discover_websites,
    parse_query,
)
from .security import USER_AGENT, normalize_domain_input
from .validator import (
    BulkExtractRequest,
    CancelRequest,
    CancelResponse,
    CrawlRequest,
    ExtractRequest,
    ExtractionSettings,
    HealthResponse,
    JobCreated,
    SearchRequest,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT_DIR / "frontend"
load_dotenv(ROOT_DIR / ".env")
load_dotenv(ROOT_DIR / "backend" / ".env")

APP_VERSION = "1.0.0"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_CONCURRENT_JOBS = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "3")))
MAX_JOB_SECONDS = max(60, int(os.getenv("MAX_JOB_SECONDS", "1800")))
SYNC_MAX_SECONDS = max(10, min(300, int(os.getenv("SYNC_MAX_SECONDS", "55"))))
IS_SERVERLESS = bool(os.getenv("VERCEL")) or os.getenv("SYNC_EXTRACTION", "0") == "1"
JOB_TTL_SECONDS = max(60, int(os.getenv("JOB_TTL_SECONDS", "900")))
MAX_RETAINED_JOBS = max(10, int(os.getenv("MAX_RETAINED_JOBS", "50")))
RATE_LIMIT_PER_MINUTE = max(10, int(os.getenv("RATE_LIMIT_PER_MINUTE", "120")))
EXTRACTION_RATE_LIMIT = max(2, int(os.getenv("EXTRACTION_RATE_LIMIT", "12")))
CRAWL_RATE_LIMIT_SECONDS = float(os.getenv("CRAWL_RATE_LIMIT_SECONDS", "0.75"))
TRUST_PROXY = os.getenv("TRUST_PROXY", "0") == "1"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("leadscope")
# httpx logs full request URLs at INFO, which would include Google API keys.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

google_config = GoogleConfig.from_env()
TERMINAL_STATES = {"done", "error", "cancelled"}
EXTRACTION_PATHS = {
    "/api/search",
    "/api/extract",
    "/api/extract-sync",
    "/api/bulk-extract",
    "/api/crawl",
}

CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
    "https://cdn.tailwindcss.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://fonts.googleapis.com https://cdn.jsdelivr.net; "
    "font-src 'self' https://cdnjs.cloudflare.com https://fonts.gstatic.com data:; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def _settings_overrides(settings: ExtractionSettings) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for key in ("industry", "country", "city", "email_category"):
        value = getattr(settings, key)
        if value:
            overrides[key] = value
    if settings.target_emails is not None:
        overrides["target_emails"] = settings.target_emails
    if settings.target_websites is not None:
        overrides["target_websites"] = settings.target_websites
    return overrides


def _build_spec(
    query: str | None,
    domains: list[str],
    urls: list[str],
    settings: ExtractionSettings,
) -> QuerySpec:
    if query:
        spec = parse_query(query, overrides=_settings_overrides(settings))
    else:
        spec = QuerySpec(
            raw="manual website list",
            industry=settings.industry,
            country_name=settings.country,
            city=settings.city,
            email_category=settings.email_category,
            target_emails=settings.target_emails or 50,
            target_websites=settings.target_websites
            or max(1, len(domains) + len(urls)),
        )

    if domains or urls:
        manual_domains = list(domains)
        for url in urls:
            with contextlib.suppress(ValueError):
                manual_domains.append(normalize_domain_input(url))
        spec.domains = list(dict.fromkeys(manual_domains))
        if settings.target_websites is None:
            spec.target_websites = max(1, len(spec.domains))

    location_parts = [part for part in (spec.city, spec.country_name) if part]
    spec.location_label = ", ".join(location_parts) if location_parts else None
    spec.search_queries = build_search_queries(spec)
    return spec


def _manual_targets(domains: list[str], urls: list[str], limit: int) -> list[CrawlTarget]:
    targets: list[CrawlTarget] = []
    seen: set[str] = set()
    for domain in domains:
        key = registrable_domain(domain) or domain
        if key in seen:
            continue
        seen.add(key)
        targets.append(CrawlTarget(
            url=f"https://{domain}",
            domain=key,
            title=domain,
            source="provided",
            score=100.0,
        ))
    for url in urls:
        with contextlib.suppress(ValueError):
            host = normalize_domain_input(url)
            key = registrable_domain(host) or host
            if key in seen:
                continue
            seen.add(key)
            targets.append(CrawlTarget(
                url=url,
                domain=key,
                title=key,
                source="provided",
                score=100.0,
            ))
    return targets[: max(1, min(limit, 200))]


@dataclass
class Job:
    id: str
    kind: str
    runner: Callable[["Job"], Awaitable[None]]
    parsed: dict[str, Any] | None = None
    state: str = "queued"
    phase: str = "queued"
    message: str = "Waiting to start"
    progress: int = 0
    current_domain: str | None = None
    target_emails: int = 50
    target_websites: int = 25
    partial: bool = False
    completed_sites: int = 0
    total_sites: int = 0
    websites: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    seen_emails: set[str] = field(default_factory=set)
    total_email_occurrences: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=lambda: {
        "websites_discovered": 0,
        "websites_crawled": 0,
        "websites_failed": 0,
        "emails_discovered": 0,
        "unique_emails": 0,
        "duplicates_removed": 0,
        "pages_crawled": 0,
        "pages_failed": 0,
    })
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None

    def stats_snapshot(self) -> dict[str, int]:
        data = dict(self.stats)
        data["unique_emails"] = len(self.results)
        data["emails_discovered"] = self.total_email_occurrences
        data["duplicates_removed"] = max(0, self.total_email_occurrences - len(self.results))
        data["target_emails"] = self.target_emails
        data["target_websites"] = self.target_websites
        data["target_reached"] = bool(
            self.target_emails and len(self.results) >= self.target_emails
        )
        return data

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "state": self.state,
            "phase": self.phase,
            "message": self.message,
            "progress": self.progress,
            "current_domain": self.current_domain,
            "parsed": self.parsed,
            "stats": self.stats_snapshot(),
            "warnings": self.warnings,
            "errors": self.errors[-50:],
            "websites": self.websites[:50],
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "finished_at": _iso(self.finished_at),
        }

    def results_payload(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "state": self.state,
            "complete": self.state in TERMINAL_STATES,
            "message": self.message,
            "partial": self.partial,
            "parsed": self.parsed,
            "stats": self.stats_snapshot(),
            "warnings": self.warnings,
            "errors": self.errors[-50:],
            "websites": self.websites,
            "results": self.results,
            "finished_at": _iso(self.finished_at),
        }


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
        self._janitor: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        if self.client is None or self.client.is_closed:
            self.client = httpx.AsyncClient(
                follow_redirects=False,
                trust_env=os.getenv("HTTPX_TRUST_ENV", "0") == "1",
                timeout=httpx.Timeout(25.0, connect=10.0),
                limits=httpx.Limits(max_connections=60, max_keepalive_connections=20),
                headers={"User-Agent": USER_AGENT},
            )
        if self._janitor is None or self._janitor.done():
            self._janitor = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        for job in list(self.jobs.values()):
            job.cancel_event.set()
            if job.task and not job.task.done():
                job.task.cancel()
        tasks = [job.task for job in self.jobs.values() if job.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._janitor:
            self._janitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._janitor
            self._janitor = None
        if self.client and not self.client.is_closed:
            await self.client.aclose()
        self.client = None

    def create_job(
        self,
        *,
        kind: str,
        runner: Callable[[Job], Awaitable[None]],
        parsed: dict[str, Any] | None,
        target_emails: int,
        target_websites: int,
    ) -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            kind=kind,
            runner=runner,
            parsed=parsed,
            target_emails=max(1, target_emails),
            target_websites=max(1, target_websites),
        )
        job.total_sites = job.target_websites
        self.jobs[job.id] = job
        self._prune_retained()
        job.task = asyncio.create_task(self._execute(job))
        return job

    async def _execute(self, job: Job) -> None:
        async with self._semaphore:
            if job.cancel_event.is_set():
                self._finish(job, "cancelled", "Extraction cancelled")
                return
            job.state = "running"
            job.phase = "starting"
            job.message = "Starting extraction"
            job.updated_at = time.time()
            try:
                await asyncio.wait_for(job.runner(job), timeout=MAX_JOB_SECONDS)
                if job.cancel_event.is_set():
                    self._finish(job, "cancelled", "Extraction cancelled")
                else:
                    if not job.results:
                        job.warnings.append(
                            "No publicly listed email addresses were found. "
                            "Try more websites, a different location, or a broader industry."
                        )
                    self._finish(
                        job,
                        "done",
                        f"Extraction complete: {len(job.results)} unique email(s) "
                        f"from {job.stats['websites_crawled']} website(s)",
                    )
            except asyncio.CancelledError:
                self._finish(job, "cancelled", "Extraction cancelled")
                raise
            except CrawlCancelled:
                self._finish(job, "cancelled", "Extraction cancelled")
            except SearchConfigError as exc:
                self._fail_or_cancel(job, str(exc), "configuration")
            except SearchQuotaError as exc:
                self._fail_or_cancel(job, str(exc), "quota")
            except SearchError as exc:
                self._fail_or_cancel(job, str(exc), "search")
            except asyncio.TimeoutError:
                self._fail_or_cancel(job, "Extraction timed out. Try fewer websites.", "timeout")
            except Exception as exc:  # noqa: BLE001 - never leak internals to the client
                logger.exception("Extraction job %s failed", job.id)
                job.errors.append({"scope": "internal", "message": exc.__class__.__name__})
                self._fail_or_cancel(job, "Extraction failed unexpectedly. Please try again.", "internal")

    def _finish(self, job: Job, state: str, message: str) -> None:
        job.state = state
        job.phase = state
        job.message = message
        job.finished_at = time.time()
        job.updated_at = job.finished_at
        if state == "done":
            job.progress = 100

    def _fail(self, job: Job, message: str, scope: str) -> None:
        job.errors.append({"scope": scope, "message": message})
        self._finish(job, "error", message)

    def _fail_or_cancel(self, job: Job, message: str, scope: str) -> None:
        if job.cancel_event.is_set():
            self._finish(job, "cancelled", "Extraction cancelled")
        else:
            self._fail(job, message, scope)

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if job.state in TERMINAL_STATES:
            return job
        job.cancel_event.set()
        if job.task and not job.task.done():
            job.task.cancel()
        job.state = "cancelled"
        job.phase = "cancelled"
        job.message = "Extraction cancelled"
        job.finished_at = time.time()
        job.updated_at = job.finished_at
        return job

    def active_count(self) -> int:
        return sum(1 for job in self.jobs.values() if job.state in {"queued", "running"})

    def _prune_retained(self) -> None:
        if len(self.jobs) <= MAX_RETAINED_JOBS:
            return
        finished = sorted(
            (job for job in self.jobs.values() if job.state in TERMINAL_STATES),
            key=lambda job: job.finished_at or job.created_at,
        )
        for job in finished[: len(self.jobs) - MAX_RETAINED_JOBS]:
            self.jobs.pop(job.id, None)

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                for job_id, job in list(self.jobs.items()):
                    expired = (
                        job.finished_at is not None
                        and now - job.finished_at > JOB_TTL_SECONDS
                    )
                    stale = now - job.created_at > MAX_JOB_SECONDS + 3600
                    if expired or stale:
                        job.cancel_event.set()
                        if job.task and not job.task.done():
                            job.task.cancel()
                        self.jobs.pop(job_id, None)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Job cleanup error")


manager = JobManager()


async def _crawl_targets(job: Job, targets: list[CrawlTarget], settings: ExtractionSettings) -> None:
    assert manager.client is not None
    job.phase = "crawling"
    job.message = f"Crawling {len(targets)} website(s)"
    job.progress = 15
    job.total_sites = len(targets)
    job.stats["websites_discovered"] = len(targets)
    job.updated_at = time.time()

    config = CrawlConfig(
        max_pages_per_domain=settings.max_pages_per_domain,
        max_concurrent_sites=settings.concurrency,
        per_domain_concurrency=min(2, settings.concurrency),
        timeout=settings.timeout,
        respect_robots=True,
        rate_limit_seconds=CRAWL_RATE_LIMIT_SECONDS,
    )

    async def on_progress(update: dict[str, Any]) -> None:
        job.phase = "crawling"
        if update.get("message"):
            job.message = str(update["message"])[:200]
        if update.get("current_domain"):
            job.current_domain = str(update["current_domain"])
        job.updated_at = time.time()

    def update_progress() -> None:
        if job.target_emails and len(job.results) >= job.target_emails:
            job.progress = 98
        elif job.total_sites:
            job.progress = min(95, 15 + int(80 * job.completed_sites / job.total_sites))

    async def on_site(site: SiteCrawlResult) -> None:
        if site.pages_crawled > 0:
            job.stats["websites_crawled"] += 1
        else:
            job.stats["websites_failed"] += 1
        job.stats["pages_crawled"] += site.pages_crawled
        job.stats["pages_failed"] += site.pages_failed
        job.total_email_occurrences += site.raw_email_count
        for email in site.emails:
            if email.email in job.seen_emails:
                continue
            job.seen_emails.add(email.email)
            job.results.append(email.to_dict())
        job.results.sort(key=lambda item: item["email"])
        job.completed_sites += 1
        for error in site.errors[:3]:
            if len(job.errors) < 50:
                job.errors.append({"scope": site.domain, "message": error[:200]})
        update_progress()
        job.message = (
            f"Crawled {job.completed_sites} of {job.total_sites} sites, "
            f"{len(job.results)} unique email(s)"
        )
        job.updated_at = time.time()

    crawler = Crawler(
        manager.client,
        config,
        cancel_event=job.cancel_event,
        progress=on_progress,
    )
    await crawler.crawl_many(
        targets,
        should_stop=lambda: job.cancel_event.is_set()
        or bool(job.target_emails and len(job.results) >= job.target_emails),
        on_site=on_site,
    )
    if job.cancel_event.is_set():
        raise CrawlCancelled()
    update_progress()
    if job.target_emails and len(job.results) >= job.target_emails:
        job.message = f"Target reached: {len(job.results)} unique email(s)"
        job.progress = 98
    job.updated_at = time.time()


async def _run_targets(job: Job, targets: list[CrawlTarget], settings: ExtractionSettings) -> None:
    job.websites = [
        {
            "url": target.url,
            "domain": target.domain,
            "title": target.title,
            "source": target.source,
            "kind": "company",
            "score": target.score,
        }
        for target in targets
    ]
    await _crawl_targets(job, targets, settings)


async def _run_query(job: Job, spec: QuerySpec, settings: ExtractionSettings) -> None:
    assert manager.client is not None
    job.phase = "searching"
    job.message = "Discovering business websites with Google"
    job.progress = 5
    job.updated_at = time.time()

    discovery: DiscoveryResult = await discover_websites(
        manager.client,
        spec,
        google_config,
        limit=spec.target_websites,
        use_places=settings.use_places,
    )
    job.warnings.extend(discovery.warnings)
    job.websites = [website.to_dict() for website in discovery.websites]
    job.stats["websites_discovered"] = len(discovery.websites)
    job.progress = 12
    job.updated_at = time.time()

    targets = [
        CrawlTarget(
            url=website.url,
            domain=registrable_domain(website.domain) or website.domain,
            title=website.title,
            source=website.source,
            score=website.score,
        )
        for website in discovery.websites
    ]
    if not targets:
        raise SearchError("No websites were discovered for this search.")
    await _crawl_targets(job, targets, settings)


async def _run_bulk(job: Job, req: BulkExtractRequest, spec: QuerySpec) -> None:
    targets = _manual_targets(req.domains, req.urls, spec.target_websites)
    if not targets:
        raise SearchError("No valid websites were provided.")
    await _run_targets(job, targets, req.settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.start()
    logger.info(
        "LeadScope started: google_cse=%s google_places=%s",
        google_config.cse_ready,
        google_config.places_ready,
    )
    yield
    await manager.stop()


app = FastAPI(
    title="Business Email Extractor & Lead Finder",
    version=APP_VERSION,
    description=(
        "Google-powered business email discovery with a Python crawler. "
        "No database, no accounts and no permanent server-side storage."
    ),
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

_origins = [item.strip() for item in os.getenv("ALLOWED_ORIGINS", "*").split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class RateLimitMiddleware:
    """Small in-memory token window per client IP and endpoint group."""

    def __init__(self, asgi_app, *, limit: int, extraction_limit: int, window: float = 60.0):
        self.app = asgi_app
        self.limit = limit
        self.extraction_limit = extraction_limit
        self.window = window
        self.hits: dict[str, list[float]] = {}

    def _client_ip(self, scope) -> str:
        if TRUST_PROXY:
            for header, value in scope.get("headers", []):
                if header == b"x-forwarded-for":
                    return value.decode("latin-1").split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        if scope["path"] in {"/api/health", "/api/config", "/api/openapi.json"}:
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        is_extraction = scope["method"] == "POST" and (
            path in EXTRACTION_PATHS or path.startswith("/api/jobs/")
        )
        limit = self.extraction_limit if is_extraction else self.limit
        key = f"{self._client_ip(scope)}:{'extract' if is_extraction else 'api'}"
        now = time.monotonic()
        bucket = [stamp for stamp in self.hits.get(key, []) if now - stamp < self.window]
        if len(bucket) >= limit:
            response = JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please wait a minute and try again."},
                headers={"Retry-After": "60"},
            )
            await response(scope, receive, send)
            return
        bucket.append(now)
        self.hits[key] = bucket
        if len(self.hits) > 5000:
            self.hits = {
                bucket_key: stamps
                for bucket_key, stamps in self.hits.items()
                if any(now - stamp < self.window for stamp in stamps)
            }
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
                headers["Content-Security-Policy"] = CSP
                if scope["path"].startswith("/api/"):
                    headers["Cache-Control"] = "no-store"
                    headers["Pragma"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(
    RateLimitMiddleware,
    limit=RATE_LIMIT_PER_MINUTE,
    extraction_limit=EXTRACTION_RATE_LIMIT,
)
app.add_middleware(SecurityHeadersMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    details: list[str] = []
    for error in exc.errors()[:6]:
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        details.append(f"{location}: {error.get('msg', 'Invalid value')}")
    return JSONResponse(
        status_code=422,
        content={"detail": "Invalid request.", "errors": details},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled API error")
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


def _search_http_error(exc: SearchError) -> StarletteHTTPException:
    if isinstance(exc, SearchConfigError):
        return StarletteHTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, SearchQuotaError):
        return StarletteHTTPException(status_code=429, detail=str(exc))
    return StarletteHTTPException(status_code=502, detail=str(exc))


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=APP_VERSION,
        database=False,
        google_cse_configured=google_config.cse_ready,
        google_places_configured=google_config.places_ready,
        active_jobs=manager.active_count(),
        timestamp=_iso(time.time()) or "",
    )


@app.get("/api/config")
async def config() -> dict[str, Any]:
    return {
        "version": APP_VERSION,
        "database": False,
        "storage": "browser",
        "google_cse_configured": google_config.cse_ready,
        "google_places_configured": google_config.places_ready,
        "google_configured": google_config.any_ready,
        "runtime": "vercel" if os.getenv("VERCEL") else "local",
        "sync_extraction": IS_SERVERLESS,
        "sync_max_seconds": SYNC_MAX_SECONDS,
        "robots_respected": True,
        "max_target_emails": 1000,
        "max_target_websites": 200,
        "max_domains": 200,
        "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
        "job_ttl_seconds": JOB_TTL_SECONDS,
        "rate_limit_per_minute": RATE_LIMIT_PER_MINUTE,
    }


@app.post("/api/parse-query")
async def parse_query_endpoint(payload: SearchRequest) -> dict[str, Any]:
    try:
        spec = parse_query(payload.query, overrides=_settings_overrides(payload.settings))
    except ValueError as exc:
        raise StarletteHTTPException(status_code=422, detail=str(exc)) from exc
    return {"parsed": spec.to_dict()}


@app.post("/api/search")
async def search(payload: SearchRequest) -> dict[str, Any]:
    await manager.start()
    assert manager.client is not None
    try:
        spec = parse_query(payload.query, overrides=_settings_overrides(payload.settings))
        discovery = await discover_websites(
            manager.client,
            spec,
            google_config,
            limit=payload.max_results,
            use_places=payload.use_places,
        )
    except SearchError as exc:
        raise _search_http_error(exc) from exc
    except ValueError as exc:
        raise StarletteHTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "parsed": spec.to_dict(),
        "websites": [website.to_dict() for website in discovery.websites],
        "queries": discovery.queries,
        "warnings": discovery.warnings,
        "providers": discovery.providers,
    }


@app.post("/api/extract", response_model=JobCreated, status_code=202)
async def extract(payload: ExtractRequest) -> JobCreated:
    await manager.start()
    try:
        spec = _build_spec(payload.query, payload.domains, payload.urls, payload.settings)
    except ValueError as exc:
        raise StarletteHTTPException(status_code=422, detail=str(exc)) from exc

    manual = _manual_targets(payload.domains, payload.urls, spec.target_websites)
    if manual:
        runner = lambda job: _run_targets(job, manual, payload.settings)  # noqa: E731
        kind = "domains"
    else:
        runner = lambda job: _run_query(job, spec, payload.settings)  # noqa: E731
        kind = "query"

    job = manager.create_job(
        kind=kind,
        runner=runner,
        parsed=spec.to_dict(),
        target_emails=spec.target_emails,
        target_websites=spec.target_websites,
    )
    return JobCreated(
        job_id=job.id,
        state=job.state,
        kind=job.kind,
        parsed=job.parsed,
        warnings=job.warnings,
    )


@app.post("/api/extract-sync")
async def extract_sync(payload: ExtractRequest) -> dict[str, Any]:
    """Run one extraction inside the request, for serverless hosts like Vercel."""
    await manager.start()
    try:
        spec = _build_spec(payload.query, payload.domains, payload.urls, payload.settings)
    except ValueError as exc:
        raise StarletteHTTPException(status_code=422, detail=str(exc)) from exc

    manual = _manual_targets(payload.domains, payload.urls, spec.target_websites)
    if manual:
        runner = lambda job: _run_targets(job, manual, payload.settings)  # noqa: E731
        kind = "domains"
    else:
        runner = lambda job: _run_query(job, spec, payload.settings)  # noqa: E731
        kind = "query"

    job = Job(
        id=uuid.uuid4().hex,
        kind=kind,
        runner=runner,
        parsed=spec.to_dict(),
        target_emails=spec.target_emails,
        target_websites=spec.target_websites,
    )
    job.total_sites = job.target_websites
    job.state = "running"
    job.phase = "starting"
    job.message = "Starting extraction"
    job.progress = 5

    try:
        await asyncio.wait_for(runner(job), timeout=SYNC_MAX_SECONDS)
        if not job.results:
            job.warnings.append(
                "No publicly listed email addresses were found. Try more websites, "
                "a different location, or a broader industry."
            )
        job.state = "done"
        job.phase = "done"
        job.progress = 100
        job.message = (
            f"Extraction complete: {len(job.results)} unique email(s) "
            f"from {job.stats['websites_crawled']} website(s)"
        )
    except asyncio.TimeoutError:
        if job.results:
            job.partial = True
            job.state = "done"
            job.phase = "done"
            job.progress = 100
            job.warnings.append(
                "The serverless time limit was reached before the target. "
                "These results are partial. Reduce the target or run the local "
                "async server for a complete crawl."
            )
            job.message = (
                f"Partial extraction: {len(job.results)} unique email(s) from "
                f"{job.stats['websites_crawled']} website(s) before the time limit"
            )
        else:
            job.state = "error"
            job.phase = "error"
            job.errors.append({
                "scope": "timeout",
                "message": (
                    "Extraction timed out on this serverless deployment. "
                    "Reduce the target or run the local async server for larger jobs."
                ),
            })
            job.message = "Extraction timed out"
    except (SearchConfigError, SearchQuotaError, SearchError) as exc:
        job.state = "error"
        job.phase = "error"
        job.errors.append({"scope": "search", "message": str(exc)})
        job.message = str(exc)
    except CrawlCancelled:
        job.state = "cancelled"
        job.phase = "cancelled"
        job.message = "Extraction cancelled"
    except Exception as exc:  # noqa: BLE001 - never leak internals to the client
        logger.exception("Synchronous extraction failed")
        job.state = "error"
        job.phase = "error"
        job.errors.append({"scope": "internal", "message": exc.__class__.__name__})
        job.message = "Extraction failed unexpectedly. Please try again."
    finally:
        job.finished_at = time.time()
        job.updated_at = job.finished_at

    return job.results_payload()


@app.post("/api/bulk-extract", response_model=JobCreated, status_code=202)
async def bulk_extract(payload: BulkExtractRequest) -> JobCreated:
    await manager.start()
    try:
        spec = _build_spec(None, payload.domains, payload.urls, payload.settings)
    except ValueError as exc:
        raise StarletteHTTPException(status_code=422, detail=str(exc)) from exc

    targets = _manual_targets(payload.domains, payload.urls, spec.target_websites)
    if not targets:
        raise StarletteHTTPException(status_code=422, detail="No valid websites were provided.")

    job = manager.create_job(
        kind="bulk",
        runner=lambda job: _run_bulk(job, payload, spec),  # noqa: E731
        parsed=spec.to_dict(),
        target_emails=spec.target_emails,
        target_websites=spec.target_websites,
    )
    return JobCreated(
        job_id=job.id,
        state=job.state,
        kind=job.kind,
        parsed=job.parsed,
        warnings=job.warnings,
    )


@app.post("/api/crawl")
async def crawl_single(payload: CrawlRequest) -> dict[str, Any]:
    await manager.start()
    assert manager.client is not None
    config = CrawlConfig(
        max_pages_per_domain=payload.max_pages,
        max_concurrent_sites=1,
        per_domain_concurrency=1,
        timeout=20.0,
        respect_robots=True,
        rate_limit_seconds=CRAWL_RATE_LIMIT_SECONDS,
    )
    crawler = Crawler(manager.client, config)
    domain = registrable_domain(normalize_domain_input(payload.url)) or ""
    site = await crawler.crawl_site(CrawlTarget(url=payload.url, domain=domain))
    return {
        "website": site.to_dict(),
        "stats": {
            "pages_crawled": site.pages_crawled,
            "pages_failed": site.pages_failed,
            "emails_discovered": site.raw_email_count,
            "unique_emails": len(site.emails),
            "duplicates_removed": max(0, site.raw_email_count - len(site.emails)),
        },
    }


@app.post("/api/cancel", response_model=CancelResponse)
async def cancel(payload: CancelRequest) -> CancelResponse:
    job = manager.cancel(payload.job_id)
    if job is None:
        raise StarletteHTTPException(status_code=404, detail="Job not found or already expired.")
    return CancelResponse(job_id=job.id, state=job.state, message=job.message)


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    job = manager.get(job_id)
    if job is None:
        raise StarletteHTTPException(status_code=404, detail="Job not found or already expired.")
    return job.snapshot()


@app.get("/api/jobs/{job_id}/results")
async def job_results(job_id: str) -> dict[str, Any]:
    job = manager.get(job_id)
    if job is None:
        raise StarletteHTTPException(status_code=404, detail="Job not found or already expired.")
    return job.results_payload()


@app.get("/api/results/{job_id}")
async def job_results_alias(job_id: str) -> dict[str, Any]:
    return await job_results(job_id)


@app.get("/api/progress/{job_id}")
async def progress_stream(job_id: str, request: Request) -> StreamingResponse:
    async def event_stream():
        last_payload: dict[str, Any] | None = None
        last_keepalive = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            job = manager.get(job_id)
            if job is None:
                yield (
                    "event: error\n"
                    f"data: {json.dumps({'detail': 'Job not found or expired.'})}\n\n"
                )
                break
            payload = job.snapshot()
            if payload != last_payload:
                yield f"data: {json.dumps(payload)}\n\n"
                last_payload = payload
                last_keepalive = time.monotonic()
            if job.state in TERMINAL_STATES:
                yield f"event: done\ndata: {json.dumps(payload)}\n\n"
                break
            if time.monotonic() - last_keepalive > 15:
                yield ": keep-alive\n\n"
                last_keepalive = time.monotonic()
            await asyncio.sleep(0.4)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/style.css")
async def stylesheet() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "style.css", media_type="text/css")


@app.get("/script.js")
async def script() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "script.js", media_type="application/javascript")


@app.get("/export.js")
async def export_script() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "export.js", media_type="application/javascript")


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/robots.txt")
async def robots() -> Response:
    return Response(
        "User-agent: *\nDisallow: /api/\nAllow: /\n",
        media_type="text/plain",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )
