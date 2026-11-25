from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.search_engine import (
    DiscoveryResult,
    GoogleConfig,
    SearchConfigError,
    SearchQuotaError,
    SearchResult,
)
from tests.test_crawler import handler_for


def find_rate_limiter():
    stack = main.app.middleware_stack
    while stack is not None:
        if isinstance(stack, main.RateLimitMiddleware):
            return stack
        stack = getattr(stack, "app", None)
    return None


@pytest.fixture
def api_client(monkeypatch):
    handler = handler_for()

    async def fake_start(self):
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        self._janitor = None

    monkeypatch.setattr(main.JobManager, "start", fake_start)
    monkeypatch.setattr(main, "CRAWL_RATE_LIMIT_SECONDS", 0.0)
    with TestClient(main.app) as client:
        main.manager.jobs.clear()
        limiter = find_rate_limiter()
        if limiter:
            limiter.hits.clear()
        yield client


def wait_job(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["state"] in {"done", "error", "cancelled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


def test_health_and_config(api_client):
    health = api_client.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["database"] is False
    assert "google_cse_configured" in body

    docs = api_client.get("/api/docs")
    assert docs.status_code == 200
    assert "cdn.jsdelivr.net" in docs.headers["content-security-policy"]

    config = api_client.get("/api/config")
    assert config.status_code == 200
    body = config.json()
    assert body["storage"] == "browser"
    assert body["database"] is False
    assert body["robots_respected"] is True
    assert body["runtime"] in {"local", "vercel"}
    assert isinstance(body["sync_extraction"], bool)
    assert 3 <= body["sync_batch_size"] <= 50


def test_parse_query_endpoint(api_client):
    response = api_client.post(
        "/api/parse-query",
        json={"query": "Find 500 real estate company emails in the United Kingdom"},
    )
    assert response.status_code == 200
    parsed = response.json()["parsed"]
    assert parsed["industry"] == "real estate"
    assert parsed["country_code"] == "GB"
    assert parsed["target_emails"] == 500
    assert parsed["search_queries"]


def test_sync_extraction_end_to_end(api_client):
    response = api_client.post(
        "/api/extract-sync",
        json={
            "domains": ["example.com"],
            "settings": {"concurrency": 1, "max_pages_per_domain": 6},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "done", payload
    assert payload["complete"] is True
    assert payload["stats"]["websites_crawled"] == 1
    assert payload["stats"]["unique_emails"] == 10
    emails = {record["email"] for record in payload["results"]}
    assert "contact@northwind.co.uk" in emails


def test_sync_extraction_returns_partial_on_timeout(api_client, monkeypatch):
    async def slow_query(job, spec, settings):
        job.stats["websites_discovered"] = 1
        job.stats["websites_crawled"] = 1
        job.total_email_occurrences = 1
        job.results.append({
            "email": "owner@example.com",
            "company": "Example",
            "domain": "example.com",
            "category": "general",
            "syntax_valid": True,
            "source_url": "https://example.com/contact",
        })
        await asyncio.sleep(5)

    monkeypatch.setattr(main, "_run_query", slow_query)
    monkeypatch.setattr(main, "SYNC_MAX_SECONDS", 0.05)

    response = api_client.post(
        "/api/extract-sync",
        json={"query": "Find business emails in Canada"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "done"
    assert payload["partial"] is True
    assert payload["message"].startswith("Partial extraction:")
    assert [record["email"] for record in payload["results"]] == ["owner@example.com"]
    assert payload["warnings"]


def test_bulk_extraction_end_to_end(api_client):
    response = api_client.post(
        "/api/bulk-extract",
        json={
            "domains": ["example.com"],
            "settings": {"concurrency": 1, "max_pages_per_domain": 6},
        },
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    job = wait_job(api_client, job_id)
    assert job["state"] == "done", job
    assert job["stats"]["websites_discovered"] == 1
    assert job["stats"]["websites_crawled"] == 1
    assert job["stats"]["websites_failed"] == 0
    assert job["stats"]["unique_emails"] == 10
    assert job["stats"]["duplicates_removed"] > 0

    results = api_client.get(f"/api/jobs/{job_id}/results")
    assert results.status_code == 200
    payload = results.json()
    assert payload["complete"] is True
    emails = {record["email"] for record in payload["results"]}
    assert "contact@northwind.co.uk" in emails
    assert "jane.doe@northwind.co.uk" in emails
    for record in payload["results"]:
        assert record["extracted_at"]
        assert record["syntax_valid"] is True
        assert record["source_url"].startswith("https://example.com/")

    alias = api_client.get(f"/api/results/{job_id}")
    assert alias.status_code == 200
    assert alias.json()["results"] == payload["results"]


def test_concurrent_jobs_do_not_interfere(api_client):
    first = api_client.post(
        "/api/bulk-extract",
        json={"domains": ["example.com"], "settings": {"concurrency": 1, "max_pages_per_domain": 2}},
    ).json()["job_id"]
    second = api_client.post(
        "/api/bulk-extract",
        json={"domains": ["example.org"], "settings": {"concurrency": 1, "max_pages_per_domain": 2}},
    ).json()["job_id"]

    first_job = wait_job(api_client, first)
    second_job = wait_job(api_client, second)
    assert first_job["state"] == "done"
    assert second_job["state"] == "done"

    first_emails = {row["email"] for row in api_client.get(f"/api/jobs/{first}/results").json()["results"]}
    second_emails = {row["email"] for row in api_client.get(f"/api/jobs/{second}/results").json()["results"]}
    assert "info@northwind.co.uk" in first_emails
    assert "hello@acme.org" in second_emails
    assert "hello@acme.org" not in first_emails
    assert "info@northwind.co.uk" not in second_emails


def test_cancel_endpoint(api_client, monkeypatch):
    monkeypatch.setattr(main, "CRAWL_RATE_LIMIT_SECONDS", 0.75)
    created = api_client.post(
        "/api/extract",
        json={
            "domains": ["example.com"],
            "settings": {"concurrency": 1, "max_pages_per_domain": 12},
        },
    )
    assert created.status_code == 202
    job_id = created.json()["job_id"]

    cancelled = api_client.post("/api/cancel", json={"job_id": job_id})
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"

    job = api_client.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "cancelled"
    assert job["message"]


def test_cancel_unknown_job(api_client):
    response = api_client.post("/api/cancel", json={"job_id": "does-not-exist"})
    assert response.status_code == 404


def test_job_and_results_not_found(api_client):
    assert api_client.get("/api/jobs/nope").status_code == 404
    assert api_client.get("/api/jobs/nope/results").status_code == 404


def test_single_crawl_endpoint(api_client):
    response = api_client.post("/api/crawl", json={"url": "https://example.com/", "max_pages": 2})
    assert response.status_code == 200
    payload = response.json()
    assert payload["website"]["pages_crawled"] == 2
    assert payload["stats"]["unique_emails"] > 0
    assert payload["website"]["emails"][0]["source_url"].startswith("https://example.com/")


def test_validation_errors_are_safe(api_client):
    response = api_client.post("/api/extract", json={})
    assert response.status_code == 422
    body = response.json()
    assert body["detail"] == "Invalid request."
    assert body["errors"]

    response = api_client.post("/api/crawl", json={"url": "http://127.0.0.1/admin"})
    assert response.status_code == 422

    response = api_client.post("/api/extract", json={"query": "x"})
    assert response.status_code == 422


def test_search_endpoint_uses_discovery(api_client, monkeypatch):
    async def fake_discovery(client, spec, config, **kwargs):
        return DiscoveryResult(
            websites=[SearchResult(
                url="https://acme.com/contact",
                domain="acme.com",
                title="Acme",
                source="google_cse",
                score=80.0,
            )],
            queries=spec.search_queries,
            providers=["google_cse"],
        )

    monkeypatch.setattr(main, "discover_websites", fake_discovery)
    monkeypatch.setattr(main, "google_config", GoogleConfig(api_key="key", cse_id="cx"))
    response = api_client.post("/api/search", json={"query": "Find marketing agencies in Canada"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["websites"][0]["domain"] == "acme.com"
    assert payload["parsed"]["industry"] == "marketing"


def test_search_endpoint_maps_config_and_quota_errors(api_client, monkeypatch):
    async def config_error(client, spec, config, **kwargs):
        raise SearchConfigError("Google is not configured")

    monkeypatch.setattr(main, "discover_websites", config_error)
    response = api_client.post("/api/search", json={"query": "Find marketing agencies in Canada"})
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]

    async def quota_error(client, spec, config, **kwargs):
        raise SearchQuotaError("Quota exceeded")

    monkeypatch.setattr(main, "discover_websites", quota_error)
    response = api_client.post("/api/search", json={"query": "Find marketing agencies in Canada"})
    assert response.status_code == 429


def test_query_extraction_uses_discovery_then_crawls(api_client, monkeypatch):
    async def fake_discovery(client, spec, config, **kwargs):
        return DiscoveryResult(
            websites=[SearchResult(
                url="https://example.com/",
                domain="example.com",
                title="Example",
                source="google_cse",
                score=80.0,
            )],
            queries=spec.search_queries,
            providers=["google_cse"],
        )

    monkeypatch.setattr(main, "discover_websites", fake_discovery)
    monkeypatch.setattr(main, "google_config", GoogleConfig(api_key="key", cse_id="cx"))
    response = api_client.post(
        "/api/extract",
        json={"query": "Find property companies in the United Kingdom", "settings": {"max_pages_per_domain": 3}},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    job = wait_job(api_client, job_id)
    assert job["state"] == "done", job
    assert job["stats"]["websites_crawled"] == 1
    results = api_client.get(f"/api/jobs/{job_id}/results").json()
    assert any(row["website_domain"] == "example.com" for row in results["results"])


def test_sse_progress_reaches_done(api_client):
    created = api_client.post(
        "/api/bulk-extract",
        json={"domains": ["example.org"], "settings": {"concurrency": 1, "max_pages_per_domain": 1}},
    )
    job_id = created.json()["job_id"]
    wait_job(api_client, job_id)

    with api_client.stream("GET", f"/api/progress/{job_id}") as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "data:" in body
    assert "event: done" in body
    assert '"state": "done"' in body


def test_sse_unknown_job_reports_error(api_client):
    with api_client.stream("GET", "/api/progress/nope") as response:
        body = "".join(response.iter_text())
    assert "event: error" in body


def test_rate_limit_middleware_blocks_extraction_burst():
    async def dummy_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = main.RateLimitMiddleware(dummy_app, limit=5, extraction_limit=1)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/extract",
        "client": ("203.0.113.9", 1234),
        "headers": [],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def run_once():
        messages = []

        async def send(message):
            messages.append(message)

        await middleware(scope, receive, send)
        return messages

    first = asyncio.run(run_once())
    second = asyncio.run(run_once())
    assert first[0]["status"] == 200
    assert second[0]["status"] == 429
