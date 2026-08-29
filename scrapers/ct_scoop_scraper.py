#!/usr/bin/env python
"""Scrape Connecticut Scoop articles and save results to Excel/JSON.

This script is derived from `cs.ipynb`.
"""

from selenium import webdriver
from selenium.common.exceptions import SessionNotCreatedException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
import pandas as pd
from datetime import datetime, date, timedelta
import json
from selenium.webdriver.chrome.options import Options
from pathlib import Path
import os
import shutil

try:
    from webdriver_manager.chrome import ChromeDriverManager
    _HAS_WDM = True
except ImportError:
    _HAS_WDM = False


class DateEncoder(json.JSONEncoder):
    """JSON encoder that converts date/datetime objects to ISO strings."""

    def default(self, obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        return super().default(obj)


def _find_chrome_binary() -> str | None:
    """Return a usable Chrome/Chromium binary path for this environment."""
    # Respect env vars used by some CI systems.
    for env_var in ("CHROME_BIN", "GOOGLE_CHROME_BIN"):
        path = os.environ.get(env_var)
        if path and os.path.exists(path):
            return path

    # Try common executable names first, then absolute paths.
    candidates = [
        "google-chrome",
        "google-chrome-stable",
        "chromium-browser",
        "chromium",
        "/snap/bin/chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]

    for candidate in candidates:
        if os.path.isabs(candidate):
            if os.path.exists(candidate):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found

    return None


def _build_options(headless_new: bool = True) -> Options:
    options = Options()
    options.add_argument("--headless=new" if headless_new else "--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--disable-features=VizDisplayCompositor")
    options.add_argument("--window-size=1920,1080")
    # Suppress the "Chrome is being controlled by automated software" banner
    # and reduce headless fingerprint detectability.
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    )
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    return options


def _make_driver() -> webdriver.Chrome:
    chrome_path = _find_chrome_binary()
    if chrome_path:
        print(f"Using Chrome/Chromium binary: {chrome_path}")
    else:
        print("Warning: No Chrome/Chromium binary found; relying on system defaults.")

    # Use webdriver-manager to download the exact matching ChromeDriver version,
    # bypassing any mismatched chromedriver already present in PATH.
    if _HAS_WDM:
        print("Using webdriver-manager to resolve ChromeDriver.")
        driver_path = ChromeDriverManager().install()
        service = Service(executable_path=driver_path)
    else:
        print("webdriver-manager not available; falling back to Selenium Manager.")
        service = Service()

    options = _build_options(headless_new=True)
    if chrome_path:
        options.binary_location = chrome_path

    def _patch_driver(d: webdriver.Chrome) -> webdriver.Chrome:
        """Remove the navigator.webdriver property that bot-detectors look for."""
        d.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
        )
        return d

    try:
        driver = webdriver.Chrome(service=service, options=options)
        return _patch_driver(driver)
    except SessionNotCreatedException as exc:
        print("First attempt failed; retrying with legacy --headless flag...")
        print(str(exc))
        options = _build_options(headless_new=False)
        if chrome_path:
            options.binary_location = chrome_path
        driver = webdriver.Chrome(service=service, options=options)
        return _patch_driver(driver)


