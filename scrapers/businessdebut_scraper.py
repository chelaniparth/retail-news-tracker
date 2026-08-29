#!/usr/bin/env python
"""Scrape Business Debut articles and save results to CSV/JSON.

Fetches articles from https://www.businessdebut.com across multiple pages
and writes businessdebut_latest.json for the dashboard.
"""

import json
import os
import time
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )
}

BASE_URL = "https://www.businessdebut.com"
MAX_PAGES = int(os.environ.get("BIZ_DEBUT_MAX_PAGES", "3"))


class DateEncoder(json.JSONEncoder):
    """JSON encoder that converts date/datetime objects to ISO strings."""

    def default(self, obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        return super().default(obj)


def scrape_page(url):
    """Fetch and parse articles from a single page. Returns list of dicts or None."""
    try:
        response = requests.get(url, headers=headers, timeout=10)
    except requests.RequestException as exc:
        print(f"  ✗ Request error: {exc} — stopping.")
        return None

    if response.status_code != 200:
        print(f"  ✗ Status {response.status_code} — stopping.")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    articles = soup.find_all("article", class_="gh-card")

    if not articles:
        print("  ✗ No articles found — likely reached the last page.")
        return None

    results = []
    for article in articles:
        h3 = article.find("h3")
        a_tag = article.find("a", class_="gh-card-link")
        time_tag = article.find("time", class_="gh-card-date")

        title = h3.text.strip() if h3 else None
        link = a_tag["href"] if a_tag else None
        # Make absolute URL if relative
        if link and link.startswith("/"):
            link = BASE_URL + link
        date_str = time_tag["datetime"] if time_tag and time_tag.has_attr("datetime") else None

        results.append({"title": title, "link": link, "date": date_str})

    return results


def main():
    today = datetime.today().date()
    print(f"Scraping up to {MAX_PAGES} pages...\n")

    content = []

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/page/{page}/"
        print(f"Fetching page {page}: {url}")

        articles = scrape_page(url)
        if articles is None:
            break

        content.extend(articles)
        print(f"  ✓ Found {len(articles)} articles (total so far: {len(content)})")
        time.sleep(1)

    # Build DataFrame
    df = pd.DataFrame(content) if content else pd.DataFrame(columns=["title", "link", "date"])

    if not df.empty:
        df["date"] = pd.to_datetime(df["date"], errors="coerce", utc=True)
        df["date"] = df["date"].dt.tz_localize(None)
        df = df.drop_duplicates(subset="link")
        df = df.sort_values("date", ascending=False).reset_index(drop=True)

    print(f"\nDone! {len(df)} articles scraped.")

    # ── Output directories ────────────────────────────────────────────────────
    BIZ_DIR = Path("data/businessdebut")
    BIZ_DIR.mkdir(parents=True, exist_ok=True)

    MASTER_DIR = Path("master_file")
    MASTER_DIR.mkdir(parents=True, exist_ok=True)

    # Save CSV
    export_path = BIZ_DIR / f"businessdebut_articles_{datetime.now().strftime('%Y-%m-%d')}.csv"
    df_csv = df.copy()
    if "date" in df_csv.columns:
        df_csv["date"] = df_csv["date"].astype(str)
    df_csv.to_csv(export_path, index=False)
    print(f"Saved CSV to {export_path}")

    # Prepare JSON-safe data (dates as ISO strings)
    json_df = df.copy()
    if "date" in json_df.columns:
        json_df["date"] = json_df["date"].dt.strftime("%Y-%m-%d").where(json_df["date"].notna(), "")

    json_payload = {
        "last_updated": today.strftime("%Y-%m-%d"),
        "data": json_df.to_dict(orient="records"),
    }

    json_path = "businessdebut_latest.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_payload, f, ensure_ascii=False, indent=2, cls=DateEncoder)
    print(f"Saved JSON to {json_path}")

    # ── Master file — accumulates all daily results ───────────────────────────
    MASTER_FILE = MASTER_DIR / "businessdebut_master.csv"

    df_new = df.copy()
    df_new['Date_Appended'] = today.strftime("%Y-%m-%d")
    df_new['date'] = pd.to_datetime(df_new['date'], errors='coerce')

    if MASTER_FILE.exists():
        df_master = pd.read_csv(MASTER_FILE, encoding='utf-8')
        df_master['date'] = pd.to_datetime(df_master['date'], errors='coerce')
        df_master = pd.concat([df_master, df_new], ignore_index=True)
    else:
        df_master = df_new

    df_master = df_master.drop_duplicates(subset=['link'])
    df_master = df_master.sort_values('date', ascending=False)
    df_master.to_csv(MASTER_FILE, index=False, encoding='utf-8')
    print(f"✓ Master file updated: {MASTER_FILE}  ({len(df_master)} total rows)")


if __name__ == "__main__":
    main()