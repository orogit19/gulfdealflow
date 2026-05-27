# GulfDealFlow — Project Context
_Paste this at the start of every new Claude chat for instant context._

---

## What It Is
An independent GCC venture capital intelligence platform. Tracks startup funding rounds across the Gulf Cooperation Council. Think Bloomberg terminal meets VC data. Built as a portfolio/showcase project.

**Live URLs:**
- Frontend: https://gulfdealflow.vercel.app
- Backend API: https://gulfdealflow-api.vercel.app

---

## Stack
- **Frontend:** React + Vite + Tailwind CSS
- **Backend:** FastAPI, deployed as serverless functions on Vercel
- **Database:** Supabase (anon key on backend, RLS enforced, service role key never pushed)
- **Data pipeline:** Google Colab notebooks (Crunchbase processor, news scraper, master merger)

---

## Current State
- **200+ deals** in the database, all with dates (backfill complete)
- **Total capital:** ~$2.63B+ tracked
- **Countries:** UAE, Saudi Arabia, Kuwait, Bahrain, Oman, Qatar
- **Stages:** Pre-Seed, Seed, Series A, Series B, Series C+, Growth, Undisclosed

---

## Pages / Features
| Tab | Status | Notes |
|-----|--------|-------|
| Deal Explorer | ✅ Live | Filters (country, sector, stage, year), search, CSV export, pagination, adjustable rows per page |
| Dashboard | ✅ Live | Recharts — deal volume by year, country/sector breakdown |
| Insights | ✅ Live | — |
| Investors | ✅ Live | Investor profiles with timeline — shows when and what each investor invested in |
| About | ✅ Live | Mission, methodology, live coverage stats from API |

---

## Design
- Dark slate background (`#0f1117`)
- White text
- Teal accent colour
- Clean, professional — no gradients or flashy animations

---

## API Endpoints
- `GET /deals` — list deals, filters: `country`, `sector`, `stage`, `year`, `search`
- `GET /deals/{id}` — single deal by deal_id
- `GET /stats` — totals + breakdowns by country/sector/stage

---

## Known Flags / Tech Debt
- `/stats` endpoint currently aggregates in Python — should move to a Supabase RPC/database function when deals exceed ~500 rows
- Search is backend (`ilike` on company_name, lead_investor, sector) — good
- Pagination is live — good

---

## What's Been Built Session by Session
1. Data pipeline — Colab notebooks for Crunchbase processing, news scraping, master merging
2. FastAPI backend + Supabase integration
3. Vercel deployment (frontend + backend, CORS locked)
4. React frontend — Deal Explorer, Dashboard with recharts
5. About page, SEO, robots.txt, favicon
6. Investor profiles + timeline (horizontal modal, deal cards above/below axis, stage badge colours: Pre-Seed purple, Seed blue, Series A teal, Series B green, Series C+ amber, Growth orange)
7. Pagination + adjustable rows per page
8. CSV export (client-side, current filtered view)

---

## Notes
- Also working on a separate project called **Vantium** (active, waiting on regulatory approval — unrelated to GulfDealFlow)
- Builder signs off as "Built by Oro — Dubai, UAE"
