"""Load gcc_ventures_master_v4.csv into the Supabase `deals` table.

Usage:
    python load_data.py             # upsert all rows
    python load_data.py --truncate  # delete existing rows first, then insert

Reads SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, and CSV_PATH from .env.
Uses the service-role key because RLS would otherwise block writes.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from supabase import Client, create_client

TEXT_COLUMNS = [
    "deal_id", "company_name", "country", "city", "date", "stage",
    "sector", "description", "founded_year", "website",
    "lead_investor", "co_investors", "investor_types", "source", "notes",
]
BATCH_SIZE = 100


def clean_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    s = str(value).strip()
    return s or None


def clean_amount(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def clean_bool(value) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isnan(value):
        return None
    s = str(value).strip().lower()
    if s in {"true", "t", "yes", "y", "1"}:
        return True
    if s in {"false", "f", "no", "n", "0"}:
        return False
    return None


def row_to_record(row: pd.Series) -> dict:
    record = {col: clean_text(row.get(col)) for col in TEXT_COLUMNS}
    record["amount_usd"] = clean_amount(row.get("amount_usd"))
    record["disclosed"] = clean_bool(row.get("disclosed"))
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--truncate", action="store_true",
                        help="Delete all rows before inserting.")
    args = parser.parse_args()

    load_dotenv(Path(__file__).parent / ".env")
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    csv_path_raw = os.environ.get("CSV_PATH", "../phase1/gcc_ventures_master_v4.csv")

    if not url or not key:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env",
              file=sys.stderr)
        return 1

    csv_path = (Path(__file__).parent / csv_path_raw).resolve()
    if not csv_path.exists():
        print(f"ERROR: CSV not found at {csv_path}", file=sys.stderr)
        return 1

    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False, na_values=[""])
    records = [row_to_record(row) for _, row in df.iterrows()]
    print(f"Read {len(records)} rows from {csv_path.name}")

    client: Client = create_client(url, key)

    if args.truncate:
        # Postgres-side delete; the filter is a no-op match-all.
        client.table("deals").delete().neq("deal_id", "__never__").execute()
        print("Truncated existing rows.")

    # Upsert keyed on (company_name, stage, date) — the unique constraint
    # added by migration 002. A company can have multiple rounds at the same
    # stage as long as the dates differ; reloading a row that matches the
    # triple updates it in place.
    inserted = 0
    for start in range(0, len(records), BATCH_SIZE):
        batch = records[start:start + BATCH_SIZE]
        client.table("deals").upsert(batch, on_conflict="company_name,stage,date").execute()
        inserted += len(batch)
        print(f"  Upserted {inserted}/{len(records)}")

    count_resp = client.table("deals").select("deal_id", count="exact").execute()
    print(f"Done. Table now has {count_resp.count} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
