"""Email discovery, normalisation, classification and attribution.

Extraction is limited to information a website publishes in its HTML (visible
text, ``mailto:`` links, attributes and JSON-LD). No mailbox probing, SMTP
verification or authentication bypass is performed.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

EMAIL_RE = re.compile(
    r"(?<![\w.!#$%&'*+/=?^`{|}~-])"
    r"([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64})"
    r"@"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,24})"
    r"(?![\w-])"
)

# name [at] domain [dot] com / name(at)domain(dot)com
_OBFUSCATED_BRACKET_RE = re.compile(
    r"(?<![\w.!#$%&'*+/=?^`{|}~-])"
    r"([A-Za-z0-9][A-Za-z0-9._%+-]{0,63})"
    r"\s*(?:\[at\]|\(at\)|\{at\}|<at>)\s*"
    r"((?:[A-Za-z0-9-]+\.)*[A-Za-z0-9-]+)"
    r"\s*(?:\[dot\]|\(dot\)|\{dot\}|<dot>)\s*"
    r"([A-Za-z]{2,24}(?:\s*(?:\[dot\]|\(dot\)|\{dot\}|<dot>)\s*[A-Za-z]{2,24})*)",
    re.IGNORECASE,
)

# name AT domain DOT com (uppercase only to limit false positives)
_OBFUSCATED_WORD_RE = re.compile(
    r"(?<![\w.!#$%&'*+/=?^`{|}~-])"
    r"([A-Za-z0-9][A-Za-z0-9._%+-]{0,63})"
    r"\s+AT\s+"
    r"((?:[A-Za-z0-9-]+\.)*[A-Za-z0-9-]+)"
    r"\s+DOT\s+"
    r"([A-Za-z]{2,24}(?:\s+DOT\s+[A-Za-z]{2,24})*)"
)

# name(at)domain.com without an obfuscated dot
_AT_ONLY_RE = re.compile(
    r"(?<![\w.!#$%&'*+/=?^`{|}~-])"
    r"([A-Za-z0-9][A-Za-z0-9._%+-]{0,63})"
    r"\s*(?:\[at\]|\(at\)|\{at\}|<at>)\s*"
    r"((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,24})"
    r"(?!\s*(?:\[dot\]|\(dot\)|\{dot\}|<dot>))",
    re.IGNORECASE,
)

_SPACED_AT_RE = re.compile(
    r"(?<![\w.!#$%&'*+/=?^`{|}~-])"
    r"([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64})"
    r"\s+@\s+"
    r"((?:[A-Za-z0-9-]+\s*\.\s*)+[A-Za-z]{2,24})"
)

_LOCAL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}$")
_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
_TLD_RE = re.compile(r"^(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{2,59})$")

_ASSET_TLDS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "ico", "bmp",
    "css", "js", "mjs", "json", "xml", "map", "woff", "woff2", "ttf",
    "eot", "otf", "mp4", "mp3", "webm", "pdf", "zip", "gz",
}
_PLACEHOLDER_DOMAINS = {
    "example.com", "example.org", "example.net", "example.edu",
    "domain.com", "yourdomain.com", "your-domain.com", "yourdomain.co.uk",
    "email.com", "test.com", "sample.com", "yourcompany.com", "company.com",
    "website.com", "yoursite.com", "site.com", "mydomain.com",
    "sentry.io", "wixpress.com", "sentry-next.wixpress.com",
}
_PLACEHOLDER_LOCALS = {
    "yourname", "your.name", "your-email", "youremail", "email", "name",
    "username", "user", "test", "example", "sample", "someone", "somebody",
    "firstname", "lastname", "first.last",
}

_EMAIL_METHOD_PRIORITY = {
    "text": 1,
    "attribute": 2,
    "jsonld": 3,
    "obfuscated": 4,
    "spaced": 4,
    "mailto": 5,
}

CATEGORY_LABELS = {
    "general": "General",
    "sales": "Sales",
    "marketing": "Marketing",
    "support": "Support",
    "careers": "Careers",
    "press": "Press",
    "owner": "Business owner",
    "other": "Other",
}

_CATEGORY_RULES: tuple[tuple[str, set[str]], ...] = (
    ("careers", {
        "careers", "career", "jobs", "job", "hr", "recruitment", "recruiting",
        "recruiter", "talent", "hiring", "people", "personnel", "apply",
        "resume", "cv", "vacancies", "vacancy",
    }),
    ("press", {
        "press", "media", "news", "newsroom", "pr", "publicity",
        "communications", "comms", "journalist", "editor", "editorial",
    }),
    ("support", {
        "support", "help", "helpdesk", "service", "services", "customer",
        "customers", "customerservice", "customercare", "care", "techsupport",
        "supportteam", "assistance", "clientsupport",
    }),
    ("owner", {
        "owner", "founder", "cofounder", "ceo", "cto", "cfo", "coo",
        "director", "principal", "president", "managing", "head", "manager",
        "leadership",
    }),
    ("sales", {
        "sales", "business", "bd", "partnerships", "partner", "deals", "deal",
        "quotes", "quote", "orders", "order", "commercial", "accounts",
        "account", "procurement", "tenders",
    }),
    ("marketing", {
        "marketing", "growth", "advertising", "ads", "brand", "social",
        "content", "seo", "digital", "campaigns", "campaign",
    }),
    ("general", {
        "info", "contact", "hello", "hi", "enquiries", "enquiry", "inquiries",
        "inquiry", "admin", "office", "team", "mail", "email", "general",
        "reception", "ask", "inbox", "main",
    }),
)

_CC_TLD_COUNTRY = {
    "uk": "GB", "de": "DE", "fr": "FR", "es": "ES", "it": "IT", "nl": "NL",
    "be": "BE", "ie": "IE", "ca": "CA", "us": "US", "au": "AU", "nz": "NZ",
    "in": "IN", "sg": "SG", "ae": "AE", "za": "ZA", "br": "BR", "mx": "MX",
    "jp": "JP", "cn": "CN", "hk": "HK", "ch": "CH", "at": "AT", "se": "SE",
    "no": "NO", "dk": "DK", "fi": "FI", "pl": "PL", "pt": "PT", "cz": "CZ",
    "gr": "GR", "tr": "TR", "ru": "RU", "il": "IL", "sa": "SA", "qa": "QA",
    "kw": "KW", "bh": "BH", "om": "OM", "eg": "EG", "ng": "NG", "ke": "KE",
    "gh": "GH", "pk": "PK", "bd": "BD", "lk": "LK", "np": "NP", "ph": "PH",
    "my": "MY", "id": "ID", "th": "TH", "vn": "VN", "kr": "KR", "tw": "TW",
    "ar": "AR", "cl": "CL", "co": "CO", "pe": "PE", "uy": "UY", "ec": "EC",
    "ve": "VE", "ro": "RO", "hu": "HU", "bg": "BG", "hr": "HR", "si": "SI",
    "sk": "SK", "rs": "RS", "ee": "EE", "lv": "LV", "lt": "LT", "ua": "UA",
    "is": "IS", "mt": "MT", "cy": "CY", "lu": "LU", "jo": "JO", "lb": "LB",
    "ma": "MA", "tn": "TN", "dz": "DZ",
}


@dataclass(slots=True)
class ExtractedEmail:
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalise_email(value: str) -> str:
    email = unquote(value or "").strip().strip(".,;:()<>[]{}\"'/\\=")
    email = email.replace("mailto:", "", 1) if email.lower().startswith("mailto:") else email
    email = email.split("?", 1)[0]
    return email.strip().lower()


def is_valid_email_syntax(email: str) -> bool:
    if not email or len(email) > 254 or email.count("@") != 1:
        return False
    local, _, domain = email.partition("@")
    if not local or not domain or len(local) > 64:
        return False
    if not _LOCAL_RE.match(local):
        return False
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    domain = domain.rstrip(".")
    if len(domain) > 253:
        return False
    labels = domain.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.match(label) for label in labels):
        return False
    if not _TLD_RE.match(labels[-1]):
        return False
    if labels[-1].lower() in _ASSET_TLDS:
        return False
    if local in _PLACEHOLDER_LOCALS:
        return False
    if any(domain == placeholder or domain.endswith(f".{placeholder}") for placeholder in _PLACEHOLDER_DOMAINS):
        return False
    return True


def classify_email(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    tokens = {token for token in re.split(r"[^a-z0-9]+", local) if token}
    compact = re.sub(r"[^a-z0-9]+", "", local)
    for category, keywords in _CATEGORY_RULES:
        if tokens & keywords:
            return category
        for keyword in keywords:
            if len(keyword) >= 4 and keyword in compact:
                return category
    return "other"


def infer_country_from_domain(domain: str) -> str:
    tld = (domain.rsplit(".", 1)[-1] if domain else "").lower()
    return _CC_TLD_COUNTRY.get(tld, "Unknown")


def _clean_company_name(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"\s+", " ", value).strip()
    for separator in (" | ", " – ", " — ", " - ", " :: ", " : "):
        if separator in text:
            first = text.split(separator, 1)[0].strip()
            if first and first.lower() not in {"home", "welcome", "contact us", "contact"}:
                text = first
                break
    text = re.sub(r"^(home|welcome)\s*[-–—|:]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*(official website|homepage|website)$", "", text, flags=re.IGNORECASE)
    return text.strip()[:120]


def _domain_to_name(domain: str) -> str:
    label = domain.split(".")[0] if domain else ""
    label = label.replace("-", " ").strip()
    return label.title() if label else ""


def _jsonld_nodes(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _jsonld_nodes(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _jsonld_nodes(item)


def _parse_jsonld(soup: BeautifulSoup) -> tuple[list[str], str, str]:
    emails: list[str] = []
    company = ""
    country = ""
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = script.string or script.get_text()
        if not raw or len(raw) > 500_000:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for node in _jsonld_nodes(data):
            if not company:
                name = node.get("name")
                if isinstance(name, str) and name.strip():
                    company = _clean_company_name(name)
            email_value = node.get("email")
            if isinstance(email_value, str):
                matches = EMAIL_RE.findall(email_value)
                if matches:
                    emails.extend(f"{local}@{domain}" for local, domain in matches)
                elif "@" in email_value:
                    emails.append(email_value)
            address = node.get("address")
            if isinstance(address, dict):
                country_value = address.get("addressCountry")
                if isinstance(country_value, str) and len(country_value.strip()) <= 60:
                    country = country_value.strip()
                elif isinstance(country_value, dict):
                    country_name = country_value.get("name")
                    if isinstance(country_name, str):
                        country = country_name.strip()
    return emails, company, country


def _extract_company(soup: BeautifulSoup, domain: str) -> str:
    meta = soup.find("meta", attrs={"property": "og:site_name"})
    if meta and meta.get("content"):
        name = _clean_company_name(meta["content"])
        if name:
            return name
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for node in _jsonld_nodes(data):
            if node.get("@type") in {"Organization", "LocalBusiness", "Corporation"}:
                name = node.get("name")
                if isinstance(name, str) and name.strip():
                    cleaned = _clean_company_name(name)
                    if cleaned:
                        return cleaned
    if soup.title and soup.title.string:
        name = _clean_company_name(soup.title.string)
        if name:
            return name
    h1 = soup.find("h1")
    if h1:
        name = _clean_company_name(h1.get_text(" ", strip=True))
        if name and len(name) <= 80:
            return name
    return _domain_to_name(domain)


_COUNTRY_NAMES = {
    "united kingdom": "GB", "great britain": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "united states": "US", "united states of america": "US", "usa": "US",
    "canada": "CA", "australia": "AU", "germany": "DE", "france": "FR",
    "spain": "ES", "italy": "IT", "netherlands": "NL", "ireland": "IE",
    "india": "IN", "singapore": "SG", "united arab emirates": "AE",
    "south africa": "ZA", "new zealand": "NZ", "brazil": "BR",
    "mexico": "MX", "japan": "JP", "china": "CN", "sweden": "SE",
    "norway": "NO", "denmark": "DK", "finland": "FI", "switzerland": "CH",
    "austria": "AT", "belgium": "BE", "portugal": "PT", "poland": "PL",
    "czech republic": "CZ", "czechia": "CZ", "greece": "GR", "turkey": "TR",
    "israel": "IL", "saudi arabia": "SA", "qatar": "QA", "kuwait": "KW",
    "egypt": "EG", "nigeria": "NG", "kenya": "KE", "pakistan": "PK",
    "bangladesh": "BD", "philippines": "PH", "malaysia": "MY",
    "indonesia": "ID", "thailand": "TH", "vietnam": "VN",
    "south korea": "KR", "taiwan": "TW", "hong kong": "HK",
    "argentina": "AR", "chile": "CL", "colombia": "CO", "peru": "PE",
    "romania": "RO", "hungary": "HU", "bulgaria": "BG", "croatia": "HR",
    "slovenia": "SI", "slovakia": "SK", "serbia": "RS", "estonia": "EE",
    "latvia": "LV", "lithuania": "LT", "ukraine": "UA", "iceland": "IS",
    "malta": "MT", "cyprus": "CY", "luxembourg": "LU", "jordan": "JO",
    "lebanon": "LB", "morocco": "MA", "tunisia": "TN", "algeria": "DZ",
}


def _normalise_country(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    code = _COUNTRY_NAMES.get(cleaned.lower())
    if code:
        return code
    if re.fullmatch(r"[A-Za-z]{2}", cleaned):
        return cleaned.upper()
    return cleaned[:60]


def _extract_country(soup: BeautifulSoup, domain: str, jsonld_country: str) -> str:
    if jsonld_country:
        return _normalise_country(jsonld_country)
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        lang = str(html_tag["lang"])
        match = re.search(r"[-_]([A-Za-z]{2})$", lang)
        if match:
            code = match.group(1).upper()
            if code in _CC_TLD_COUNTRY.values():
                return code
    return infer_country_from_domain(domain)


def _page_type(page_url: str) -> str:
    path = (urlsplit(page_url).path or "/").lower()
    if re.search(r"contact|kontakt|contacto|contactez|get-in-touch|reach-us", path):
        return "contact"
    if re.search(r"about|company|who-we-are|our-story|impressum|imprint", path):
        return "about"
    if re.search(r"team|leadership|people|management|staff|meet-the-team", path):
        return "team"
    if re.search(r"support|help|faq|customer-service", path):
        return "support"
    if re.search(r"career|jobs|vacanc|join-us", path):
        return "careers"
    if re.search(r"press|media|news", path):
        return "press"
    if re.search(r"sales|partner", path):
        return "sales"
    return "home" if path in {"", "/"} else "other"


def _collect_mailto(soup: BeautifulSoup) -> list[str]:
    found: list[str] = []
    for anchor in soup.find_all(href=True):
        href = str(anchor.get("href", "")).strip()
        if not href.lower().startswith("mailto:"):
            continue
        payload = unquote(href[7:].split("?", 1)[0])
        for part in re.split(r"[,;]", payload):
            candidate = part.strip()
            if candidate:
                found.append(candidate)
    return found


def _collect_attribute_emails(soup: BeautifulSoup) -> list[str]:
    found: list[str] = []
    for tag in soup.find_all(True):
        for attr_value in tag.attrs.values():
            values = attr_value if isinstance(attr_value, list) else [attr_value]
            for value in values:
                if not isinstance(value, str) or "@" not in value:
                    continue
                if value.strip().lower().startswith("mailto:"):
                    continue
                found.extend(match[0] + "@" + match[1] for match in EMAIL_RE.findall(value))
    return found


_DOT_TOKEN_RE = re.compile(r"\[dot\]|\(dot\)|\{dot\}|<dot>|DOT", re.IGNORECASE)


def _join_obfuscated(local: str, domain: str, tail: str) -> str:
    cleaned_tail = re.sub(r"\s+", "", _DOT_TOKEN_RE.sub(".", tail))
    return f"{local}@{domain}.{cleaned_tail}"


def _obfuscated_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for local, domain, tld in _OBFUSCATED_BRACKET_RE.findall(text):
        candidates.append(_join_obfuscated(local, domain, tld))
    for local, domain, tld in _OBFUSCATED_WORD_RE.findall(text):
        candidates.append(_join_obfuscated(local, domain, tld))
    for local, domain in _AT_ONLY_RE.findall(text):
        candidates.append(f"{local}@{domain}")
    for local, domain in _SPACED_AT_RE.findall(text):
        cleaned_domain = re.sub(r"\s+", "", domain)
        candidates.append(f"{local}@{cleaned_domain}")
    return candidates


def extract_page_metadata(
    html: str,
    page_url: str,
    website_domain: str,
) -> tuple[str, str]:
    """Return the best company name and country found on a page."""
    if not html:
        return "", "Unknown"
    soup = BeautifulSoup(html, "lxml")
    _, _, jsonld_country = _parse_jsonld(soup)
    company = _extract_company(soup, website_domain)
    country = _extract_country(soup, website_domain, jsonld_country)
    return company, country


def extract_emails_with_stats(
    html: str,
    page_url: str,
    *,
    website_domain: str,
    company: str | None = None,
    country: str | None = None,
) -> tuple[list[ExtractedEmail], int]:
    """Extract emails and return ``(unique_emails, raw_valid_occurrences)``."""
    if not html:
        return [], 0

    soup = BeautifulSoup(html, "lxml")
    jsonld_emails, jsonld_company, jsonld_country = _parse_jsonld(soup)

    resolved_company = company or _extract_company(soup, website_domain)
    resolved_country = country or _extract_country(soup, website_domain, jsonld_country)
    page_type = _page_type(page_url)

    for tag in soup(["script", "style", "template"]):
        tag.decompose()
    visible_text = soup.get_text(" ", strip=True)

    sources: list[tuple[str, str]] = []
    sources.extend((email, "mailto") for email in _collect_mailto(soup))
    sources.extend((email, "jsonld") for email in jsonld_emails)
    sources.extend((email, "attribute") for email in _collect_attribute_emails(soup))
    for match in EMAIL_RE.finditer(visible_text):
        sources.append((f"{match.group(1)}@{match.group(2)}", "text"))
    sources.extend((email, "obfuscated") for email in _obfuscated_candidates(visible_text))

    by_email: dict[str, ExtractedEmail] = {}
    raw_count = 0
    for raw_email, method in sources:
        email = normalise_email(raw_email)
        if not email or not is_valid_email_syntax(email):
            continue
        raw_count += 1
        existing = by_email.get(email)
        if existing and _EMAIL_METHOD_PRIORITY[method] <= _EMAIL_METHOD_PRIORITY[existing.method]:
            continue
        domain = email.rsplit("@", 1)[-1]
        category = classify_email(email)
        by_email[email] = ExtractedEmail(
            email=email,
            domain=domain,
            website_domain=website_domain,
            company=resolved_company,
            source_url=page_url,
            category=category,
            category_label=CATEGORY_LABELS[category],
            country=resolved_country,
            syntax_valid=True,
            method=method,
            page_type=page_type,
            extracted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    return list(by_email.values()), raw_count


def extract_emails_from_html(
    html: str,
    page_url: str,
    *,
    website_domain: str,
    company: str | None = None,
    country: str | None = None,
) -> list[ExtractedEmail]:
    """Extract all publicly published emails from one HTML page."""
    emails, _ = extract_emails_with_stats(
        html,
        page_url,
        website_domain=website_domain,
        company=company,
        country=country,
    )
    return emails
