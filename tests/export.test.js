const test = require("node:test");
const assert = require("node:assert/strict");
const exporter = require("../frontend/export.js");

const rows = [
  {
    email: "sales@acme.com",
    company: "Acme, Inc.",
    domain: "acme.com",
    website_domain: "acme.com",
    category: "sales",
    category_label: "Sales",
    country: "US",
    source_url: "https://acme.com/contact?ref=lead&type=business",
    page_type: "contact",
    syntax_valid: true,
    extracted_at: "2026-01-02T03:04:05+00:00"
  },
  {
    email: "hello@blue-door.co.uk",
    company: 'Blue "Door" Ltd',
    domain: "blue-door.co.uk",
    website_domain: "blue-door.co.uk",
    category: "general",
    category_label: "General",
    country: "GB",
    source_url: "https://blue-door.co.uk/",
    page_type: "home",
    syntax_valid: true,
    extracted_at: "2026-01-02T03:05:00+00:00"
  }
];

test("CSV export includes BOM, headers and escaped fields", () => {
  const csv = exporter.toCsv(rows);
  assert.ok(csv.startsWith("\uFEFF"));
  assert.match(csv, /Email,Company,Email domain,Website domain,Category,Country,Source URL,Page type,Validation,Extracted at/);
  assert.match(csv, /"Acme, Inc\."/);
  assert.match(csv, /"Blue ""Door"" Ltd"/);
  assert.match(csv, /sales@acme\.com/);
  assert.match(csv, /Valid syntax/);
});

test("TXT export contains one email per line", () => {
  assert.equal(exporter.toTxt(rows), "sales@acme.com\nhello@blue-door.co.uk");
});

test("JSON export is valid and preserves records", () => {
  const parsed = JSON.parse(exporter.toJson(rows));
  assert.equal(parsed.length, 2);
  assert.equal(parsed[0].email, "sales@acme.com");
  assert.equal(parsed[1].company, 'Blue "Door" Ltd');
});

test("XLSX row data uses spreadsheet-friendly headers", () => {
  const sheetRows = exporter.toSheetRows(rows);
  assert.equal(sheetRows.length, 2);
  assert.equal(sheetRows[0].Email, "sales@acme.com");
  assert.equal(sheetRows[0]["Website domain"], "acme.com");
  assert.equal(sheetRows[0].Validation, "Valid syntax");
});

test("CSV escaping handles newlines and commas", () => {
  assert.equal(exporter.csvEscape("a,b"), '"a,b"');
  assert.equal(exporter.csvEscape('a"b'), '"a""b"');
  assert.equal(exporter.csvEscape("a\nb"), '"a\nb"');
  assert.equal(exporter.csvEscape("plain"), "plain");
});

test("Filename is deterministic and filesystem safe", () => {
  const name = exporter.filename("csv", new Date(2026, 0, 2, 3, 4, 5));
  assert.equal(name, "leadscope-emails-20260102-0304.csv");
});
