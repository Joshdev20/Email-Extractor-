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
- Google Places API (New) business discovery, with optional Custom Search
  JSON API support for existing customers.
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
   |-- search_engine.py  Google Places + optional CSE + NL query parser
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
- Google Cloud project with Places API (New) and billing, or a Maps Demo Key;
  optional Custom Search JSON API access for existing customers
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
discovery reads `GOOGLE_PLACES_API_KEY` from the server environment; there is no
in-app setup screen and no key is ever sent to the browser.

### Environment variables

Copy `.env.example` to `.env`. Never commit `.env`.

| Variable | Required | Purpose |
| --- | --- | --- |
| `GOOGLE_PLACES_API_KEY` | Recommended | Places API (New) key for business website discovery |
| `GOOGLE_API_KEY` | Existing CSE customers | Server-side API key for Custom Search JSON API |
| `GOOGLE_CSE_ID` | Existing CSE customers | Programmable Search Engine ID (`cx`) |
| `ALLOWED_ORIGINS` | No | CORS origins, default `*`; set explicit origins in production |
| `MAX_CONCURRENT_JOBS` | No | Concurrent extraction jobs, default `3` |
| `MAX_JOB_SECONDS` | No | Hard job timeout, default `1800` |
| `SYNC_EXTRACTION` | No | Set `1` to run each extraction inside one request; Vercel sets this automatically |
| `SYNC_MAX_SECONDS` | No | Timeout for a synchronous request, default `55` |
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

## Deploy to Vercel

The repository includes a Vercel entrypoint and static routing:

- `api/index.py` wraps the FastAPI app with `mangum`.
- `vercel.json` serves `frontend/` as static files and routes `/api/*` to the
  Python function.
- `.vercelignore` keeps `.venv`, tests, and local env files out of the upload.

1. Install the CLI and deploy:

   ```bash
   npm install -g vercel
   vercel
   vercel --prod
   ```

2. Add `GOOGLE_PLACES_API_KEY` in **Vercel → Project → Settings →
   Environment Variables** for Production, Preview, and Development. Redeploy
   after adding it. Do not commit a `.env` file.
3. Open the deployment URL. `/api/health` should report
   `"google_places_configured": true`.

Vercel uses the synchronous extraction path. `/api/config` returns
`"sync_extraction": true`, and the browser posts to `/api/extract-sync`. The
function runs discovery and crawling inside one HTTP request and returns the
results directly. This is necessary because a serverless function cannot keep
an in-memory background job alive after it sends a response.

`vercel.json` uses the legacy `builds` format for the Python function and the
static frontend. Vercel rejects a `functions` block in the same file, so set the
function duration in **Vercel → Project → Settings → Functions → Max Duration**
(for example, 60 seconds). Vercel plan limits still apply. If the 55-second
internal budget is reached, the API returns the emails already found with
`"partial": true` and a warning instead of discarding them. Large jobs should
run on the local async server, where the UI uses SSE and `/api/extract`. No job
state is shared between Vercel invocations, and no database is used.

---

## Google API setup

### Important availability note

Google's [Custom Search JSON API](https://developers.google.com/custom-search/v1/overview)
is **closed to new customers**. Existing customers have until **January 1,
2027** to migrate. New Google Cloud projects receive:

```text
403 — This project does not have the access to Custom Search JSON API.
```

No API key, OAuth client, or key restriction can change that. For a new
deployment, use **Places API (New)** instead. The CSE code path remains available
for projects that already had access before the closure.

### Recommended: Places API (New)

