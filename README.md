# LeadScope — Business Email Extractor & Lead Finder

LeadScope finds **publicly listed business email addresses** from websites. You
describe the businesses you want in plain language, Google discovers candidate
websites, and a Python crawler reads their public contact pages. Results are
categorised, source-attributed, and returned to the browser for export.

There is **no database, no account system, and no permanent server-side result
storage**. Active jobs live in Python memory. Browser history lives in
`localStorage`. Exports are ordinary files you own.

---

## What it does

- Natural-language search: industry, country, city, email category, target
  count, and optional domains.
- Google Custom Search JSON API discovery, with optional Google Places API
  (New) business discovery.
- Async Python crawler with `robots.txt`, link prioritisation, per-domain page
  limits, concurrency limits, timeouts, retries, and cancellation.
- Email extraction from visible text, `mailto:` links, HTML attributes,
  JSON-LD, and visibly obfuscated forms such as `name [at] domain [dot] com`.
- Deduplication, syntax validation, domain association, source URL, company,
  country, timestamp, and category.
- Mobile-first dashboard with filters, selection, copy, and CSV / TXT / JSON /
  XLSX export.
- Local-only history with an explicit **Erase local history** control.

LeadScope does **not** perform SMTP verification, mailbox probing, or any form
of account verification. A syntax-valid email is not proof that it is
deliverable or that the owner consented to receive marketing.

---

## Architecture

```
Browser (HTML/CSS/JS + Tailwind + Font Awesome)
   |  fetch / SSE
   v
FastAPI (backend/main.py)
   |-- validator.py      Pydantic input validation
   |-- security.py       SSRF checks, safe fetch, size limits
   |-- search_engine.py  Google CSE + Places + NL query parser
   |-- crawler.py        async crawling, robots.txt, rate limits
   |-- email_extractor.py extraction, dedupe, categorisation
   |
   +-- in-memory JobManager (no database)
```

Google results are **discovery sources**, not proof that an email exists or is
valid. The crawler only reports addresses that are actually published on a
page it fetched.

---

## Requirements

- Python 3.10+ (tested on Python 3.13)
- A modern browser
- Optional: Google Cloud project with billing for Google search / Places
- Optional for development: Node.js 18+ (for the `export.js` tests)

---

## Quick start

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # add Google keys if you have them

python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000/>.

The frontend is served by FastAPI from `frontend/`. API documentation is at
<http://localhost:8000/api/docs>.

Manual domain extraction works without Google credentials. Natural-language
discovery requires Google credentials and shows a clear setup banner until they
are configured.

### Environment variables

Copy `.env.example` to `.env`. Never commit `.env`.

| Variable | Required | Purpose |
| --- | --- | --- |
| `GOOGLE_API_KEY` | For Google discovery | Server-side API key for Custom Search JSON API |
| `GOOGLE_CSE_ID` | For Google discovery | Programmable Search Engine ID (`cx`) |
| `GOOGLE_PLACES_API_KEY` | Optional | Places API (New) key for business website discovery |
| `ALLOWED_ORIGINS` | No | CORS origins, default `*`; set explicit origins in production |
| `MAX_CONCURRENT_JOBS` | No | Concurrent extraction jobs, default `3` |
| `MAX_JOB_SECONDS` | No | Hard job timeout, default `1800` |
| `JOB_TTL_SECONDS` | No | In-memory job retention, default `900` |
| `MAX_RETAINED_JOBS` | No | Maximum retained job snapshots, default `50` |
| `RATE_LIMIT_PER_MINUTE` | No | General per-client request limit, default `120` |
| `EXTRACTION_RATE_LIMIT` | No | Extraction POSTs per minute per client, default `12` |
| `CRAWL_RATE_LIMIT_SECONDS` | No | Minimum delay between requests to one domain, default `0.75` |
| `TRUST_PROXY` | No | Set `1` only behind a trusted reverse proxy |
| `HTTPX_TRUST_ENV` | No | Set `1` to let httpx use proxy environment variables |
| `LOG_LEVEL` | No | Python log level, default `INFO` |
| `HOST` / `PORT` | No | Used by `python -m backend.main`, default `0.0.0.0:8000` |

