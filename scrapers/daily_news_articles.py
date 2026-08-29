"""
Retail & Industry News Scraper v10
Changes from v9:
  1. FIXED link decoding — was resolving to publisher homepages, not article URLs.
     Root cause: HEAD redirect lands on domain root, not the article.
     New decode pipeline:
       Tier A: base64 decode of the Google News path segment (most reliable, free)
       Tier B: Zyte proxy redirect resolution (follows Google redirect to real article)
       Tier C: Scrape Google's redirect page for data-n-au / c-wiz article URL
       Tier D: googlenewsdecoder library
       Tier E: Keep Google News URL (fallback)
  2. REMOVED HEAD request tier — it was the source of homepage URLs.
  3. IMPROVED geo filter — catches leeds-live, salisburyjournal, deeside, gcnews.com.au
"""

import argparse
import requests
import pandas as pd
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
import time
import re
import random
import base64
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from collections import Counter
import json
import os
import urllib3

# Suppress SSL warnings when Zyte verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# IPython display is optional (not available in GitHub Actions / plain Python)
try:
    from IPython.display import FileLink, display as _ipy_display
    def display(x): _ipy_display(x)
except ImportError:
    def display(x): pass
    def FileLink(x): return x

try:
    from googlenewsdecoder import new_decoderv1
    HAS_GND = True
except ImportError:
    try:
        import subprocess
        subprocess.check_call(["pip", "install", "googlenewsdecoder", "-q"])
        from googlenewsdecoder import new_decoderv1
        HAS_GND = True
    except Exception:
        HAS_GND = False

# ─────────────────────────────────────────────────────────
# ZYTE PROXY CONFIGURATION
# ─────────────────────────────────────────────────────────

# Load .env for local development (no-op if not installed or file missing)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ZYTE_API_KEY = os.environ.get("ZYTE_API_KEY", "")  # set in .env locally or GitHub Secret in CI

ZYTE_PROXIES = {
    scheme: f"http://{ZYTE_API_KEY}:@api.zyte.com:8011"
    for scheme in ("http", "https")
}

# Set to "zyte-ca.crt" if you have the cert, or False to skip SSL verification
ZYTE_CA_CERT = False

# Persistent Zyte session — reuses connections for speed
_ZYTE_SESSION = requests.Session()
_ZYTE_SESSION.proxies.update(ZYTE_PROXIES)
_ZYTE_SESSION.verify = ZYTE_CA_CERT

# One-time Zyte connectivity check — skip Tier B entirely if proxy is unreachable
def _check_zyte_available() -> bool:
    if not ZYTE_API_KEY:
        print("⚠️  ZYTE_API_KEY not set — Tier B (Zyte) disabled")
        return False
    try:
        r = _ZYTE_SESSION.get("https://httpbin.org/ip", timeout=6)
        print(f"✅ Zyte proxy reachable (status={r.status_code})")
        return True
    except Exception as e:
        print(f"⚠️  Zyte proxy unreachable ({type(e).__name__}) — Tier B disabled for this run")
        return False

_ZYTE_AVAILABLE = _check_zyte_available()

# ─────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────

NOW_UTC    = datetime.now(timezone.utc)
CUTOFF_UTC = NOW_UTC - timedelta(hours=48)

REGIONS = [
    ("US:en", "USA"),
    ("CA:en", "Canada"),
]

TARGET_SITES = [
    "grocerydive.com", "supermarketnews.com", "retaildive.com",
    "costar.com", "cnbc.com", "chainstoreage.com", "nrn.com",
    "qsrmagazine.com", "wwd.com", "businessofhome.com",
    "drugstoreage.com", "twice.com", "globenewswire.com",
    "businesswire.com", "prnewswire.com",
    "businessinsider.com", "nj.com", "bisnow.com",
    "foxbusiness.com", "retailtouchpoints.com", "retailwire.com",
    "thestreet.com", "reuters.com", "yahoo.com", "newsday.com",
    "buzzfeed.com", "benzinga.com", "marketwatch.com", "bloomberg.com", "wsj.com",
]

SITE_QUERY = " OR ".join([f"site:{s}" for s in TARGET_SITES[:8]])

OPENING_KWS = [
    # 🔴 Highest signal — direct opening confirmation
    "grand opening",
    "now open",
    "soft opening",
    "open for business",
    "ribbon cutting",

    # 🟠 Pre-opening announcements
    "opening soon",
    "coming soon",
    "plans to open",
    "new store",
    "new location",
    "expanding to",

    # 🟡 Construction / development phase
    "breaks ground",
    "groundbreaking ceremony",
    "under construction",
    "construction underway",
    "certificate of occupancy",

    # 🟢 Early-stage / permit signals
    "permit filed",
    "zoning approval",
    "lease signed",
    "build out",
]

