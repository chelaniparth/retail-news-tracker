#!/usr/bin/env python3
"""
Generic Groq Auto Extractor — Banner, BusinessDebut, Daily News, CT Scoop,
and Bankruptcy all share this one script instead of each getting its own
copy (ct_scoop_auto_extract.py's separate single-key/CSV-based flow is
retired in favor of this; Restaurant keeps restaurant_auto_extract.py,
which already works). Reads whatever raw-articles JSON a source's scraper
already produces, batches the articles through Groq using the same
master-prompt table format the manual Claude-paste workflow has always
used, then feeds the combined Markdown straight into load_markdown_batch.py
— reusing its already-proven parsing/normalization/DB-write logic instead
of duplicating it a fourth time.

Usage:
    python groq_extract_and_load.py <source> <input.json>
    python groq_extract_and_load.py <source> <input.json> --dry-run
    python groq_extract_and_load.py banner latest_news.json
    python groq_extract_and_load.py businessdebut businessdebut_latest.json
    python groq_extract_and_load.py daily_news docs/news_data.json
    python groq_extract_and_load.py ct_scoop ct_scoop_latest.json
    python groq_extract_and_load.py daily_news_bankruptcy bankruptcy_latest.json --prompt bankruptcy

<source> must be one of: banner, businessdebut, daily_news, ct_scoop, daily_news_bankruptcy
<input.json> is tolerant of each scraper's own field names (url/link/
direct_link, title/heading, published_date/published/date, ...) — see normalize_article().

Environment:
    GROQ_API_KEYS, GROQ_API_KEYS_2 — comma-separated Groq keys, rotated per
                                       batch and on rate-limit errors (same
                                       pool restaurant_auto_extract.py uses)
    SUPABASE_URL, SUPABASE_KEY — or SUPABASE_DB_HOST/PORT/USER/PASSWORD/NAME
                                       — passed straight through to
                                       load_markdown_batch.py for the load step
"""

import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq

VALID_SOURCES = {"banner", "businessdebut", "daily_news", "daily_news_bankruptcy", "ct_scoop"}

BATCH_SIZE = 5
MAX_CHARS = 3000

# Identical to restaurant_auto_extract.py / ct_scoop_auto_extract.py's prompt
# — same 8-column table load_markdown_batch.py already knows how to parse.
GENERAL_PROMPT = """\
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

📌 Rules
• Add one row per article in the order the articles are given
• If an article contains multiple businesses, create a separate row for each
• If an article includes both openings and closures, extract each separately
• For Published Date → copy exactly the value from the "Published:" line in the article metadata
• If an article has zero relevant business opening or closure information, still include a row with:
  - Store Name: "No qualifying business found"
  - Other columns: "N/A"

🚫 Strict constraints
• ❌ No assumptions  • ❌ No external data  • ❌ No inferred addresses or dates  • ❌ No rewriting or normalizing status text

📎 Final section (mandatory)
At the very end of your response, add:
Non-working or unusable articles List:
• Article number — Reason (paywall / no business details / duplicate / text missing / etc.)
If none, write: None

✅ Articles below — extract now:\
"""