def main():
    # All links in a list
    links = [
        "https://www.theconnecticutscoop.com/statewide-news.html",
        "https://www.theconnecticutscoop.com/tolland-county.html",
        "https://www.theconnecticutscoop.com/windham-county.html",
        "https://www.theconnecticutscoop.com/hartford-county.html",
        "https://www.theconnecticutscoop.com/new-haven-county.html",
        "https://www.theconnecticutscoop.com/litchfield-county.html",
        "https://www.theconnecticutscoop.com/fairfield-county.html",
        "https://www.theconnecticutscoop.com/new-london-county.html",
        "https://www.theconnecticutscoop.com/middlesex-county.html",
        "https://www.theconnecticutscoop.com/massachusetts.html",
    ]

    all_links = []

    # ---- Date filter (configurable via env var CT_SCOOP_DAYS) ----
    today = datetime.today().date()
    lookback_days = int(os.environ.get("CT_SCOOP_DAYS", "2"))
    two_days_ago = today - timedelta(days=lookback_days)
    print(f"Filtering articles from {two_days_ago.isoformat()} onwards (last {lookback_days} days)")

    # Create driver ONCE
    driver = _make_driver()
    wait = WebDriverWait(driver, 30)

    try:
        for url in links:
            print(f"Scraping: {url}")
            driver.get(url)

            try:
                wait.until(
                    EC.presence_of_all_elements_located(
                        (By.CSS_SELECTOR, "div.waddons-blog-card")
                    )
                )
            except Exception as te:
                print(f"[WARN] Timeout waiting for blog cards on {url}: {te}")
                src = driver.page_source or ""
                print(f"[DEBUG] page_source snippet (first 2000 chars):\n{src[:2000]}")
                print(f"[DEBUG] current URL after get: {driver.current_url}")
                print("[INFO] Skipping this page and continuing.")
                continue

            cards = driver.find_elements(By.CSS_SELECTOR, "div.waddons-blog-card")
            print(f"Found {len(cards)} card(s) on page: {url}")

            # Dump innerHTML of the first card once per page so we can see the real structure
            if cards:
                print(f"[DEBUG] First card innerHTML:\n{cards[0].get_attribute('innerHTML')[:1500]}")

            import re

            # Date selectors to try in order — expand as we learn the real structure
            DATE_SELECTORS = [
                "div.waddons-blog-meta",
                "span.waddons-blog-meta",
                "div.waddons-blog-date",
                "span.waddons-blog-date",
                "time",
                "div.waddons-blog-footer",
                "span.waddons-blog-footer",
            ]

            for card in cards:

                # ---- Heading ----
                heading_elements = card.find_elements(By.CSS_SELECTOR, "div.waddons-blog-header")
                heading = heading_elements[0].text.strip() if heading_elements else ""

                # ---- Date — try multiple selectors, then fall back to full card text ----
                article_date = None
                meta_text = None
                date_text = None

                for sel in DATE_SELECTORS:
                    els = card.find_elements(By.CSS_SELECTOR, sel)
                    if els:
                        meta_text = els[0].text.strip()
                        break

                # If none of the targeted selectors found a date, scan the whole card text
                if not meta_text:
                    meta_text = card.text.strip()

                if meta_text:
                    match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", meta_text)
                    if match:
                        date_text = match.group(1)
                        try:
                            article_date = datetime.strptime(date_text, "%m/%d/%Y").date()
                        except Exception:
                            article_date = None
                    else:
                        # Also try "Month DD, YYYY" format (e.g. "May 9, 2026")
                        match2 = re.search(
                            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}",
                            meta_text, re.IGNORECASE
                        )
                        if match2:
                            date_text = match2.group(0)
                            for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
                                try:
                                    article_date = datetime.strptime(date_text, fmt).date()
                                    break
                                except Exception:
                                    continue

                # ---- Link ----
                link_elements = card.find_elements(By.CSS_SELECTOR, "a.waddons-blog-card-link-full")
                link = link_elements[0].get_attribute("href") if link_elements else ""

                # DEBUG: show what we parsed for the card (helps in CI logs)
                print("Card debug:", {
                    "heading": heading,
                    "meta_text": meta_text,
                    "date_text": date_text,
                    "article_date": article_date,
                    "link": link,
                })

                # ---- Filter last 2 days ----
                if link and article_date and article_date >= two_days_ago:
                    print("Added:", heading, article_date)

                    all_links.append({
                        "heading": heading,
                        "date": article_date,
                        "link": link,
                    })

    finally:
        driver.quit()

    # Build DataFrame (handle case where no articles were found)
    df = pd.DataFrame(all_links)
    if df.empty:
        print("Warning: no articles were found; producing empty output files.")
        df = pd.DataFrame(columns=["heading", "date", "link"])
    else:
        print("DataFrame columns before normalization:", df.columns.tolist())
        if "link" not in df.columns:
            df["link"] = ""
        if "date" not in df.columns:
            df["date"] = pd.NaT

        # Remove duplicates
        df = df.drop_duplicates(subset=["link"])

        # Sort newest first
        try:
            df = df.sort_values(by="date", ascending=False)
        except KeyError as exc:
            print("Warning: 'date' column missing when sorting; adding placeholder and retrying.")
            df["date"] = pd.NaT
            df = df.sort_values(by="date", ascending=False)
        except Exception as exc:
            print("Warning: could not sort by date (missing or invalid values).", exc)

    print("Total articles found:", len(df))

    # ── Output directory (shared by daily XLSX and master file) ─────────────
    CT_SCOOP_DIR = Path("data/ct_scoop")
    CT_SCOOP_DIR.mkdir(parents=True, exist_ok=True)

    MASTER_CT_SCOOP_FILE = Path("master_file")
    MASTER_CT_SCOOP_FILE.mkdir(parents=True, exist_ok=True)

    # Save to Excel (fallback to CSV if openpyxl is missing)
    export_path = CT_SCOOP_DIR / f"ct_scoop_links_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    try:
        df.to_excel(export_path, index=False)
        print(f"Saved {len(df)} records to {export_path}")
    except ModuleNotFoundError as exc:
        fallback_path = str(export_path).replace(".xlsx", ".csv")
        df.to_csv(fallback_path, index=False)
        print(
            "openpyxl not installed (", exc, "); saved to CSV instead:", fallback_path
        )

    # Save JSON for dashboard tab (convert date objects to strings)
    json_path = "ct_scoop_latest.json"
    json_data = df.copy()
    if "date" in json_data.columns:
        json_data["date"] = json_data["date"].astype(str)

    json_payload = {
        "last_updated": today.strftime("%Y-%m-%d"),
        "data": json_data.to_dict(orient="records"),
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_payload, f, ensure_ascii=False, indent=2, cls=DateEncoder)
    print(f"Saved JSON to {json_path}")

    # ── Master file — accumulates all daily results ──────────────────────────
    MASTER_FILE = MASTER_CT_SCOOP_FILE / "ct_scoop_master.csv"

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