CLOSING_KWS = [
    # 🔴 Highest signal — confirmed closure
    "store closing",
    "permanent closure",
    "going out of business",
    "store closing sale",
    "everything must go",

    # 🟠 Multi-location / chain closures
    "closing stores",
    "closing locations",
    "shutting down locations",
    "closing all locations",

    # 🟡 Financial distress signals
    "chapter 11",
    "bankruptcy",
    "liquidation",
    "ceases operations",

    # 🟢 Soft signals
    "closing soon",
    "shutters",
    "last day",
]

# ─────────────────────────────────────────────────────────
# MODE  (--mode opening | closing | all)
# ─────────────────────────────────────────────────────────
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--mode", choices=["opening", "closing", "all"], default="all")
_MODE = _parser.parse_known_args()[0].mode

ACTIVE_KWS = (
    OPENING_KWS if _MODE == "opening" else
    CLOSING_KWS if _MODE == "closing" else
    OPENING_KWS + CLOSING_KWS
)

print(f"🏷️  Mode: {_MODE.upper()}  ({len(ACTIVE_KWS)} keywords)")

LOCATION_ACTION_PATTERN = re.compile(
    r'\b(open|opening|opened|close|closing|closed|closure|shut|shutting|'
    r'location|locations|store|stores|branch|branches|outlet|outlets|'
    r'expand|expanding|expansion|relocat|new site|new unit)\b',
    re.IGNORECASE
)

INDUSTRIES = {
    "Apparel": [
        "clothing store", "fashion retailer", "apparel store", "apparel chain",
        "clothing chain", "fashion chain", "garment store",
    ],
    "Arts & Crafts": [
        "arts and crafts store", "hobby store", "craft store", "craft retailer",
        "hobby shop",
    ],
    "Auto Parts": [
        "auto parts store", "auto parts retailer", "automotive parts",
        "car parts store",
    ],
    "Books / Toys / Gaming": [
        "bookstore", "book store", "toy store", "toy retailer",
        "gaming store", "game store",
    ],
    "Convenience Stores / Distributors": [
        "convenience store", "c-store", "gas station convenience",
        "fuel station store", "convenience chain",
    ],
    "Cruise Lines": [
        "cruise line", "cruise terminal", "cruise port", "cruise ship homeport",
    ],
    "Department Stores": [
        "department store", "department chain",
    ],
    "Drug Retailers": [
        "pharmacy chain", "drugstore chain", "drug store", "drug retailer",
        "retail pharmacy",
    ],
    "Electronics / Distributors": [
        "electronics store", "electronics retailer", "consumer electronics store",
        "tech retailer",
    ],
    "Experiential": [
        "entertainment venue", "experiential retail", "family entertainment",
        "escape room", "trampoline park", "adventure park",
    ],
    "Facility & Support Services": [
        "facility services", "facilities management", "support services provider",
    ],
    "Financial Services": [
        "bank branch", "credit union branch", "financial services branch",
        "bank location",
    ],
    "Foodservice Distributors": [
        "foodservice distributor", "food distributor", "restaurant supply",
    ],
    "Footwear": [
        "shoe store", "footwear store", "footwear retailer", "shoe retailer",
        "sneaker store",
    ],
    "Furniture / Mattress": [
        "furniture store", "mattress store", "furniture retailer",
        "mattress retailer", "home furniture store",
    ],
    "Grocery": [
        "grocery store", "supermarket", "food store", "grocery chain",
        "supermarket chain",
    ],
    "Grocery Wholesale": [
        "wholesale grocery", "warehouse club", "wholesale club",
        "cash and carry",
    ],
    "Gyms / Fitness": [
        "gym", "fitness center", "fitness studio", "health club",
        "fitness chain",
    ],
    "Health / Beauty": [
        "beauty store", "beauty retailer", "salon", "spa", "health store",
        "wellness store", "cosmetics store",
    ],
    "Home Improvement / Building Materials": [
        "home improvement store", "hardware store", "building materials store",
        "home improvement chain",
    ],
    "Hospitality": [
        "hotel", "motel", "resort", "inn", "hotel chain", "hotel brand",
    ],
    "Housewares & Home Furnishings": [
        "housewares store", "home goods store", "home furnishings store",
        "kitchenware store",
    ],
    "International": [
        "international retailer", "global retail chain",
    ],
    "Jewelry & Accessories": [
        "jewelry store", "jewellery store", "accessories store",
        "jewelry retailer",
    ],
    "Mass Merchandisers": [
        "mass merchandiser", "big box store", "discount store",
        "supercenter",
    ],
    "Movie Theatres": [
        "movie theater", "movie theatre", "cinema", "multiplex",
        "theater chain",
    ],
    "Office & Computer": [
        "office supply store", "computer store", "office retailer",
        "stationery store",
    ],
    "Pet Care": [
        "pet store", "pet supply store", "pet retailer", "veterinary clinic",
        "animal hospital",
    ],
    "Pharmaceutical / Medical": [
        "medical clinic", "health center", "urgent care center",
        "medical center", "outpatient clinic",
    ],
    "Restaurants": [
        "restaurant", "fast food chain", "quick service restaurant",
        "fast casual", "diner", "eatery chain", "food chain",
    ],
    "Retail REITs": [
        "shopping center", "shopping mall", "retail plaza", "strip mall",
        "retail REIT", "outlet mall",
    ],
    "Specialty / Other": [
        "specialty retailer", "specialty store", "niche retailer",
    ],
    "Sporting Goods": [
        "sporting goods store", "sports retailer", "outdoor retailer",
        "sports chain",
    ],
}