# Event Type is fixed to "Bankruptcy" (not asked to classify Opening/Closing/
# Remodel) and Status is steered toward the 6 labels seed_distress_demo()
# already created for the Bankruptcy event_type in observation_statuses —
# normalize_status()'s new "Bankruptcy" rules in load_markdown_batch.py map
# whatever wording actually comes back onto those same 6 labels.
BANKRUPTCY_PROMPT = """\
You are an expert, precise data extractor specialized in retail and restaurant business bankruptcies. I will provide multiple news articles (each usually starting with its source URL). For EVERY article, extract the following information strictly and only from the text provided — no assumptions, no external knowledge, no guessing zip codes, no inferring dates or statuses:

🔍 Extract these fields
• Store/Shop/Restaurant Name (the company or chain that filed for bankruptcy)
• Location or Full Address with zip code (if no zip code is mentioned, write exactly the address given; if no address at all, write "Address not specified")
• Event Type (always write exactly "Bankruptcy")
• Event Date (the date of the bankruptcy filing, court action, or closure; write exact date or month/year if mentioned; otherwise write exactly "Not specified")
• Status (write the closest match to one of: "Chapter 11 filed", "Chapter 7 filed", "Restructuring", "Asset sale sought", "Liquidating", "Emerged from bankruptcy" — based only on what the article actually says; if none of these fit, use the closest direct wording from the article instead of inventing one)
• Short Description (exactly 2–3 concise sentences summarizing ONLY what the article says — no opinions, no extra context)

📊 Output format
Create ONE clean Markdown table with these exact column headers (in this order):
| Store/Shop/Restaurant Name | Location or Full Address with zip code | Event Type | Event Date | Status | Short Description | Article Link | Published Date |

📌 Rules
• Add one row per article in the order the articles are given
• If an article contains multiple companies, create a separate row for each
• For Published Date → copy exactly the value from the "Published:" line in the article metadata
• If an article has zero relevant bankruptcy information, still include a row with:
  - Store Name: "No qualifying business found"
  - Other columns: "N/A"

🚫 Strict constraints
• ❌ No assumptions  • ❌ No external data  • ❌ No inferred addresses or dates  • ❌ No rewriting status text beyond matching it to the closest listed option

📎 Final section (mandatory)
At the very end of your response, add:
Non-working or unusable articles List:
• Article number — Reason (paywall / no business details / duplicate / text missing / etc.)
If none, write: None

✅ Articles below — extract now:\
"""

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def _parse_keys(env_var: str) -> list:
    return [k.strip() for k in os.environ.get(env_var, "").split(",") if k.strip()]


GROQ_KEYS = _parse_keys("GROQ_API_KEYS") + _parse_keys("GROQ_API_KEYS_2") + _parse_keys("GROQ_API_KEYS_3")
if not GROQ_KEYS and os.environ.get("GROQ_API_KEY"):
    GROQ_KEYS = [os.environ["GROQ_API_KEY"]]


def is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "rate_limit" in msg or "rate limit" in msg


def make_chain(api_key: str, system_prompt: str):
    llm = ChatGroq(
        model="openai/gpt-oss-120b",
        api_key=api_key,
        temperature=0,
        max_tokens=2048,
    )
    prompt_template = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "{articles_text}"),
    ])
    return prompt_template | llm | StrOutputParser()


def _extract_body_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    body = (
        soup.find("article")
        or soup.find("main")
        or soup.find("div", class_=lambda c: c and "content" in c.lower())
        or soup
    )
    return body.get_text(separator=" ", strip=True)[:MAX_CHARS]


