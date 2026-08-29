#!/usr/bin/env python3
"""
CT Scoop Auto Extractor
Reads ct_scoop_latest.json → fetches each article → calls Groq API (free)
with structured extraction prompt → saves to ct_scoop_extraction_latest.json
and master_file/ct_scoop_master_extraction.csv.

Usage:
    python ct_scoop_auto_extract.py
    python ct_scoop_auto_extract.py --batch-size 5
    python ct_scoop_auto_extract.py --max-articles 10
    python ct_scoop_auto_extract.py --reset

Requirements:
    pip install langchain-groq langchain-core requests beautifulsoup4 pandas
    Set GROQ_API_KEY environment variable (free key at console.groq.com).
"""

import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq

# ── Config ─────────────────────────────────────────────────────────────────────
OUT_PATH    = Path("ct_scoop_extraction_latest.json")
MASTER_DIR  = Path("master_file")
MASTER_FILE = MASTER_DIR / "ct_scoop_master_extraction.csv"
BATCH_SIZE  = 5      # articles per Groq call (CT Scoop articles are short)
MAX_CHARS   = 3000   # characters to extract per article body

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

SYSTEM_PROMPT = """\
You are an expert, precise data extractor specialized in retail and restaurant openings and closures. I will provide multiple news articles (each usually starting with its source URL). For EVERY article, extract the following information strictly and only from the text provided — no assumptions, no external knowledge, no guessing zip codes, no inferring dates or statuses:

🔍 Extract these fields
• Store/Shop/Restaurant Name
• Location or Full Address with zip code (if no zip code is mentioned, write exactly the address given; if no address at all, write "Address not specified")
• Event Type (write exactly "Opening" or "Closing" or "remodel" based only on the article content)
• Event Date
  - For openings → Opening Date
  - For closures → Closing Date (write exact date or month/year if mentioned; otherwise write exactly "Not specified")
• Status
  - For openings → use phrasing like: "under construction", "opening soon", "set to open", "recently opened", "grand opening on…", "planned for", etc.
  - For closures → use phrasing like: "closed", "permanently closed", "closing soon", "set to close", "shut down", "liquidation", etc.
  👉 Use the exact phrasing or closest direct wording from the article — do NOT invent or normalize
• Short Description (exactly 2–3 concise sentences summarizing ONLY what the article says — no opinions, no extra context)

📊 Output format
Create ONE clean Markdown table with these exact column headers (in this order):
| Store/Shop/Restaurant Name | Location or Full Address with zip code | Event Type | Event Date | Status | Short Description | Article Link | Published Date |

🌎 Geographic filter (STRICT)
• Only extract businesses located in the USA or Canada
• If the article is about a business in any other country (UK, Australia, India, UAE, etc.) → DO NOT add any row to the table; add it ONLY to the Non-working list as "Outside USA/Canada"
• If an article covers both USA/Canada locations AND international locations → extract only the USA/Canada rows, skip the rest

📌 Rules
• Add one row per article in the order the articles are given
• If an article contains multiple businesses, create a separate row for each
• If an article includes both openings and closures, extract each separately
• For Published Date → copy exactly the value from the "Published:" line in the article metadata
• If a USA/Canada article has zero relevant business opening or closure information, still include a row with:
  - Store Name: "No qualifying business found"
  - Other columns: "N/A"

🚫 Strict constraints
• ❌ No assumptions  • ❌ No external data  • ❌ No inferred addresses or dates  • ❌ No rewriting or normalizing status text
• ❌ No extraction of businesses outside USA or Canada

📎 Final section (mandatory)
At the very end of your response, add:
Non-working or unusable articles List:
• Article number — Reason (paywall / no business details / duplicate / text missing / Outside USA/Canada / etc.)
If none, write: None

✅ Articles below — extract now:\
"""

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# ── Column → JSON key map ──────────────────────────────────────────────────────
COLUMN_MAP = {
    "store/shop/restaurant name":             "store_name",
    "store/restaurant":                       "store_name",
    "store / restaurant":                     "store_name",
    "location or full address with zip code": "location",
    "location or full address":               "location",
    "location":                               "location",
    "event type":                             "event_type",
    "event date":                             "event_date",
    "status":                                 "status",
    "short description":                      "short_description",
    "article link":                           "article_link",
    "article":                                "article_link",
    "published date":                         "published_date",
    "published":                              "published_date",
}