# ─────────────────────────────────────────────────────────
# NOISE FILTER
# ─────────────────────────────────────────────────────────
NOISE_PATTERN = re.compile(
    r'\b(stock|shares|investor|closing bell|wall st|quarterly|dividend|'
    r'earnings|nasdaq|nyse|ipo|merger|acquisition|fiscal|revenue guidance|'
    r'market cap|share price|analyst|downgrade|upgrade|hedge fund)\b|'
    r'\b(fashion week|runway|catwalk|lookbook|collection launch|awards ceremony|'
    r'spring.summer collection|fall.winter collection|capsule collection)\b|'
    r'\b(film festival|box office|concert|shooting|stabbing|crime|arrest|'
    r'lawsuit|litigation|settle[md]|indictment|guilty|verdict)\b|'
    r'\b(splash pad|splash park|swimming pool|water park|skate park|'
    r'playground|dog park|community park)\b|'
    r'\b(opening ceremony|olympic|paralympic|world cup|super bowl)\b|'
    r'\b(perfume|fragrance|parfumerie|boutique parfum)\b|'
    r'\b(renovation|ramp|depot history|historical character|heritage building|'
    r'refurb|remodel)\b|'
    r'\b(recipe|ingredient|how to cook|cooking tip|meal prep|nutrition fact|'
    r'calorie|caloric)\b|'
    r'\b(review|rating|ranked|ranking|best .{1,20} list|top \d+ )\b|'
    r'\b(layoff|laid off|workforce reduction|job cut|redundanc|strike|'
    r'union negotiat|labor deal)\b|'
    r'\b(cap rate|net operating income|NOI|EBITDA|same.store sales|'
    r'comp sales|comparable sales)\b',
    re.IGNORECASE
)

GEO_BLOCK_PATTERN = re.compile(
    r'\b(uk|u\.k\.|united kingdom|britain|british|england|scotland|wales|'
    r'ireland|australia|australian|melbourne|sydney|brisbane|perth|'
    r'new zealand|india|indian|mumbai|delhi|bangalore|'
    r'south africa|jamaica|nigeria|kenya|ghana|'
    r'germany|france|spain|italy|netherlands|sweden|norway|denmark|'
    r'singapore|hong kong|japan|china|korea|'
    r'montenegro|hungary|pakistan|'
    r'london|manchester|birmingham|leeds|bristol|liverpool|'
    r'edinburgh|glasgow|cardiff|belfast|sheffield|nottingham|'
    r'oxfordshire|yorkshire|wycombe|scarborough|battersea|brixton|'
    r'coventry|leicester|bournemouth|wandsworth|'
    r'\.co\.uk|\.co\.za|\.com\.au|\.co\.nz|\.ie)\b',
    re.IGNORECASE
)

GEO_BLOCK_DOMAIN_SUFFIXES = {
    '.co.uk', '.co.za', '.co.nz', '.co.in',
    '.com.au', '.com.sg',
    '.ie',
    '.in',
    '.au',
}

GEO_BLOCK_DOMAINS = {
    'aol.com',
    'iol.co.za', 'ewn.co.za', 'jamaicaobserver.com', 'thesun.co.uk',
    'mirror.co.uk', 'express.co.uk', 'independent.co.uk', 'birminghammail.co.uk',
    'thestar.co.uk', 'examinerlive.co.uk', 'swindonadvertiser.co.uk',
    'punchline-gloucester.com', 'leeds-live.co.uk', 'salisburyjournal.co.uk',
    'deeside.com',
    'gcnews.com.au', 'concreteplayground.com',
    'vijesti.me', 'agoranotizia.it', 'mid-day.com',
    'hollywoodreporterindia.com', 'lifestyleasia.com',
    'starnewskorea.com', 'openthemagazine.com', 'road.cc',
    'infashionbusiness.com',
}

_HOMEPAGE_PATTERN = re.compile(r'^https?://[^/]+/?$')

def _is_homepage(url: str) -> bool:
    return bool(_HOMEPAGE_PATTERN.match(url.rstrip('/')))

def is_geo_relevant(title: str, url: str, source: str) -> bool:
    try:
        domain = url.split('/')[2].replace('www.', '').lower()
        if domain in GEO_BLOCK_DOMAINS:
            return False
        if any(domain.endswith(suffix) for suffix in GEO_BLOCK_DOMAIN_SUFFIXES):
            return False
    except Exception:
        pass
    if GEO_BLOCK_PATTERN.search(title):
        return False
    return True