def _fetch_article_plain(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return _extract_body_text(resp.text)
    except Exception as exc:
        return f"[Could not fetch article: {exc}]"


# Lazily created, reused across every article in the run, and only ever
# started at all if a plain fetch turns out too thin to need it -- most
# sources never touch this. None = not yet tried; False = tried and
# unavailable (no selenium installed / no Chrome on this runner), so later
# calls skip straight past it instead of retrying a doomed import each time.
_selenium_driver = None


def _get_selenium_driver():
    global _selenium_driver
    if _selenium_driver is not None:
        return _selenium_driver
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument(f"user-agent={HEADERS['User-Agent']}")
        chrome_bin = os.environ.get("CHROME_BIN")
        if chrome_bin:
            options.binary_location = chrome_bin
        driver_path = os.environ.get("CHROMEDRIVER_PATH")
        service = Service(driver_path) if driver_path else Service()
        _selenium_driver = webdriver.Chrome(service=service, options=options)
        print("  (Selenium/Chrome available -- will use it for JS-rendered article pages)")
    except Exception as exc:
        print(f"  (Selenium unavailable, staying on plain-HTTP fetch only: {exc})")
        _selenium_driver = False
    return _selenium_driver


def _fetch_article_selenium(url: str) -> str:
    driver = _get_selenium_driver()
    if not driver:
        return ""
    try:
        driver.get(url)
        time.sleep(3)  # let client-side JS render the article body
        rendered = _extract_body_text(driver.page_source)
        print(f"    (selenium render: {len(rendered)} chars)")
        return rendered
    except Exception as exc:
        print(f"    (selenium fetch failed: {exc})")
        return ""


def fetch_article(url: str) -> str:
    text = _fetch_article_plain(url)
    # A JS-rendered page (CT Scoop's site does exactly this) returns a full
    # HTML page -- just nothing but nav/menu chrome, since the real article
    # body only appears after client-side JS runs. Confirmed directly
    # against a live CT Scoop URL: that nav-only junk text is ~538 chars on
    # its own, so a 200-char threshold never actually caught it and the
    # Selenium fallback silently never fired. 900 sits comfortably above
    # that junk-text length while still being well under what a real
    # article body runs (articles here are typically well over 1000 chars,
    # capped at MAX_CHARS=3000).
    if len(text) < 900 and not text.startswith("[Could not fetch"):
        rendered = _fetch_article_selenium(url)
        if len(rendered) > len(text):
            return rendered
    return text


def close_selenium_driver():
    global _selenium_driver
    if _selenium_driver:
        try:
            _selenium_driver.quit()
        except Exception:
            pass
    _selenium_driver = None


# ── Field normalization — every scraper spells these slightly differently ──
URL_KEYS = ["direct_link", "link", "url", "article_link"]
TITLE_KEYS = ["title", "heading"]
DATE_KEYS = ["published_date", "published", "date"]


def normalize_article(raw: dict) -> dict:
    lower = {k.lower(): v for k, v in raw.items() if v is not None}

    def pick(keys):
        for k in keys:
            v = lower.get(k)
            if v:
                return str(v)
        return ""

    return {"url": pick(URL_KEYS), "title": pick(TITLE_KEYS), "published": pick(DATE_KEYS)}


def load_articles(input_path: Path) -> list:
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        articles = raw.get("data") or raw.get("articles") or []
    else:
        articles = raw
    # Banner's latest_news.json nests articles as {analyst_name: [article, ...]}
    # instead of a flat list -- flatten every analyst's articles together.
    if isinstance(articles, dict):
        flat = []
        for items in articles.values():
            if isinstance(items, list):
                flat.extend(items)
        articles = flat
    out = []
    for a in articles:
        norm = normalize_article(a)
        if norm["url"]:
            out.append(norm)
    return out


# ── Already-processed state (avoid re-spending Groq quota on the same URLs
#    every time the daily cron re-scrapes an overlapping article window) ──
def state_path(source: str) -> Path:
    return Path(f"{source}_groq_state.json")


def load_processed(source: str) -> set:
    p = state_path(source)
    if p.exists():
        try:
            return set(json.loads(p.read_text(encoding="utf-8")).get("processed_urls", []))
        except Exception:
            pass
    return set()


def save_processed(source: str, processed: set) -> None:
    state_path(source).write_text(
        json.dumps({"last_updated": date.today().isoformat(), "processed_urls": sorted(processed)},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    batch_size = int(args[args.index("--batch-size") + 1]) if "--batch-size" in args else BATCH_SIZE
    if "--batch-size" in args:
        i = args.index("--batch-size")
        args = args[:i] + args[i + 2:]
    max_articles = int(args[args.index("--max-articles") + 1]) if "--max-articles" in args else None
    if "--max-articles" in args:
        i = args.index("--max-articles")
        args = args[:i] + args[i + 2:]
    prompt_name = "general"
    if "--prompt" in args:
        i = args.index("--prompt")
        prompt_name = args[i + 1]
        args = args[:i] + args[i + 2:]

    if len(args) < 2:
        print("Usage: python groq_extract_and_load.py <source> <input.json> [--dry-run] [--batch-size N] [--max-articles N] [--prompt bankruptcy]")
        sys.exit(1)
    source, input_path = args[0], Path(args[1])

    if source not in VALID_SOURCES:
        print(f"source must be one of {sorted(VALID_SOURCES)}")
        sys.exit(1)
    if not GROQ_KEYS:
        print("Set GROQ_API_KEYS (and optionally GROQ_API_KEYS_2), or GROQ_API_KEY.")
        sys.exit(1)
    if not input_path.exists():
        print(f"{input_path} not found.")
        sys.exit(1)

    system_prompt = BANKRUPTCY_PROMPT if prompt_name == "bankruptcy" else GENERAL_PROMPT

    articles = load_articles(input_path)
    if max_articles:
        articles = articles[:max_articles]

    processed = load_processed(source)
    articles = [a for a in articles if a["url"] not in processed]

    if not articles:
        print("No new articles to extract — nothing to do.")
        return

    total_batches = (len(articles) + batch_size - 1) // batch_size
    print(f"{len(articles)} new article(s)  |  batch size: {batch_size}  |  {total_batches} batch(es)  |  {len(GROQ_KEYS)} Groq key(s)  |  prompt: {prompt_name}\n")

    md_parts = []
    newly_processed = set()

    for b_start in range(0, len(articles), batch_size):
        batch = articles[b_start: b_start + batch_size]
        b_num = b_start // batch_size + 1
        b_end = b_start + len(batch)
        print(f"-- Batch {b_num}/{total_batches}  (articles {b_start + 1}-{b_end}) --")

        blocks = []
        for i, art in enumerate(batch, b_start + 1):
            print(f"  [{i:>3}] {art['url'][:80]}")
            body = fetch_article(art["url"])
            block = f"--- Article {i} ---\nURL: {art['url']}\nTitle: {art['title']}\nPublished: {art['published']}\n\n{body}"
            blocks.append(block)
            newly_processed.add(art["url"])
            time.sleep(0.5)

        articles_text = "\n\n".join(blocks)

        response_text = None
        for attempt in range(len(GROQ_KEYS)):
            key = GROQ_KEYS[(b_num - 1 + attempt) % len(GROQ_KEYS)]
            print(f"  -> Calling Groq API (openai/gpt-oss-120b, key #{(b_num - 1 + attempt) % len(GROQ_KEYS) + 1}) for {len(blocks)} article(s)...")
            try:
                response_text = make_chain(key, system_prompt).invoke({"articles_text": articles_text})
                break
            except Exception as exc:
                if is_rate_limit_error(exc) and attempt < len(GROQ_KEYS) - 1:
                    print("  WARNING: Rate limited on this key — trying next key...")
                    continue
                print(f"  Groq API error: {exc}")
                break

        if response_text is None:
            print("  Skipping this batch.\n")
            continue

        md_parts.append(response_text)
        print()
        if b_end < len(articles):
            time.sleep(1)

    if not md_parts:
        print("No batches succeeded — nothing to load.")
        return

    out_md = Path(f"{source}_extraction_latest.md")
    out_md.write_text("\n\n".join(md_parts), encoding="utf-8")
    print(f"Wrote combined extraction -> {out_md}")

    if dry_run:
        print("\n--dry-run: skipping load_markdown_batch.py.")
        return

    result = subprocess.run(
        [sys.executable, "load_markdown_batch.py", source, str(out_md)],
        cwd=Path(__file__).parent,
    )
    if result.returncode != 0:
        print("load_markdown_batch.py failed — not marking articles as processed, will retry next run.")
        sys.exit(1)

    save_processed(source, processed | newly_processed)
    print(f"\nDone. {len(newly_processed)} article(s) marked processed for next run.")


if __name__ == "__main__":
    try:
        main()
    finally:
        close_selenium_driver()
