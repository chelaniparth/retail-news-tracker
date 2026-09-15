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

Environment (either works — REST is tried first):
    SUPABASE_URL, SUPABASE_KEY (service_role)
    or SUPABASE_DB_HOST/PORT/USER/PASSWORD/NAME (direct Postgres connection,
    same vars the backend itself uses)
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
    # Labels match the 6 observation_statuses rows seed_distress_demo() already
    # created for the Bankruptcy event_type -- the extraction prompt steers the
    # model toward this exact wording, but free text still needs mapping onto
    # the DB's fixed status set the same way Opening/Closing/Remodel do.
    "Bankruptcy": [
        (["chapter 7"], "chapter 7 filed"),
        (["chapter 11"], "chapter 11 filed"),
        (["emerged from bankruptcy", "emerged from chapter", "exited bankruptcy"], "emerged from bankruptcy"),
        (["asset sale", "selling its", "sell its", "sale of its"], "asset sale sought"),
        (["liquidat"], "liquidating"),
        (["restructur"], "restructuring"),
    ],
}
STATUS_FALLBACK = {
    "Opening": "planned opening", "Closing": "planned closing", "Remodel": "under renovation",
    "Bankruptcy": "chapter 11 filed",
}


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


def db_connect():
    import psycopg2
    return psycopg2.connect(
        host=os.environ["SUPABASE_DB_HOST"],
        port=os.environ.get("SUPABASE_DB_PORT", "6543"),
        user=os.environ["SUPABASE_DB_USER"],
        password=os.environ["SUPABASE_DB_PASSWORD"],
        dbname=os.environ.get("SUPABASE_DB_NAME", "postgres"),
    )


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
    use_rest = bool(SUPABASE_URL and SUPABASE_KEY)
    use_db = bool(os.environ.get("SUPABASE_DB_HOST") and os.environ.get("SUPABASE_DB_PASSWORD"))
    if not use_rest and not use_db:
        print("Set SUPABASE_URL + SUPABASE_KEY, or SUPABASE_DB_HOST/PORT/USER/PASSWORD/NAME.")
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

    conn = db_connect() if use_db else None
    if use_db:
        cur = conn.cursor()
        cur.execute("SELECT event_type_id, name FROM event_types")
        event_type_ids = {name: eid for eid, name in cur.fetchall()}
        cur.execute("SELECT event_type_id, label, status_id FROM observation_statuses")
        status_ids = {(eid, label.lower()): sid for eid, label, sid in cur.fetchall()}
        cur.execute("SELECT article_link, company_name FROM store_events WHERE source = %s", (source,))
        existing_keys = {(link, name) for link, name in cur.fetchall()}
        cur.execute("SELECT company_name FROM companies")
        canonical_name = {}
        for (name,) in cur.fetchall():
            canonical_name.setdefault(name.lower(), name)
        cur.close()
    else:
        event_types = sb_get(SUPABASE_URL, SUPABASE_KEY, "event_types?select=*")
        event_type_ids = {e["name"]: e["event_type_id"] for e in event_types}
        obs = sb_get(SUPABASE_URL, SUPABASE_KEY, "observation_statuses?select=*")
        status_ids = {(o["event_type_id"], o["label"].lower()): o["status_id"] for o in obs}
        existing_events = sb_get(SUPABASE_URL, SUPABASE_KEY, f"store_events?source=eq.{source}&select=article_link,company_name")
        existing_keys = {(e["article_link"], e["company_name"]) for e in existing_events}
        canonical_name = {}
        for c in sb_get(SUPABASE_URL, SUPABASE_KEY, "companies?select=company_name"):
            canonical_name.setdefault(c["company_name"].lower(), c["company_name"])

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

        # companies.company_name is unique case-insensitively; store_events/
        # scraped_articles FK to it by exact string. Two rows differing only
        # by case (a real occurrence in scraped data) must resolve to the
        # exact same casing or the companies insert dedupes to one variant
        # while these rows still reference another, breaking the FK.
        store_name = canonical_name.setdefault(store_name.lower(), store_name)

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
        if conn:
            conn.close()
        return

    unique_companies = sorted(companies_seen)

    if use_db:
        from psycopg2.extras import execute_values
        cur = conn.cursor()
        if unique_companies:
            execute_values(cur, "INSERT INTO companies (company_name) VALUES %s ON CONFLICT ((lower(company_name))) DO NOTHING",
                            [(c,) for c in unique_companies])
        if articles_rows:
            cols = ["source", "link", "title", "published_date", "company_name", "summary", "city", "state"]
            execute_values(
                cur,
                f"INSERT INTO scraped_articles ({', '.join(cols)}) VALUES %s "
                f"ON CONFLICT (source, link) DO UPDATE SET "
                + ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in ("source", "link")),
                [tuple(r[c] for c in cols) for r in articles_rows],
            )
        if event_rows:
            cols = ["source", "article_link", "published_date", "company_name", "store_name",
                    "event_type_id", "observation_status_id", "event_date_raw", "comment",
                    "address_line1", "city", "state", "zip_code"]
            execute_values(
                cur,
                f"INSERT INTO store_events ({', '.join(cols)}) VALUES %s",
                [tuple(r.get(c) for c in cols) for r in event_rows],
            )
        conn.commit()
        cur.close()
        conn.close()
    else:
        for i in range(0, len(unique_companies), 500):
            sb_post(SUPABASE_URL, SUPABASE_KEY, "companies",
                    [{"company_name": c} for c in unique_companies[i:i + 500]],
                    on_conflict="company_name", resolution="ignore-duplicates")

        for i in range(0, len(articles_rows), 500):
            sb_post(SUPABASE_URL, SUPABASE_KEY, "scraped_articles", articles_rows[i:i + 500],
                    on_conflict="source,link", resolution="merge-duplicates")

        for i in range(0, len(event_rows), 500):
            sb_post(SUPABASE_URL, SUPABASE_KEY, "store_events", event_rows[i:i + 500])

    print(f"\nLoaded {len(articles_rows)} scraped_articles + {len(event_rows)} store_events rows for source={source}")


if __name__ == "__main__":
    main()
