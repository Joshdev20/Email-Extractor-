from __future__ import annotations

import json

import httpx
import pytest

from backend.search_engine import (
    DiscoveryResult,
    GoogleConfig,
    GoogleSearch,
    SearchConfigError,
    SearchError,
    SearchQuotaError,
    build_places_queries,
    build_search_queries,
    discover_websites,
    parse_query,
)

CONFIG = GoogleConfig(api_key="test-key", cse_id="test-cx", places_api_key="places-key")


def item(link: str, title: str = "", snippet: str = "") -> dict:
    return {"link": link, "title": title, "snippet": snippet}


class FakeGoogle:
    def __init__(self, *, cse_items=None, cse_error=None, places=None, places_error=None):
        self.cse_items = cse_items or []
        self.cse_error = cse_error
        self.places = places or []
        self.places_error = places_error
        self.cse_requests: list[httpx.URL] = []
        self.places_requests: list[httpx.Request] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/customsearch/v1":
            self.cse_requests.append(request.url)
            if self.cse_error is not None:
                return self.cse_error
            start = int(request.url.params.get("start", "1"))
            return httpx.Response(200, json={"items": self.cse_items if start == 1 else []})
        if request.url.path == "/v1/places:searchText":
            self.places_requests.append(request)
            if self.places_error is not None:
                return self.places_error
            return httpx.Response(200, json={"places": self.places})
        return httpx.Response(404, json={"error": "not found"})


async def make_client(fake: FakeGoogle) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))


def quota_response() -> httpx.Response:
    return httpx.Response(
        403,
        json={
            "error": {
                "code": 403,
                "message": "Daily Limit Exceeded",
                "errors": [{"reason": "dailyLimitExceeded"}],
            }
        },
    )


def invalid_key_response() -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "error": {
                "code": 400,
                "message": "API key not valid. Please pass a valid API key.",
                "errors": [{"reason": "keyInvalid"}],
            }
        },
    )


def cse_no_access_response() -> httpx.Response:
    return httpx.Response(
        403,
        json={
            "error": {
                "code": 403,
                "message": "This project does not have the access to Custom Search JSON API.",
                "errors": [{"reason": "forbidden"}],
            }
        },
    )


def places_access_denied_response() -> httpx.Response:
    return httpx.Response(
        403,
        json={"error": {"status": "PERMISSION_DENIED", "message": "Permission denied on the service."}},
    )