1. Enable **Places API (New)** in the
   [Google Cloud console](https://console.cloud.google.com/apis/library/places.googleapis.com).
2. Attach a billing account, or get a no-cost
   [Maps Demo Key](https://developers.google.com/maps/demo-key). The demo key
   requires a personal Google Account, has daily quota limits, and is for
   prototyping rather than production.
3. Create an API key under **APIs & Services → Credentials**.
4. Restrict the key to **Places API (New)**, not the legacy **Places API**.
   Set application restrictions to **None** for server-side use, or an IP
   restriction for a fixed server address.
5. Put the key in `GOOGLE_PLACES_API_KEY`. Leave the CSE variables blank.

Places Text Search is designed for business and category queries such as
"marketing agencies in Canada". It returns official websites where Google has
one. It is not a general web search API and does not support search operators
such as `site:`.

### Optional: Custom Search JSON API (existing customers only)

Use this only if the Google Cloud project already had Custom Search JSON API
access before the closure.

1. Enable **Custom Search JSON API** in that same project.
2. Create a Programmable Search Engine at
   <https://programmablesearchengine.google.com/controlpanel/create> and choose
   **Search the entire web**.
3. Copy the **Search engine ID** (`cx`).
4. Create or reuse an API key in the same project, restricted to **Custom
   Search API**.
5. Set `GOOGLE_API_KEY` and `GOOGLE_CSE_ID`.

### Configure and restart

```dotenv
# Recommended for new deployments
GOOGLE_PLACES_API_KEY=your_places_api_new_key

# Existing Custom Search customers only
GOOGLE_API_KEY=your_google_api_key
GOOGLE_CSE_ID=your_programmable_search_engine_id
```

Restart FastAPI after editing `.env`. `GET /api/health` reports whether each
integration is configured. Places-only discovery works with the CSE variables
blank.

### Quotas and pricing

- Places API (New) is usage-billed. Text Search returns up to 20 places per
  page and up to 60 per query. LeadScope follows page tokens up to 60 results
  and may issue up to three query variants (companies, agency, services),
  stopping as soon as the website target is met. The `places.websiteUri` field
  is in the Text Search Enterprise SKU, so check the current
  [Places pricing](https://developers.google.com/maps/documentation/places/web-service/usage-and-billing)
  before production use.
- A Maps Demo Key has no billing but a limited daily quota. It is intended for
  prototyping.
- Custom Search JSON API is closed to new customers. Existing customers keep
  their free daily allowance (currently 100 queries/day) and paid queries until
  the service ends on January 1, 2027. Each query returns at most 10 results.
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
2. **Discover websites.** Google Places and/or CSE results are normalised to
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

Current results in this environment: **106 Python tests passed** and **6 Node
tests passed**.

Coverage includes:

- natural-language parsing examples and overrides
- Google CSE/Places request parameters, Places pagination and query variants,
  deduplication, ranking, quota and configuration errors
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

- Live Places API (New) discovery returned real UK real-estate websites; the
  query variants produced 27 unique company domains for a 40-site target.
- A live Canada marketing run using `/api/extract` discovered 4 websites,
  crawled all 4, and returned 17 unique emails from 93 occurrences (76
  duplicates removed).
- `POST /api/extract-sync` against `https://www.canadacreate.com` returned
  `info@canadacreate.com` from the contact page and completed in about 3
  seconds.
- The same sync flow was exercised through the mobile frontend at 390 px with
  `SYNC_EXTRACTION=1`: progress reached 100%, the result card appeared, and the
  dashboard stayed within the viewport.
- Chromium checks at 320, 390, 768, and 1024 px reported
  `scrollWidth === innerWidth`; phones and tablets use cards, and the table
  appears at 1024 px and above.
- Form controls computed to `16px` on mobile, which prevents iOS focus zoom.
- The Vercel `Mangum` handler returned `200` for `/api/health` and for a
  synchronous `POST /api/extract-sync` event.

A full 500-email target was not run. Places caps each query at 60 results, and
large jobs need multiple queries or a local async run. Extraction does not
verify deliverability.

---

## Mobile usage

1. Open the app URL in Safari (iPhone) or Chrome (Android).
2. Use the menu button for Workspace, Results, History, and API docs.
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
│   ├── search_engine.py     Google Places + optional CSE + NL parser
│   ├── crawler.py           async crawler and aggregation
│   ├── email_extractor.py   extraction, dedupe, categorisation
│   ├── validator.py         Pydantic request models
│   └── security.py          SSRF checks and safe fetch
├── api/
│   └── index.py             Vercel Mangum entrypoint
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
├── .vercelignore
├── AGENTS.md
├── pytest.ini
├── requirements.txt
├── requirements-dev.txt
├── vercel.json
└── README.md
```

---

## Troubleshooting

**"Google search is not configured"**
Add `GOOGLE_PLACES_API_KEY` to `.env` (or to Vercel's environment variables) and
restart or redeploy. Custom Search customers can add `GOOGLE_API_KEY` and
`GOOGLE_CSE_ID` instead. Manual domain mode does not need Google.

**`503` from `/api/search`**
The key is missing, invalid, restricted, or the API is not enabled. For Places,
confirm Places API (New) is enabled, the key allows `places.googleapis.com`, and
billing or a Maps Demo Key is active. For Custom Search, note that the API is
closed to new customers; existing projects must enable it in the key's project.
Check the Google Cloud console and the server log.

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
