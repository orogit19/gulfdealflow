"""
GCC Venture Intelligence — News Scraper
========================================
Scrapes Google News RSS for GCC startup funding announcements,
extracts deal data using pattern matching, and outputs a staging
CSV for manual review before merging into the master dataset.

This scraper is intentionally conservative — it flags deals for
human review rather than auto-adding them. You review the staging
file, fill in any blanks, then run the merger.

USAGE:
    python 02_news_scraper.py

OUTPUT:
    news_staging.csv — deals extracted from news, needs your review
"""

import requests
import re
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime
import time

# ── CONFIG ─────────────────────────────────────────────────────────────────────
OUTPUT_PATH    = "news_staging.csv"
REQUEST_DELAY  = 2   # seconds between requests — be polite
# ──────────────────────────────────────────────────────────────────────────────

# Search queries — each targets a slightly different angle
RSS_QUERIES = [
    "startup funding UAE million 2024",
    "startup funding Saudi Arabia million 2024",
    "venture capital GCC funding round 2024",
    "raises million seed series UAE",
    "raises million series Saudi Arabia",
    "raises million Kuwait Bahrain Oman Qatar startup",
    "startup funding UAE million 2025",
    "venture capital Saudi Arabia 2025",
]

GCC_COUNTRIES = ["UAE", "United Arab Emirates", "Dubai", "Abu Dhabi", "Sharjah",
                 "Saudi Arabia", "Riyadh", "Jeddah", "NEOM",
                 "Kuwait", "Bahrain", "Oman", "Muscat", "Qatar", "Doha"]

STAGE_PATTERNS = {
    "Pre-Seed":  r"\bpre[\-\s]?seed\b",
    "Seed":      r"\bseed\s+(round|funding|stage)\b|\bseed\b(?!\s+series)",
    "Series A":  r"\bseries\s+a\b",
    "Series B":  r"\bseries\s+b\b",
    "Series C+": r"\bseries\s+[cdefg]\b",
    "Growth":    r"\bgrowth\s+(round|equity|stage)\b|\blate[\-\s]stage\b",
}

SECTOR_KEYWORDS = {
    "Fintech":                  ["fintech", "payment", "neobank", "lending", "insurtech", "crypto", "defi", "remittance"],
    "Proptech":                 ["proptech", "real estate tech", "property tech"],
    "Logistics & Supply Chain": ["logistics", "supply chain", "last mile", "freight", "delivery", "fleet"],
    "Healthtech":               ["healthtech", "health tech", "telehealth", "medtech", "digital health", "pharmatech"],
    "Edtech":                   ["edtech", "ed tech", "education tech", "e-learning", "online learning"],
    "E-commerce & Retail":      ["e-commerce", "ecommerce", "marketplace", "retail tech", "d2c", "direct-to-consumer"],
    "SaaS & Enterprise Software":["saas", "enterprise software", "b2b software", "cloud platform"],
    "Deep Tech & AI":           ["ai startup", "artificial intelligence", "machine learning", "deep tech", "robotics", "drone"],
    "Energy & Cleantech":       ["cleantech", "clean energy", "solar", "renewable", "green tech", "sustainability"],
    "Media & Entertainment":    ["media tech", "streaming", "gaming", "content platform", "creator economy"],
    "Food & Agritech":          ["food tech", "agritech", "agri-tech", "restaurant tech", "foodservice"],
}

def build_rss_url(query):
    encoded = query.replace(" ", "+")
    return f"https://news.google.com/rss/search?q={encoded}&hl=en&gl=US&ceid=US:en"

def extract_amount(text):
    """Extract funding amount from article text and convert to USD."""
    text_lower = text.lower()

    # Pattern: $X million / $X.X million / $XM
    patterns = [
        r"\$(\d+(?:\.\d+)?)\s*billion",
        r"\$(\d+(?:\.\d+)?)\s*million",
        r"\$(\d+(?:\.\d+)?)\s*m\b",
        r"(\d+(?:\.\d+)?)\s*million\s*(?:dollar|usd)",
        r"(\d+(?:\.\d+)?)\s*billion\s*(?:dollar|usd)",
        r"aed\s*(\d+(?:\.\d+)?)\s*million",
        r"sar\s*(\d+(?:\.\d+)?)\s*million",
    ]

    multipliers = {
        "billion": 1_000_000_000,
        "million": 1_000_000,
        "m":       1_000_000,
    }

    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            amount = float(match.group(1))
            if "billion" in pattern:
                amount *= 1_000_000_000
            elif "million" in pattern or r"\bm\b" in pattern:
                amount *= 1_000_000
            # AED/SAR rough conversion
            if "aed" in pattern:
                amount = amount * 1_000_000 / 3.67
            if "sar" in pattern:
                amount = amount * 1_000_000 / 3.75
            return int(amount), True
    return None, False