def places_quota_response() -> httpx.Response:
    return httpx.Response(
        429,
        json={"error": {"status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}},
    )


@pytest.mark.parametrize(
    "query, industry, country, city, category, count",
    [
        (
            "Find 500 publicly listed business email addresses belonging to real estate companies in the United Kingdom.",
            "real estate", "GB", None, None, 500,
        ),
        ("Find marketing agencies in Canada and extract their business emails.", "marketing", "CA", None, None, 50),
        ("Find web development companies in the United States.", "web development", "US", None, None, 50),
        ("Find customer support emails from e-commerce companies.", "e-commerce", None, None, "support", 50),
        ("Find recruitment agencies in London.", "recruitment", "GB", "London", None, 50),
        ("Find publicly listed business contacts for software companies in Germany.", "software", "DE", None, None, 50),
        ("Find 120 sales emails from fintech startups in Berlin, Germany.", "fintech", "DE", "Berlin", "sales", 120),
        ("Find Alibaba suppliers in the United States.", "Alibaba suppliers", "US", None, None, 50),
        ("Find Adobe 365 partners in Canada.", "Adobe 365 partners", "CA", None, None, 50),
        ("Find Microsoft Teams consultants in London.", "Microsoft Teams consultants", "GB", "London", None, 50),
        ("Find Microsoft 365 partners in Germany.", "Microsoft 365 partners", "DE", None, None, 50),
        ("Find Salesforce partners in Canada.", "Salesforce partners", "CA", None, None, 50),
    ],
)
def test_parse_query_examples(query, industry, country, city, category, count):
    spec = parse_query(query)
    assert spec.industry == industry
    assert spec.country_code == country
    assert spec.city == city
    assert spec.email_category == category
    assert spec.target_emails == count
    assert spec.search_queries


def test_parse_query_extracts_domains_without_country_false_positive():
    spec = parse_query("extract emails from example.com and acme.co.uk")
    assert spec.domains == ["example.com", "acme.co.uk"]
    assert spec.country_code is None
    assert spec.search_queries[0].startswith("site:example.com")


def test_parse_query_overrides():
    spec = parse_query(
        "Find marketing agencies in Canada",
        overrides={
            "industry": "legal services",
            "country": "Germany",
            "city": "Berlin",
            "email_category": "sales",
            "target_emails": 10,
            "target_websites": 3,
        },
    )
    assert spec.industry == "legal services"
    assert spec.country_code == "DE"
    assert spec.city == "Berlin"
    assert spec.email_category == "sales"
    assert spec.target_emails == 10
    assert spec.target_websites == 3


def test_build_places_queries_uses_company_agency_and_services_variants():
    spec = parse_query("Find real estate companies in the United Kingdom")
    assert build_places_queries(spec) == [
        "real estate companies in United Kingdom",
        "real estate agency in United Kingdom",
        "real estate services in United Kingdom",
    ]


def test_build_places_queries_keeps_category_forms():
    spec = parse_query("Find Alibaba suppliers in Canada")
    assert build_places_queries(spec) == ["Alibaba suppliers in Canada"]
    spec = parse_query("Find Microsoft Teams consultants in Canada")
    assert build_places_queries(spec) == ["Microsoft Teams consultants in Canada"]


def test_build_search_queries_uses_site_operators_for_domains():
    spec = parse_query("extract emails from example.com")
    queries = build_search_queries(spec)
    assert any("site:example.com" in query for query in queries)


@pytest.mark.asyncio
async def test_cse_search_success_and_pagination_parameters():
    fake = FakeGoogle(cse_items=[item("https://acme.com/contact", "Acme", "Contact")])
    async with await make_client(fake) as client:
        results = await GoogleSearch(client, CONFIG).cse_search("test", gl="uk", cr="countryGB")
    assert len(results) == 1
    assert results[0].domain == "acme.com"
    request = fake.cse_requests[0]
    assert request.params["start"] == "1"
    assert request.params["num"] == "10"
    assert request.params["key"] == "test-key"
    assert request.params["cx"] == "test-cx"
    assert request.params["gl"] == "uk"
    assert request.params["cr"] == "countryGB"


@pytest.mark.asyncio
async def test_cse_quota_error():
    fake = FakeGoogle(cse_error=quota_response())
    async with await make_client(fake) as client:
        with pytest.raises(SearchQuotaError):
            await GoogleSearch(client, CONFIG).cse_search("test")


@pytest.mark.asyncio
async def test_cse_invalid_key_error():
    fake = FakeGoogle(cse_error=invalid_key_response())
    async with await make_client(fake) as client:
        with pytest.raises(SearchConfigError):
            await GoogleSearch(client, CONFIG).cse_search("test")


@pytest.mark.asyncio
async def test_cse_closed_to_new_customers_message():
    fake = FakeGoogle(cse_error=cse_no_access_response())
    async with await make_client(fake) as client:
        with pytest.raises(SearchConfigError, match="closed to new customers"):
            await GoogleSearch(client, CONFIG).cse_search("test")


@pytest.mark.asyncio
async def test_places_search_success():
    fake = FakeGoogle(places=[{
        "displayName": {"text": "Acme"},
        "websiteUri": "https://acme.com/contact",
        "formattedAddress": "London, UK",
    }])
    async with await make_client(fake) as client:
        results = await GoogleSearch(client, CONFIG).places_search("marketing in London")
    assert len(results) == 1
    assert results[0].source == "places"
    assert results[0].domain == "acme.com"
    assert fake.places_requests[0].headers["x-goog-api-key"] == "places-key"


@pytest.mark.asyncio
async def test_places_search_follows_page_token():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if body.get("pageToken") == "next":
            return httpx.Response(200, json={"places": [{
                "displayName": {"text": "Beta"},
                "websiteUri": "https://beta.example",
            }]})
        return httpx.Response(200, json={
            "places": [{
                "displayName": {"text": "Alpha"},
                "websiteUri": "https://alpha.example",
            }],
            "nextPageToken": "next",
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await GoogleSearch(client, CONFIG).places_search("test", max_results=2)
    assert [result.domain for result in results] == ["alpha.example", "beta.example"]
    assert json.loads(requests[1].content)["pageToken"] == "next"


@pytest.mark.asyncio
async def test_places_quota_error():
    fake = FakeGoogle(places_error=places_quota_response())
    async with await make_client(fake) as client:
        with pytest.raises(SearchQuotaError):
            await GoogleSearch(client, CONFIG).places_search("marketing in London")


@pytest.mark.asyncio
async def test_places_access_denied_mentions_billing_and_new_api():
    fake = FakeGoogle(places_error=places_access_denied_response())
    async with await make_client(fake) as client:
        with pytest.raises(SearchConfigError, match="Places API \\(New\\)"):
            await GoogleSearch(client, CONFIG).places_search("marketing in London")


@pytest.mark.asyncio
async def test_discover_places_only_without_cse():
    fake = FakeGoogle(places=[{
        "displayName": {"text": "Acme"},
        "websiteUri": "https://acme.com/contact",
        "formattedAddress": "Toronto, Canada",
    }])
    config = GoogleConfig(api_key="", cse_id="", places_api_key="places-key")
    spec = parse_query("Find marketing agencies in Canada")
    async with await make_client(fake) as client:
        discovery = await discover_websites(client, spec, config, limit=5, use_places=True)
    assert [website.domain for website in discovery.websites] == ["acme.com"]
    assert "google_places" in discovery.providers
    assert not fake.cse_requests


@pytest.mark.asyncio
async def test_discover_cse_closure_raises_config_error():
    fake = FakeGoogle(cse_error=cse_no_access_response())
    spec = parse_query("Find marketing agencies in Canada")
    async with await make_client(fake) as client:
        with pytest.raises(SearchConfigError, match="closed to new customers"):
            await discover_websites(client, spec, CONFIG, limit=5, use_places=False, max_cse_calls=1)


@pytest.mark.asyncio
async def test_discover_requires_google_credentials():
    spec = parse_query("Find marketing agencies in Canada")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        with pytest.raises(SearchConfigError):
            await discover_websites(client, spec, GoogleConfig())


@pytest.mark.asyncio
async def test_discover_provided_domains_bypasses_google():
    spec = parse_query("extract emails from example.com and acme.co.uk")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        discovery = await discover_websites(client, spec, GoogleConfig())
    assert [website.domain for website in discovery.websites] == ["example.com", "acme.co.uk"]
    assert all(website.source == "provided" for website in discovery.websites)


@pytest.mark.asyncio
async def test_discover_dedupes_and_ranks_companies_above_social_and_directories():
    fake = FakeGoogle(cse_items=[
        item("https://acme.com/contact", "Acme contact", "Get in touch"),
        item("https://www.acme.com/about", "Acme about", "About Acme"),
        item("https://facebook.com/acme", "Acme on Facebook", ""),
        item("https://yelp.com/biz/acme", "Acme reviews", ""),
        item("https://beta.co.uk/contact", "Beta", "Contact Beta"),
    ])
    spec = parse_query("Find marketing agencies in Canada")
    async with await make_client(fake) as client:
        discovery = await discover_websites(
            client, spec, CONFIG, limit=10, use_places=False, max_cse_calls=1
        )
    domains = [website.domain for website in discovery.websites]
    assert domains.count("acme.com") == 1
    assert domains[0] in {"acme.com", "beta.co.uk"}
    assert domains[-1] in {"facebook.com", "yelp.com"}
    assert "google_cse" in discovery.providers


@pytest.mark.asyncio
async def test_discover_merges_places_results():
    fake = FakeGoogle(
        cse_items=[item("https://acme.com/contact", "Acme", "Contact")],
        places=[
            {"displayName": {"text": "Acme"}, "websiteUri": "https://acme.com", "formattedAddress": "Toronto"},
            {"displayName": {"text": "Northwind"}, "websiteUri": "https://northwind.ca", "formattedAddress": "Toronto"},
        ],
    )
    spec = parse_query("Find marketing agencies in Canada")
    async with await make_client(fake) as client:
        discovery = await discover_websites(
            client, spec, CONFIG, limit=10, use_places=True, max_cse_calls=1
        )
    domains = {website.domain for website in discovery.websites}
    assert {"acme.com", "northwind.ca"} <= domains
    assert "google_cse" in discovery.providers
    assert "google_places" in discovery.providers


@pytest.mark.asyncio
async def test_discover_returns_partial_results_when_places_quota_hit():
    fake = FakeGoogle(
        cse_items=[item("https://acme.com/contact", "Acme", "Contact")],
        places_error=places_quota_response(),
    )
    spec = parse_query("Find marketing agencies in Canada")
    async with await make_client(fake) as client:
        discovery = await discover_websites(
            client, spec, CONFIG, limit=10, use_places=True, max_cse_calls=1
        )
    assert discovery.websites
    assert discovery.warnings
    assert "google_cse" in discovery.providers


@pytest.mark.asyncio
async def test_discover_maps_invalid_key_to_config_error_when_no_results():
    fake = FakeGoogle(cse_error=invalid_key_response())
    spec = parse_query("Find marketing agencies in Canada")
    async with await make_client(fake) as client:
        with pytest.raises(SearchConfigError):
            await discover_websites(client, spec, CONFIG, limit=10, use_places=False, max_cse_calls=1)


@pytest.mark.asyncio
async def test_discover_raises_quota_when_no_results():
    fake = FakeGoogle(cse_error=quota_response())
    spec = parse_query("Find marketing agencies in Canada")
    async with await make_client(fake) as client:
        with pytest.raises(SearchQuotaError):
            await discover_websites(client, spec, CONFIG, limit=10, use_places=False, max_cse_calls=1)


@pytest.mark.asyncio
async def test_discover_raises_when_google_returns_nothing():
    fake = FakeGoogle(cse_items=[])
    spec = parse_query("Find marketing agencies in Canada")
    async with await make_client(fake) as client:
        with pytest.raises(SearchError):
            await discover_websites(client, spec, CONFIG, limit=10, use_places=False, max_cse_calls=1)


@pytest.mark.asyncio
async def test_discover_respects_limit():
    fake = FakeGoogle(cse_items=[item(f"https://site{i}.com/contact", f"Site {i}") for i in range(20)])
    spec = parse_query("Find marketing agencies in Canada")
    async with await make_client(fake) as client:
        discovery = await discover_websites(client, spec, CONFIG, limit=3, use_places=False, max_cse_calls=1)
    assert len(discovery.websites) == 3