# ─────────────────────────────────────────────────────────
# TITLE CLEANING
# ─────────────────────────────────────────────────────────
_PUBLISHER_SUFFIXES = re.compile(
    r'(?:[-–—|•·]\s*)'
    r'(?:'
    r'grocery dive|supermarket news|retail dive|'
    r'chain store age|qsr magazine|nation\'?s restaurant news|'
    r'drug store news|twice|wwd|business of home|'
    r'globe newswire|business wire|pr newswire|'
    r'costar|cnbc|reuters|bloomberg|wsj|'
    r'the wall street journal|new york times|'
    r'yahoo(?: finance| news)?|msn(?: money)?|'
    r'cnn(?: business)?|fox business|abc news|nbc news|'
    r'business insider|bisnow|retailwire|retail touchpoints|'
    r'the street|fox news|'
    r'[a-z0-9 ]{1,40}\.com'
    r')'
    r'.*$',
    re.IGNORECASE
)
_SEP_RE = re.compile(r'\s[-–—|•·]\s')

def clean_title(raw_title: str, intent_kw: str) -> str:
    title = _PUBLISHER_SUFFIXES.sub('', raw_title).strip()
    parts = [p.strip() for p in _SEP_RE.split(title) if p.strip()]
    if not parts:
        return raw_title.strip()
    kw_lower = intent_kw.lower()
    for part in parts:
        if kw_lower in part.lower():
            return part
    for part in parts:
        if LOCATION_ACTION_PATTERN.search(part):
            return part
    return parts[0]

# ─────────────────────────────────────────────────────────
# RELEVANCE SCORING
# ─────────────────────────────────────────────────────────
def relevance_score(title: str, intent_kw: str, industry_terms: list) -> int:
    score = 0
    title_lower = title.lower()
    if intent_kw.lower() in title_lower:
        score += 3
    industry_hits = sum(1 for t in industry_terms if t.lower() in title_lower)
    if industry_hits > 0:
        score += 2 + min(industry_hits - 1, 3)
    if LOCATION_ACTION_PATTERN.search(title):
        score += 2
    return score

MIN_RELEVANCE_SCORE = 2

# ─────────────────────────────────────────────────────────
# PROXY & BATCH CONFIG
# ─────────────────────────────────────────────────────────
PROXIES = []   # Add your own proxies here if needed (separate from Zyte)

BATCH_SIZE   = 20          # smaller batches → more frequent long pauses
BATCH_PAUSE  = (25, 50)   # was (5,15) — longer gaps prevent CAPTCHA triggers
WORKERS      = 3           # was 8 — fewer concurrent Google requests

# ─────────────────────────────────────────────────────────
# HTTP SESSION POOL
# ─────────────────────────────────────────────────────────
_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://news.google.com/",
}

HEADERS = _BASE_HEADERS