# ── Parsing helpers ────────────────────────────────────────────────────────────
def clean_cell(text: str) -> str:
    text = re.sub(
        r'\[([^\]]*)\]\(([^)]*)\)',
        lambda m: m.group(2) if m.group(2).startswith("http") else m.group(1),
        text,
    )
    return re.sub(r'\*+', '', text).strip()


def is_separator(line: str) -> bool:
    return bool(re.match(r'^\s*\|?\s*[-:]+\s*(\|\s*[-:]+\s*)+\|?\s*$', line))


def parse_table(text: str) -> list[dict]:
    rows, headers, keys = [], [], []
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if in_table:
                in_table = False
            continue
        if '|' not in line or is_separator(line):
            continue
        cells = [clean_cell(c) for c in line.strip().strip('|').split('|')]
        if not headers:
            headers = cells
            for h in headers:
                keys.append(COLUMN_MAP.get(h.lower(), h.lower().replace(' ', '_').replace('/', '_')))
            in_table = True
            continue
        if not in_table:
            continue
        if len(cells) < len(keys):
            cells += [''] * (len(keys) - len(cells))
        row = {keys[i]: cells[i] for i in range(len(keys))}
        et = row.get('event_type', '').strip().lower()
        row['event_type'] = {
            'opening': 'Opening', 'closing': 'Closing', 'remodel': 'Remodel'
        }.get(et, row.get('event_type', ''))
        if all(v in ('', '—', '-', 'N/A') for v in row.values()):
            continue
        rows.append(row)
    return rows


def parse_non_working(text: str) -> list[dict]:
    out = []
    m = re.search(r'Non[- ]?working[^\n]*\n(.*?)(?:\Z)', text, re.IGNORECASE | re.DOTALL)
    if not m:
        return out
    for line in m.group(1).splitlines():
        line = line.strip().lstrip('•-*').strip()
        if not line or line.lower() == 'none':
            continue
        hit = re.match(r'^(https?://\S+|Article\s*\d+|\d+)[^\w]*[:\-–—]?\s*(.*)', line, re.IGNORECASE)
        if hit:
            out.append({"identifier": hit.group(1).strip(), "reason": hit.group(2).strip()})
        else:
            out.append({"identifier": line, "reason": ""})
    return out


