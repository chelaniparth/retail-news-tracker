#!/usr/bin/env python3
"""
Loads one or more newsbatch_N_output.md files (the markdown extraction
table produced by the manual Claude-paste workflow -- daily_news_prepare.py
writes newsbatch_N.txt prompts, a human pastes each into Claude, Claude
returns a table in this exact format) into this app's dashboard schema:
raw rows into scraped_articles, extracted events into store_events.

Table format expected in each .md file:
    | Store/Shop/Restaurant Name | Location or Full Address with zip code | Event Type | Event Date | Status | Short Description | Article Link | Published Date |
    |---|---|---|---|---|---|---|---|
    | ... | ... | ... | ... | ... | ... | ... | ... |

A trailing "Non-working or unusable articles" section (if present) is
ignored -- only the main table rows are loaded.

Usage:
    python load_markdown_batch.py <source> <file1.md> [file2.md ...]
    python load_markdown_batch.py businessdebut newsbatch_1_output.md newsbatch_2_output.md --dry-run

`source` must be one of the values allowed by the DB's own CHECK
constraint on store_events.source / scraped_articles.source (banner,
businessdebut, ct_scoop, restaurant, daily_news, daily_news_bankruptcy).

Environment:
    SUPABASE_URL, SUPABASE_KEY (service_role)
"""

import os
import re
import sys
from datetime import date
from pathlib import Path

import requests

LOCATION_RE = re.compile(r'^(?P<rest>.*),\s*(?P<state>[A-Za-z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$')

DATE_HINT_RE = re.compile(
    r'\b(january|february|march|april|may|june|july|august|september|october|november|december|'
    r'spring|summer|fall|autumn|winter|q[1-4]|20\d{2})\b',
    re.IGNORECASE,
)

# Same best-effort mapping used for the restaurant pipeline (see
# load_extraction_to_dashboard.py) -- keeps the Status filter a small,
# useful set instead of one label per article. First match wins.
STATUS_RULES = {
    "Opening": [
        (["grand opening"], "grand opening"),
        (["opened", "re-opened", "reopened", "launched", "debuted", "now open"], "opened"),
        (["construction", "build-out", "buildout", "permit", "development stage",
          "completed within", "final stages", "under construction"], "under construction"),
        (["coming soon", " soon", "getting ready", "days from"], "opening soon"),
        (["slated", "targeted", "targeting", "expected to open", "estimated to open",
          "set to open", "should open", "soft open", "due to open", "due to welcome",
          "planned opening", "planned for"], "set to open"),
        (["plan", "propos", "consider", "moving forward", "not yet announced",
          "not been announced", "no opening date"], "planned opening"),
    ],
    "Closing": [
        (["shut down", "shutting down"], "shut down"),
        (["permanently"], "permanently closed"),
        (["closing soon", " soon"], "closing soon"),
        (["closed", "closes"], "closed"),
        (["set to close", "slated to close", "targeted to close", "scheduled to close",
          "expected to shut", "will close", "cease operations"], "set to close"),
        (["closing", "plan"], "planned closing"),
    ],
    "Remodel": [
        (["reopened", "re-opened"], "reopened after remodel"),
        (["plan"], "renovation planned"),
        (["remodel", "new concept", "unveiled"], "remodeling"),
        (["renovat"], "under renovation"),
    ],
}
STATUS_FALLBACK = {"Opening": "planned opening", "Closing": "planned closing", "Remodel": "under renovation"}


def normalize_status(event_type_name: str, status_text: str) -> str:
    text = (status_text or "").lower()
    has_date = bool(DATE_HINT_RE.search(text))
    for keywords, label in STATUS_RULES.get(event_type_name, []):
        if any(k in text for k in keywords):
            return label
    if has_date:
        return {"Opening": "set to open", "Closing": "set to close", "Remodel": "renovation planned"}.get(
            event_type_name, STATUS_FALLBACK.get(event_type_name, "")
        )
    return STATUS_FALLBACK.get(event_type_name, "")


def parse_location(location: str) -> dict:
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
    address_line1, city = (parts if len(parts) == 2 else (None, parts[0]))
    return {"address_line1": address_line1 or None, "city": city or None, "state": state, "zip_code": zip_code}


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
    resp = requests.post(url, headers=sb_headers(key, f"resolution={resolution},return=minimal"), json=rows, timeout=60)
    if not resp.ok:
        raise RuntimeError(f"POST {table} failed [{resp.status_code}]: {resp.text[:500]}")


def split_md_row(line: str) -> list:
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def is_separator_row(cells: list) -> bool:
    return all(re.match(r'^:?-+:?$', c) for c in cells if c)


def parse_markdown_table(text: str) -> list:
    """Returns a list of dicts with keys matching the 8 master-prompt columns."""
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if "|" in line and "Store" in line and ("Restaurant Name" in line or "Shop" in line):
            header_idx = i
            break
    if header_idx is None:
        return []

    headers = [h.lower() for h in split_md_row(lines[header_idx])]
    rows = []
    for line in lines[header_idx + 1:]:
        line = line.strip()
        if not line or "|" not in line:
            if rows:  # blank line after the table body ends the table
                break
            continue
        cells = split_md_row(line)
        if is_separator_row(cells):
            continue
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))
        row = dict(zip(headers, cells))
        rows.append(row)
    return rows