def extract_stage(text):
    """Extract funding stage from article text."""
    text_lower = text.lower()
    for stage, pattern in STAGE_PATTERNS.items():
        if re.search(pattern, text_lower):
            return stage
    return "Undisclosed"

def extract_country(text):
    """Extract GCC country from article text."""
    for country in GCC_COUNTRIES:
        if country.lower() in text.lower():
            if country in ["Dubai", "Abu Dhabi", "Sharjah"]:
                return "UAE", country
            if country in ["Riyadh", "Jeddah", "NEOM"]:
                return "Saudi Arabia", country
            if country == "Muscat":
                return "Oman", country
            if country == "Doha":
                return "Qatar", country
            return country, ""
    return None, ""

def extract_sector(text):
    """Detect sector from article text."""
    text_lower = text.lower()
    for sector, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                return sector
    return "Other"

def extract_company(title):
    """
    Rough company name extraction from headlines like:
    'Tabby raises $200M in Series D' or 'Dubai fintech Tabby secures $200M'
    """
    # Pattern: "<Company> raises/secures/closes"
    match = re.search(r"^([A-Z][a-zA-Z0-9\s\-\.]+?)\s+(?:raises?|secures?|closes?|lands?|gets?)\s", title)
    if match:
        return match.group(1).strip()
    # Pattern: "funding for <Company>"
    match = re.search(r"(?:funding|investment)\s+for\s+([A-Z][a-zA-Z0-9\s\-\.]+)", title)
    if match:
        return match.group(1).strip()
    return ""  # flag for manual review

def scrape_news():
    print("Starting GCC news scraper...\n")
    all_deals = []
    seen_titles = set()

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }

    for query in RSS_QUERIES:
        url = build_rss_url(query)
        print(f"Querying: {query}")

        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                print(f"  ✗ Status {r.status_code}")
                continue

            soup = BeautifulSoup(r.text, "xml")
            items = soup.find_all("item")
            print(f"  ✓ Found {len(items)} articles")

            for item in items:
                title   = item.find("title").get_text(strip=True) if item.find("title") else ""
                link    = item.find("link").get_text(strip=True) if item.find("link") else ""
                pub_raw = item.find("pubDate").get_text(strip=True) if item.find("pubDate") else ""
                desc    = item.find("description").get_text(strip=True) if item.find("description") else ""

                # Deduplicate
                if title in seen_titles:
                    continue
                seen_titles.add(title)

                # Only process if it looks like a funding story
                funding_signals = ["raises", "raise", "secures", "funding", "series", "seed",
                                   "million", "billion", "investment", "backed"]
                full_text = (title + " " + desc).lower()
                if not any(sig in full_text for sig in funding_signals):
                    continue

                # Country filter
                country, city = extract_country(full_text)
                if not country:
                    continue

                # Extract deal data
                amount, disclosed = extract_amount(full_text)
                stage             = extract_stage(full_text)
                sector            = extract_sector(full_text)
                company           = extract_company(title)

                # Parse date
                try:
                    date_obj = datetime.strptime(pub_raw[:16], "%a, %d %b %Y")
                    date_str = date_obj.strftime("%Y-%m")
                except:
                    date_str = ""

                deal = {
                    "deal_id":        "",
                    "company_name":   company,       # review if blank
                    "country":        country,
                    "city":           city,
                    "date":           date_str,
                    "stage":          stage,
                    "amount_usd":     amount if amount else "",
                    "disclosed":      "TRUE" if disclosed else "FALSE",
                    "sector":         sector,
                    "description":    "",            # fill manually
                    "founded_year":   "",
                    "website":        "",
                    "lead_investor":  "",
                    "co_investors":   "",
                    "investor_types": "",
                    "source":         "Google News",
                    "notes":          f"REVIEW | Title: {title[:120]} | Link: {link}",
                }
                all_deals.append(deal)

        except Exception as e:
            print(f"  ✗ Error: {e}")

        time.sleep(REQUEST_DELAY)

    if not all_deals:
        print("\nNo deals found. This may be a network issue — try running on your local machine.")
        return

    df = pd.DataFrame(all_deals)
    df.to_csv(OUTPUT_PATH, index=False)

    print(f"\n{'='*50}")
    print(f"✓ Extracted {len(df)} potential deals")
    print(f"✓ Saved staging file: {OUTPUT_PATH}")
    print(f"\n⚠ NEXT STEP: Open {OUTPUT_PATH} and review each row.")
    print("  - Fill in blank company names (check the 'notes' column for the headline)")
    print("  - Verify amounts and stages")
    print("  - Add descriptions, websites, investor info where you can")
    print("  - Delete any rows that aren't real funding deals")
    print("  Then run 03_merger.py to add them to the master dataset.")

if __name__ == "__main__":
    scrape_news()
