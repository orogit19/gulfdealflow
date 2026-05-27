"""
GCC Venture Intelligence — Crunchbase Processor
================================================
Takes a raw CSV export from Crunchbase and normalises it
to the GCC Venture schema.

HOW TO GET THE CRUNCHBASE EXPORT:
1. Go to crunchbase.com
2. Click "Discover" → "Companies"
3. Add filters:
   - Headquarters Location: United Arab Emirates, Saudi Arabia,
     Kuwait, Bahrain, Oman, Qatar
   - Funding Type: Seed, Series A, Series B, Series C, etc.
   - Last Funding Date: 2020-01-01 to present
4. Click "Export" (free tier gives you up to 1000 rows)
5. Save the CSV and point CRUNCHBASE_CSV_PATH to it below

USAGE:
    python 01_crunchbase_processor.py

OUTPUT:
    crunchbase_cleaned.csv — ready to review and merge
"""

import pandas as pd
import re
import os
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────────────────────
CRUNCHBASE_CSV_PATH = "crunchbase_export.csv"   # path to your raw export
OUTPUT_PATH         = "crunchbase_cleaned.csv"
# ──────────────────────────────────────────────────────────────────────────────

# Map Crunchbase country names → your standard names
COUNTRY_MAP = {
    "United Arab Emirates": "UAE",
    "Saudi Arabia":         "Saudi Arabia",
    "Kingdom of Saudi Arabia": "Saudi Arabia",
    "Kuwait":               "Kuwait",
    "Bahrain":              "Bahrain",
    "Oman":                 "Oman",
    "Qatar":                "Qatar",
}

# Map Crunchbase funding types → your stage taxonomy
STAGE_MAP = {
    "pre_seed":             "Pre-Seed",
    "seed":                 "Seed",
    "series_a":             "Series A",
    "series_b":             "Series B",
    "series_c":             "Series C+",
    "series_d":             "Series C+",
    "series_e":             "Series C+",
    "series_f":             "Series C+",
    "growth":               "Growth",
    "venture":              "Undisclosed",
    "angel":                "Pre-Seed",
    "convertible_note":     "Pre-Seed",
    "equity_crowdfunding":  "Seed",
    "corporate_round":      "Growth",
    "debt_financing":       "Growth",
    "grant":                "Other",
    "undisclosed":          "Undisclosed",
}

# Map broad Crunchbase categories → your sector taxonomy
SECTOR_MAP = {
    "financial services":   "Fintech",
    "fintech":              "Fintech",
    "payments":             "Fintech",
    "insurance":            "Fintech",
    "lending":              "Fintech",
    "real estate":          "Proptech",
    "proptech":             "Proptech",
    "logistics":            "Logistics & Supply Chain",
    "supply chain":         "Logistics & Supply Chain",
    "transportation":       "Logistics & Supply Chain",
    "health":               "Healthtech",
    "healthcare":           "Healthtech",
    "medical":              "Healthtech",
    "edtech":               "Edtech",
    "education":            "Edtech",
    "e-commerce":           "E-commerce & Retail",
    "retail":               "E-commerce & Retail",
    "marketplace":          "E-commerce & Retail",
    "saas":                 "SaaS & Enterprise Software",
    "enterprise software":  "SaaS & Enterprise Software",
    "software":             "SaaS & Enterprise Software",
    "artificial intelligence": "Deep Tech & AI",
    "machine learning":     "Deep Tech & AI",
    "deep tech":            "Deep Tech & AI",
    "energy":               "Energy & Cleantech",
    "clean energy":         "Energy & Cleantech",
    "cleantech":            "Energy & Cleantech",
    "media":                "Media & Entertainment",
    "entertainment":        "Media & Entertainment",
    "food":                 "Food & Agritech",
    "agriculture":          "Food & Agritech",
    "agritech":             "Food & Agritech",
}

