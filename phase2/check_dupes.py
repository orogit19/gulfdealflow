"""Post-load duplication audit. Reports two views:

1. HARD duplicates — same (company_name, stage, date). The unique constraint
   should make this zero. If it isn't, something bypassed the constraint.
2. SOFT duplicates — same (company_name, country) with 3+ rows. Most of these
   are real (multi-round companies); a long tail of obvious dupes here would
   suggest the loader's date normalisation needs more work.
"""
from __future__ import annotations
import os, sys
from collections import Counter
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(Path(__file__).parent / ".env")
url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
client = create_client(url, key)

rows = client.table("deals").select(
    "company_name, country, stage, date"
).execute().data
print(f"Total rows: {len(rows)}")

hard = Counter((r["company_name"], r["stage"], r["date"]) for r in rows)
hard_dupes = [(k, c) for k, c in hard.items() if c > 1]
print(f"HARD dupes (company, stage, date): {len(hard_dupes)}")
for k, c in hard_dupes[:10]:
    print(f"  {c}x {k}")

soft = Counter((r["company_name"], r.get("country") or "") for r in rows if r["company_name"])
multi = sorted(((k, c) for k, c in soft.items() if c >= 3), key=lambda x: -x[1])
print(f"\nCompanies with 3+ rows in their country (likely multi-round, eyeball for dupes):")
for (name, country), c in multi[:20]:
    print(f"  {c}x {name} ({country})")
