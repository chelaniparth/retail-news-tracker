from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.os_manager import ChromeType
from bs4 import BeautifulSoup
import os
import time
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
import pandas as pd
import json

# ── Chrome setup (GitHub Actions compatible) ──────────────────────────────────
def get_chrome_version():
    """Detect installed Chrome version for webdriver-manager."""
    for cmd in [
        ["google-chrome", "--version"],
        ["google-chrome-stable", "--version"],
        ["chromium-browser", "--version"],
        ["chromium", "--version"],
    ]:
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode()
            version = out.strip().split()[-1]   # e.g. "124.0.6367.91"
            print(f"  Detected Chrome: {version} via '{cmd[0]}'")
            return version
        except Exception:
            continue
    print("  ⚠️  Could not detect Chrome version — letting webdriver-manager auto-detect")
    return None


def get_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    # On CI: use the exact Chrome + ChromeDriver installed by browser-actions/setup-chrome
    # (version-matched, avoids the localhost timeout caused by mismatched binaries).
    # Locally: fall back to webdriver-manager auto-detection.
    chrome_bin = os.environ.get("CHROME_BIN")
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")

    if chrome_bin:
        options.binary_location = chrome_bin
        print(f"  Using Chrome binary: {chrome_bin}")

    if chromedriver_path:
        print(f"  Using ChromeDriver: {chromedriver_path}")
        service = Service(chromedriver_path)
    else:
        chrome_version = get_chrome_version()
        driver_path = ChromeDriverManager(driver_version=chrome_version).install()
        service = Service(driver_path)

    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(180)
    return driver


# ── Config ────────────────────────────────────────────────────────────────────
URL       = "https://whatnow.com/category/restaurants/"
MAX_PAGES = 7

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# ── Scrape post listing ───────────────────────────────────────────────────────
MAX_LOAD_RETRIES = 3

driver = get_driver()

try:
    page_load_ok = False
    for attempt in range(1, MAX_LOAD_RETRIES + 1):
        try:
            driver.get(URL)
            page_load_ok = True
            break
        except Exception as err:
            print(f"⚠️  Page load attempt {attempt}/{MAX_LOAD_RETRIES} failed: {type(err).__name__}: {err}")
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            if attempt < MAX_LOAD_RETRIES:
                print(f"   Retrying in 10s …")
                time.sleep(10)

    if not page_load_ok:
        print("❌  Site unreachable after all retries — exiting gracefully.")
        driver.quit()
        exit(0)

    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_all_elements_located((By.CLASS_NAME, "p-wrap"))
        )
    except Exception:
        print("⚠️  No posts found on initial load — check if selector changed")

    def count_posts():
        return len(driver.find_elements(By.CLASS_NAME, "p-wrap"))

    pages_viewed = 1
    while pages_viewed < MAX_PAGES:
        prev_count = count_posts()
        try:
            view_more = driver.find_element(
                By.XPATH, "//a[contains(@class, 'loadmore-trigger')]"
            )
        except Exception:
            print(f"No more 'View More' button — stopping at page {pages_viewed}")
            break

        driver.execute_script("arguments[0].click();", view_more)
        try:
            WebDriverWait(driver, 10).until(lambda d: count_posts() > prev_count)
            pages_viewed += 1
            print(f"  Loaded page {pages_viewed}/{MAX_PAGES}")
            time.sleep(0.5)
        except Exception:
            print("Click did not load new posts or timed out")
            break

    page_source = driver.page_source

finally:
    driver.quit()   # always quit — even if scraping throws an error

# ── Parse posts ───────────────────────────────────────────────────────────────
soup  = BeautifulSoup(page_source, "html.parser")
posts = soup.select("div.p-wrap")

rows      = []
seen_urls = set()

for p in posts:
    a    = p.select_one("h4.entry-title a.p-url") or p.select_one("h4.entry-title a")
    href = a.get("href") if a else None
    if not href or href in seen_urls:
        continue

    time_el  = p.select_one("time[datetime]") or p.select_one("time")
    post_dt  = None
    date_str = None

    if time_el:
        if time_el.has_attr("datetime"):
            dt_str = time_el["datetime"].replace("Z", "+00:00")
            try:
                post_dt = datetime.fromisoformat(dt_str)
                if post_dt.tzinfo is None:
                    post_dt = post_dt.replace(tzinfo=timezone.utc)
                date_str = post_dt.strftime("%B %d, %Y")
            except Exception:
                date_str = time_el.get_text(strip=True)
        else:
            date_str = time_el.get_text(strip=True)

    seen_urls.add(href)
    rows.append({"date": date_str, "url": href})

print(f"\n📋 Posts within last 48 hours: {len(rows)}")

if not rows:
    print("No posts found in window — exiting without writing files.")
    exit(0)

# ── Fetch individual article pages ────────────────────────────────────────────
for i, row in enumerate(rows, 1):
    try:
        resp = requests.get(row["url"], headers=HEADERS, timeout=15)
        resp.raise_for_status()
        article_soup = BeautifulSoup(resp.text, "html.parser")

        row["title"] = (
            article_soup.title.string.strip() if article_soup.title else ""
        )

        address  = None
        addr_div = article_soup.find(
            "div", class_="bottom_infowindow bottom_infowindow0 only_one"
        )
        if addr_div:
            h3 = addr_div.find("h3")
            if h3:
                address = h3.get_text(strip=True)
        row["address"] = address or ""

        print(f"  [{i}/{len(rows)}] ✓ {row['title'][:60]}")

    except Exception as e:
        row["title"]   = ""
        row["address"] = ""
        print(f"  [{i}/{len(rows)}] ✗ Failed to fetch {row['url']} — {e}")

    time.sleep(0.3)   # polite delay between article requests

# ── Output directories ────────────────────────────────────────────────────────
REST_DIR = Path("data/restaurant")
REST_DIR.mkdir(parents=True, exist_ok=True)

MASTER_DIR = Path("master_file")
MASTER_DIR.mkdir(parents=True, exist_ok=True)

# ── Save CSV ──────────────────────────────────────────────────────────────────
today    = datetime.now().strftime("%Y-%m-%d")
csv_file = REST_DIR / f"Daily_restaurants_{today}.csv"
df       = pd.DataFrame(rows, columns=["date", "title", "address", "url"])
df.to_csv(csv_file, index=False, encoding="utf-8")
print(f"\n✅ CSV  saved → {csv_file} ({len(df)} rows)")

# ── Save JSON ─────────────────────────────────────────────────────────────────
json_payload = {
    "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    "total":        len(rows),
    "data":         rows,
}
with open("restaurant_latest.json", "w", encoding="utf-8") as f:
    json.dump(json_payload, f, ensure_ascii=False, indent=2)

print(f"✅ JSON saved → restaurant_latest.json ({len(rows)} records)")

# ── Master file — accumulates all daily results ───────────────────────────────
MASTER_FILE = MASTER_DIR / "restaurant_master.csv"

df_new = df.copy()
df_new['Date_Appended'] = today
df_new['date'] = pd.to_datetime(df_new['date'], errors='coerce')

if MASTER_FILE.exists():
    df_master = pd.read_csv(MASTER_FILE, encoding='utf-8')
    df_master['date'] = pd.to_datetime(df_master['date'], errors='coerce')
    df_master = pd.concat([df_master, df_new], ignore_index=True)
else:
    df_master = df_new

df_master = df_master.drop_duplicates(subset=['url'])
df_master = df_master.sort_values('date', ascending=False)
df_master.to_csv(MASTER_FILE, index=False, encoding='utf-8')
print(f"✅ Master file updated: {MASTER_FILE}  ({len(df_master)} total rows)")