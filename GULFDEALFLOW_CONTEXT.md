# GulfDealFlow — Project Context
_Paste this at the start of every new Claude Code session for instant context._

---

## What It Is
An independent GCC venture capital intelligence platform. Tracks startup funding rounds across the Gulf Cooperation Council. Think Bloomberg terminal meets VC data. Built as a portfolio/showcase project by Stavros Gaiganis (signs off as "Built by Oro — Dubai, UAE").

**Live URLs:**
- Frontend: https://gulfdealflow.vercel.app
- Backend API: https://gulfdealflow-api.vercel.app

**Repo:** https://github.com/orogit19/gulfdealflow

---

## Stack
- **Frontend:** React 18 + Vite + Tailwind CSS (`phase3/`)
- **Backend:** FastAPI + uvicorn, deployed as Vercel serverless functions (`phase2/`)
- **Database:** Supabase (Postgres). Anon key for public reads (RLS enforced). Service role key for writes/ingestion — never committed.
- **AI extraction:** OpenAI API (deal extraction from article text)
- **Data pipeline:** Google Colab notebooks in `phase1/` (Crunchbase processor, news scraper, master merger)

---

## Monorepo Layout
```
phase1/   — data pipeline scripts & notebooks (Colab)
phase2/   — FastAPI backend (main.py, migrations/, schema.sql, tests)
phase3/   — React frontend (src/App.jsx — single file, ~180KB)
Insights/ — published article markdown files
```

All frontend logic lives in `phase3/src/App.jsx` (single file by design).
All backend logic lives in `phase2/main.py` (single file by design).

---

## Database State
- **312 deals** tracked, all with dates
- **$7.26B** total capital
- **Countries:** UAE (146), Saudi Arabia (109), Kuwait, Qatar, Bahrain, Oman
- **Stages:** Pre-Seed, Seed, Series A, Series B, Series C+, Growth, Undisclosed

---

## Frontend — Tabs / Pages
| Tab | Component | Notes |
|-----|-----------|-------|
| Deal Explorer | `Explorer` | Filters (country, sector, stage, year), search, pagination, CSV export |
| Dashboard | `Dashboard` | Recharts charts + GCC choropleth map (react-simple-maps + d3-geo) |
| Insights | `Insights` / `ArticleReader` | Article feed; articles defined in `ARTICLES` array in App.jsx |
| Investors | `InvestorDirectory` | Leaderboard + horizontal timeline modal per investor |
| Review | `ReviewQueue` | **Admin only** — hidden from public (see Admin UI below) |
| About | `About` | Mission, methodology, live stats from API |

**Design:** dark slate `#0f1117`, white text, teal accent `#06b6d4`. No gradients.

---

## Backend — API Endpoints

### Public
| Method | Path | Notes |
|--------|------|-------|
| GET | `/` | Root |
| GET | `/health` | `{"ok": true, ...}` |
| GET | `/deals` | List deals — filters: `country`, `sector`, `stage`, `year`, `search` |
| GET | `/deals/{id}` | Single deal |
| GET | `/stats` | Totals + breakdowns (aggregated in Python — known tech debt, move to RPC at >500 rows) |
| GET | `/investors/leaderboard` | Ranked investors with deal/capital/sector stats |

### Admin (require `X-Admin-Key` header)
| Method | Path | Notes |
|--------|------|-------|
| GET | `/admin/config-status` | Env var health check |
| GET | `/admin/db-status` | Checks migration 009 applied |
| GET | `/cron/discover` | Runs RSS discovery (Vercel cron, Bearer auth) |
| POST | `/discover/rss` | Manual RSS discovery trigger |
| POST | `/ingest/url` | Fetch + store a raw article URL |
| POST | `/ingest/extract/{id}` | AI deal extraction from raw source |
| POST | `/ingest/portfolio-page` | VC portfolio scrape |
| GET | `/raw-sources` | List raw sources |
| GET | `/raw-sources/{id}` | Single raw source |
| POST | `/raw-sources/{id}/refetch` | Re-fetch an article |
| GET | `/ingestion-logs` | Ingestion audit log |
| GET | `/extracted-deals` | Queue of AI-extracted drafts |
| GET | `/extracted-deals/stats` | Queue stats by status/readiness |
| PATCH | `/extracted-deals/{id}` | Edit draft fields |
| POST | `/extracted-deals/{id}/approve` | Approve → writes to `deals` table |
| POST | `/extracted-deals/{id}/reject` | Reject draft |
| POST | `/extracted-deals/{id}/reopen` | Reopen rejected draft |
| POST | `/extracted-deals/{id}/reextract` | Re-run AI extraction |
| POST | `/extracted-deals/actions/reject-non-funding` | Batch reject non-funding |
| POST | `/extracted-deals/actions/reject-duplicates` | Batch reject duplicates |
| POST | `/relationships/{id}/enrich-funding` | Funding enrichment for VC relationships |

