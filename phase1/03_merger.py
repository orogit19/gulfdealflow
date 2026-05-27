"""
GCC Venture Intelligence — Master Merger
==========================================
Combines all cleaned source files into one master dataset,
deduplicates, assigns deal IDs, and produces the final CSV.

Run this after you've reviewed your staging files.

USAGE:
    python 03_merger.py

INPUT FILES (any combination):
    gcc_ventures_template.csv   — your manual entries
    crunchbase_cleaned.csv      — output from 01_crunchbase_processor.py
    news_staging.csv            — reviewed output from 02_news_scraper.py

OUTPUT:
    gcc_ventures_master.csv     — your single source of truth
"""

import pandas as pd
import os
from datetime import datetime

# ── CONFIG ─────────────────────────────────────────────────────────────────────
SOURCE_FILES = [
    "gcc_ventures_template.csv",
    "crunchbase_cleaned.csv",
    "news_staging.csv",
]
OUTPUT_PATH = "gcc_ventures_master.csv"
# ──────────────────────────────────────────────────────────────────────────────

REQUIRED_COLUMNS = [
    "deal_id", "company_name", "country", "city", "date", "stage",
    "amount_usd", "disclosed", "sector", "description", "founded_year",
    "website", "lead_investor", "co_investors", "investor_types",
    "source", "notes"
]

VALID_COUNTRIES = ["UAE", "Saudi Arabia", "Kuwait", "Bahrain", "Oman", "Qatar"]
VALID_STAGES    = ["Pre-Seed", "Seed", "Series A", "Series B", "Series C+",
                   "Growth", "Undisclosed"]
VALID_SECTORS   = ["Fintech", "Proptech", "Logistics & Supply Chain", "Healthtech",
                   "Edtech", "E-commerce & Retail", "SaaS & Enterprise Software",
                   "Deep Tech & AI", "Energy & Cleantech", "Media & Entertainment",
                   "Food & Agritech", "Other"]

def load_source(path):
    """Load a source CSV, ensuring all required columns exist."""
    df = pd.read_csv(path, dtype=str)
    df = df.fillna("")
    # Add any missing columns
    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[REQUIRED_COLUMNS]

def deduplicate(df):
    """
    Fuzzy-ish deduplication:
    - Exact match on company_name + date → keep first
    - Exact match on company_name + stage (catches same deal from multiple sources)
    """
    before = len(df)

    # Normalise for comparison
    df["_name_norm"]  = df["company_name"].str.lower().str.strip()
    df["_date_norm"]  = df["date"].str[:7]  # YYYY-MM
    df["_stage_norm"] = df["stage"].str.lower().str.strip()

    # Drop exact duplicates first
    df = df.drop_duplicates(subset=["_name_norm", "_date_norm"])

    # Drop same company + stage (likely same deal reported twice)
    df = df.drop_duplicates(subset=["_name_norm", "_stage_norm"], keep="first")

    # Drop helper columns
    df = df.drop(columns=["_name_norm", "_date_norm", "_stage_norm"])

    after = len(df)
    print(f"  Deduplication: {before} → {after} (removed {before - after} duplicates)")
    return df

def validate(df):
    """Flag rows with data quality issues in the notes column."""
    issues = []
    for i, row in df.iterrows():
        row_issues = []
        if row["company_name"].strip() == "":
            row_issues.append("MISSING: company_name")
        if row["country"] not in VALID_COUNTRIES:
            row_issues.append(f"INVALID COUNTRY: {row['country']}")
        if row["stage"] not in VALID_STAGES:
            row_issues.append(f"INVALID STAGE: {row['stage']}")
        if row["sector"] not in VALID_SECTORS:
            row_issues.append(f"INVALID SECTOR: {row['sector']}")
        if row["date"] == "":
            row_issues.append("MISSING: date")

        if row_issues:
            existing_note = row["notes"]
            df.at[i, "notes"] = " | ".join(row_issues) + (" | " + existing_note if existing_note else "")
            issues.append(i)

    if issues:
        print(f"  ⚠ {len(issues)} rows have data quality issues (flagged in 'notes' column)")
    else:
        print(f"  ✓ All rows passed validation")
    return df

def assign_ids(df):
    """Assign sequential deal IDs."""
    df = df.reset_index(drop=True)
    df["deal_id"] = [f"GCC{str(i+1).zfill(4)}" for i in range(len(df))]
    return df

def print_summary(df):
    """Print a clean summary of the master dataset."""
    print(f"\n{'='*55}")
    print(f"  GCC VENTURE MASTER DATASET SUMMARY")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*55}")
    print(f"  Total deals:        {len(df)}")

    disclosed = df[df["disclosed"].str.upper() == "TRUE"]
    if len(disclosed) > 0:
        total_capital = disclosed["amount_usd"].replace("", "0").astype(float).sum()
        print(f"  Total capital (disclosed): ${total_capital/1e9:.2f}B")

    print(f"\n  BY COUNTRY:")
    for country, count in df["country"].value_counts().items():
        print(f"    {country:<20} {count}")

    print(f"\n  BY STAGE:")
    for stage, count in df["stage"].value_counts().items():
        print(f"    {stage:<20} {count}")

    print(f"\n  BY SECTOR:")
    for sector, count in df["sector"].value_counts().items():
        print(f"    {sector:<30} {count}")

    print(f"\n  BY SOURCE:")
    for source, count in df["source"].value_counts().items():
        print(f"    {source:<25} {count}")
    print(f"{'='*55}\n")

def merge():
    print("GCC Venture Intelligence — Master Merger\n")

    all_frames = []

    for path in SOURCE_FILES:
        if os.path.exists(path):
            print(f"Loading: {path}")
            df = load_source(path)
            # Skip template row (the example rows with deal_id = 1,2,3)
            df = df[df["company_name"].str.strip() != ""]
            print(f"  {len(df)} rows loaded")
            all_frames.append(df)
        else:
            print(f"Skipping (not found): {path}")

    if not all_frames:
        print("\nERROR: No source files found. Run the other scripts first.")
        return

    print(f"\nMerging {len(all_frames)} source(s)...")
    master = pd.concat(all_frames, ignore_index=True)

    print("Deduplicating...")
    master = deduplicate(master)

    print("Validating...")
    master = validate(master)

    print("Assigning deal IDs...")
    master = assign_ids(master)

    # Sort by date descending
    master["_sort_date"] = pd.to_datetime(master["date"], format="%Y-%m", errors="coerce")
    master = master.sort_values("_sort_date", ascending=False).drop(columns=["_sort_date"])
    master = master.reset_index(drop=True)

    master.to_csv(OUTPUT_PATH, index=False)
    print(f"✓ Saved: {OUTPUT_PATH}")

    print_summary(master)
    print(f"Your master dataset is ready: {OUTPUT_PATH}")

if __name__ == "__main__":
    merge()
