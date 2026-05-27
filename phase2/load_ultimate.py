"""Load ultimate_gcc_funding_2020_2024_combined.csv into the Supabase `deals`
table.

The source CSV has a different schema and far messier values than the original
loader handles (`load_data.py`): amounts mix forms like "3M" / "3 million" /
"3,000,000" / "Undisclosed", dates range from "Sep 2024" to "2024 (round
referenced in 2025 post)", and stages drift in capitalisation and verbosity.

This script normalises those fields before upserting on
(company_name, stage, date) — the same uniqueness key as the original loader —
so semantic duplicates (same deal reported by two sources with different date
strings) collapse instead of bloating the table.

Usage:
    python load_ultimate.py [--dry-run]

Reads SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY from .env (same as load_data.py).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections import Counter
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from supabase import Client, create_client

CSV_PATH = Path(__file__).parent.parent / "ultimate_gcc_funding_2020_2024_combined.csv"
BATCH_SIZE = 100

# Source column → target column.
COLUMN_MAP = {
    "Company":            "company_name",
    "Country":            "country",
    "Stage":              "stage",
    "Sub-sector":         "sector",
    "Lead Investor":      "lead_investor",
    "Co-investors":       "co_investors",
    "Website":            "website",
    "Included From":      "source",
}

# Free-form placeholders meaning "no value" — treated as null throughout.
SENTINELS = {
    "", "not stated", "not captured", "not disclosed", "undisclosed",
    "unknown", "n/a", "na", "tbd", "-", "—",
    "not fully specified", "not fully specified in snippet",
    "not fully visible in source", "not fully visible in snippet",
    "not fully visible in 2024 snippet", "not fully listed in snippet",
    "not stated in accessible snippet",
}

MONTHS = {
    "jan": "01", "january":   "01",
    "feb": "02", "february":  "02",
    "mar": "03", "march":     "03",
    "apr": "04", "april":     "04",
    "may": "05",
    "jun": "06", "june":      "06",
    "jul": "07", "july":      "07",
    "aug": "08", "august":    "08",
    "sep": "09", "sept": "09", "september": "09",
    "oct": "10", "october":   "10",
    "nov": "11", "november":  "11",
    "dec": "12", "december":  "12",
}

# Common stage value canonicalisation. Anything not matched here keeps its
# cleaned (title-cased, single-space) form so we don't accidentally collide
# distinct stages — only the obvious case/dash variants are merged.
STAGE_CANON = {
    "pre-seed":        "Pre-Seed",
    "preseed":         "Pre-Seed",
    "pre seed":        "Pre-Seed",
    "seed":            "Seed",
    "series a":        "Series A",
    "series b":        "Series B",
    "series c":        "Series C+",
    "series c+":       "Series C+",
    "series d":        "Series C+",
    "series e":        "Series C+",
    "pre-series a":    "Pre-Series A",
    "pre series a":    "Pre-Series A",
    "bridge":          "Bridge",
    "growth":          "Growth",
}


def is_sentinel(s: str | None) -> bool:
    if s is None:
        return True
    return s.strip().lower() in SENTINELS


def clean_text(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    if is_sentinel(s):
        return None
    return s


def parse_amount(raw) -> int | None:
    """Best-effort USD parse. Returns None for "Undisclosed", text-only
    amounts ("Seven-figure seed"), or anything we can't confidently read."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip()
    if is_sentinel(s):
        return None
    low = s.lower()

    # "X million" / "X.Y million" / "Xm"
    m = re.search(r"([\d,.]+)\s*(million|m)\b", low)
    if m:
        try:
            return int(float(m.group(1).replace(",", "")) * 1_000_000)
        except ValueError:
            pass

    # "Xk" / "X thousand"
    m = re.search(r"([\d,.]+)\s*(thousand|k)\b", low)
    if m:
        try:
            return int(float(m.group(1).replace(",", "")) * 1_000)
        except ValueError:
            pass

    # "Xb" / "X billion"
    m = re.search(r"([\d,.]+)\s*(billion|b)\b", low)
    if m:
        try:
            return int(float(m.group(1).replace(",", "")) * 1_000_000_000)
        except ValueError:
            pass

    # Plain digit groups: "3,000,000", "200,000", "150000000". Strip any
    # trailing "+" before parsing ("2,000,000+").
    m = re.search(r"([\d,]+)\+?", s)
    if m:
        digits = m.group(1).replace(",", "")
        if digits.isdigit() and len(digits) >= 4:
            try:
                return int(digits)
            except ValueError:
                pass

    return None


