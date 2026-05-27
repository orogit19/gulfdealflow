"""One-shot data cleanup for the deals table. Uses the service-role key
because the updates are blocked by RLS for anon.

Re-runnable: every fix is idempotent (sets target values regardless of
current state). Reports rows actually changed per fix.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(Path(__file__).parent / ".env")

c = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
sys.stdout.reconfigure(encoding="utf-8")


def apply(label: str, filter_kv: tuple[str, str], updates: dict) -> None:
    """Apply `updates` to rows matching filter_kv. Logs before/after."""
    field, value = filter_kv
    before = c.table("deals").select("deal_id, company_name, sector").eq(field, value).execute().data
    if not before:
        print(f"  SKIP {label}: no rows match {field}={value!r}")
        return
    c.table("deals").update(updates).eq(field, value).execute()
    after = c.table("deals").select("deal_id, company_name, sector").eq(
        list(updates.keys())[0], list(updates.values())[0]
    ).in_("deal_id", [r["deal_id"] for r in before]).execute().data
    for b in before:
        post = next((a for a in after if a["deal_id"] == b["deal_id"]), None)
        print(f"  {label:38s} {b['deal_id']}  "
              f"{b['company_name']!r:15s} {b['sector']!r:30s}  ->  "
              f"{(post or b)['company_name']!r:15s} {(post or b)['sector']!r}")


print("=== Applying fixes ===\n")

# Trukker (GCC0094) — same company as TruKKer; normalize name + sector.
apply("Trukker -> TruKKer (Logistics)",
      ("deal_id", "GCC0094"),
      {"company_name": "TruKKer", "sector": "Logistics & Supply Chain"})

# Pluto (GCC0097) — mis-tagged AI; the brand is fintech per GCC0086.
apply("Pluto AI row -> Fintech",
      ("deal_id", "GCC0097"),
      {"sector": "Fintech"})

# Single-row recategorizations.
for company, new_sector in [
    ("Thndr",          "Fintech"),
    ("Pure Harvest",   "Food & Agritech"),
    ("IO Kitchens",    "Food & Agritech"),
    ("SellAnyCar.com", "E-commerce & Retail"),
    ("Seez",           "E-commerce & Retail"),
    ("MealPlanet",     "Food & Agritech"),
]:
    apply(f"{company} -> {new_sector}",
          ("company_name", company),
          {"sector": new_sector})

print("\n=== Done ===")
