"""
Merges docs/news_data_opening.json and docs/news_data_closing.json
into docs/news_data.json. Run by the GitHub Actions merge job after
both scraper jobs complete.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DOCS        = "docs"
INPUTS      = ["news_data_opening.json", "news_data_closing.json"]
OUTPUT      = "news_data.json"

articles = []
for fname in INPUTS:
    path = os.path.join(DOCS, fname)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            articles.extend(json.load(f).get("articles", []))
        print(f"  Loaded {path}")
    else:
        print(f"  Warning: {path} not found — skipping")

# Dedup by title (case-insensitive), keep highest relevance_score
seen: dict = {}
for art in articles:
    key = art.get("title", "").lower().strip()
    if key not in seen or art.get("relevance_score", 0) > seen[key].get("relevance_score", 0):
        seen[key] = art

merged = sorted(
    seen.values(),
    key=lambda x: (x.get("relevance_score", 0), x.get("published_date") or ""),
    reverse=True,
)

os.makedirs(DOCS, exist_ok=True)
output_path = os.path.join(DOCS, OUTPUT)
with open(output_path, "w", encoding="utf-8") as f:
    json.dump(
        {"generated": datetime.now(timezone.utc).isoformat(), "articles": merged},
        f, indent=2, ensure_ascii=False,
    )

print(f"Merged {len(merged)} unique articles → {output_path}")

# ── Master file — accumulates all daily results ───────────────────────────────
MASTER_DIR = Path("master_file")
MASTER_DIR.mkdir(parents=True, exist_ok=True)
MASTER_FILE = MASTER_DIR / "daily_news_master.csv"

today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
df_new = pd.DataFrame(merged)
if not df_new.empty:
    df_new["Date_Appended"] = today_str
    df_new["published_date"] = pd.to_datetime(df_new["published_date"], errors="coerce")

    if MASTER_FILE.exists():
        df_master = pd.read_csv(MASTER_FILE, encoding="utf-8")
        df_master["published_date"] = pd.to_datetime(df_master["published_date"], errors="coerce")
        df_master = pd.concat([df_master, df_new], ignore_index=True)
    else:
        df_master = df_new

    df_master = df_master.drop_duplicates(subset=["direct_link", "title"])
    df_master = df_master.sort_values("published_date", ascending=False)
    df_master.to_csv(MASTER_FILE, index=False, encoding="utf-8")
    print(f"Master file updated: {MASTER_FILE}  ({len(df_master)} total rows)")
else:
    print("No articles to append to master file.")