def get_field(row: dict, *keys) -> str:
    for k in keys:
        for hk, v in row.items():
            if k in hk:
                return v.strip()
    return ""


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    if len(args) < 2:
        print("Usage: python load_markdown_batch.py <source> <file1.md> [file2.md ...] [--dry-run]")
        sys.exit(1)
    source, files = args[0], args[1:]

    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Set SUPABASE_URL and SUPABASE_KEY environment variables.")
        sys.exit(1)

    all_rows = []
    for f in files:
        p = Path(f)
        if not p.exists():
            print(f"  Skipping missing file: {f}")
            continue
        parsed = parse_markdown_table(p.read_text(encoding="utf-8"))
        print(f"  {f}: {len(parsed)} row(s)")
        all_rows.extend(parsed)

    if not all_rows:
        print("No rows parsed — nothing to do.")
        return

    event_types = sb_get(SUPABASE_URL, SUPABASE_KEY, "event_types?select=*")
    event_type_ids = {e["name"]: e["event_type_id"] for e in event_types}
    obs = sb_get(SUPABASE_URL, SUPABASE_KEY, "observation_statuses?select=*")
    status_ids = {(o["event_type_id"], o["label"].lower()): o["status_id"] for o in obs}
    existing_events = sb_get(SUPABASE_URL, SUPABASE_KEY, f"store_events?source=eq.{source}&select=article_link,company_name")
    existing_keys = {(e["article_link"], e["company_name"]) for e in existing_events}

    articles_rows, event_rows, companies_seen, skipped = [], [], set(), 0
    articles_seen_links = set()  # scraped_articles is unique per (source, link) --
                                  # a roundup article covering several companies
                                  # (e.g. "Aldi: four new stores") must only produce
                                  # one row here, even though it produces one
                                  # store_events row per company.

    for row in all_rows:
        store_name = get_field(row, "store/shop/restaurant name", "store name")
        location = get_field(row, "location")
        event_type_raw = get_field(row, "event type")
        event_date = get_field(row, "event date")
        status_raw = get_field(row, "status")
        short_desc = get_field(row, "short description")
        article_link = get_field(row, "article link")
        published_date = get_field(row, "published date")

        if not store_name or not article_link or store_name.lower().startswith("no qualifying business"):
            skipped += 1
            continue

        loc = parse_location(location)
        if article_link not in articles_seen_links:
            articles_seen_links.add(article_link)
            articles_rows.append({
                "source": source,
                "link": article_link,
                "title": store_name,
                "published_date": published_date or None,
                "company_name": store_name,
                "summary": short_desc or None,
                "city": loc["city"],
                "state": loc["state"],
            })
        companies_seen.add(store_name)

        key = (article_link, store_name)
        if key in existing_keys:
            continue
        existing_keys.add(key)

        event_type = event_type_raw.strip().capitalize() if event_type_raw.strip().lower() != "n/a" else ""
        event_type_id = event_type_ids.get(event_type)
        status_id = None
        if event_type_id and status_raw and status_raw.strip().upper() != "N/A":
            label = normalize_status(event_type, status_raw)
            status_id = status_ids.get((event_type_id, label.lower()))

        comment = f"{date.today().isoformat()}, According to source - {short_desc}" if short_desc and short_desc.upper() != "N/A" else None

        event_rows.append({
            "source": source,
            "article_link": article_link,
            "published_date": published_date or None,
            "company_name": store_name,
            "store_name": store_name,
            "event_type_id": event_type_id,
            "observation_status_id": status_id,
            "event_date_raw": event_date if event_date and event_date.upper() != "N/A" else None,
            "comment": comment,
            **loc,
        })

    print(f"\nSource: {source}")
    print(f"  Parsed rows (incl. 'no qualifying business'): {len(all_rows)}")
    print(f"  Skipped (no qualifying business / missing link): {skipped}")
    print(f"  Raw articles to upsert into scraped_articles:   {len(articles_rows)}")
    print(f"  New store_events rows to insert:                {len(event_rows)}")
    print(f"  Unique companies:                               {len(companies_seen)}")

    if dry_run:
        print("\n--dry-run: no writes performed. Sample store_events row:")
        if event_rows:
            import json
            print(json.dumps(event_rows[0], indent=2, ensure_ascii=False))
        return

    unique_companies = sorted(companies_seen)
    for i in range(0, len(unique_companies), 500):
        sb_post(SUPABASE_URL, SUPABASE_KEY, "companies",
                [{"company_name": c} for c in unique_companies[i:i + 500]],
                on_conflict="company_name", resolution="ignore-duplicates")

    for i in range(0, len(articles_rows), 500):
        sb_post(SUPABASE_URL, SUPABASE_KEY, "scraped_articles", articles_rows[i:i + 500],
                on_conflict="source,link", resolution="merge-duplicates")

    for i in range(0, len(event_rows), 500):
        sb_post(SUPABASE_URL, SUPABASE_KEY, "store_events", event_rows[i:i + 500])

    print(f"\n✅  Loaded {len(articles_rows)} scraped_articles + {len(event_rows)} store_events rows for source={source}")


if __name__ == "__main__":
    main()
