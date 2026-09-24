#!/usr/bin/env python3
"""
Loads warn.py's output (warn_latest.json) into scraped_articles, source
'warn' -- WARN Act notices are official state filings with no article
text to extract, so unlike every other source here there's no LLM step:
the fields are already exactly what the DB needs.

WARN notices have no article URL of their own (they're filings, not news
articles) -- scraped_articles.link is left NULL for them, same as the
existing demo rows in seed_distress_demo(). Since Postgres never treats
two NULLs as equal, the (source, link) unique constraint can't dedupe
these, so this script does its own dedup by (company_name, city, state,
notice_date) against what's already in the table before inserting.

Usage:
    python load_warn_to_dashboard.py warn_latest.json
    python load_warn_to_dashboard.py warn_latest.json --days 120
    python load_warn_to_dashboard.py warn_latest.json --dry-run

--days limits which notices get loaded, by notice_date, to keep this from
dumping years of historical WARN filings into the dashboard in one shot
(the scraped file already had ~5,800 records full history) -- default 120
days keeps the tab focused on filings still relevant to today's tracking.

Environment (either works):
    SUPABASE_URL, SUPABASE_KEY (service_role)
    or SUPABASE_DB_HOST/PORT/USER/PASSWORD/NAME
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

SOURCE = "warn"


def parse_date(s: str):
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def sb_headers(key: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def sb_get(base: str, key: str, path: str) -> list:
    """Paginates via PostgREST's Range header -- a single unpaginated request
    silently truncates at PostgREST's configured max-rows (commonly 1000),
    which companies/store_events are both past by now."""
    all_rows, page_size, offset = [], 1000, 0
    while True:
        headers = sb_headers(key)
        headers["Range-Unit"] = "items"
        headers["Range"] = f"{offset}-{offset + page_size - 1}"
        resp = requests.get(f"{base}/rest/v1/{path}", headers=headers, timeout=30)
        resp.raise_for_status()
        page = resp.json()
        all_rows.extend(page)
        if len(page) < page_size:
            return all_rows
        offset += page_size


def sb_post(base: str, key: str, table: str, rows: list) -> None:
    if not rows:
        return
    resp = requests.post(
        f"{base}/rest/v1/{table}",
        headers={**sb_headers(key), "Prefer": "return=minimal"},
        json=rows, timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(f"POST {table} failed [{resp.status_code}]: {resp.text[:500]}")


def db_connect():
    import psycopg2
    return psycopg2.connect(
        host=os.environ["SUPABASE_DB_HOST"], port=os.environ.get("SUPABASE_DB_PORT", "6543"),
        user=os.environ["SUPABASE_DB_USER"], password=os.environ["SUPABASE_DB_PASSWORD"],
        dbname=os.environ.get("SUPABASE_DB_NAME", "postgres"),
    )


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    days = 120
    if "--days" in args:
        i = args.index("--days")
        days = int(args[i + 1])
        args = args[:i] + args[i + 2:]
    if not args:
        print("Usage: python load_warn_to_dashboard.py <warn_latest.json> [--days 120] [--dry-run]")
        sys.exit(1)

    p = Path(args[0])
    if not p.exists():
        print(f"❌  {p} not found.")
        sys.exit(1)

    raw = json.loads(p.read_text(encoding="utf-8"))
    records = raw.get("data", raw) if isinstance(raw, dict) else raw

    cutoff = date.today() - timedelta(days=days)
    records = [r for r in records if (parse_date(r.get("notice_date")) or date.min) >= cutoff]
    print(f"  {len(records)} record(s) within the last {days} days")

    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
    use_rest = bool(SUPABASE_URL and SUPABASE_KEY)
    use_db = bool(os.environ.get("SUPABASE_DB_HOST") and os.environ.get("SUPABASE_DB_PASSWORD"))
    if not use_rest and not use_db:
        print("Set SUPABASE_URL + SUPABASE_KEY, or SUPABASE_DB_HOST/PORT/USER/PASSWORD/NAME.")
        sys.exit(1)

    conn = db_connect() if use_db else None
    if use_db:
        cur = conn.cursor()
        cur.execute(
            "SELECT company_name, city, state, extra_data->>'notice_date' FROM scraped_articles WHERE source = %s",
            (SOURCE,),
        )
        existing_keys = {tuple(row) for row in cur.fetchall()}
        cur.close()
    else:
        existing = sb_get(SUPABASE_URL, SUPABASE_KEY, f"scraped_articles?source=eq.{SOURCE}&select=company_name,city,state,extra_data")
        existing_keys = {
            (e.get("company_name"), e.get("city"), e.get("state"), (e.get("extra_data") or {}).get("notice_date"))
            for e in existing
        }

    # companies.company_name is unique case-insensitively, and scraped_articles
    # FKs to it by exact string -- two WARN records that only differ by case
    # (real data has these, e.g. "SMBC Manubank" vs "SMBC MANUBANK") must all
    # resolve to the exact same casing, or the companies insert dedupes to one
    # variant while a row here still references the other, breaking the FK.
    # canonical_name remembers the first-seen casing per lowercase key.
    canonical_name: dict = {}
    if use_db:
        cur = conn.cursor()
        cur.execute("SELECT company_name FROM companies")
        for (name,) in cur.fetchall():
            canonical_name.setdefault(name.lower(), name)
        cur.close()
    else:
        for c in sb_get(SUPABASE_URL, SUPABASE_KEY, "companies?select=company_name"):
            canonical_name.setdefault(c["company_name"].lower(), c["company_name"])

    companies_seen, new_rows = set(), []
    for r in records:
        company = (r.get("company") or "").strip()
        if not company:
            continue
        company = canonical_name.setdefault(company.lower(), company)
        key = (company, r.get("city"), r.get("state"), r.get("notice_date"))
        if key in existing_keys:
            continue
        existing_keys.add(key)
        companies_seen.add(company)
        new_rows.append({
            "source": SOURCE,
            "company_name": company,
            "city": r.get("city"),
            "state": r.get("state"),
            "published_date": r.get("notice_date"),
            "extra_data": {
                "notice_date": r.get("notice_date"),
                "layoff_date": r.get("layoff_date"),
                "employees_affected": r.get("employees_affected"),
                "closure_type": r.get("closure_type"),
                "notes": r.get("notes") or None,
            },
        })

    print(f"  {len(new_rows)} new row(s) to insert, {len(companies_seen)} unique companies")
    if dry_run:
        if new_rows:
            print(json.dumps(new_rows[0], indent=2, ensure_ascii=False))
        print("\n--dry-run: no writes performed.")
        return

    if not new_rows:
        print("Nothing new to load.")
        if conn:
            conn.close()
        return

    if use_db:
        from psycopg2.extras import execute_values, Json
        cur = conn.cursor()
        # companies also has a case-insensitive unique index (lower(company_name))
        # separate from the exact-match one -- ON CONFLICT (company_name) alone
        # doesn't satisfy that index, so two names differing only by case (real
        # WARN data has these) raise a raw UniqueViolation instead of no-op-ing.
        # Targeting the same expression the index uses covers both.
        unique_companies = sorted(companies_seen)
        if unique_companies:
            execute_values(cur, "INSERT INTO companies (company_name) VALUES %s ON CONFLICT ((lower(company_name))) DO NOTHING",
                            [(c,) for c in unique_companies])
        cols = ["source", "company_name", "city", "state", "published_date", "extra_data"]
        execute_values(
            cur,
            f"INSERT INTO scraped_articles ({', '.join(cols)}) VALUES %s",
            [(r["source"], r["company_name"], r["city"], r["state"], r["published_date"], Json(r["extra_data"]))
             for r in new_rows],
        )
        conn.commit()
        cur.close()
        conn.close()
    else:
        unique_companies = sorted(companies_seen)
        for i in range(0, len(unique_companies), 500):
            sb_post(SUPABASE_URL, SUPABASE_KEY, "companies",
                    [{"company_name": c} for c in unique_companies[i:i + 500]])
        for i in range(0, len(new_rows), 500):
            sb_post(SUPABASE_URL, SUPABASE_KEY, "scraped_articles", new_rows[i:i + 500])

    print(f"\nLoaded {len(new_rows)} scraped_articles rows for source={SOURCE}")


if __name__ == "__main__":
    main()
