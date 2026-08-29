#!/usr/bin/env python3
"""
Loads a source's scraped + extracted master CSVs into this app's actual
dashboard schema (companies / event_types / observation_statuses /
scraped_articles / store_events) via the Supabase REST API, so results show
up in frontend/index.html's per-source Articles + Extraction tabs.

This is a separate destination from sync_to_supabase.py's flat
<source>_master / <source>_master_extraction tables — those are written by
the scraper workflows for archival/audit, but the dashboard reads from the
relational schema this script populates instead.

Usage:
    python load_extraction_to_dashboard.py restaurant --dry-run
    python load_extraction_to_dashboard.py restaurant

Environment:
    SUPABASE_URL, SUPABASE_KEY (service_role)
"""

import os
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import requests

MASTER_DIR = Path("master_file")

SOURCE_CONFIG = {
    "restaurant": {
        "raw_csv": "restaurant_master.csv",           # date,title,address,url,Date_Appended
        "extraction_csv": "restaurant_master_extraction.csv",
        "raw_link_col": "url",
        "raw_title_col": "title",
        "raw_published_col": "date",
        "raw_address_col": "address",
    },
}


def sb_headers(key: str, prefer: str = None) -> dict:
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    return h


def sb_get(base: str, key: str, path: str) -> list:
    resp = requests.get(f"{base}/rest/v1/{path}", headers=sb_headers(key), timeout=30)
    resp.raise_for_status()
    return resp.json()


def sb_post(base: str, key: str, table: str, rows: list, on_conflict: str = None, resolution: str = "merge-duplicates") -> None:
    if not rows:
        return
    url = f"{base}/rest/v1/{table}"
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
    prefer = f"resolution={resolution},return=minimal"
    resp = requests.post(url, headers=sb_headers(key, prefer), json=rows, timeout=60)
    if not resp.ok:
        raise RuntimeError(f"POST {table} failed [{resp.status_code}]: {resp.text[:500]}")


LOCATION_RE = re.compile(r'^(?P<rest>.*),\s*(?P<state>[A-Za-z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$')

DATE_HINT_RE = re.compile(
    r'\b(january|february|march|april|may|june|july|august|september|october|november|december|'
    r'spring|summer|fall|autumn|winter|q[1-4]|20\d{2})\b',
    re.IGNORECASE,
)

# Best-effort mapping of free-form extracted status text onto this dashboard's
# existing curated observation_statuses labels (kept intentionally small and
# per-event-type, since the Status column is an Excel-style checkbox filter —
# dozens of near-duplicate free-text labels would make it useless). The exact
# original wording from the article is never lost: it's always preserved
# verbatim in store_events.comment (the Description column), regardless of
# which bucket the status lands in here. Order matters — first match wins.
STATUS_RULES = {
    "Opening": [
        (["grand opening"], "grand opening"),
        (["opened", "re-opened", "reopened", "launched", "debuted"], "opened"),  # checked before "soon"/date guards below
        (["construction", "build-out", "buildout", "permit", "development stage",
          "completed within", "final stages"], "under construction"),
        (["coming soon", " soon", "getting ready", "days from"], "opening soon"),
        (["slated", "targeted", "targeting", "expected to open", "estimated to open",
          "set to open", "should open", "soft open"], "set to open"),
        (["plan", "propos", "consider", "moving forward", "not yet announced",
          "not been announced", "no opening date"], "planned opening"),
    ],
    "Closing": [
        (["shut down"], "shut down"),
        (["permanently"], "permanently closed"),
        (["closing soon", " soon", "end of the month", "end of this month", "this month"], "closing soon"),
        (["closed"], "closed"),
        (["set to close", "slated to close", "targeted to close"], "set to close"),
        (["closing", "plan"], "planned closing"),
    ],
    "Remodel": [
        (["reopened", "re-opened"], "reopened after remodel"),
        (["plan"], "renovation planned"),
        (["remodel"], "remodeling"),
        (["renovat"], "under renovation"),
    ],
}

STATUS_FALLBACK = {
    "Opening": "planned opening",
    "Closing": "planned closing",
    "Remodel": "under renovation",
}


def normalize_status(event_type_name: str, status_text: str) -> str:
    text = (status_text or "").lower()
    has_date = bool(DATE_HINT_RE.search(text))
    for keywords, label in STATUS_RULES.get(event_type_name, []):
        if any(k in text for k in keywords):
            return label
    if has_date:
        # A concrete month/season/year with no other keyword match still reads
        # as a firm-ish target rather than a vague one.
        return {"Opening": "set to open", "Closing": "set to close", "Remodel": "renovation planned"}.get(
            event_type_name, STATUS_FALLBACK.get(event_type_name, "")
        )
    return STATUS_FALLBACK.get(event_type_name, "")


def parse_location(location: str) -> dict:
    """Best-effort split of 'Street, City, ST ZIP' into address_line1/city/state/zip_code.
    Never guesses values that aren't in the string — if the trailing 'ST ZIP'
    pattern isn't found, everything goes into address_line1 and city/state/zip
    stay null rather than being mangled."""
    location = (location or "").strip()
    if not location or location.lower() in ("address not specified", "n/a", "not specified"):
        return {"address_line1": None, "city": None, "state": None, "zip_code": None}

    m = LOCATION_RE.match(location)
    if not m:
        return {"address_line1": location, "city": None, "state": None, "zip_code": None}

    rest = m.group("rest").strip()
    state = m.group("state").upper()
    zip_code = m.group("zip")
    parts = [p.strip() for p in rest.rsplit(",", 1)]
    if len(parts) == 2:
        address_line1, city = parts
    else:
        address_line1, city = None, parts[0]
    return {"address_line1": address_line1 or None, "city": city or None, "state": state, "zip_code": zip_code}


