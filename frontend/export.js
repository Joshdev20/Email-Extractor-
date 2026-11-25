(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.LeadScopeExport = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const HEADERS = [
    "Email",
    "Company",
    "Email domain",
    "Website domain",
    "Category",
    "Country",
    "Source URL",
    "Page type",
    "Validation",
    "Extracted at"
  ];

  function csvEscape(value) {
    const text = String(value === null || value === undefined ? "" : value);
    return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  }

  function toCsv(rows) {
    const lines = [HEADERS.map(csvEscape).join(",")];
    (rows || []).forEach((row) => {
      lines.push([
        row.email,
        row.company,
        row.domain,
        row.website_domain,
        row.category_label || row.category,
        row.country,
        row.source_url,
        row.page_type,
        row.syntax_valid ? "Valid syntax" : "Check",
        row.extracted_at
      ].map(csvEscape).join(","));
    });
    return `\uFEFF${lines.join("\r\n")}`;
  }

  function toTxt(rows) {
    return (rows || []).map((row) => row.email).filter(Boolean).join("\n");
  }

  function toJson(rows) {
    return JSON.stringify(rows || [], null, 2);
  }

  function toSheetRows(rows) {
    return (rows || []).map((row) => ({
      Email: row.email,
      Company: row.company,
      "Email domain": row.domain,
      "Website domain": row.website_domain,
      Category: row.category_label || row.category,
      Country: row.country,
      "Source URL": row.source_url,
      "Page type": row.page_type,
      Validation: row.syntax_valid ? "Valid syntax" : "Check",
      "Extracted at": row.extracted_at
    }));
  }

  function filename(extension, date) {
    const value = date instanceof Date ? date : new Date();
    const pad = (part) => String(part).padStart(2, "0");
    return `leadscope-emails-${value.getFullYear()}${pad(value.getMonth() + 1)}${pad(value.getDate())}-${pad(value.getHours())}${pad(value.getMinutes())}.${extension}`;
  }

  return { HEADERS, csvEscape, toCsv, toTxt, toJson, toSheetRows, filename };
});
