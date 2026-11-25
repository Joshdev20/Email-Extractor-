from __future__ import annotations

from backend.email_extractor import (
    classify_email,
    extract_emails_from_html,
    extract_emails_with_stats,
    extract_page_metadata,
    is_valid_email_syntax,
)
from tests.conftest import read_fixture


def test_extracts_all_supported_methods_and_attribution():
    emails = extract_emails_from_html(
        read_fixture("home.html"),
        "https://northwind.co.uk/",
        website_domain="northwind.co.uk",
    )
    by_email = {email.email: email for email in emails}

    assert "info@northwind.co.uk" in by_email
    assert "sales@northwind.co.uk" in by_email
    assert by_email["support@northwind.co.uk"].method == "mailto"
    assert by_email["press@northwind.co.uk"].method == "attribute"
    assert by_email["careers@northwind.co.uk"].method == "obfuscated"
    assert by_email["hello@northwind.co.uk"].method == "jsonld"
    assert by_email["info@northwind.co.uk"].source_url == "https://northwind.co.uk/"
    assert by_email["info@northwind.co.uk"].website_domain == "northwind.co.uk"
    assert by_email["info@northwind.co.uk"].company == "Northwind Property Group"
    assert by_email["info@northwind.co.uk"].country == "GB"
    assert by_email["info@northwind.co.uk"].syntax_valid is True
    assert by_email["info@northwind.co.uk"].extracted_at


def test_rejects_placeholders_assets_and_duplicate_occurrences():
    emails, raw_count = extract_emails_with_stats(
        read_fixture("home.html"),
        "https://northwind.co.uk/",
        website_domain="northwind.co.uk",
    )
    values = {email.email for email in emails}
    assert "yourname@example.com" not in values
    assert "logo@2x.png" not in values
    assert all(not value.endswith((".png", ".jpg", ".css")) for value in values)
    assert raw_count > len(emails)


def test_classifies_every_category():
    assert classify_email("sales@example.com") == "sales"
    assert classify_email("marketing@example.com") == "marketing"
    assert classify_email("support@example.com") == "support"
    assert classify_email("careers@example.com") == "careers"
    assert classify_email("press@example.com") == "press"
    assert classify_email("founder@example.com") == "owner"
    assert classify_email("info@example.com") == "general"
    assert classify_email("accounts@example.com") == "sales"
    assert classify_email("random@example.com") == "other"


def test_syntax_validation_is_strict_but_not_a_deliverability_claim():
    assert is_valid_email_syntax("sales@acme.co.uk") is True
    assert is_valid_email_syntax("first.last+tag@sub.acme.com") is True
    assert is_valid_email_syntax("a@b") is False
    assert is_valid_email_syntax("a..b@acme.com") is False
    assert is_valid_email_syntax(".a@acme.com") is False
    assert is_valid_email_syntax("a@acme.png") is False
    assert is_valid_email_syntax("user@example.com") is False
    assert is_valid_email_syntax("abc@o1.ingest.sentry.io") is False
    assert is_valid_email_syntax("a@acme.com trailing") is False


def test_contact_page_source_and_page_type():
    emails = extract_emails_from_html(
        read_fixture("contact.html"),
        "https://northwind.co.uk/contact-us",
        website_domain="northwind.co.uk",
    )
    contact = next(email for email in emails if email.email == "contact@northwind.co.uk")
    assert contact.page_type == "contact"
    assert contact.source_url == "https://northwind.co.uk/contact-us"
    assert contact.category == "general"


def test_obfuscated_formats():
    html = """
    <html><body>
      <p>a [at] acme [dot] co [dot] uk</p>
      <p>b(at)acme(dot)com</p>
      <p>c AT acme DOT com</p>
      <p>d @ acme . com</p>
    </body></html>
    """
    values = {email.email for email in extract_emails_from_html(
        html, "https://acme.com/contact", website_domain="acme.com"
    )}
    assert {"a@acme.co.uk", "b@acme.com", "c@acme.com", "d@acme.com"} <= values


def test_page_metadata_falls_back_to_title_and_domain():
    company, country = extract_page_metadata(
        read_fixture("no_emails.html"), "https://unknown.example/", "unknown.example"
    )
    assert company == "No contact details"
    assert country == "Unknown"


def test_html_attributes_and_jsonld_email_with_mailto_prefix():
    html = """
    <html><head>
      <script type="application/ld+json">
        {"@type": "LocalBusiness", "name": "Blue Door Ltd", "email": "mailto:hello@bluedoor.com"}
      </script>
    </head><body>
      <div data-email="ops@bluedoor.com"></div>
    </body></html>
    """
    values = {email.email for email in extract_emails_from_html(
        html, "https://bluedoor.com/", website_domain="bluedoor.com"
    )}
    assert "hello@bluedoor.com" in values
    assert "ops@bluedoor.com" in values
