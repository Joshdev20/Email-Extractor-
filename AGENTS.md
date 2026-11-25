# LeadScope repository notes

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # optional: add Google credentials
python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/`. API docs are at `/api/docs`.

## Test

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q
node --test tests/export.test.js
```

Tests are offline: `tests/conftest.py` replaces DNS resolution with a public
address, and Google/crawl HTTP calls use `httpx.MockTransport` with fixtures in
`tests/fixtures/`. Do not add live network calls to the default suite.

## Architecture

- `backend/main.py` - FastAPI app, in-memory `JobManager`, SSE progress,
  rate limiting, security headers, static frontend serving.
- `backend/search_engine.py` - Google Places/CSE client, natural-language
  `QuerySpec` parser (keyword based, not an LLM), domain ranking.
- `backend/crawler.py` - async crawler, robots.txt, link priority, per-domain
  limits/rate limiting, cancellation, result aggregation.
- `backend/email_extractor.py` - regex/BeautifulSoup extraction, obfuscated
  emails, JSON-LD, dedupe, syntax checks, categorisation.
- `backend/security.py` - SSRF validation, safe fetch with redirect checks,
  response-size limits, domain normalisation.
- `backend/validator.py` - Pydantic request models and input cleaning.
- `frontend/` - static mobile-first UI; `export.js` is a UMD module shared with
  the Node tests.
- `api/index.py` - Vercel `mangum` entrypoint. `vercel.json` serves `frontend/`
  statically and routes `/api/*` to the function.
- On Vercel (`VERCEL` env is set), `/api/config` returns
  `sync_extraction: true` and `sync_batch_size: 6`. For natural-language
  queries the frontend calls `/api/search`, splits the discovered websites into
  batches, calls `/api/extract-sync` once per batch, and merges the results in
  the browser. Manual domain lists are batched directly. Serverless functions
  cannot keep in-memory background jobs alive, so this batching is what allows
  large targets. Local mode keeps async jobs and SSE.

## Google API availability

- Custom Search JSON API is closed to new customers (existing access ends
  January 1, 2027). New projects get `403 This project does not have the access
  to Custom Search JSON API`; no credential change fixes that.
- New deployments use Places API (New), which requires billing or a no-cost
  Maps Demo Key. The key must allow `places.googleapis.com` (Places API (New)),
  not the legacy Places API.
- Places-only discovery works with `GOOGLE_API_KEY` and `GOOGLE_CSE_ID` blank.

## Invariants

- No database, no accounts, no permanent server-side result storage. Jobs live
  in memory only and expire (`JOB_TTL_SECONDS`, default 900s).
- Google credentials are read from environment variables on the server and are
  never sent to the browser.
- Extraction never performs SMTP verification or bypasses access controls.
- Keep `frontend/export.js` dependency-free so `node --test` can load it.
