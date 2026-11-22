# Module: Monitoring (changes, prices, theme drift, alerts)

Status: **built 2026-11-02** on `feat/monitoring` (`src/modules/monitoring/`). Step 4 of the roadmap in `MODULE_ANALYST_ACTION_CARDS.md`. A check is offline and $0: it reads the scrapes already made. Only the opt-in scheduled re-scrape contacts websites.

## What a check produces

| Part | What it is |
|---|---|
| Per website | Its latest scrape against the one that was latest at the previous check (first time: the scrape before); new, removed and changed pages; template changes; drift (share of pages that changed) |
| Per changed page | Title/description/H1/canonical rewrites (only when both scrapes have the field), lines added and removed, drift (share of its lines), price changes, new numbers, its theme, whether it is a pillar; main-content words before and after; H2/H3 headings added and removed; terms new to the headings and H1 and terms dropped from them (acronyms and words of 4+ letters, common words left out); tables, pricing blocks (an amount with a period: per month, per user...), FAQ answers and forms added or removed; **meaning** (old against new, 0 to 1, see below) |
| Price changes | An amount (currency, or a percentage) in the same wording in both scrapes with a different value (`$25 per month` → `$19 per month`) |
| Template changes | A line added or removed on 3+ pages at once (navigation, "Read more", related-article links): reported once per website, not as a change of every page |
| Theme drift | The project's theme map is rebuilt (ids carried over); new pages are placed in their themes, removed pages in the previous check's map; ownership before and after |
| Competitor moves | `competitor_content_growth` (a page grew by 500+ words; high on a pillar), `competitor_messaging_shift` (new heading terms with 2+ headings added or removed, or a new H1; high on a pillar), `competitor_new_tables` (new tables or pricing blocks; high when pricing), `competitor_topic_drift` (meaning old vs new below `CP_MONITOR_TOPIC_DRIFT`, default 0.85; high on a pillar) |
| Alerts | high: competitor push into a client theme (>= push threshold new pages, default 3), ownership moving away from the client, competitor price change, client pages removed. medium: competitor building a theme the client lacks, competitor title/H1 rewrites, pillar pages changing >= 30%, client price change. low/info: competitor pages removed, client content changes, ownership moving towards the client |

## Automation

- **Auto-check** (on by default, local only): the app server's watcher (every 5 minutes) runs a check when a website of the project finished a new scrape since the last check.
- **Scheduled re-scrape** (off by default): `rescrape_every_days` (1-90) queues a scrape of every website when the last one is older than that. It contacts the websites (free tiers, polite as always), so it is an opt-in per project, set by someone who unlocked it.
- **Smart recrawl** (`smart_recrawl`, on by default; `monitoring/recrawl.py`): a scheduled pass follows a **recrawl plan**, page by page: never captured -> check; **core** page -> check every pass; the website's sitemap `<lastmod>` later than our last capture -> check; last capture `other_every_days` (default 30) or more ago -> check; otherwise **carry forward** with no request ("sitemap: unchanged since the last capture" or "not a core page; next check by ..."). Core pages (up to `core_limit`, default 50, per website, in this order): `core_urls` pinned by a person, theme pillar pages, header-menu links (`<run>.site.json`), comparison and decision pages (funnel stage), addresses naming pricing/plans/compare/vs/solutions/products/platform/features/services, the home page and pages one level from it. Last capture times come from the latest finished run's `.records.jsonl` (a carried-forward page keeps its original capture time, so it comes due).
- **Sitemaps**: `robots.txt` `Sitemap:` lines, else `/sitemap.xml` and `/sitemap_index.xml`; sitemap indexes followed; only the website's own domain; gzip; at most 25 files, 15 MB each, 200,000 addresses; XML with a DTD or entities refused (stdlib parser). A refusal (401/403/429/503) is said as such ("the website refused the sitemap request"); the plan then relies on core pages and the interval. No browser or fingerprint tricks are used for sitemaps.
- **Carrying forward** (`--skip-fetch` on `cli dryrun`, `skip_fetch.json` in the run folder, `Baseline.plan_skips`): the fetch worker sends no request for a planned skip with a reusable capture and reuses it (`change_basis` `planned_skip;...`, capture time kept). Checked pages still get the conditional request (304 ends it), then bytes and visible-text comparison.
- **Plans** are kept (`plans/<id>.json`, `plans/latest.json`) for previews (`POST .../monitoring/recrawl`, reads sitemaps only), manual recrawls (`{"queue": true}`, queues one scrape per website with what is due; nothing queued when nothing is due) and scheduled passes; downloadable as the `recrawl` report.

## Where it is used

- **App**: the project's **Monitoring** tab (run a check after unlocking, alerts by severity, websites with expandable changes, prices, template changes, theme drift, watch settings, workbook; **Download** the latest check and **Download history** of every check, each in Excel, CSV, Word, PDF or JSON, see MODULE_DOWNLOADS.md).
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

## Topic drift (meaning, old against new)

Line drift counts reworded sentences as changes, so a page reworded from top to bottom scores 100% while still saying the same thing. `monitoring/meaning.py` compares **meaning**: each changed page's own lines before and after (template lines removed; **site furniture** removed: a line on 5+ pages or 2% of a scrape's pages, the rule of the semantic report; lines of fewer than 3 words left out; up to 200 lines) are embedded with the website's knowledge-base model, each version becomes the normalised mean of its line vectors, and the score is their cosine (0 to 1).

- **Model:** the one the website's index was built with (`access.embedder_for`), so a check loads nothing new: bge-m3 on a model server, or the CPU fallback model. A website indexed with the words-only stand-in gets no score and a note ("meaning not scored: ...") on its row; a failing model also gives a note, and the rest of the check finishes.
- **Why furniture goes first:** on johnhancock.com (272 pages, CPU model) pages from different parts of the site scored a median 0.96 against each other with the menus, footers and disclaimers kept, so no topic change could ever fall below 0.85. With the 239 furniture lines removed: 68 pairs from different sections (a topic change) all below 0.85 (median 0.38); 40 pages with three new paragraphs added all above (median 0.99); 38 pages against their own first half (doubled in size) median 0.96, 2 below.
- **Thin pages:** a version with fewer than 20 own words is not scored.
- **Cache:** line vectors by model and text hash in `<app data>/monitoring/meaning.sqlite`, so each check embeds only new lines.
- **Alert:** `competitor_topic_drift` for competitor pages below `CP_MONITOR_TOPIC_DRIFT` (0.85), lowest first, with the page's new heading terms; high when a pillar page moved. `CP_MONITOR_MEANING=false` turns scoring off. Client pages show their score without an alert.
- **Shown:** a "Same topic 0.97" / "Topic changed 0.62" tag on each changed page in the Monitoring tab; a "Meaning (old vs new)" column in the workbook's changed pages and the model on each website's row; `topic_drift_pages` in the check's stats.

## Not done yet

- Intent drift (a page moving from informing to selling) beyond topic: needs labels from a local model.
- Sitemaps behind bot protection: the plan says the website refused them and works without dates (fetching them with a browser fingerprint is a separate decision).
- Pinning core pages in the app (the setting exists; set it by API for now).

- Ranking drops (needs the Research module).
- Notifications outside the app (email, chat).
- Structured-data and image changes per page.

## Lite-scanned competitors

See [MODULE_LITE_SCAN.md](MODULE_LITE_SCAN.md): a lite run is compared only with an earlier lite run (pages in both samples only); recrawl baselines ignore lite runs; a recrawl repeats the lite scan of a website followed by lite scans.
