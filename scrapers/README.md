# Scrapers

Retail/restaurant news scrapers, migrated in from the `banner_news_article` project so they can be
triggered on demand from this repo's GitHub Actions tab.

## Scripts

| Script | Source | Notes |
|--------|--------|-------|
| `fetch_banner_store_news.py` | Google News RSS | Reads `analyst.csv` for store/keyword mapping |
| `daily_news_articles.py` | Google News RSS | `--mode opening` / `--mode closing`; multi-tier link decoding |
| `businessdebut_scraper.py` | BusinessDebut.com | Retail store announcements |
| `restaurant_scraper.py` | Restaurant listings | Selenium (headless Chrome) |
| `ct_scoop_scraper.py` + `ct_scoop_auto_extract.py` | CT Scoop | Selenium scrape, then Groq LLM extraction |
| `warn.py` | State WARN Act portals | Selenium + Playwright, state-specific handlers |
| `company_website_comingsoon.py` | 16 individual retailer sites | Selenium / Playwright / Patchright / requests mix |
| `bizjournals_scraper.py` | bizjournals.com | Playwright + stealth; exits if Cloudflare blocks it |
| `bankruptcy.py` | Google News RSS (bankruptcy edition) | Chapter 11 / Chapter 7 closings only |
| `sync_to_supabase.py` / `merge_results.py` | — | Shared post-processing, used by the workflows above |

## Running manually

Each scraper has its own workflow under `.github/workflows/scraper-*.yml`, trigger with
**Actions tab → pick workflow → Run workflow**. They are **manual-trigger only (no cron)** —
the original repo (`dhirajm1902/banner_news_article`) still runs these on schedule, so adding
schedules here too would double-write the same Supabase tables.

## Required repo secrets

Settings → Secrets and variables → Actions, on **this** repo (they don't carry over from the
other one):

- `SUPABASE_URL`, `SUPABASE_KEY` — used by `sync_to_supabase.py` (5 of 9 workflows: banner-news,
  daily-news, businessdebut, restaurant, ct-scoop). Points at the `zswfbeziqbtjmckniuxt` project
  (this repo's own Supabase) — **not** the original Banner project's DB. `SUPABASE_KEY` must be
  the `service_role` key, not the anon/publishable one. Run `supabase_schema.sql` once in that
  project's SQL editor before the first sync — the tables don't exist there by default.
- `GROQ_API_KEY` — used by `ct_scoop_auto_extract.py` (optional; step skips if unset)
- `GROQ_API_KEYS` — comma-separated, used by `restaurant_auto_extract.py`, rotated per batch and on rate limits (optional; step skips if unset)

`GITHUB_TOKEN` is provided automatically by Actions.
