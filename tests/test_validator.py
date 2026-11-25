from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.validator import (
    BulkExtractRequest,
    CrawlRequest,
    ExtractRequest,
    ExtractionSettings,
    SearchRequest,
    SiteAuth,
)


def test_extraction_settings_defaults_and_bounds():
    settings = ExtractionSettings()
    assert settings.concurrency == 5
    assert settings.max_pages_per_domain == 6
    assert settings.target_emails is None
    assert settings.respect_robots is True

    with pytest.raises(ValidationError):
        ExtractionSettings(concurrency=0)
    with pytest.raises(ValidationError):
        ExtractionSettings(max_pages_per_domain=20)
    with pytest.raises(ValidationError):
        ExtractionSettings(target_emails=5000)
    with pytest.raises(ValidationError):
        ExtractionSettings(email_category="finance")
    with pytest.raises(ValidationError):
        ExtractionSettings(respect_robots=False)
    with pytest.raises(ValidationError):
        ExtractionSettings(unknown_field=True)


def test_search_request_validation():
    request = SearchRequest(query="Find marketing agencies in Canada")
    assert request.max_results == 20
    assert SearchRequest(query="Find agencies", max_results=200).max_results == 200
    with pytest.raises(ValidationError):
        SearchRequest(query="ab")
    with pytest.raises(ValidationError):
        SearchRequest(query="Find agencies", max_results=1000)


def test_extract_request_requires_a_source():
    with pytest.raises(ValidationError):
        ExtractRequest()
    request = ExtractRequest(query="Find marketing agencies in Canada")
    assert request.query.startswith("Find")
    request = ExtractRequest(domains=["Example.com", "https://acme.co.uk/contact"])
    assert request.domains == ["example.com", "acme.co.uk"]


def test_extract_request_blocks_private_and_invalid_domains():
    with pytest.raises(ValidationError):
        ExtractRequest(domains=["localhost"])
    with pytest.raises(ValidationError):
        ExtractRequest(domains=["127.0.0.1"])
    with pytest.raises(ValidationError):
        ExtractRequest(domains=["169.254.169.254"])
    with pytest.raises(ValidationError):
        ExtractRequest(urls=["http://127.0.0.1/admin"])
    with pytest.raises(ValidationError):
        ExtractRequest(urls=["ftp://example.com"])


def test_bulk_request_deduplicates_domains():
    request = BulkExtractRequest(domains=["example.com", "https://example.com/about", "acme.co.uk"])
    assert request.domains == ["example.com", "acme.co.uk"]


def test_crawl_request_validation():
    request = CrawlRequest(url="example.com")
    assert request.url == "https://example.com"
    with pytest.raises(ValidationError):
        CrawlRequest(url="http://localhost/")
    with pytest.raises(ValidationError):
        CrawlRequest(url="https://example.com", max_pages=50)


def test_site_auth_normalises_domain_and_builds_headers():
    auth = SiteAuth(domain="https://Example.com/contact", authorization="Bearer abc", cookie="sid=1")
    assert auth.domain == "example.com"
    assert auth.headers == {"Authorization": "Bearer abc", "Cookie": "sid=1"}
    settings = ExtractionSettings(site_auth=[auth])
    assert settings.auth_header_map() == {"example.com": {"Authorization": "Bearer abc", "Cookie": "sid=1"}}


def test_site_auth_rejects_missing_or_unsafe_credentials():
    with pytest.raises(ValidationError):
        SiteAuth(domain="example.com")
    with pytest.raises(ValidationError):
        SiteAuth(domain="example.com", cookie="sid=1\nInjected: yes")
