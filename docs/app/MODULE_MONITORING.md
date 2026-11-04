# Module: Monitoring (changes, prices, theme drift, alerts)

Status: **built 2026-11-02** on `feat/monitoring` (`src/modules/monitoring/`). Step 4 of the roadmap in `MODULE_ANALYST_ACTION_CARDS.md`. A check is offline and $0: it reads the scrapes already made. Only the opt-in scheduled re-scrape contacts websites.

## What a check produces

| Part | What it is |
|---|---|
| Per website | Its latest scrape against the one that was latest at the previous check (first time: the scrape before); new, removed and changed pages; template changes; drift (share of pages that changed) |
| Per changed page | Title/description/H1/canonical rewrites (only when both scrapes have the field), lines added and removed, drift (share of its lines), price changes, new numbers, its theme, whether it is a pillar; main-content words before and after; H2/H3 headings added and removed; terms new to the headings and H1 and terms dropped from them (acronyms and words of 4+ letters, common words left out); tables, pricing blocks (an amount with a period: per month, per user...), FAQ answers and forms added or removed |
| Price changes | An amount (currency, or a percentage) in the same wording in both scrapes with a different value (`$25 per month` → `$19 per month`) |
| Template changes | A line added or removed on 3+ pages at once (navigation, "Read more", related-article links): reported once per website, not as a change of every page |
| Theme drift | The project's theme map is rebuilt (ids carried over); new pages are placed in their themes, removed pages in the previous check's map; ownership before and after |
| Competitor moves | `competitor_content_growth` (a page grew by 500+ words; high on a pillar), `competitor_messaging_shift` (new heading terms with 2+ headings added or removed, or a new H1; high on a pillar), `competitor_new_tables` (new tables or pricing blocks; high when pricing) |
| Alerts | high: competitor push into a client theme (>= push threshold new pages, default 3), ownership moving away from the client, competitor price change, client pages removed. medium: competitor building a theme the client lacks, competitor title/H1 rewrites, pillar pages changing >= 30%, client price change. low/info: competitor pages removed, client content changes, ownership moving towards the client |

## Automation

- **Auto-check** (on by default, local only): the app server's watcher (every 5 minutes) runs a check when a website of the project finished a new scrape since the last check.
- **Scheduled re-scrape** (off by default): `rescrape_every_days` (1-90) queues a scrape of every website when the last one is older than that. It contacts the websites (free tiers, polite as always), so it is an opt-in per project, set by someone who unlocked it.
- **Smart recrawl** (`smart_recrawl`, on by default; `monitoring/recrawl.py`): a scheduled pass follows a **recrawl plan**, page by page: never captured -> check; **core** page -> check every pass; the website's sitemap `<lastmod>` later than our last capture -> check; last capture `other_every_days` (default 30) or more ago -> check; otherwise **carry forward** with no request ("sitemap: unchanged since the last capture" or "not a core page; next check by ..."). Core pages (up to `core_limit`, default 50, per website, in this order): `core_urls` pinned by a person, theme pillar pages, header-menu links (`<run>.site.json`), comparison and decision pages (funnel stage), addresses naming pricing/plans/compare/vs/solutions/products/platform/features/services, the home page and pages one level from it. Last capture times come from the latest finished run's `.records.jsonl` (a carried-forward page keeps its original capture time, so it comes due).
- **Sitemaps**: `robots.txt` `Sitemap:` lines, else `/sitemap.xml` and `/sitemap_index.xml`; sitemap indexes followed; only the website's own domain; gzip; at most 25 files, 15 MB each, 200,000 addresses; XML with a DTD or entities refused (stdlib parser). A refusal (401/403/429/503) is said as such ("the website refused the sitemap request"); the plan then relies on core pages and the interval. No browser or fingerprint tricks are used for sitemaps.
- **Carrying forward** (`--skip-fetch` on `cli dryrun`, `skip_fetch.json` in the run folder, `Baseline.plan_skips`): the fetch worker sends no request for a planned skip with a reusable capture and reuses it (`change_basis` `planned_skip;...`, capture time kept). Checked pages still get the conditional request (304 ends it), then bytes and visible-text comparison.
- **Plans** are kept (`plans/<id>.json`, `plans/latest.json`) for previews (`POST .../monitoring/recrawl`, reads sitemaps only), manual recrawls (`{"queue": true}`, queues one scrape per website with what is due; nothing queued when nothing is due) and scheduled passes; downloadable as the `recrawl` report.

## Where it is used

- **App**: the project's **Monitoring** tab (run a check after unlocking, alerts by severity, websites with expandable changes, prices, template changes, theme drift, watch settings, workbook).
- **CLI**: `monitor check | show | watch --project <id> [--auto-check on|off] [--rescrape-every-days N] [--push-threshold N]`.
- **API**: `GET/POST /api/projects/{id}/monitoring`, `GET .../monitoring/workbook`, `GET/POST .../monitoring/watch` (also `smart_recrawl`, `other_every_days`, `use_sitemaps`, `core_urls`), `GET/POST .../monitoring/recrawl` (latest plan; preview, or `{"queue": true}` to recrawl now; unlock token).
- **Action cards**: `get_monitoring_alerts` (latest check, or runs one); `get_changes` now uses the same diff engine and also returns template changes, prices and drift. Competitor Strategy Radar (v2) reads the alerts first; the refresh-queue and executive cards may use them.
- **Storage**: `app_data/monitoring/<project>/check-*.json`, `latest.json`, `watch.json`, `<check>_monitoring.xlsx` (Alerts, Websites, Changed pages, New and removed pages, Theme drift, How to read).

## Measured on real data

John Hancock test project, the earlier 107-page scrape against the 318-page one (offline): 2 s; 206 new pages placed in their themes (116 in fund notices, 53 monthly distributions, 30 earnings), 4 changed pages (a correction notice, an H1 rewrite, a form page gaining a video), 9 template lines (related-article titles, "Read more", episode navigation), no removed pages. Before the fixes listed below, the same comparison reported 100 changed pages and two false "removed" alerts.

## Edge cases handled (most found on that real data)

| Case | Handling |
|---|---|
| The engine cut pages into sections differently between versions | Pages compared line by line, ignoring case, spacing and punctuation |
| A field captured only in the newer scrape (title was missing before) | Not a rewrite: both values must exist and differ |
| Related-article links and "Read more" on every page | Template changes, reported once per website (3+ pages) |
| Tracking pixels, files, error and blocked pages | Not watched (stamp flags) |
| A page still on the site but now a duplicate or excluded | Not "removed": removed means absent from the newer scrape |
| Nothing scraped since the last check | Reported as unchanged; no repeated alerts |
| Only one scrape of a website | Kept as the baseline for the next check |
| Scrapes running | The watcher waits until they finish |
| No website has addresses when a re-scrape is due | Skipped quietly |
| A re-scrape is due again soon after one was queued | Waits a full interval since the last queued re-scrape |

## Not done yet

- Meaning-based drift (old against new page embeddings, "did it change topic or intent"): waits for Ollama and bge-m3.
- Sitemaps behind bot protection: the plan says the website refused them and works without dates (fetching them with a browser fingerprint is a separate decision).
- Pinning core pages in the app (the setting exists; set it by API for now).

- Ranking drops (needs the Research module).
- Notifications outside the app (email, chat).
- Structured-data and image changes per page.