**Admin key:** set `ADMIN_API_KEY` in `phase2/.env`. If unset, all routes open (dev convenience). Never put it in a `VITE_*` variable.

---

## Admin / Ingestion Pipeline

The ingestion loop: **RSS discovery → URL ingest → AI extraction → human review → approve to deals table.**

1. Vercel cron runs `GET /cron/discover` daily at 03:00 UTC — queries Google News RSS, filters GCC/funding signals, stores candidates as `pending` raw sources
2. Operator opens Review tab → triggers ingest on candidates → AI extracts deal fields → draft lands in queue
3. Operator reviews draft in Review tab, edits fields, approves → deal written to `deals` table with `reviewed_at` + `approved_deal_id` audit columns

**Review tab** (`ReviewQueue` component, `phase3/src/App.jsx` ~line 2810):
- Only visible when `ADMIN_UI_ENABLED` is true (`DEV` mode or `VITE_ENABLE_ADMIN_UI=true`)
- Admin key stored in localStorage, injected on every request via `apiFetch()`
- Features: queue list, status/readiness filters, editable draft form, source article panel, approve/reject/reopen/reextract, ingestion logs, approval preview, batch ops, RSS discovery trigger, URL ingest, config health checks

---

## Security
- `require_admin_api_key` HTTP middleware gates all admin/ingest routes
- SSRF protection (`_assert_public_fetch_target`) blocks fetches to localhost, private IPs, AWS metadata
- CORS: locked to `CORS_ORIGINS` env var in production; open in dev
- Custom response headers (`X-GDF-Duplicate`, `X-GDF-Raw-Source-Id`, `X-GDF-Fetch-Status`, `X-GDF-Existing-Extraction`, `X-GDF-Extracted-Deal-Id`) tell frontend what happened on each ingest call

---

## Database Migrations
| File | Status | What it does |
|------|--------|--------------|
| 001–007 | ✅ Applied | Core schema, ingestion tables, AI fields, portfolio, relationships |
| 008 | ✅ Applied | Added `source_url` column to `extracted_deals` |
| **009** | ⚠️ **PENDING** | Adds `reviewed_at` + `approved_deal_id` audit columns to `extracted_deals` |

**Migration 009 must be applied before the approve endpoint works.** Run `phase2/migrations/009_extracted_deal_review_audit.sql` in Supabase SQL Editor.

---

## Tests
`phase2/test_review_helpers.py` — 41 tests, all passing.

Run with:
```
cd phase2
C:\Users\stgai\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe run_tests.py
```

Covers: admin key middleware, CORS headers, ingest URL (duplicate/new/fail/pending), extract (duplicate/empty), refetch, approve idempotency, config status, env parsing, SSRF protection, URL fetch construction, Google News URL resolution, RSS filtering, cron auth.

---

## Environment Variables

### phase2/.env (backend)
```
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
SUPABASE_ANON_KEY=
OPENAI_API_KEY=
ADMIN_API_KEY=          # protects admin routes
CRON_SECRET=            # protects /cron/discover
CORS_ORIGINS=https://gulfdealflow.vercel.app
ARTICLE_FETCH_TIMEOUT_SECONDS=20
ARTICLE_FETCH_MAX_BYTES=5000000
```

### phase3/.env (frontend)
```
VITE_API_URL=http://localhost:8000
VITE_ENABLE_ADMIN_UI=true   # shows Review tab in production
```

---

## Deployment
- Both frontend and backend deploy automatically from `git push origin master` via Vercel
- Backend: `phase2/vercel.json` rewrites all routes to `/api/index`
- Frontend: `phase3/` — Vite build, static output

**Vercel env vars** (set in Vercel dashboard, not committed): `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY`, `OPENAI_API_KEY`, `ADMIN_API_KEY`, `CRON_SECRET`, `CORS_ORIGINS`

---

## Tech Debt / Known Issues
- `/stats` aggregates in Python — move to Supabase RPC when deals exceed ~500 rows
- Migration 009 not yet applied to production Supabase (see above)

---

## Build History
1. Data pipeline — Colab notebooks for Crunchbase + news scraping + merging
2. FastAPI backend + Supabase integration
3. Vercel deployment (frontend + backend)
4. React frontend — Deal Explorer, Dashboard with Recharts
5. About page, SEO, robots.txt, favicon
6. Investor profiles + horizontal timeline modal
7. Pagination + adjustable rows per page + CSV export
8. GCC choropleth map (react-simple-maps)
9. Insights tab + ArticleReader view
10. **Admin ingestion review pipeline** — RSS discovery, URL ingest, AI extraction, ReviewQueue UI, approve/reject workflow, audit trail, 41-test suite