Google credentials stay on the Python side. The browser never receives them.

---

## Google API setup

### 1. Enable APIs

In [Google Cloud Console](https://console.cloud.google.com/):

1. Create or select a project.
2. Enable **Custom Search JSON API**.
3. Optionally enable **Places API (New)**. Places requires an active billing
   account.
4. Create an API key under **APIs & Services → Credentials**. Restrict the key
   to the APIs above and, where possible, to your server's IP address.

### 2. Create a Programmable Search Engine

1. Open <https://programmablesearchengine.google.com/controlpanel/create>.
2. Choose **Search the entire web**. A search engine restricted to specific
   sites will only discover those sites.
3. Copy the **Search engine ID** (`cx`).

### 3. Configure and restart

```dotenv
GOOGLE_API_KEY=your_google_api_key
GOOGLE_CSE_ID=your_programmable_search_engine_id
GOOGLE_PLACES_API_KEY=your_places_api_key
```

Restart FastAPI after editing `.env`. `GET /api/health` reports whether each
integration is configured.

### Quotas and pricing

- Custom Search JSON API has a free daily query allowance (currently 100
  queries/day) and paid queries beyond it, with a daily cap. Each query returns
  at most 10 results. LeadScope issues up to 6 CSE calls per discovery job by
  default (up to 60 raw results, before deduplication).
- Places API (New) is usage-billed. Text Search requests return up to 20 places
  per call; LeadScope requests no more than the remaining target.
- Limits, prices, and availability change. Check the Google Cloud console and
  the official documentation before relying on them.
- LeadScope never scrapes Google result pages as a substitute for the API.

When credentials are missing, invalid, or quota-limited, the API returns a
clear error (`503`, `429`, or a job error) and the UI shows it. Manual domain
mode remains available.

---

## How natural-language parsing works

The parser is **Python keyword and pattern matching, not an AI model**. It
extracts:

- industry / business category (for example real estate, marketing, software)
- country name or code (for example United Kingdom → `GB`)
- city (for example London, Berlin)
- target email count (`Find 500 ...`)
- email category (sales, support, careers, press, etc.)
- explicit domains (`example.com`, `https://acme.co.uk/contact`)

It then builds several Google queries, including operators such as:

```text
"marketing agency" "contact" "Canada" email
site:example.com contact
site:example.com "@example.com"
```

UI overrides (industry, country, city, category, target emails, target
websites) take precedence over parsed values. `POST /api/parse-query` returns
the interpreted plan so users can inspect it before crawling.

---

## Extraction pipeline

1. **Validate input.** Pydantic models clean domains and URLs. Schemes other
   than HTTP/HTTPS, embedded credentials, non-standard ports, localhost,
   private IPs, and cloud metadata hostnames are rejected.
2. **Discover websites.** Google CSE and/or Places results are normalised to
   registrable domains, deduplicated, and ranked. Official company sites rank
   above social networks and directories.
3. **Crawl politely.** For each site:
   - fetch and honour `robots.txt` (a 401/403 disallow blocks the site; a 5xx
     fetch failure fails open);
   - fetch the start page and prioritise `/contact`, `/contact-us`, `/about`,
     `/about-us`, `/team`, `/our-team`, `/company`, `/support`, `/sales`;
   - stay on the same registrable domain;
   - cap pages per domain and requests per second;
   - use timeouts, retries with backoff, and a maximum response size.
4. **Extract emails.** Visible text, `mailto:` links, HTML attributes, JSON-LD,
   and visible obfuscation. Remove placeholders, asset filenames, and
   duplicates.
5. **Categorise and attribute.** General, sales, marketing, support, careers,
   press, business owner, other. Each result keeps its source URL and page type.
6. **Return to browser.** The server keeps the job only in memory for the
   configured TTL. The browser stores a compact history entry locally.

### Crawling limits and cancellation

`ExtractionSettings` supports `concurrency` (1–10), `max_pages_per_domain`
(1–15; the UI slider exposes 1–12), `target_emails` (1–1000), `target_websites`
(1–200), request timeout,
robots toggle (must remain enabled), Places toggle, and email category filter.
`POST /api/cancel` sets a per-job event and cancels the task. Jobs have
independent state, so concurrent jobs do not share result sets.

LeadScope does not bypass CAPTCHAs, authentication, paywalls, or other access
controls, and does not crawl unrelated pages.

---

## API reference

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Health, version, no-database flag, Google configuration status |
| `GET` | `/api/config` | Public runtime configuration for the UI |
| `POST` | `/api/parse-query` | Interpret a natural-language query without crawling |
| `POST` | `/api/search` | Discover websites through Google (synchronous) |
| `POST` | `/api/extract` | Start a job from a query or explicit domains/URLs |
| `POST` | `/api/bulk-extract` | Start a job from multiple domains/URLs |
| `POST` | `/api/crawl` | Crawl one URL synchronously |
| `POST` | `/api/cancel` | Cancel a running job |
| `GET` | `/api/jobs/{job_id}` | Job state, progress, stats, warnings |
| `GET` | `/api/jobs/{job_id}/results` | Results and final stats |
| `GET` | `/api/progress/{job_id}` | Server-Sent Events progress stream |
| `GET` | `/api/docs` | Interactive OpenAPI documentation |

SSE events are named `progress`, `done`, and `error`. The browser falls back to
polling `GET /api/jobs/{id}` if the stream cannot connect.

### Examples

```bash
curl -s http://localhost:8000/api/health

curl -s -X POST http://localhost:8000/api/parse-query \
  -H 'Content-Type: application/json' \
  -d '{"query":"Find 500 real estate company emails in the United Kingdom"}'

curl -s -X POST http://localhost:8000/api/bulk-extract \
  -H 'Content-Type: application/json' \
  -d '{"domains":["example.com"],"settings":{"max_pages_per_domain":4}}'

curl -s -X POST http://localhost:8000/api/crawl \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.mozilla.org/en-US/contact/","max_pages":3}'

curl -N http://localhost:8000/api/progress/<job_id>
```

With Google configured:

```bash
curl -s -X POST http://localhost:8000/api/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"Find marketing agencies in Canada","max_results":10}'
```

---

## Frontend

- `frontend/index.html` — mobile-first layout, Tailwind CDN, Font Awesome CDN,
  SheetJS CDN for XLSX.
- `frontend/style.css` — design tokens, responsive cards/tables, print-free
  layout, no horizontal scroll at phone widths.
- `frontend/script.js` — job orchestration, SSE + polling fallback, filters,
  selection, clipboard, localStorage history/preferences, toasts.
- `frontend/export.js` — dependency-free UMD export module shared with the Node
  tests.

Design uses deep navy, white, neutral grey, and professional green. No emojis.
Primary touch targets are large, the results table collapses into cards on
phones, and the mobile menu is keyboard-accessible.

### Export formats

| Format | Notes |
| --- | --- |
| CSV | UTF-8 with BOM for Excel; escaped fields |
| TXT | One email per line |
| JSON | Full result records with metadata |
| XLSX | SheetJS workbook with spreadsheet-friendly headers |

Exports use the currently filtered rows. If SheetJS is unavailable, the UI
reports the error; CSV/TXT/JSON still work.

### Local browser storage

- `leadscope_history_v1` — up to 12 compact extraction snapshots.
- `leadscope_prefs_v1` — UI mode, industry, country, city, category, targets,
  crawl settings.
- No query text or domain list is saved in preferences.
- **Erase local history** removes `leadscope_history_v1`; **Clear results**
  removes the current in-page result set only.

---

## No-database design

- No SQL, NoSQL, document, or serverless database is used or required.
- No user accounts, sessions, cookies for identity, or authentication.
- Active jobs live in a Python dictionary in one process. They expire after
  `JOB_TTL_SECONDS` and disappear on restart.
- The server never writes extracted emails to disk.
- Results reach the browser and are downloadable as files.
- If you run multiple Uvicorn workers, jobs are isolated per worker; use one
  worker unless you add a shared job broker (which would change this design).

---

## Security model

- **Server-side secrets.** Google keys are read from the environment and never
  serialised to the browser.
- **SSRF protection.** Only `http`/`https`, standard ports, no embedded
  credentials, no localhost/private/link-local/reserved addresses, no cloud
  metadata hostnames. DNS answers are checked, redirects are revalidated, and
  redirects are capped.
- **Bounded fetches.** Connect/read timeouts, maximum redirects, maximum
  response bytes, and an HTML/text content-type allowlist.
- **Input validation.** Pydantic models bound every numeric field and clean
  domains/URLs. Invalid requests return `422` without internal details.
- **Safe errors.** Internal exceptions are logged server-side; clients get a
  generic message and a stable error scope.
- **Rate limiting.** Per-client general and extraction limits, plus per-domain
  crawl throttling.
- **Headers/CSP.** `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
  `Permissions-Policy`, and a Content-Security-Policy allow only the CDNs the UI
  needs.
- **Residual risks.** DNS rebinding between validation and connection is
  mitigated but not eliminated by application-level checks. JavaScript-rendered
  contact details are not executed. Only publicly reachable pages are crawled.

Do not expose this app as an open proxy for arbitrary crawling. Put it behind
authentication or network restrictions if it is internet-facing, and review
Google's terms and local privacy/marketing law before use.

---

## Testing

Install development dependencies:

```bash
pip install -r requirements-dev.txt
```

Run the Python suite:

```bash
python3 -m pytest -q
```

Run the JavaScript export tests:

```bash
node --test tests/export.test.js
```

Current results in this environment: **98 Python tests passed** and **6 Node
tests passed**.

Coverage includes:

- natural-language parsing examples and overrides
- Google CSE/Places request parameters, deduplication, ranking, quota and
  configuration errors
- crawling fixtures, robots allow/disallow/failure, link priority, page caps,
  source attribution, deduplication, cancellation, and bulk isolation
- email extraction methods (text, mailto, attributes, JSON-LD, obfuscation),
  placeholder/asset rejection, syntax checks, and all categories
- SSRF blocks (localhost, private IPs, metadata endpoints, credentials,
  non-standard ports, bad schemes), redirect revalidation, size limits, and
  content-type rejection
- Pydantic validation and domain normalisation
- API jobs, SSE, cancellation, concurrent-job isolation, rate limiting, error
  mapping, and `/api/docs` CSP
- export escaping and file formats

Fixtures live in `tests/fixtures/`. The suite is offline: DNS is stubbed and
HTTP uses `httpx.MockTransport`.

### Live verification performed

- `POST /api/crawl` against `https://www.mozilla.org/en-US/contact/` returned
  `mozspaces@mozilla.com` and `trademark-permissions@mozilla.com` (3 valid
  occurrences, 2 unique, 1 duplicate removed).
- The mobile UI at 390 px extracted the same site, showed 2 unique emails,
  filtered, sorted, removed a row, selected all, copied to the clipboard, and
  downloaded CSV, TXT, JSON, and XLSX files with verified contents.
- The same run confirmed no horizontal scrolling at 320, 375, 390, 768, and
  1440 px; card-only results on phones and the table on desktop; local history
  restore and erase; the mobile menu; and the clear
  "Google search is not configured" error path.

Google's live APIs were **not** called because this environment has no Google
credentials. Their request construction and failure handling are covered by
mocked tests; configure keys as described above to exercise live discovery.

---

## Mobile usage

1. Open the app URL in Safari (iPhone) or Chrome (Android).
2. Use the menu button for Workspace, Results, History, Google setup, and API
   docs.
3. Choose **Describe**, **Domains**, or **Single URL**.
4. Set optional industry, country, city, category, target emails, and target
   websites. Expand **Advanced crawling** for concurrency and page limits.
5. Tap **Start extraction**. Progress, current website, warnings, and cancel
   are shown in the live panel.
6. Results appear as touch-friendly cards. Use search, category/domain filters,
   and sorting; select individual emails or all; copy; export.
7. Add the page to your home screen for a full-screen experience.

The layout avoids horizontal scrolling and keeps controls at touch size.

---

## Project structure

```text
business-email-extractor/
├── backend/
│   ├── __init__.py
│   ├── main.py              FastAPI app, jobs, SSE, middleware
│   ├── search_engine.py     Google CSE/Places + NL parser
│   ├── crawler.py           async crawler and aggregation
│   ├── email_extractor.py   extraction, dedupe, categorisation
│   ├── validator.py         Pydantic request models
│   └── security.py          SSRF checks and safe fetch
├── frontend/
│   ├── index.html
│   ├── style.css
│   ├── script.js
│   └── export.js
├── tests/
│   ├── fixtures/
│   ├── conftest.py
│   ├── test_api.py
│   ├── test_crawler.py
│   ├── test_email_extractor.py
│   ├── test_export_js.py
│   ├── test_search_engine.py
│   ├── test_security.py
│   ├── test_validator.py
│   └── export.test.js
├── .env.example
├── .gitignore
├── AGENTS.md
├── pytest.ini
├── requirements.txt
├── requirements-dev.txt
└── README.md
```

---

## Troubleshooting

**"Google search is not configured"**
Add `GOOGLE_API_KEY` and `GOOGLE_CSE_ID` to `.env`, then restart the server.
Manual domain mode does not need Google.

**`503` from `/api/search`**
The key is missing, invalid, restricted, or the API is not enabled. Check the
Google Cloud console and the server log.

**`429`**
Google quota or the application rate limit was hit. Wait, reduce the target,
or use manual domains. The response includes a `Retry-After` header where
applicable.

**A site returns zero emails**
The address may be behind a form, rendered by JavaScript, obfuscated in a way
that is not visibly parseable, blocked by `robots.txt`, or simply not published.
LeadScope does not guess addresses.

**SSE progress does not update behind a proxy**
Disable proxy buffering for `/api/progress/`, or rely on the built-in polling
fallback.

**XLSX export fails**
`cdn.jsdelivr.net` must be reachable. CSV, TXT, and JSON exports do not need it.

**Rate limits affect all users behind a proxy**
Set `TRUST_PROXY=1` only when a trusted reverse proxy supplies
`X-Forwarded-For`. Otherwise leave it at `0`.

**A job disappeared**
Jobs are memory-only and expire after `JOB_TTL_SECONDS`; restarting the server
clears them. Export results you need to keep.

---

## Responsible use and limitations

- Extract only publicly listed business contact details. Do not use LeadScope
  to harvest personal data at scale.
- Respect GDPR, PECR, CAN-SPAM, and local marketing rules. A published address
  is not automatically consent for unsolicited email.
- Respect website terms, `robots.txt`, and rate limits. Do not use the tool to
  overload sites or bypass access controls.
- Syntax validation is not deliverability verification. No SMTP probing is
  performed.
- Google results can include directories, social profiles, and stale pages.
  Ranking reduces but does not eliminate noise.
- The crawler does not execute JavaScript, solve CAPTCHAs, log in, or defeat
  paywalls.
- The app is intentionally single-node and database-free. Horizontal scaling
  would require an external job store, which is outside this design.