def _make_session(proxy: str | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(_BASE_HEADERS)
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


_SESSION_POOL: list[requests.Session] = (
    [_make_session(p) for p in PROXIES] if PROXIES else [_make_session()]
)

_thread_session_map: dict[int, requests.Session] = {}
_thread_map_lock = threading.Lock()


def _get_session() -> requests.Session:
    tid = threading.get_ident()
    with _thread_map_lock:
        if tid not in _thread_session_map:
            idx = len(_thread_session_map) % len(_SESSION_POOL)
            _thread_session_map[tid] = _SESSION_POOL[idx]
    return _thread_session_map[tid]


_SESSION = _SESSION_POOL[0]

# ─────────────────────────────────────────────────────────
# URL UTILITIES
# ─────────────────────────────────────────────────────────
_TRACKING_PARAMS = re.compile(
    r'[?&](?:utm_\w+|fbclid|guccounter|_ga|_gl|'
    r'ref|amp|mc_cid|mc_eid|mkt_tok|yclid|gclid)'
    r'=[^&]*',
    re.IGNORECASE
)

def _clean_url(url: str) -> str:
    url = _TRACKING_PARAMS.sub('', url)
    url = re.sub(r'\?&', '?', url)
    url = re.sub(r'[?&]+$', '', url)
    return url

def _is_google_url(url: str) -> bool:
    return 'news.google.com' in url

def _is_valid_article_url(url: str) -> bool:
    if not url or _is_google_url(url):
        return False
    if _is_homepage(url):
        return False
    return True

# ─────────────────────────────────────────────────────────
# LINK DECODE — ARTICLE-LEVEL URL RESOLUTION
# ─────────────────────────────────────────────────────────

_RSS_SOURCE_URLS: dict[str, str] = {}
_tier_hits: Counter = Counter()
_CAPTCHA_BACKOFF_UNTIL: float = 0.0   # epoch time — global RSS pause when CAPTCHA detected


def _decode_base64(google_url: str) -> str | None:
    """
    Tier A: Decode the base64 path segment of a Google News URL.
    Free, no HTTP call needed.
    Tries multiple extraction patterns to maximise success rate.
    """
    try:
        parsed  = urlparse(google_url)
        segs    = [p for p in parsed.path.split('/') if p]
        if not segs:
            return None
        path_id = segs[-1].split('?')[0]
        padded  = path_id + '=' * (-len(path_id) % 4)
        raw     = base64.urlsafe_b64decode(padded)

        # Pattern 1: all printable-ASCII URL bytes (most reliable)
        for m in re.finditer(rb'https?://[\x21-\x7e]+', raw):
            candidate = m.group(0).decode('utf-8', errors='ignore').rstrip('.)')
            if _is_valid_article_url(candidate) and 'google.com' not in candidate:
                return _clean_url(candidate)

        # Pattern 2: URL may contain non-ASCII chars after UTF-8 decode
        text = raw.decode('utf-8', errors='ignore')
        for m in re.finditer(r'https?://[^\x00-\x1f\s"\'<>\\]+', text):
            candidate = m.group(0).rstrip('.)')
            if _is_valid_article_url(candidate) and 'google.com' not in candidate:
                return _clean_url(candidate)

    except Exception:
        pass
    return None


def _decode_zyte(google_url: str) -> str | None:
    """
    Tier B: Resolve Google News redirect via Zyte proxy.
    Zyte follows the redirect chain and returns the final real article URL.
    """
    if not _ZYTE_AVAILABLE:
        return None
    try:
        response = _ZYTE_SESSION.get(
            google_url,
            allow_redirects=True,
            timeout=10,
        )
        final_url = response.url
        if final_url and _is_valid_article_url(final_url):
            return _clean_url(final_url)
    except Exception:
        pass
    return None


def _decode_scrape(google_url: str) -> str | None:
    """
    Tier C: Fetch the Google News article redirect page and extract the real URL.
    Parses data-n-au / c-wiz / meta refresh / window.location from the HTML.
    """
    try:
        resp = _get_session().get(
            google_url,
            timeout=10,
            allow_redirects=False,
            headers={
                **HEADERS,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            }
        )

        loc = resp.headers.get("Location", "")
        if loc and _is_valid_article_url(loc):
            return _clean_url(loc)

        html = resp.text

        m = re.search(r'data-n-au=["\']([^"\']+)["\']', html)
        if m:
            url = m.group(1).strip()
            if _is_valid_article_url(url):
                return _clean_url(url)

        m = re.search(r'"(https?://(?!.*news\.google\.com)[^"]{20,})"', html)
        if m:
            url = m.group(1).strip()
            if _is_valid_article_url(url):
                return _clean_url(url)

        m = re.search(
            r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+'
            r'content=["\'][^;]*;\s*url=([^"\'>\s]+)',
            html, re.IGNORECASE
        )
        if m:
            url = m.group(1).strip()
            if _is_valid_article_url(url):
                return _clean_url(url)

        m = re.search(r"window\.location\s*=\s*['\"]([^'\"]+)['\"]", html)
        if m:
            url = m.group(1).strip()
            if _is_valid_article_url(url):
                return _clean_url(url)

    except Exception:
        pass
    return None


def _decode_gnd(google_url: str) -> str | None:
    """Tier D: googlenewsdecoder library — last resort."""
    if not HAS_GND:
        return None
    for attempt in range(2):
        time.sleep(1.0 * (attempt + 1) + random.uniform(0, 0.3))
        try:
            result = new_decoderv1(google_url, interval=2)
            if result and result.get("status"):
                url = result.get("decoded_url", "")
                if _is_valid_article_url(url):
                    return _clean_url(url)
        except Exception:
            continue
    return None


def decode_link(google_url: str) -> str:
    # Tier 0: cached source URL from RSS <source url="..."> element (free, no HTTP)
    src = _RSS_SOURCE_URLS.get(google_url)
    if src and _is_valid_article_url(src):
        _tier_hits["0_rss_src"] += 1
        return _clean_url(src)

    # Tier A: base64 path decode (free, no HTTP)
    decoded = _decode_base64(google_url)
    if decoded:
        _tier_hits["A_base64"] += 1
        return decoded

    # Small jitter before any HTTP call
    time.sleep(random.uniform(0.1, 0.4))

    # Tier B: Zyte proxy redirect resolution
    decoded = _decode_zyte(google_url)
    if decoded:
        _tier_hits["B_zyte"] += 1
        return decoded

    # Tier D: googlenewsdecoder library (moved before Tier C — avoids hitting Google directly)
    decoded = _decode_gnd(google_url)
    if decoded:
        _tier_hits["D_gnd"] += 1
        return decoded

    # Tier C: scrape Google redirect page (last resort — can trigger CAPTCHA)
    decoded = _decode_scrape(google_url)
    if decoded:
        _tier_hits["C_scrape"] += 1
        return decoded

    _tier_hits["fail"] += 1
    return google_url  # keep Google News URL as fallback

# ─────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────
def parse_pub_date(raw: str):
    try:
        return pd.to_datetime(raw, utc=True)
    except Exception:
        return None


def build_query(intent_kw: str, industry_terms: list, use_sites: bool) -> str:
    industry_str = " OR ".join(f'"{t}"' for t in industry_terms[:3])
    site_part    = f" ({SITE_QUERY})" if use_sites else ""
    return f'"{intent_kw}" ({industry_str}){site_part} when:2d'


def fetch_rss(url: str) -> list:
    global _CAPTCHA_BACKOFF_UNTIL
    # Honour any active global CAPTCHA backoff before making this request
    wait = _CAPTCHA_BACKOFF_UNTIL - time.time()
    if wait > 0:
        time.sleep(wait + random.uniform(0, 5))

    try:
        r = _get_session().get(url, timeout=20)
        if r.status_code == 429:
            _CAPTCHA_BACKOFF_UNTIL = time.time() + 120
            print("  [RSS 429] Rate-limited — global backoff 2 min")
            return []
        if r.status_code != 200:
            return []
        content = r.content
        if b"<item>" not in content:
            snippet = content[:1000].lower()
            if b"captcha" in snippet or b"unusual traffic" in snippet:
                _CAPTCHA_BACKOFF_UNTIL = time.time() + 180
                print("  [RSS CAPTCHA] Google CAPTCHA detected — global backoff 3 min")
            elif b"<html" in content[:200]:
                print(f"  [RSS BLOCKED] Google returned HTML — possible rate limit")
            return []
        content = re.sub(rb'\s+xmlns(?::\w+)?="[^"]+"', b'', content)
        root  = ET.fromstring(content)
        items = root.findall(".//item")

        for item in items:
            try:
                lnk    = item.findtext("link") or ""
                src_el = item.find("source")
                if lnk and src_el is not None:
                    src_url = src_el.get("url", "")
                    if _is_valid_article_url(src_url):
                        _RSS_SOURCE_URLS[lnk] = src_url
            except Exception:
                continue

        return items
    except ET.ParseError:
        return []
    except Exception:
        return []


def fetch_one(intent_kw: str, industry: str, industry_terms: list,
              use_sites: bool, region_ceid: str, region_label: str) -> list:
    time.sleep(random.uniform(0.8, 2.5))

    status  = "Opening" if intent_kw in OPENING_KWS else "Closing"
    country = region_ceid.split(':')[0]

    def _fetch_and_parse(with_sites: bool) -> list:
        query = build_query(intent_kw, industry_terms, with_sites)
        url   = (
            f"https://news.google.com/rss/search?"
            f"q={requests.utils.quote(query)}"
            f"&hl=en-US&gl={country}&ceid={region_ceid}"
        )
        items = fetch_rss(url)
        rows  = []
        for item in items:
            raw_title = (item.findtext("title") or "").strip()
            if not raw_title:
                continue

            title = clean_title(raw_title, intent_kw)

            if NOISE_PATTERN.search(title):
                continue

            src = item.findtext("source") or ""
            lnk = item.findtext("link") or ""
            if not is_geo_relevant(title, lnk, src):
                continue

            pub_raw = item.findtext("pubDate") or ""
            pub_dt  = parse_pub_date(pub_raw)
            if pub_dt and pub_dt < CUTOFF_UTC:
                continue

            if not LOCATION_ACTION_PATTERN.search(title):
                continue

            score = relevance_score(title, intent_kw, industry_terms)
            if score < MIN_RELEVANCE_SCORE:
                continue

            rows.append({
                "status":          status,
                "industry":        industry,
                "keyword":         intent_kw,
                "region":          region_label,
                "title":           title,
                "source":          src or "Unknown",
                "published_date":  pub_dt,
                "google_link":     lnk,
                "relevance_score": score,
            })
        return rows

    rows = _fetch_and_parse(use_sites)
    if not rows and use_sites:
        time.sleep(random.uniform(0.3, 0.8))
        rows = _fetch_and_parse(False)
    return rows

# ─────────────────────────────────────────────────────────
# BUILD TASK LIST
# ─────────────────────────────────────────────────────────
tasks = []
for industry, terms in INDUSTRIES.items():
    for intent_kw in ACTIVE_KWS:
        for ceid, region_label in REGIONS:
            tasks.append((intent_kw, industry, terms, True, ceid, region_label))

print(f"📋 Total tasks : {len(tasks)}")
print(f"🕐 Window      : last 48 hours from {NOW_UTC.strftime('%Y-%m-%d %H:%M UTC')}\n")

# ─────────────────────────────────────────────────────────
# PARALLEL RSS FETCH  (batched to avoid rate-limiting)
# ─────────────────────────────────────────────────────────
all_results = []
batches     = [tasks[i : i + BATCH_SIZE] for i in range(0, len(tasks), BATCH_SIZE)]
proxy_info  = f"{len(_SESSION_POOL)} prox{'ies' if len(_SESSION_POOL) > 1 else 'y (direct)'}"

print(f"📡 Fetching RSS — {len(batches)} batches × ≤{BATCH_SIZE} tasks | "
      f"{WORKERS} workers | {proxy_info}")
print(f"   Pause between batches: {BATCH_PAUSE[0]}–{BATCH_PAUSE[1]}s\n")

for b_idx, batch in enumerate(batches, 1):
    print(f"  🔄 Batch {b_idx}/{len(batches)}  ({len(batch)} tasks)...")
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(fetch_one, *t): t for t in batch}
        for fut in as_completed(futures):
            rows = fut.result()
            all_results.extend(rows)
    print(f"     ✓ {len(all_results)} raw rows so far")

    if b_idx < len(batches):
        pause = random.uniform(*BATCH_PAUSE)
        print(f"     ⏸  Sleeping {pause:.0f}s before next batch...")
        time.sleep(pause)