# ── Article fetcher ────────────────────────────────────────────────────────────
def fetch_article(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        body = (
            soup.find("article")
            or soup.find("main")
            or soup.find("div", class_=lambda c: c and "content" in c.lower())
            or soup
        )
        return body.get_text(separator=" ", strip=True)[:MAX_CHARS]
    except Exception as exc:
        return f"[Could not fetch article: {exc}]"


# ── Persistence ────────────────────────────────────────────────────────────────
def load_existing() -> tuple[list, list]:
    if OUT_PATH.exists():
        try:
            p = json.loads(OUT_PATH.read_text(encoding="utf-8"))
            return p.get("data", []), p.get("non_working", [])
        except Exception:
            pass
    return [], []


def save_json(data: list, non_working: list) -> None:
    OUT_PATH.write_text(
        json.dumps(
            {"last_updated": date.today().isoformat(), "data": data, "non_working": non_working},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )


def save_master_csv(data: list) -> None:
    if not data:
        print("  No rows to save to master CSV.")
        return
    MASTER_DIR.mkdir(parents=True, exist_ok=True)
    df_new = pd.DataFrame(data)
    df_new["Date_Appended"] = date.today().isoformat()

    if MASTER_FILE.exists():
        df_master = pd.read_csv(MASTER_FILE, encoding="utf-8", dtype=str)
        df_master = pd.concat([df_master, df_new], ignore_index=True)
    else:
        df_master = df_new

    if "article_link" in df_master.columns:
        df_master = df_master.drop_duplicates(subset=["article_link"])

    df_master.to_csv(MASTER_FILE, index=False, encoding="utf-8")
    print(f"✓ Master CSV updated: {MASTER_FILE}  ({len(df_master)} total rows)")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    batch_size   = int(args[args.index('--batch-size')   + 1]) if '--batch-size'   in args else BATCH_SIZE
    max_articles = int(args[args.index('--max-articles') + 1]) if '--max-articles' in args else None
    reset        = '--reset' in args

    if not GROQ_API_KEY:
        print("❌  GROQ_API_KEY environment variable is not set.")
        print("    Set it with:  set GROQ_API_KEY=gsk_...")
        sys.exit(1)

    if reset:
        save_json([], [])
        print(f"✓  Reset {OUT_PATH}")

    scoop_path = Path("ct_scoop_latest.json")
    if not scoop_path.exists():
        print("❌  ct_scoop_latest.json not found.")
        sys.exit(1)

    raw = json.loads(scoop_path.read_text(encoding="utf-8"))
    articles = raw.get("data", []) if isinstance(raw, dict) else raw
    if max_articles:
        articles = articles[:max_articles]

    if not articles:
        print("No articles in ct_scoop_latest.json — nothing to extract.")
        return

    total_batches = (len(articles) + batch_size - 1) // batch_size
    print(f"✓  {len(articles)} article(s)  |  batch size: {batch_size}  |  {total_batches} batch(es)\n")

    llm = ChatGroq(
        model="llama-3.3-70b-versatile",
        api_key=GROQ_API_KEY,
        temperature=0,
        max_tokens=8192,
    )
    prompt_template = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", "{articles_text}"),
    ])
    chain = prompt_template | llm | StrOutputParser()

    all_rows, all_nw = load_existing()

    # Skip articles already present in the JSON output
    already_done = {r.get("article_link", "") for r in all_rows}

    for b_start in range(0, len(articles), batch_size):
        batch = articles[b_start: b_start + batch_size]
        b_num = b_start // batch_size + 1
        b_end = b_start + len(batch)
        print(f"━━ Batch {b_num}/{total_batches}  (articles {b_start + 1}–{b_end}) ━━")

        blocks = []
        for i, art in enumerate(batch, b_start + 1):
            url     = art.get("link", "")
            heading = art.get("heading", "")
            pub     = art.get("date", "")

            if url in already_done:
                print(f"  [{i:>3}] SKIP (already extracted): {url[:80]}")
                continue

            print(f"  [{i:>3}] {url[:80]}")
            body = fetch_article(url)
            blocks.append(
                f"--- Article {i} ---\n"
                f"URL: {url}\n"
                f"Headline: {heading}\n"
                f"Published: {pub}\n\n"
                f"{body}"
            )
            time.sleep(0.5)

        if not blocks:
            print("  All articles in this batch already extracted — skipping.\n")
            continue

        print(f"\n  → Calling Groq API (llama-3.3-70b-versatile) for {len(blocks)} article(s)...")
        try:
            response_text = chain.invoke({"articles_text": "\n\n".join(blocks)})
        except Exception as exc:
            print(f"  ❌  Groq API error: {exc}")
            print("  Skipping this batch.")
            continue

        batch_rows = parse_table(response_text)
        batch_nw   = parse_non_working(response_text)
        all_rows.extend(batch_rows)
        all_nw.extend(batch_nw)
        save_json(all_rows, all_nw)

        print(f"  ✓ {len(batch_rows)} row(s) extracted  (running total: {len(all_rows)})")
        if batch_nw:
            print(f"    Non-working: {len(batch_nw)}")
        print()

        if b_end < len(articles):
            time.sleep(1)  # brief pause between batches to respect rate limits

    save_master_csv(all_rows)
    print(f"\n✅  Complete!  {len(all_rows)} row(s) → {OUT_PATH}")


if __name__ == "__main__":
    main()