def clean_amount(value):
    """Convert Crunchbase money strings like '$12,000,000' to integer."""
    if pd.isna(value) or value == "" or value == "--":
        return None, False
    # Remove currency symbols, commas, spaces
    cleaned = re.sub(r"[^\d.]", "", str(value))
    if cleaned == "":
        return None, False
    try:
        return int(float(cleaned)), True
    except ValueError:
        return None, False

def map_sector(categories_str):
    """Map Crunchbase categories string to your sector taxonomy."""
    if pd.isna(categories_str) or categories_str == "":
        return "Other"
    cats = [c.strip().lower() for c in str(categories_str).split(",")]
    for cat in cats:
        for key, sector in SECTOR_MAP.items():
            if key in cat:
                return sector
    return "Other"

def map_stage(funding_type):
    """Map Crunchbase funding type to your stage taxonomy."""
    if pd.isna(funding_type) or funding_type == "":
        return "Undisclosed"
    key = str(funding_type).lower().replace(" ", "_")
    return STAGE_MAP.get(key, "Undisclosed")

def format_date(date_str):
    """Normalise various date formats to YYYY-MM."""
    if pd.isna(date_str) or date_str == "":
        return None
    for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%Y-%m", "%B %Y", "%b %Y"]:
        try:
            dt = datetime.strptime(str(date_str).strip(), fmt)
            return dt.strftime("%Y-%m")
        except ValueError:
            continue
    return str(date_str)[:7]  # fallback: take first 7 chars

def process_crunchbase(input_path, output_path):
    print(f"Reading: {input_path}")
    df = pd.read_csv(input_path, skiprows=4)  # Crunchbase exports have 4 header rows

    print(f"Raw rows: {len(df)}")
    print(f"Columns found: {list(df.columns)}\n")

    # ── Build output records ──────────────────────────────────────────────────
    records = []
    for i, row in df.iterrows():

        # Country — filter to GCC only
        raw_country = str(row.get("Headquarters Location", "")).split(",")[0].strip()
        country = COUNTRY_MAP.get(raw_country)
        if not country:
            continue  # skip non-GCC

        # City — second part of Headquarters Location
        location_parts = str(row.get("Headquarters Location", "")).split(",")
        city = location_parts[1].strip() if len(location_parts) > 1 else ""

        # Amount
        amount, disclosed = clean_amount(row.get("Funding Amount"))
        if not disclosed:
            # Try Total Funding Amount as fallback
            amount, disclosed = clean_amount(row.get("Total Funding Amount"))

        record = {
            "deal_id":        "",                          # assigned at merge
            "company_name":   str(row.get("Organization Name", "")).strip(),
            "country":        country,
            "city":           city,
            "date":           format_date(row.get("Last Funding Date")),
            "stage":          map_stage(row.get("Last Funding Type")),
            "amount_usd":     amount if amount else "",
            "disclosed":      "TRUE" if disclosed else "FALSE",
            "sector":         map_sector(row.get("Industries")),
            "description":    str(row.get("Description", "")).strip()[:200],
            "founded_year":   str(row.get("Founded Date", ""))[:4],
            "website":        str(row.get("Website", "")).strip(),
            "lead_investor":  "",                          # not in basic export
            "co_investors":   "",
            "investor_types": "",
            "source":         "Crunchbase",
            "notes":          "",
        }

        # Skip if no company name or date
        if not record["company_name"] or not record["date"]:
            continue

        records.append(record)

    output_df = pd.DataFrame(records)
    output_df.to_csv(output_path, index=False)

    print(f"✓ Processed {len(output_df)} GCC deals")
    print(f"✓ Saved to: {output_path}")
    print(f"\nCountry breakdown:")
    print(output_df["country"].value_counts().to_string())
    print(f"\nSector breakdown:")
    print(output_df["sector"].value_counts().to_string())

if __name__ == "__main__":
    if not os.path.exists(CRUNCHBASE_CSV_PATH):
        print(f"ERROR: File not found — {CRUNCHBASE_CSV_PATH}")
        print("Export your Crunchbase data and place it in the same folder as this script.")
    else:
        process_crunchbase(CRUNCHBASE_CSV_PATH, OUTPUT_PATH)