def build_scraped_articles(source: str, raw_df: pd.DataFrame, cfg: dict) -> list:
    rows = []
    for _, r in raw_df.iterrows():
        rows.append({
            "source": source,
            "link": (r.get(cfg["raw_link_col"]) or "").strip() or None,
            "title": (r.get(cfg["raw_title_col"]) or "").strip() or None,
            "published_date": (r.get(cfg["raw_published_col"]) or "").strip() or None,
            "address": (r.get(cfg["raw_address_col"]) or "").strip() or None,
        })
    return [r for r in rows if r["link"]]


def build_store_events(source: str, ext_df: pd.DataFrame, existing_keys: set,
                        event_type_ids: dict, status_ids: dict) -> list:
    rows = []

    for _, r in ext_df.iterrows():
        store_name = (r.get("store_name") or "").strip()
        article_link = (r.get("article_link") or "").strip()
        event_type = (r.get("event_type") or "").strip()
        status = (r.get("status") or "").strip()
        if not store_name or not article_link:
            continue

        key = (article_link, store_name)
        if key in existing_keys:
            continue

        event_type_id = event_type_ids.get(event_type)
        status_id = None
        if event_type_id and status:
            label = normalize_status(event_type, status)
            status_id = status_ids.get((event_type_id, label.lower()))

        loc = parse_location(r.get("location", ""))
        short_desc = (r.get("short_description") or "").strip()
        # store_events.comment has a CHECK constraint requiring the literal
        # phrase "According to source" (see master_events_schema_v3's
        # migration SQL) — matches this app's existing citation convention.
        comment = f"{date.today().isoformat()}, According to source - {short_desc}" if short_desc else None
        rows.append({
            "source": source,
            "article_link": article_link,
            "published_date": (r.get("published_date") or "").strip() or None,
            "company_name": store_name,
            "store_name": store_name,
            "event_type_id": event_type_id,
            "observation_status_id": status_id,
            "event_date_raw": (r.get("event_date") or "").strip() or None,
            "comment": comment,
            **loc,
        })
        existing_keys.add(key)  # avoid inserting the same (link, store) twice within this batch

    return rows


def main():
    args = sys.argv[1:]
    if not args or args[0] not in SOURCE_CONFIG:
        print(f"Usage: python load_extraction_to_dashboard.py <{'|'.join(SOURCE_CONFIG)}> [--dry-run]")
        sys.exit(1)
    source = args[0]
    dry_run = "--dry-run" in args
    cfg = SOURCE_CONFIG[source]

    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Set SUPABASE_URL and SUPABASE_KEY environment variables.")
        sys.exit(1)

    raw_path = MASTER_DIR / cfg["raw_csv"]
    ext_path = MASTER_DIR / cfg["extraction_csv"]
    if not raw_path.exists() or not ext_path.exists():
        print(f"Missing {raw_path} or {ext_path}")
        sys.exit(1)

    raw_df = pd.read_csv(raw_path, dtype=str).fillna("")
    ext_df = pd.read_csv(ext_path, dtype=str).fillna("")

    event_types = sb_get(SUPABASE_URL, SUPABASE_KEY, "event_types?select=*")
    event_type_ids = {e["name"]: e["event_type_id"] for e in event_types}

    obs = sb_get(SUPABASE_URL, SUPABASE_KEY, "observation_statuses?select=*")
    status_ids = {(o["event_type_id"], o["label"].lower()): o["status_id"] for o in obs}

    existing_events = sb_get(SUPABASE_URL, SUPABASE_KEY, f"store_events?source=eq.{source}&select=article_link,company_name")
    existing_keys = {(e["article_link"], e["company_name"]) for e in existing_events}

    articles_rows = build_scraped_articles(source, raw_df, cfg)
    event_rows = build_store_events(source, ext_df, existing_keys, event_type_ids, status_ids)

    unique_companies = sorted({r["company_name"] for r in event_rows if r["company_name"]})
    unmapped_status = sum(1 for r in event_rows if r["event_type_id"] and not r["observation_status_id"])

    print(f"Source: {source}")
    print(f"  Raw articles to upsert into scraped_articles: {len(articles_rows)}")
    print(f"  New store_events rows to insert:               {len(event_rows)}  (of {len(ext_df)} extracted rows; rest already loaded)")
    print(f"  New companies needed:                          {len(unique_companies)}")
    if unmapped_status:
        print(f"  ⚠️  {unmapped_status} row(s) had a status that didn't normalize cleanly (observation_status_id will be null)")

    if dry_run:
        print("\n--dry-run: no writes performed. Sample store_events row:")
        if event_rows:
            import json
            print(json.dumps(event_rows[0], indent=2, ensure_ascii=False))
        return

    # 1) companies
    sb_post(SUPABASE_URL, SUPABASE_KEY, "companies",
            [{"company_name": c} for c in unique_companies],
            on_conflict="company_name", resolution="ignore-duplicates")

    # 2) raw articles (upsert on source+link so re-runs just refresh them)
    sb_post(SUPABASE_URL, SUPABASE_KEY, "scraped_articles", articles_rows,
            on_conflict="source,link", resolution="merge-duplicates")

    # 3) new store_events rows (no unique constraint — dedup already done in-app above)
    sb_post(SUPABASE_URL, SUPABASE_KEY, "store_events", event_rows)

    print(f"\n✅  Loaded {len(articles_rows)} scraped_articles + {len(event_rows)} store_events rows for source={source}")


if __name__ == "__main__":
    main()
