"""Pydantic request/response models and input sanitisation."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .security import normalise_url_for_fetch, normalize_domain_input

MAX_DOMAINS = 200
MAX_URLS = 50

_EMAIL_CATEGORIES = {
    "general", "sales", "marketing", "support", "careers", "press", "owner", "other",
}
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_text(value: str | None, *, max_length: int) -> str | None:
    if value is None:
        return None
    cleaned = _CONTROL_CHARS.sub("", str(value)).strip()
    if len(cleaned) > max_length:
        raise ValueError(f"Must be at most {max_length} characters")
    return cleaned


def _clean_domains(values: list[str]) -> list[str]:
    if len(values) > MAX_DOMAINS:
        raise ValueError(f"At most {MAX_DOMAINS} domains are allowed")
    cleaned: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            continue
        try:
            domain = normalize_domain_input(item)
        except ValueError as exc:
            raise ValueError(f"Invalid domain '{item[:80]}': {exc}") from exc
        if domain not in cleaned:
            cleaned.append(domain)
    return cleaned


def _clean_urls(values: list[str]) -> list[str]:
    if len(values) > MAX_URLS:
        raise ValueError(f"At most {MAX_URLS} URLs are allowed")
    cleaned: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            continue
        url = normalise_url_for_fetch(item)
        normalize_domain_input(url)
        if url not in cleaned:
            cleaned.append(url)
    return cleaned


class ExtractionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_emails: Annotated[int | None, Field(default=None, ge=1, le=1000)] = None
    target_websites: Annotated[int | None, Field(default=None, ge=1, le=200)] = None
    country: Annotated[str | None, Field(default=None, max_length=80)] = None
    industry: Annotated[str | None, Field(default=None, max_length=80)] = None
    city: Annotated[str | None, Field(default=None, max_length=80)] = None
    email_category: Annotated[str | None, Field(default=None, max_length=30)] = None
    concurrency: Annotated[int, Field(default=5, ge=1, le=10)] = 5
    max_pages_per_domain: Annotated[int, Field(default=6, ge=1, le=15)] = 6
    timeout: Annotated[float, Field(default=15.0, ge=5.0, le=60.0)] = 15.0
    use_places: bool = True
    respect_robots: bool = True

    @field_validator("country", "industry", "city", mode="before")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        return _clean_text(value, max_length=80)

    @field_validator("email_category", mode="before")
    @classmethod
    def clean_category(cls, value: str | None) -> str | None:
        cleaned = _clean_text(value, max_length=30)
        if not cleaned:
            return None
        lowered = cleaned.lower()
        if lowered not in _EMAIL_CATEGORIES:
            raise ValueError(
                "email_category must be one of: " + ", ".join(sorted(_EMAIL_CATEGORIES))
            )
        return lowered

    @field_validator("respect_robots")
    @classmethod
    def enforce_robots(cls, value: bool) -> bool:
        if not value:
            raise ValueError("robots.txt compliance cannot be disabled")
        return True


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: Annotated[str, Field(min_length=3, max_length=1000)]
    max_results: Annotated[int, Field(default=20, ge=1, le=200)] = 20
    use_places: bool = True
    settings: ExtractionSettings = Field(default_factory=ExtractionSettings)

    @field_validator("query")
    @classmethod
    def clean_query(cls, value: str) -> str:
        cleaned = _clean_text(value, max_length=1000) or ""
        if len(cleaned) < 3:
            raise ValueError("query must be at least 3 characters")
        return cleaned


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: Annotated[str | None, Field(default=None, max_length=1000)] = None
    domains: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    settings: ExtractionSettings = Field(default_factory=ExtractionSettings)

    @field_validator("query")
    @classmethod
    def clean_query(cls, value: str | None) -> str | None:
        cleaned = _clean_text(value, max_length=1000)
        if cleaned is not None and len(cleaned) < 3:
            raise ValueError("query must be at least 3 characters")
        return cleaned

    @field_validator("domains")
    @classmethod
    def clean_domains(cls, value: list[str]) -> list[str]:
        return _clean_domains(value)

    @field_validator("urls")
    @classmethod
    def clean_urls(cls, value: list[str]) -> list[str]:
        return _clean_urls(value)

    @model_validator(mode="after")
    def require_source(self) -> "ExtractRequest":
        if not self.query and not self.domains and not self.urls:
            raise ValueError("Provide a query, domains or URLs")
        return self


class BulkExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    domains: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    settings: ExtractionSettings = Field(default_factory=ExtractionSettings)

    @field_validator("domains")
    @classmethod
    def clean_domains(cls, value: list[str]) -> list[str]:
        return _clean_domains(value)

    @field_validator("urls")
    @classmethod
    def clean_urls(cls, value: list[str]) -> list[str]:
        return _clean_urls(value)

    @model_validator(mode="after")
    def require_source(self) -> "BulkExtractRequest":
        if not self.domains and not self.urls:
            raise ValueError("Provide at least one domain or URL")
        return self


class CrawlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: Annotated[str, Field(min_length=4, max_length=2048)]
    max_pages: Annotated[int, Field(default=6, ge=1, le=10)] = 6

    @field_validator("url")
    @classmethod
    def clean_url(cls, value: str) -> str:
        url = normalise_url_for_fetch(value)
        normalize_domain_input(url)
        return url


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    job_id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class EmailRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    domain: str
    website_domain: str
    company: str
    source_url: str
    category: str
    category_label: str
    country: str
    syntax_valid: bool
    method: str
    page_type: str
    extracted_at: str


class JobCreated(BaseModel):
    job_id: str
    state: str
    kind: str
    parsed: dict | None = None
    warnings: list[str] = Field(default_factory=list)


class CancelResponse(BaseModel):
    job_id: str
    state: str
    message: str


class HealthResponse(BaseModel):
    status: str
    version: str
    database: bool
    google_cse_configured: bool
    google_places_configured: bool
    active_jobs: int
    timestamp: str
