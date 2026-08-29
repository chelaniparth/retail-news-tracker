#!/usr/bin/env python3
"""
sync_to_supabase.py
───────────────────
Reads all master CSVs from master_file/ and upserts each into the
corresponding Supabase table. Safe to re-run — duplicates are skipped.

Usage:
    python sync_to_supabase.py

Required environment variables:
    SUPABASE_URL   — https://xxxx.supabase.co
    SUPABASE_KEY   — service_role key (from Supabase → Settings → API)
"""

import math
import os
import sys
from pathlib import Path

import pandas as pd
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌  Set SUPABASE_URL and SUPABASE_KEY environment variables.")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

MASTER_DIR = Path("master_file")
CHUNK_SIZE = 500  # rows per upsert batch

# CSV filename → (Supabase table name, unique conflict column for upsert)
TABLE_CONFIG = {
    "businessdebut_master.csv": ("businessdebut_master", "link"),
    "restaurant_master.csv":    ("restaurant_master",    "url"),
    "ct_scoop_master.csv":               ("ct_scoop_master",            "link"),
    "ct_scoop_master_extraction.csv":    ("ct_scoop_master_extraction",  "article_link"),
    "daily_news_master.csv":    ("daily_news_master",    "direct_link"),
    "banner_news_master.csv":   ("banner_news_master",   "Link"),
}


def clean_records(df: pd.DataFrame) -> list[dict]:
    """Convert DataFrame to JSON-safe list of dicts (NaN/NaT → None)."""
    records = df.where(pd.notnull(df), None).to_dict(orient="records")
    return [
        {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in row.items()}
        for row in records
    ]


def sync_table(csv_name: str, table: str, conflict_col: str) -> None:
    csv_path = MASTER_DIR / csv_name
    if not csv_path.exists():
        print(f"  ⚠️  {csv_name} not found — skipping.")
        return

    df = pd.read_csv(csv_path, encoding="utf-8", dtype=str).fillna("")
    records = clean_records(df)

    # Deduplicate by conflict column so Postgres never sees two rows with the
    # same key in one batch (causes "ON CONFLICT DO UPDATE ... same row twice").
    seen_keys: set = set()
    deduped = []
    for rec in records:
        key = rec.get(conflict_col, "")
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(rec)
    if len(deduped) < len(records):
        print(f"    ℹ️  Dropped {len(records) - len(deduped)} duplicate '{conflict_col}' values before upsert")
    records = deduped
    total = len(records)

    for i in range(0, total, CHUNK_SIZE):
        chunk = records[i: i + CHUNK_SIZE]
        try:
            supabase.table(table).upsert(chunk, on_conflict=conflict_col).execute()
        except Exception as exc:
            msg = str(exc)
            if "PGRST205" in msg or "schema cache" in msg:
                print(f"  ⚠️  Table '{table}' not found in Supabase — skipping.")
                print(f"       Create the table first, then re-run to sync.")
                return
            raise

    print(f"  ✅  {table}: {total} rows upserted")


print("🔄  Syncing master_file/ → Supabase\n")
for csv_name, (table, conflict_col) in TABLE_CONFIG.items():
    print(f"  → {csv_name}")
    sync_table(csv_name, table, conflict_col)
print("\n✅  Sync complete!")
