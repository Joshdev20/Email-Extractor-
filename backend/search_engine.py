"""Natural-language query parsing and Google-powered website discovery.

Only Google's supported APIs are used: the Programmable Search Engine JSON API
and the Places API (New). Google result pages are never scraped.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from .crawler import registrable_domain
from .security import normalize_domain_input

CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
PLACES_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"

SOCIAL_DOMAINS = {
    "facebook.com", "linkedin.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "pinterest.com", "tiktok.com", "reddit.com", "medium.com",
    "quora.com", "github.com", "wikipedia.org", "threads.net", "snapchat.com",
}

DIRECTORY_DOMAINS = {
    "yelp.com", "yelp.co.uk", "yellowpages.com", "yellowpages.ca",
    "yellowpages.co.uk", "tripadvisor.com", "bark.com", "clutch.co",
    "goodfirms.co", "designrush.com", "sortlist.com", "trustpilot.com",
    "crunchbase.com", "glassdoor.com", "indeed.com", "upwork.com",
    "fiverr.com", "freelancer.com", "thumbtack.com", "angieslist.com",
    "houzz.com", "bbb.org", "manta.com", "hotfrog.com", "cylex.us.com",
    "brownbook.net", "thomsonlocal.com", "192.com", "pagesjaunes.fr",
    "gelbeseiten.de", "europages.co.uk", "kompass.com", "opendi.com",
    "chamberofcommerce.com", "b2bhint.com", "dnb.com", "zoominfo.com",
    "apollo.io", "rocketreach.co", "hunter.io", "leadiq.com", "lusha.com",
}

STOPWORDS = {
    "find", "get", "search", "looking", "for", "me", "please", "extract",
    "discover", "list", "publicly", "listed", "available", "business",
    "businesses", "email", "emails", "address", "addresses", "contact",
    "contacts", "belonging", "to", "from", "of", "in", "the", "and", "with",
    "that", "which", "who", "are", "is", "a", "an", "their", "up", "want",
    "need", "would", "like", "companies", "company", "agencies", "agency",
    "firms", "firm", "websites", "website", "sites", "site", "results",
    "leads", "lead", "real", "using", "about", "all", "on", "at", "by",
    "as", "or", "be", "can", "you", "your", "our", "we", "it", "its",
}

CATEGORY_ALIASES = {
    "sales": "sales",
    "marketing": "marketing",
    "support": "support",
    "customer support": "support",
    "customer service": "support",
    "help": "support",
    "career": "careers",
    "careers": "careers",
    "job": "careers",
    "jobs": "careers",
    "hr": "careers",
    "press": "press",
    "media": "press",
    "owner": "owner",
    "founder": "owner",
    "general": "general",
    "info": "general",
}

CATEGORY_QUERY_LABELS = {
    "sales": "sales",
    "marketing": "marketing",
    "support": "support",
    "careers": "careers",
    "press": "press",
    "owner": "owner",
    "general": "info",
}


@dataclass(slots=True)
class GoogleConfig:
    api_key: str = ""
    cse_id: str = ""
    places_api_key: str = ""

    @classmethod
    def from_env(cls) -> "GoogleConfig":
        return cls(
            api_key=os.getenv("GOOGLE_API_KEY", "").strip(),
            cse_id=os.getenv("GOOGLE_CSE_ID", "").strip(),
            places_api_key=os.getenv("GOOGLE_PLACES_API_KEY", "").strip(),
        )

    @property
    def cse_ready(self) -> bool:
        return bool(self.api_key and self.cse_id)

    @property
    def places_ready(self) -> bool:
        return bool(self.places_api_key)

    @property
    def any_ready(self) -> bool:
        return self.cse_ready or self.places_ready


class SearchError(Exception):
    """Base error for search discovery."""


class SearchConfigError(SearchError):
    """Google credentials are missing or rejected."""


class SearchQuotaError(SearchError):
    """Google API quota or rate limit has been exceeded."""


@dataclass(slots=True)
class SearchResult:
    url: str
    domain: str
    title: str = ""
    snippet: str = ""
    source: str = "google_cse"
    kind: str = "company"
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DiscoveryResult:
    websites: list[SearchResult]
    queries: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "websites": [website.to_dict() for website in self.websites],
            "queries": self.queries,
            "warnings": self.warnings,
            "providers": self.providers,
        }


@dataclass(slots=True)
class QuerySpec:
    raw: str
    industry: str | None = None
    country_code: str | None = None
    country_name: str | None = None
    city: str | None = None
    email_category: str | None = None
    target_emails: int = 50
    target_websites: int = 25
    domains: list[str] = field(default_factory=list)
    audience: str | None = None
    keywords: list[str] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)
    location_label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# alias -> (ISO code, canonical name)
_COUNTRY_ALIASES: dict[str, tuple[str, str]] = {}


def _register_country(code: str, name: str, aliases: tuple[str, ...]) -> None:
    for alias in (code.lower(), name.lower(), *aliases):
        _COUNTRY_ALIASES[alias.lower()] = (code, name)


_register_country("GB", "United Kingdom", (
    "uk", "u.k.", "united kingdom", "great britain", "britain", "england",
    "scotland", "wales", "northern ireland", "the united kingdom",
))
_register_country("US", "United States", (
    "usa", "u.s.", "u.s.a.", "america", "united states of america",
    "the united states", "the usa",
))
_register_country("CA", "Canada", ("canada",))
_register_country("AU", "Australia", ("australia", "aussie"))
_register_country("DE", "Germany", ("germany", "deutschland"))
_register_country("FR", "France", ("france",))
_register_country("ES", "Spain", ("spain", "espana"))
_register_country("IT", "Italy", ("italy", "italia"))
_register_country("NL", "Netherlands", ("netherlands", "holland", "the netherlands"))
_register_country("IE", "Ireland", ("ireland", "republic of ireland"))
_register_country("IN", "India", ("india",))
_register_country("SG", "Singapore", ("singapore",))
_register_country("AE", "United Arab Emirates", ("uae", "u.a.e.", "emirates", "united arab emirates"))
_register_country("ZA", "South Africa", ("south africa",))
_register_country("NZ", "New Zealand", ("new zealand",))
_register_country("BR", "Brazil", ("brazil", "brasil"))
_register_country("MX", "Mexico", ("mexico",))
_register_country("JP", "Japan", ("japan",))
_register_country("CN", "China", ("china",))
_register_country("SE", "Sweden", ("sweden",))
_register_country("NO", "Norway", ("norway",))
_register_country("DK", "Denmark", ("denmark",))
_register_country("FI", "Finland", ("finland",))
_register_country("CH", "Switzerland", ("switzerland",))
_register_country("AT", "Austria", ("austria",))
_register_country("BE", "Belgium", ("belgium",))
_register_country("PT", "Portugal", ("portugal",))
_register_country("PL", "Poland", ("poland",))
_register_country("CZ", "Czech Republic", ("czech republic", "czechia"))
_register_country("GR", "Greece", ("greece",))
_register_country("TR", "Turkey", ("turkey", "turkiye"))
_register_country("RU", "Russia", ("russia",))
_register_country("IL", "Israel", ("israel",))
_register_country("SA", "Saudi Arabia", ("saudi arabia", "ksa"))
_register_country("QA", "Qatar", ("qatar",))
_register_country("KW", "Kuwait", ("kuwait",))
_register_country("BH", "Bahrain", ("bahrain",))
_register_country("OM", "Oman", ("oman",))
_register_country("EG", "Egypt", ("egypt",))
_register_country("NG", "Nigeria", ("nigeria",))
_register_country("KE", "Kenya", ("kenya",))
_register_country("GH", "Ghana", ("ghana",))
_register_country("PK", "Pakistan", ("pakistan",))
_register_country("BD", "Bangladesh", ("bangladesh",))
_register_country("LK", "Sri Lanka", ("sri lanka",))
_register_country("NP", "Nepal", ("nepal",))
_register_country("PH", "Philippines", ("philippines",))
_register_country("MY", "Malaysia", ("malaysia",))
_register_country("ID", "Indonesia", ("indonesia",))
_register_country("TH", "Thailand", ("thailand",))
_register_country("VN", "Vietnam", ("vietnam", "viet nam"))
_register_country("KR", "South Korea", ("south korea", "korea"))
_register_country("TW", "Taiwan", ("taiwan",))
_register_country("HK", "Hong Kong", ("hong kong",))
_register_country("AR", "Argentina", ("argentina",))
_register_country("CL", "Chile", ("chile",))
_register_country("CO", "Colombia", ("colombia",))
_register_country("PE", "Peru", ("peru",))
_register_country("RO", "Romania", ("romania",))
_register_country("HU", "Hungary", ("hungary",))
_register_country("BG", "Bulgaria", ("bulgaria",))
_register_country("HR", "Croatia", ("croatia",))
_register_country("SI", "Slovenia", ("slovenia",))
_register_country("SK", "Slovakia", ("slovakia",))
_register_country("RS", "Serbia", ("serbia",))
_register_country("EE", "Estonia", ("estonia",))
_register_country("LV", "Latvia", ("latvia",))
_register_country("LT", "Lithuania", ("lithuania",))
_register_country("UA", "Ukraine", ("ukraine",))
_register_country("IS", "Iceland", ("iceland",))
_register_country("MT", "Malta", ("malta",))
_register_country("CY", "Cyprus", ("cyprus",))
_register_country("LU", "Luxembourg", ("luxembourg",))
_register_country("JO", "Jordan", ("jordan",))
_register_country("LB", "Lebanon", ("lebanon",))
_register_country("MA", "Morocco", ("morocco",))
_register_country("TN", "Tunisia", ("tunisia",))
_register_country("DZ", "Algeria", ("algeria",))

_CITIES: dict[str, tuple[str, str]] = {
    "london": ("London", "GB"), "manchester": ("Manchester", "GB"),
    "birmingham": ("Birmingham", "GB"), "leeds": ("Leeds", "GB"),
    "glasgow": ("Glasgow", "GB"), "liverpool": ("Liverpool", "GB"),
    "bristol": ("Bristol", "GB"), "edinburgh": ("Edinburgh", "GB"),
    "cardiff": ("Cardiff", "GB"), "belfast": ("Belfast", "GB"),
    "newcastle": ("Newcastle", "GB"), "sheffield": ("Sheffield", "GB"),
    "nottingham": ("Nottingham", "GB"), "cambridge": ("Cambridge", "GB"),
    "oxford": ("Oxford", "GB"), "reading": ("Reading", "GB"),
    "southampton": ("Southampton", "GB"), "brighton": ("Brighton", "GB"),
    "new york": ("New York", "US"), "new york city": ("New York", "US"),
    "los angeles": ("Los Angeles", "US"), "chicago": ("Chicago", "US"),
    "houston": ("Houston", "US"), "phoenix": ("Phoenix", "US"),
    "philadelphia": ("Philadelphia", "US"), "san antonio": ("San Antonio", "US"),
    "san diego": ("San Diego", "US"), "dallas": ("Dallas", "US"),
    "san jose": ("San Jose", "US"), "austin": ("Austin", "US"),
    "san francisco": ("San Francisco", "US"), "seattle": ("Seattle", "US"),
    "denver": ("Denver", "US"), "washington dc": ("Washington DC", "US"),
    "boston": ("Boston", "US"), "atlanta": ("Atlanta", "US"),
    "miami": ("Miami", "US"), "las vegas": ("Las Vegas", "US"),
    "portland": ("Portland", "US"), "nashville": ("Nashville", "US"),
    "toronto": ("Toronto", "CA"), "vancouver": ("Vancouver", "CA"),
    "montreal": ("Montreal", "CA"), "calgary": ("Calgary", "CA"),
    "ottawa": ("Ottawa", "CA"), "edmonton": ("Edmonton", "CA"),
    "sydney": ("Sydney", "AU"), "melbourne": ("Melbourne", "AU"),
    "brisbane": ("Brisbane", "AU"), "perth": ("Perth", "AU"),
    "adelaide": ("Adelaide", "AU"), "canberra": ("Canberra", "AU"),
    "auckland": ("Auckland", "NZ"), "wellington": ("Wellington", "NZ"),
    "berlin": ("Berlin", "DE"), "munich": ("Munich", "DE"),
    "hamburg": ("Hamburg", "DE"), "frankfurt": ("Frankfurt", "DE"),
    "cologne": ("Cologne", "DE"), "stuttgart": ("Stuttgart", "DE"),
    "paris": ("Paris", "FR"), "lyon": ("Lyon", "FR"),
    "marseille": ("Marseille", "FR"), "toulouse": ("Toulouse", "FR"),
    "madrid": ("Madrid", "ES"), "barcelona": ("Barcelona", "ES"),
    "valencia": ("Valencia", "ES"), "seville": ("Seville", "ES"),
    "rome": ("Rome", "IT"), "milan": ("Milan", "IT"),
    "naples": ("Naples", "IT"), "turin": ("Turin", "IT"),
    "amsterdam": ("Amsterdam", "NL"), "rotterdam": ("Rotterdam", "NL"),
    "the hague": ("The Hague", "NL"), "utrecht": ("Utrecht", "NL"),
    "dublin": ("Dublin", "IE"), "cork": ("Cork", "IE"),
    "mumbai": ("Mumbai", "IN"), "delhi": ("Delhi", "IN"),
    "new delhi": ("New Delhi", "IN"), "bangalore": ("Bangalore", "IN"),
    "bengaluru": ("Bengaluru", "IN"), "hyderabad": ("Hyderabad", "IN"),
    "chennai": ("Chennai", "IN"), "kolkata": ("Kolkata", "IN"),
    "pune": ("Pune", "IN"), "ahmedabad": ("Ahmedabad", "IN"),
    "singapore": ("Singapore", "SG"), "dubai": ("Dubai", "AE"),
    "abu dhabi": ("Abu Dhabi", "AE"), "sharjah": ("Sharjah", "AE"),
    "johannesburg": ("Johannesburg", "ZA"), "cape town": ("Cape Town", "ZA"),
    "durban": ("Durban", "ZA"), "lagos": ("Lagos", "NG"),
    "nairobi": ("Nairobi", "KE"), "karachi": ("Karachi", "PK"),
    "lahore": ("Lahore", "PK"), "islamabad": ("Islamabad", "PK"),
    "dhaka": ("Dhaka", "BD"), "manila": ("Manila", "PH"),
    "kuala lumpur": ("Kuala Lumpur", "MY"), "jakarta": ("Jakarta", "ID"),
    "bangkok": ("Bangkok", "TH"), "ho chi minh": ("Ho Chi Minh", "VN"),
    "hanoi": ("Hanoi", "VN"), "seoul": ("Seoul", "KR"),
    "taipei": ("Taipei", "TW"), "hong kong": ("Hong Kong", "HK"),
    "beijing": ("Beijing", "CN"), "shanghai": ("Shanghai", "CN"),
    "shenzhen": ("Shenzhen", "CN"), "tokyo": ("Tokyo", "JP"),
    "osaka": ("Osaka", "JP"), "sao paulo": ("Sao Paulo", "BR"),
    "rio de janeiro": ("Rio de Janeiro", "BR"), "mexico city": ("Mexico City", "MX"),
    "buenos aires": ("Buenos Aires", "AR"), "santiago": ("Santiago", "CL"),
    "bogota": ("Bogota", "CO"), "lima": ("Lima", "PE"),
    "stockholm": ("Stockholm", "SE"), "oslo": ("Oslo", "NO"),
    "copenhagen": ("Copenhagen", "DK"), "helsinki": ("Helsinki", "FI"),
    "zurich": ("Zurich", "CH"), "geneva": ("Geneva", "CH"),
    "vienna": ("Vienna", "AT"), "brussels": ("Brussels", "BE"),
    "lisbon": ("Lisbon", "PT"), "porto": ("Porto", "PT"),
    "warsaw": ("Warsaw", "PL"), "krakow": ("Krakow", "PL"),
    "prague": ("Prague", "CZ"), "athens": ("Athens", "GR"),
    "istanbul": ("Istanbul", "TR"), "moscow": ("Moscow", "RU"),
    "tel aviv": ("Tel Aviv", "IL"), "riyadh": ("Riyadh", "SA"),
    "doha": ("Doha", "QA"), "cairo": ("Cairo", "EG"),
    "casablanca": ("Casablanca", "MA"), "bucharest": ("Bucharest", "RO"),
    "budapest": ("Budapest", "HU"), "sofia": ("Sofia", "BG"),
    "zagreb": ("Zagreb", "HR"), "belgrade": ("Belgrade", "RS"),
    "tallinn": ("Tallinn", "EE"), "riga": ("Riga", "LV"),
    "vilnius": ("Vilnius", "LT"), "kyiv": ("Kyiv", "UA"),
    "reykjavik": ("Reykjavik", "IS"), "valletta": ("Valletta", "MT"),
    "nicosia": ("Nicosia", "CY"), "amman": ("Amman", "JO"),
    "beirut": ("Beirut", "LB"),
}

_INDUSTRY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\balibaba(?:\.com)?\b|\baliexpress\b"), "Alibaba suppliers"),
    (re.compile(r"\badobe\s+365\b"), "Adobe 365 partners"),
    (re.compile(r"\badobe\s+creative\s+cloud\b"), "Adobe Creative Cloud partners"),
    (re.compile(r"\badobe\b"), "Adobe partners"),
    (re.compile(r"\bmicrosoft\s+teams\b|\bms\s+teams\b"), "Microsoft Teams consultants"),
    (re.compile(r"\bmicrosoft\s*365\b|\boffice\s*365\b"), "Microsoft 365 partners"),
    (re.compile(r"\bsalesforce\b"), "Salesforce partners"),
    (re.compile(r"\bhubspot\b"), "HubSpot agencies"),
    (re.compile(r"\bshopify\b"), "Shopify agencies"),
    (re.compile(r"\baws\b|\bamazon\s+web\s+services\b"), "AWS partners"),
    (re.compile(r"\bgoogle\s+workspace\b|\bg\s*suite\b"), "Google Workspace partners"),
    (re.compile(r"\bsap\b"), "SAP consultants"),
    (re.compile(r"\boracle\b"), "Oracle partners"),
    (re.compile(r"\bcisco\b"), "Cisco partners"),
    (re.compile(r"\bzoom\b"), "Zoom partners"),
    (re.compile(r"\bslack\b"), "Slack consultants"),
    (re.compile(r"\breal[\s-]?estate\b|\bproperty\b|\brealtors?\b"), "real estate"),
    (re.compile(r"\be-?commerce\b|\bonline\s+retail\b"), "e-commerce"),
    (re.compile(r"\bweb\s+development\b|\bweb\s+design\b|\bweb\s+agenc"), "web development"),
    (re.compile(r"\bsoftware\b|\bsaas\b"), "software"),
    (re.compile(r"\brecruit(?:ment|ing)?\b|\bstaffing\b|\bheadhunt"), "recruitment"),
    (re.compile(r"\bmarketing\b|\bdigital\s+marketing\b|\bseo\b"), "marketing"),
    (re.compile(r"\blaw\b|\blegal\b|\bsolicitors?\b|\battorneys?\b"), "legal services"),
    (re.compile(r"\baccount(?:ing|ants?)\b|\bbookkeep"), "accounting"),
    (re.compile(r"\bhealthcare\b|\bmedical\b|\bclinics?\b|\bhospitals?\b"), "healthcare"),
    (re.compile(r"\bdental\b|\bdentists?\b"), "dental"),
    (re.compile(r"\bfinancial\b|\bfinance\b|\bbanking\b"), "financial services"),
    (re.compile(r"\binsurance\b|\binsurers?\b"), "insurance"),
    (re.compile(r"\bconstruction\b|\bbuilders?\b|\bcontractors?\b"), "construction"),
    (re.compile(r"\bmanufactur(?:ing|ers?)\b|\bfactories\b"), "manufacturing"),
    (re.compile(r"\blogistics\b|\bshipping\b|\bfreight\b|\btransport\b"), "logistics"),
    (re.compile(r"\bhospitality\b|\bhotels?\b|\brestaurants?\b|\bcafes?\b"), "hospitality"),
    (re.compile(r"\btravel\b|\btourism\b|\btravel\s+agenc"), "travel"),
    (re.compile(r"\beducation\b|\bschools?\b|\bcolleges?\b|\buniversit"), "education"),
    (re.compile(r"\bautomotive\b|\bcar\s+dealers?\b|\bgarages?\b"), "automotive"),
    (re.compile(r"\bretail\b|\be-?commerce\b|\bshops?\b|\bstores?\b"), "retail"),
    (re.compile(r"\btechnology\b|\btech\b|\bit\s+services\b|\bit\s+companies\b"), "technology"),
    (re.compile(r"\bfintech\b"), "fintech"),
    (re.compile(r"\bbiotech\b|\bpharma(?:ceutical)?s?\b"), "biotechnology"),
    (re.compile(r"\bconsult(?:ing|ants?|ancies?)\b"), "consulting"),
    (re.compile(r"\barchitect(?:ure|s)?\b"), "architecture"),
    (re.compile(r"\bengineering\b|\bengineers?\b"), "engineering"),
    (re.compile(r"\badvertising\b|\bmedia\b|\bpr\s+agenc"), "media and advertising"),
    (re.compile(r"\bphotograph(?:y|ers?)\b"), "photography"),
    (re.compile(r"\bdesign\b|\bdesigners?\b"), "design"),
    (re.compile(r"\bplumb(?:ing|ers?)\b"), "plumbing"),
    (re.compile(r"\broofing\b|\broofers?\b"), "roofing"),
    (re.compile(r"\bsolar\b|\brenewable\b|\benergy\b"), "renewable energy"),
    (re.compile(r"\bcrypto\b|\bblockchain\b|\bweb3\b"), "blockchain"),
    (re.compile(r"\bsecurity\s+services\b|\bsecurity\s+companies\b"), "security services"),
    (re.compile(r"\bcleaning\b|\bcleaners?\b"), "cleaning services"),
    (re.compile(r"\blandscap(?:ing|ers?)\b"), "landscaping"),
    (re.compile(r"\bmoving\b|\bremovals?\b"), "moving services"),
    (re.compile(r"\bcharit(?:y|ies)\b"), "charity"),
    (re.compile(r"\bnon-?profits?\b|\bngos?\b"), "nonprofit"),
)

_BUSINESS_NOUNS = (
    "companies", "company", "agencies", "agency", "firms", "firm",
    "businesses", "business", "startups", "startup", "shops", "shop",
    "stores", "store", "providers", "provider", "services", "service",
    "organizations", "organization", "organisations", "organisation",
    "enterprises", "enterprise", "brands", "brand", "vendors", "vendor",
    "suppliers", "supplier", "consultancies", "consultancy", "practices",
    "practice", "studios", "studio", "solutions", "solution",
)

_ASSET_TLDS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "css", "js", "json",
    "xml", "pdf", "zip", "mp3", "mp4", "woff", "woff2", "ttf",
}


def _country_gl(code: str) -> str:
    return "uk" if code == "GB" else code.lower()


def _lookup_country(value: str) -> tuple[str, str] | None:
    key = re.sub(r"\s+", " ", value.strip().lower())
    return _COUNTRY_ALIASES.get(key)


def _find_country(text: str) -> tuple[str, str] | None:
    for alias in sorted(_COUNTRY_ALIASES, key=len, reverse=True):
        if len(alias) <= 2 and alias not in {"uk"}:
            continue
        if re.search(rf"(?<![\w]){re.escape(alias)}(?![\w])", text):
            return _COUNTRY_ALIASES[alias]
    return None


def _find_city(text: str) -> tuple[str, str] | None:
    for city in sorted(_CITIES, key=len, reverse=True):
        pattern = (
            rf"(?:\b(?:in|near|around|from|based\s+in|located\s+in)\s+{re.escape(city)}\b)"
            rf"|(?:\b{re.escape(city)}\s*,)"
            rf"|(?:\b{re.escape(city)}\s*$)"
        )
        if re.search(pattern, text):
            return _CITIES[city]
    match = re.search(
        r"\b(?:in|near|around|based\s+in|located\s+in)\s+"
        r"([a-z][a-z .'-]{1,40}?)"
        r"(?=\s*[,.]|\s+(?:and|with|for|to|that|who)\b|$)",
        text,
    )
    if match:
        candidate = match.group(1).strip()
        if not _lookup_country(candidate):
            return (candidate.title(), "")
    return None


def _extract_count(text: str) -> int | None:
    match = re.search(
        r"\b(\d{1,4})\s*(?:publicly\s+listed\s+)?(?:business\s+)?"
        r"(?:email|emails|addresses?|contacts?|leads?|results?)\b",
        text,
    )
    if not match:
        match = re.search(
            r"\b(?:find|get|extract|discover|search\s+for|list)\s+"
            r"(?:up\s+to\s+)?(\d{1,4})\b",
            text,
        )
    if not match:
        return None
    return max(1, min(1000, int(match.group(1))))


def _extract_website_count(text: str) -> int | None:
    match = re.search(r"\b(\d{1,3})\s+(?:websites?|sites?|domains?)\b", text)
    if not match:
        return None
    return max(1, min(200, int(match.group(1))))


def _extract_category(text: str) -> str | None:
    pattern = (
        r"\b(" + "|".join(
            sorted((re.escape(key) for key in CATEGORY_ALIASES), key=len, reverse=True)
        ) + r")\s+(?:email|emails|contacts?|addresses?)\b"
    )
    match = re.search(pattern, text)
    if not match:
        return None
    return CATEGORY_ALIASES[match.group(1).lower()]


def _extract_industry(text: str) -> str | None:
    for pattern, label in _INDUSTRY_PATTERNS:
        if pattern.search(text):
            return label
    cleaned = text
    noise = (
        r"\b(?:find|get|search|looking|for|me|please|extract|discover|list|"
        r"publicly|listed|available|business|businesses|email|emails|"
        r"address|addresses|contact|contacts|belonging|to|from|of|with|that|"
        r"which|who|are|is|their|the|and|in|up|want|need|would|like)\b"
    )
    cleaned = re.sub(noise, " ", cleaned)
    cleaned = re.sub(r"\b\d{1,4}\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    nouns = "|".join(_BUSINESS_NOUNS)
    match = re.search(rf"([a-z0-9&'’./ -]{{2,60}}?)\s+(?:{nouns})\b", cleaned)
    if match:
        value = re.sub(r"\s+", " ", match.group(1)).strip(" -&")
        value = re.sub(r"^(?:the|a|an)\s+", "", value)
        if value and value not in {"business", "email", "contact"}:
            return value[:60]
    return None


def _extract_audience(text: str) -> str | None:
    match = re.search(
        r"\b(?:targeting|aimed\s+at|serving|for)\s+"
        r"(small\s+businesses|smbs?|smes?|enterprises?|startups?|"
        r"consumers?|b2b|b2c|professionals?)\b",
        text,
    )
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else None


def _extract_domains(text: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(
        r"\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})\b",
        text.lower(),
    ):
        candidate = match.group(1).rstrip(".")
        if candidate.rsplit(".", 1)[-1] in _ASSET_TLDS:
            continue
        if candidate in _COUNTRY_ALIASES or candidate in {"co.uk", "com.au", "co.nz"}:
            continue
        if candidate.startswith(("www.", "http.")):
            candidate = candidate[4:]
        try:
            domain = normalize_domain_input(candidate)
        except ValueError:
            continue
        if domain not in found:
            found.append(domain)
    return found[:50]


def _extract_keywords(text: str) -> list[str]:
    tokens = re.findall(r"[a-z][a-z0-9+#.-]{2,}", text.lower())
    seen: list[str] = []
    for token in tokens:
        token = token.strip(".-")
        if token in STOPWORDS or token in seen:
            continue
        if len(token) < 3:
            continue
        seen.append(token)
        if len(seen) >= 10:
            break
    return seen


def build_search_queries(spec: QuerySpec) -> list[str]:
    exclusions = (
        " -site:facebook.com -site:linkedin.com -site:instagram.com "
        "-site:twitter.com -site:x.com -site:youtube.com -site:pinterest.com"
    )
    if spec.domains:
        return [
            f'site:{domain} ("contact" OR "contact us" OR email)'
            for domain in spec.domains[:8]
        ]

    industry = spec.industry or "businesses"
    base = f'"{industry}"'
    if spec.location_label:
        base += f' "{spec.location_label}"'
    queries = [
        f'{base} ("contact us" OR "get in touch" OR email){exclusions}',
        f'{base} email{exclusions}',
        f'{base} ("about us" OR "our team"){exclusions}',
    ]
    if spec.email_category and spec.email_category not in {"general", "other"}:
        label = CATEGORY_QUERY_LABELS.get(spec.email_category, spec.email_category)
        queries.append(f'{base} "{label}" email{exclusions}')
    return queries[:4]


def build_places_queries(spec: QuerySpec) -> list[str]:
    """Build natural-language Places queries; Text Search does not support operators."""
    location = (spec.location_label or "").strip()
    industry = (spec.industry or "").strip()
    if not industry:
        base = "businesses"
        return [f"{base} in {location}" if location else base]

    lowered = industry.lower()
    business_forms = (
        "company", "companies", "agency", "agencies", "supplier", "suppliers",
        "vendor", "vendors", "consultant", "consultants", "partner", "partners",
        "provider", "providers", "firm", "firms", "reseller", "resellers",
        "integrator", "integrators",
    )
    if lowered.endswith(business_forms):
        terms = [industry]
    else:
        terms = [f"{industry} companies", f"{industry} agency", f"{industry} services"]

    queries: list[str] = []
    for term in terms:
        query = f"{term} in {location}" if location else term
        if query not in queries:
            queries.append(query)
    return queries[:3]


def parse_query(raw: str, *, overrides: dict[str, Any] | None = None) -> QuerySpec:
    text = re.sub(r"\s+", " ", (raw or "").strip())
    lower = text.lower()
    if not text:
        raise ValueError("A search description is required")
    if len(text) > 1000:
        raise ValueError("Search description is too long")

    domains = _extract_domains(text)
    location_text = lower
    for domain in domains:
        location_text = location_text.replace(domain, " ")

    country = _find_country(location_text)
    city_match = _find_city(location_text)
    city_name: str | None = None
    city_country: str | None = None
    if city_match:
        city_name, city_country = city_match

    country_code = country[0] if country else city_country
    country_name = country[1] if country else None
    if not country_name and country_code:
        country_name = next(
            (name for code, name in _COUNTRY_ALIASES.values() if code == country_code),
            None,
        )

    parsed_count = _extract_count(lower)
    target_emails = parsed_count or 50
    target_websites = _extract_website_count(lower)
    if target_websites is None:
        target_websites = min(150, max(10, math.ceil(target_emails / 4))) if parsed_count else 25

    spec = QuerySpec(
        raw=text,
        industry=_extract_industry(lower),
        country_code=country_code,
        country_name=country_name,
        city=city_name,
        email_category=_extract_category(lower),
        target_emails=target_emails,
        target_websites=target_websites,
        domains=domains,
        audience=_extract_audience(lower),
        keywords=_extract_keywords(lower),
    )

    overrides = overrides or {}
    if overrides.get("industry"):
        spec.industry = str(overrides["industry"]).strip()[:80]
    if overrides.get("city"):
        spec.city = str(overrides["city"]).strip()[:80]
    if overrides.get("country"):
        raw_country = str(overrides["country"]).strip()
        mapped = _lookup_country(raw_country)
        if mapped:
            spec.country_code, spec.country_name = mapped
        elif raw_country:
            spec.country_name = raw_country[:80]
            spec.country_code = None
    if overrides.get("email_category"):
        category = str(overrides["email_category"]).strip().lower()
        spec.email_category = CATEGORY_ALIASES.get(category, category)[:30]
    if overrides.get("target_emails"):
        spec.target_emails = max(1, min(1000, int(overrides["target_emails"])))
    if overrides.get("target_websites"):
        spec.target_websites = max(1, min(200, int(overrides["target_websites"])))

    location_parts = [part for part in (spec.city, spec.country_name) if part]
    spec.location_label = ", ".join(location_parts) if location_parts else None
    spec.search_queries = build_search_queries(spec)
    return spec


def _domain_of(url: str) -> str:
    try:
        hostname = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return hostname.lower().removeprefix("www.")


def _kind_of(domain: str) -> str:
    if domain in SOCIAL_DOMAINS:
        return "social"
    if domain in DIRECTORY_DOMAINS or registrable_domain(domain) in DIRECTORY_DOMAINS:
        return "directory"
    return "company"


def _rank_website(url: str, title: str, snippet: str, spec: QuerySpec, source: str) -> float:
    domain = _domain_of(url)
    score = 50.0
    if source == "places":
        score += 20
    if source == "provided":
        score += 50
    path = urlsplit(url).path.lower()
    if re.search(r"contact|kontakt|about|team|company|impressum|imprint", path):
        score += 12
    if path in {"", "/"}:
        score += 8
    kind = _kind_of(domain)
    if kind == "social":
        score -= 45
    elif kind == "directory":
        score -= 18
    haystack = f"{title} {snippet} {domain}".lower()
    if spec.industry:
        for token in spec.industry.lower().split():
            if len(token) > 3 and token in haystack:
                score += 4
    if spec.city and spec.city.lower() in haystack:
        score += 5
    if spec.country_name and spec.country_name.lower() in haystack:
        score += 4
    if spec.country_code:
        tld = domain.rsplit(".", 1)[-1]
        if tld.lower() == spec.country_code.lower() or (
            spec.country_code == "GB" and tld == "uk"
        ):
            score += 8
    return score


def _google_error_reason(payload: dict[str, Any]) -> tuple[str, str]:
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        reasons = error.get("errors")
        reason = ""
        if isinstance(reasons, list) and reasons and isinstance(reasons[0], dict):
            reason = str(reasons[0].get("reason", ""))
        return reason, str(error.get("message", "Google API error"))
    if isinstance(error, str):
        return "", error
    return "", "Google API error"


class GoogleSearch:
    def __init__(self, client: httpx.AsyncClient, config: GoogleConfig) -> None:
        self.client = client
        self.config = config

    async def cse_search(
        self,
        query: str,
        *,
        start: int = 1,
        num: int = 10,
        gl: str | None = None,
        cr: str | None = None,
    ) -> list[SearchResult]:
        if not self.config.cse_ready:
            raise SearchConfigError(
                "Google Custom Search is not configured. Set GOOGLE_API_KEY and GOOGLE_CSE_ID."
            )
        params = {
            "key": self.config.api_key,
            "cx": self.config.cse_id,
            "q": query,
            "num": min(max(num, 1), 10),
            "start": max(start, 1),
            "safe": "off",
        }
        if gl:
            params["gl"] = gl
        if cr:
            params["cr"] = cr
        try:
            response = await self.client.get(CSE_ENDPOINT, params=params, timeout=20.0)
        except httpx.HTTPError as exc:
            raise SearchError(f"Google search request failed ({exc.__class__.__name__})") from exc

        if response.status_code != 200:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            reason, message = _google_error_reason(payload)
            quota_reasons = {
                "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded",
                "quotaExceeded", "RESOURCE_EXHAUSTED",
            }
            config_reasons = {"keyInvalid", "accessNotConfigured", "badRequest", "invalid"}
            if "does not have the access to custom search json api" in message.lower():
                raise SearchConfigError(
                    "Google Custom Search JSON API is closed to new customers and this "
                    "Google Cloud project does not have access. Use Places API (New) via "
                    "GOOGLE_PLACES_API_KEY, or use an existing project that already has "
                    "Custom Search access. See "
                    "https://developers.google.com/custom-search/v1/overview"
                )
            if response.status_code == 429 or reason in quota_reasons:
                raise SearchQuotaError(
                    "Google Custom Search quota or rate limit exceeded. "
                    "Wait for the quota to reset or raise your Google Cloud limits."
                )
            if response.status_code in (400, 401, 403) and reason in config_reasons:
                raise SearchConfigError(
                    f"Google Custom Search rejected the credentials: {message}. "
                    "Check GOOGLE_API_KEY and GOOGLE_CSE_ID."
                )
            if response.status_code in (401, 403):
                detail = f" Google said: {message}." if message else ""
                raise SearchConfigError(
                    "Google Custom Search access was denied." + detail +
                    " Verify the key allows Custom Search API and that the API is enabled "
                    "in the key's project. Note that Custom Search JSON API is closed to "
                    "new customers; use Places API (New) instead."
                )
            raise SearchError(f"Google search failed (HTTP {response.status_code}).")

        try:
            payload = response.json()
        except ValueError as exc:
            raise SearchError("Google search returned an invalid response") from exc
        items = payload.get("items") or []
        results: list[SearchResult] = []
        for item in items:
            link = str(item.get("link", "")).strip()
            if not link.startswith(("http://", "https://")):
                continue
            domain = _domain_of(link)
            if not domain:
                continue
            results.append(SearchResult(
                url=link,
                domain=domain,
                title=str(item.get("title", ""))[:200],
                snippet=str(item.get("snippet", ""))[:400],
                source="google_cse",
                kind=_kind_of(domain),
            ))
        return results

    async def places_search(self, query: str, *, max_results: int = 20) -> list[SearchResult]:
        if not self.config.places_ready:
            raise SearchConfigError(
                "Google Places is not configured. Set GOOGLE_PLACES_API_KEY."
            )
        max_results = min(max(max_results, 1), 60)
        headers = {
            "X-Goog-Api-Key": self.config.places_api_key,
            "X-Goog-FieldMask": (
                "places.displayName,places.websiteUri,places.formattedAddress,"
                "places.primaryTypeDisplayName,nextPageToken"
            ),
            "Content-Type": "application/json",
        }
        results: list[SearchResult] = []
        seen: set[str] = set()
        page_token: str | None = None
        pages = 0

        while len(results) < max_results and pages < 3:
            pages += 1
            body: dict[str, Any] = {
                "textQuery": query,
                "pageSize": min(20, max_results - len(results)),
                "languageCode": "en",
            }
            if page_token:
                body["pageToken"] = page_token
            try:
                response = await self.client.post(
                    PLACES_ENDPOINT, headers=headers, json=body, timeout=20.0
                )
            except httpx.HTTPError as exc:
                raise SearchError(
                    f"Google Places request failed ({exc.__class__.__name__})"
                ) from exc

            if response.status_code != 200:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                error = payload.get("error") if isinstance(payload, dict) else None
                message = ""
                status = ""
                if isinstance(error, dict):
                    message = str(error.get("message", ""))
                    status = str(error.get("status", ""))
                if response.status_code == 429 or status == "RESOURCE_EXHAUSTED":
                    raise SearchQuotaError(
                        "Google Places quota or rate limit exceeded. Check your billing "
                        "and Places API quotas."
                    )
                if response.status_code in (401, 403):
                    raise SearchConfigError(
                        f"Google Places access was denied: "
                        f"{message or 'check GOOGLE_PLACES_API_KEY'}. "
                        "Enable Places API (New) in the key's project, allow "
                        "places.googleapis.com on the key, and attach billing or use a "
                        "Maps Demo Key."
                    )
                raise SearchError(f"Google Places search failed (HTTP {response.status_code}).")

            try:
                payload = response.json()
            except ValueError as exc:
                raise SearchError("Google Places returned an invalid response") from exc

            places = payload.get("places") or []
            for place in places:
                website = str(place.get("websiteUri", "")).strip()
                if not website.startswith(("http://", "https://")):
                    continue
                domain = _domain_of(website)
                if not domain or domain in seen:
                    continue
                seen.add(domain)
                name = place.get("displayName") or {}
                title = name.get("text", "") if isinstance(name, dict) else ""
                results.append(SearchResult(
                    url=website,
                    domain=domain,
                    title=str(title)[:200],
                    snippet=str(place.get("formattedAddress", ""))[:400],
                    source="places",
                    kind="company",
                ))
                if len(results) >= max_results:
                    break

            page_token = str(payload.get("nextPageToken") or "").strip() or None
            if not page_token:
                break

        return results


async def discover_websites(
    client: httpx.AsyncClient,
    spec: QuerySpec,
    config: GoogleConfig,
    *,
    limit: int | None = None,
    use_places: bool = True,
    max_cse_calls: int = 6,
) -> DiscoveryResult:
    """Discover company websites for a parsed query using Google APIs."""
    limit = max(1, min(limit or spec.target_websites, 200))
    discovery = DiscoveryResult(websites=[], queries=list(spec.search_queries))

    if spec.domains:
        for domain in spec.domains:
            discovery.websites.append(SearchResult(
                url=f"https://{domain}",
                domain=domain,
                title=domain,
                source="provided",
                kind="company",
                score=100.0,
            ))
            discovery.providers.append("provided")
        discovery.websites = discovery.websites[:limit]
        return discovery

    if not config.any_ready:
        raise SearchConfigError(
            "Google search is not configured. Add GOOGLE_PLACES_API_KEY for Places "
            "API (New), or GOOGLE_API_KEY and GOOGLE_CSE_ID for existing Custom "
            "Search customers, to the backend .env file. You can still extract "
            "emails by pasting website domains."
        )

    search = GoogleSearch(client, config)
    by_domain: dict[str, SearchResult] = {}
    errors: list[str] = []
    quota_hit = False
    cse_calls = 0
    gl = _country_gl(spec.country_code) if spec.country_code else None
    cr = f"country{spec.country_code}" if spec.country_code else None

    def add(result: SearchResult) -> None:
        key = registrable_domain(result.domain) or result.domain
        result.score = _rank_website(result.url, result.title, result.snippet, spec, result.source)
        existing = by_domain.get(key)
        if existing is None or result.score > existing.score:
            by_domain[key] = result

    if config.cse_ready:
        try:
            for query in spec.search_queries[:4]:
                if len(by_domain) >= limit or cse_calls >= max_cse_calls:
                    break
                page = 0
                while cse_calls < max_cse_calls and len(by_domain) < limit:
                    results = await search.cse_search(
                        query, start=page * 10 + 1, num=10, gl=gl, cr=cr
                    )
                    cse_calls += 1
                    for result in results:
                        add(result)
                    if len(results) < 10:
                        break
                    page += 1
                    if page >= 2:
                        break
            discovery.providers.append("google_cse")
        except SearchQuotaError as exc:
            quota_hit = True
            errors.append(str(exc))
        except SearchConfigError as exc:
            errors.append(str(exc))
        except SearchError as exc:
            errors.append(str(exc))

    if use_places and config.places_ready and len(by_domain) < limit:
        for places_query in build_places_queries(spec):
            if len(by_domain) >= limit:
                break
            try:
                remaining = max(1, min(60, limit - len(by_domain)))
                results = await search.places_search(places_query, max_results=remaining)
                for result in results:
                    add(result)
                if results and "google_places" not in discovery.providers:
                    discovery.providers.append("google_places")
            except SearchQuotaError as exc:
                quota_hit = True
                errors.append(str(exc))
                break
            except (SearchConfigError, SearchError) as exc:
                errors.append(str(exc))
                break

    if not discovery.websites:
        discovery.websites = sorted(by_domain.values(), key=lambda w: (-w.score, w.domain))
    else:
        discovery.websites = sorted(by_domain.values(), key=lambda w: (-w.score, w.domain))
    discovery.websites = discovery.websites[:limit]
    discovery.warnings.extend(errors)

    if not discovery.websites:
        config_error = any(
            marker in error.lower()
            for error in errors
            for marker in (
                "not configured", "credentials", "denied", "api key", "rejected",
                "closed to new customers", "does not have access", "billing",
                "maps demo key",
            )
        )
        if config_error:
            raise SearchConfigError(errors[0])
        if quota_hit:
            raise SearchQuotaError(errors[0] if errors else "Google search quota exceeded.")
        if errors:
            raise SearchError("; ".join(errors[:3]))
        raise SearchError(
            "Google returned no websites for this description. Try broadening the "
            "industry or location, or paste website domains directly."
        )
    if not discovery.providers:
        discovery.providers.append("google")
    return discovery