print(f"\n✅ RSS fetch complete: {len(all_results)} raw rows\n")

# ─────────────────────────────────────────────────────────
# DIAGNOSTIC
# ─────────────────────────────────────────────────────────
if not all_results:
    print("❌ 0 rows fetched.")
    print("   • Try lowering MIN_RELEVANCE_SCORE to 1")
    print("   • Check manually: https://news.google.com/search?q=\"grand+opening\"+grocery+when:2d")
else:
    print("📊 Sample cleaned titles:")
    for r in all_results[:5]:
        print(f"   [{r['status']}] [{r['industry']}] (score={r['relevance_score']}) {r['title']}")
    print()

# ─────────────────────────────────────────────────────────
# DEDUP + INDUSTRY VALIDATION
# ─────────────────────────────────────────────────────────
if all_results:
    df = pd.DataFrame(all_results)

    industry_terms_map = {ind: terms for ind, terms in INDUSTRIES.items()}

    def title_matches_industry(row):
        tl = row["title"].lower()
        return any(t.lower() in tl for t in industry_terms_map.get(row["industry"], []))

    validated = df[df.apply(title_matches_industry, axis=1)].copy()
    no_match  = df[~df.index.isin(validated.index)].copy()
    no_match["industry"] = "Specialty / Other"

    df = pd.concat([validated, no_match], ignore_index=True)
    df = df.drop_duplicates(subset=["google_link", "industry"])
    df = (
        df.groupby(
            ["google_link", "status", "region", "title", "source",
             "published_date", "keyword", "relevance_score"],
            as_index=False, sort=False
        ).agg(industry=("industry", lambda x: ", ".join(sorted(set(x)))))
    )
    print(f"🔎 Unique articles after URL dedup  : {len(df)}")
    print(f"📈 Avg relevance score              : {df['relevance_score'].mean():.2f}")

    # ─────────────────────────────────────────────────────
    # PARALLEL LINK DECODING
    # ─────────────────────────────────────────────────────
    _tier_hits.clear()
    print(f"\n🔗 Decoding {len(df)} links...")

    link_map: dict = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        future_to_link = {ex.submit(decode_link, gl): gl for gl in df["google_link"]}
        done = 0
        for fut in as_completed(future_to_link):
            orig = future_to_link[fut]
            link_map[orig] = fut.result()
            done += 1
            if done % 25 == 0:
                print(f"  ✓ {done}/{len(df)} links decoded")

    df["direct_link"] = df["google_link"].map(link_map)

    df.loc[df["direct_link"].apply(
        lambda u: isinstance(u, str) and _is_homepage(u)
    ), "direct_link"] = df.loc[df["direct_link"].apply(
        lambda u: isinstance(u, str) and _is_homepage(u)
    ), "google_link"]

    mask_resolved = ~df["direct_link"].str.contains("news.google.com", na=False)
    n_resolved    = mask_resolved.sum()
    n_fallback    = (~mask_resolved).sum()

    print(f"\n📎 Link decode results:")
    print(f"   ✅ Resolved to article URL       : {n_resolved}  ({n_resolved/len(df)*100:.0f}%)")
    print(f"   ⚠️  Kept as Google News URL       : {n_fallback}")
    print(f"\n   Tier breakdown:")
    for tier, count in sorted(_tier_hits.items()):
        print(f"     {tier:20s} : {count}")

    sample = df[mask_resolved]["direct_link"].head(5).tolist()
    if sample:
        print("\n   Sample resolved article URLs:")
        for u in sample:
            print(f"     → {u}")

    # ─────────────────────────────────────────────────────
    # FINAL CLEAN, TITLE DEDUP & EXPORT
    # ─────────────────────────────────────────────────────
    df["published_date"] = pd.to_datetime(df["published_date"]).dt.tz_localize(None)

    before_title_dedup = len(df)
    df["_title_key"] = df["title"].str.lower().str.strip()

    df = (
        df.sort_values(["relevance_score", "published_date"], ascending=[False, False])
          .drop_duplicates(subset=["_title_key"], keep="first")
          .drop(columns=["_title_key"])
    )

    removed = before_title_dedup - len(df)
    print(f"\n🗑️  Title dedup removed         : {removed} duplicate(s)")
    print(f"✅ Final unique articles        : {len(df)}")

    df = df.sort_values(
        ["relevance_score", "published_date", "status"],
        ascending=[False, False, True]
    )

    df = df[["status", "industry", "region", "title", "source",
             "published_date", "direct_link", "keyword", "relevance_score"]]

    # ─────────────────────────────────────────────────────
    # EXPORT  news_data.json
    # ─────────────────────────────────────────────────────
    os.makedirs("docs", exist_ok=True)

    df_out = df.copy()
    df_out["published_date"] = df_out["published_date"].astype(str)
    records = df_out.to_dict(orient="records")

    _suffix = f"_{_MODE}" if _MODE != "all" else ""
    news_data_path = os.path.join("docs", f"news_data{_suffix}.json")
    with open(news_data_path, "w", encoding="utf-8") as f:
        json.dump({"generated": NOW_UTC.isoformat(), "articles": records},
                  f, indent=2, ensure_ascii=False)
    print(f"\n💾 Saved {news_data_path}  ({len(records)} articles)")

    # ─────────────────────────────────────────────────────
    # GENERATE  claude_prompt.txt
    # ─────────────────────────────────────────────────────
    _CLAUDE_BATCH = 30
    batches = [records[i : i + _CLAUDE_BATCH]
               for i in range(0, len(records), _CLAUDE_BATCH)]

    _PROMPT_HEADER = """\
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
| Store/Shop/Restaurant Name | Location or Full Address with zip code | Event Type | Event Date | Status | Short Description | Article Link |

📌 Rules
• Add one row per article in the order the articles are given
• If an article contains multiple businesses, create a separate row for each
• If an article includes both openings and closures, extract each separately
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

✅ Articles below — extract now:

"""

    prompt_blocks = []
    for idx, batch in enumerate(batches, 1):
        label = (f" — Part {idx} of {len(batches)}" if len(batches) > 1 else "")
        articles_text = "\n".join(
            f"{i+1}. URL: {r['direct_link']}\n"
            f"   Title: {r['title']}\n"
            f"   Status hint: {r['status']} | Industry: {r['industry']}"
            for i, r in enumerate(batch)
        )
        block = (
            f"{'='*60}\n"
            f"PASTE INTO CLAUDE.AI{label}\n"
            f"{'='*60}\n\n"
            f"{_PROMPT_HEADER}"
            f"{articles_text}\n\n"
            f"{'='*60}\n"
            f"END — copy Claude's Markdown table into claude_response.txt\n"
            f"{'='*60}"
        )
        prompt_blocks.append(block)

    with open(f"claude_prompt{_suffix}.txt", "w", encoding="utf-8") as f:
        f.write("\n\n".join(prompt_blocks))

    print(f"📋 Saved claude_prompt{_suffix}.txt  "
          f"({len(batches)} batch{'es' if len(batches) > 1 else ''} "
          f"× ≤{_CLAUDE_BATCH} articles)")

    # ── Output directory (shared by daily XLSX and master file) ─────────────
    DAILY_DIR = Path("data/daily_news")
    DAILY_DIR.mkdir(parents=True, exist_ok=True)

    MASTER_DAILY_NEWS_FILE = Path("master_file")
    MASTER_DAILY_NEWS_FILE.mkdir(parents=True, exist_ok=True)


    # ── Excel export ──────────────────────────────────────
    fname = DAILY_DIR / f"Retail_Update_{NOW_UTC.strftime('%Y%_m%d_%H%M')}{_suffix}.xlsx"
    with pd.ExcelWriter(fname, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="News")
        ws = writer.sheets["News"]
        for col_cells in ws.columns:
            max_len = max((len(str(c.value or "")) for c in col_cells), default=10)
            ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 2, 60)

    # ── Master file — accumulates all daily results ───────────────────────────
    MASTER_FILE = MASTER_DAILY_NEWS_FILE / "daily_news_master.csv"

    df_new = df.copy()
    df_new['Date_Appended'] = NOW_UTC.strftime("%Y-%m-%d")
    df_new['published_date'] = pd.to_datetime(df_new['published_date'], errors='coerce')

    if MASTER_FILE.exists():
        df_master = pd.read_csv(MASTER_FILE, encoding='utf-8')
        df_master['published_date'] = pd.to_datetime(df_master['published_date'], errors='coerce')
        df_master = pd.concat([df_master, df_new], ignore_index=True)
    else:
        df_master = df_new

    df_master = df_master.drop_duplicates(subset=['direct_link', 'title'])
    df_master = df_master.sort_values('published_date', ascending=False)
    df_master.to_csv(MASTER_FILE, index=False, encoding='utf-8')
    print(f"\n📂 Master file updated: {MASTER_FILE}  ({len(df_master)} total rows)")

    print(f"\n🏁 DONE")
    print(f"   Articles : {len(df)}  |  "
          f"Window: last 48 hrs → {NOW_UTC.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"   Excel    : {fname}")
    print(f"\n👉 Next steps:")
    print(f"   1. Open claude_prompt.txt")
    print(f"   2. Paste each block into claude.ai  (one block at a time)")
    print(f"   3. Copy Claude's full JSON reply into claude_response.txt")
    print(f"   4. Run:  python save_claude_output.py")
    display(FileLink(fname))