def parse_date(raw) -> str | None:
    """Normalise to YYYY-MM. Falls back to YYYY-only if no month is given.
    Returns None when the source has nothing date-like (e.g. "2021-2023
    coverage exists in open reports"). The first 4-digit year wins to avoid
    ranges like "Dec 2023 / Jan 2024 publication" producing 2024 — we want
    the earlier reported month."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip()
    if is_sentinel(s):
        return None

    # "DD Mon YYYY" or "Mon YYYY"
    m = re.search(
        r"(?:(\d{1,2})\s+)?([A-Za-z]{3,9})\s+(\d{4})",
        s,
    )
    if m:
        mon = MONTHS.get(m.group(2).lower()[:3])
        if mon:
            return f"{m.group(3)}-{mon}"

    # "YYYY-MM" already
    m = re.match(r"(\d{4})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"

    # Bare year somewhere in the string.
    m = re.search(r"\b(20\d{2}|19\d{2})\b", s)
    if m:
        return m.group(1)

    return None


def canon_stage(raw) -> str | None:
    cleaned = clean_text(raw)
    if cleaned is None:
        return None
    return STAGE_CANON.get(cleaned.lower(), cleaned)


def make_deal_id(company: str, stage: str | None, date: str | None) -> str:
    # Deterministic, short, stable across re-runs. Same key → same id, so
    # repeated loads update in place instead of inserting fresh rows.
    raw = f"{company}|{stage or ''}|{date or ''}"
    return "ult-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:14]


def row_to_record(row: pd.Series) -> dict | None:
    company = clean_text(row.get("Company"))
    if not company:
        return None  # skip rows with no company name — nothing to key on

    stage  = canon_stage(row.get("Stage"))
    date   = parse_date(row.get("Date"))
    amount = parse_amount(row.get("Funding Amount USD"))

    record = {
        "deal_id":       make_deal_id(company, stage, date),
        "company_name":  company,
        "country":       clean_text(row.get("Country")),
        "stage":         stage,
        "date":          date,
        "amount_usd":    amount,
        "disclosed":     amount is not None,
        "sector":        clean_text(row.get("Sub-sector")),
        "lead_investor": clean_text(row.get("Lead Investor")),
        "co_investors":  clean_text(row.get("Co-investors")),
        "website":       clean_text(row.get("Website")),
        "source":        clean_text(row.get("Included From")),
    }
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and report stats without writing to Supabase.")
    args = parser.parse_args()

    if not CSV_PATH.exists():
        print(f"ERROR: CSV not found at {CSV_PATH}", file=sys.stderr)
        return 1

    df = pd.read_csv(CSV_PATH, dtype=str, keep_default_na=False, na_values=[""])
    print(f"Read {len(df)} rows from {CSV_PATH.name}")

    records: list[dict] = []
    skipped = 0
    for _, row in df.iterrows():
        rec = row_to_record(row)
        if rec is None:
            skipped += 1
            continue
        records.append(rec)

    # In-CSV duplicate report: how many rows share the upsert key?
    keys = Counter((r["company_name"], r["stage"], r["date"]) for r in records)
    intra_dupes = {k: c for k, c in keys.items() if c > 1}
    print(f"\nParsed {len(records)} rows; skipped {skipped} (no company name).")
    print(f"Unique (company, stage, date) keys: {len(keys)}")
    if intra_dupes:
        print(f"In-CSV collapse: {sum(intra_dupes.values()) - len(intra_dupes)} "
              f"rows merge into {len(intra_dupes)} keys")
        for (c, s, d), n in sorted(intra_dupes.items(), key=lambda x: -x[1])[:10]:
            print(f"  {n}× {c!r:30s} stage={s!r:20s} date={d!r}")

    # Stats on parse quality
    parsed_amounts = sum(1 for r in records if r["amount_usd"])
    parsed_dates   = sum(1 for r in records if r["date"])
    parsed_stages  = sum(1 for r in records if r["stage"])
    print(f"\nAmount parsed: {parsed_amounts}/{len(records)}")
    print(f"Date parsed:   {parsed_dates}/{len(records)}")
    print(f"Stage parsed:  {parsed_stages}/{len(records)}")

    if args.dry_run:
        print("\n--dry-run set; not writing.")
        return 0

    load_dotenv(Path(__file__).parent / ".env")
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env",
              file=sys.stderr)
        return 1
    client: Client = create_client(url, key)

    before = client.table("deals").select("deal_id", count="exact").execute().count
    print(f"\nRows in deals before upload: {before}")

    # Collapse intra-CSV duplicates ourselves before sending — otherwise the
    # upsert batch will conflict with itself.
    by_key: dict[tuple, dict] = {}
    for r in records:
        key = (r["company_name"], r["stage"], r["date"])
        # Later occurrence wins, but only if it has more info on the amount;
        # otherwise keep the first (mirrors typical "richer source last" intent).
        prev = by_key.get(key)
        if prev is None or (prev.get("amount_usd") is None and r["amount_usd"] is not None):
            by_key[key] = r
    deduped = list(by_key.values())
    print(f"After in-CSV dedup: {len(deduped)} records to upsert")

    inserted = 0
    for start in range(0, len(deduped), BATCH_SIZE):
        batch = deduped[start:start + BATCH_SIZE]
        client.table("deals").upsert(
            batch, on_conflict="company_name,stage,date"
        ).execute()
        inserted += len(batch)
        print(f"  Upserted {inserted}/{len(deduped)}")

    after = client.table("deals").select("deal_id", count="exact").execute().count
    print(f"\nRows in deals after upload:  {after}  (delta +{after - before})")

    # Post-load duplication check: groups sharing (company_name, stage, date)
    # — should be zero because of the unique constraint, but verify.
    all_rows = client.table("deals").select(
        "company_name, stage, date"
    ).execute().data
    db_keys = Counter((r["company_name"], r["stage"], r["date"]) for r in all_rows)
    db_dupes = [k for k, c in db_keys.items() if c > 1]
    print(f"DB duplicate (company,stage,date) groups: {len(db_dupes)}")

    # Soft duplicate report: same (company_name, country) with multiple rows
    # — expected for real multi-round companies, but worth eyeballing.
    soft = Counter((r["company_name"], r.get("country") or "")
                   for r in all_rows if r["company_name"])
    multi_round = sorted(((k, c) for k, c in soft.items() if c >= 3),
                        key=lambda x: -x[1])[:15]
    if multi_round:
        print("\nTop multi-round companies (≥3 rows — expected for active companies):")
        for (name, country), n in multi_round:
            print(f"  {n}× {name} ({country})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
