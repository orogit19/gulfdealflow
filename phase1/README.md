# GCC Venture Intelligence — Data Collection Suite

## Setup

```bash
pip install -r requirements.txt
```

## Workflow

### Step 1 — Crunchbase Export
1. Go to crunchbase.com → Discover → Companies
2. Filter: HQ = UAE / Saudi Arabia / Kuwait / Bahrain / Oman / Qatar
3. Filter: Funding Type = Seed, Series A, Series B, Series C
4. Filter: Last Funding Date = 2020 onwards
5. Export CSV → save as `crunchbase_export.csv` in this folder
6. Run: `python 01_crunchbase_processor.py`
7. Output: `crunchbase_cleaned.csv`

### Step 2 — News Scraper
1. Run: `python 02_news_scraper.py`
2. Output: `news_staging.csv`
3. **Open this file and review every row** — fill blanks, delete non-deals
4. The `notes` column has the original headline for reference

### Step 3 — Manual Entries
- Add any deals you find manually to `gcc_ventures_template.csv`
- Follow the format of the sample rows

### Step 4 — Merge Everything
1. Run: `python 03_merger.py`
2. Output: `gcc_ventures_master.csv` — your single source of truth

---

## Data Schema

| Field | Type | Notes |
|---|---|---|
| deal_id | string | Auto-assigned (GCC0001, GCC0002...) |
| company_name | string | Required |
| country | string | UAE, Saudi Arabia, Kuwait, Bahrain, Oman, Qatar |
| city | string | Dubai, Riyadh, Abu Dhabi etc. |
| date | YYYY-MM | Month of deal announcement |
| stage | string | Pre-Seed, Seed, Series A, Series B, Series C+, Growth, Undisclosed |
| amount_usd | integer | Full number in USD, blank if undisclosed |
| disclosed | boolean | TRUE / FALSE |
| sector | string | See sector taxonomy below |
| description | string | One line, what the company does |
| founded_year | integer | YYYY |
| website | string | Company website |
| lead_investor | string | Primary investor |
| co_investors | string | Comma-separated |
| investor_types | string | VC, CVC, Angel, Government, Family Office |
| source | string | Where you found it |
| notes | string | Any relevant context |

## Sector Taxonomy
- Fintech
- Proptech
- Logistics & Supply Chain
- Healthtech
- Edtech
- E-commerce & Retail
- SaaS & Enterprise Software
- Deep Tech & AI
- Energy & Cleantech
- Media & Entertainment
- Food & Agritech
- Other
