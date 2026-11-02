# Module: Monitoring (changes, prices, theme drift, alerts)

Status: **built 2026-11-02** on `feat/monitoring` (`src/modules/monitoring/`). Step 4 of the roadmap in `MODULE_ANALYST_ACTION_CARDS.md`. A check is offline and $0: it reads the scrapes already made. Only the opt-in scheduled re-scrape contacts websites.

## What a check produces

| Part | What it is |
|---|---|
| Per website | Its latest scrape against the one that was latest at the previous check (first time: the scrape before); new, removed and changed pages; template changes; drift (share of pages that changed) |
| Per changed page | Title/description/H1/canonical rewrites (only when both scrapes have the field), lines added and removed, drift (share of its lines), price changes, new numbers, its theme, whether it is a pillar |
| Price changes | An amount (currency, or a percentage) in the same wording in both scrapes with a different value (`$25 per month` → `$19 per month`) |
| Template changes | A line added or removed on 3+ pages at once (navigation, "Read more", related-article links): reported once per website, not as a change of every page |
| Theme drift | The project's theme map is rebuilt (ids carried over); new pages are placed in their themes, removed pages in the previous check's map; ownership before and after |
| Alerts | high: competitor push into a client theme (>= push threshold new pages, default 3), ownership moving away from the client, competitor price change, client pages removed. medium: competitor building a theme the client lacks, competitor title/H1 rewrites, pillar pages changing >= 30%, client price change. low/info: competitor pages removed, client content changes, ownership moving towards the client |

## Automation

- **Auto-check** (on by default, local only): the app server's watcher (every 5 minutes) runs a check when a website of the project finished a new scrape since the last check.
- **Scheduled re-scrape** (off by default): `rescrape_every_days` (1-90) queues a scrape of every website when the last one is older than that. It contacts the websites (free tiers, polite as always), so it is an opt-in per project, set by someone who unlocked it.

## Where it is used

- **App**: the project's **Monitoring** tab (run a check after unlocking, alerts by severity, websites with expandable changes, prices, template changes, theme drift, watch settings, workbook).
- **CLI**: `monitor check | show | watch --project <id> [--auto-check on|off] [--rescrape-every-days N] [--push-threshold N]`.
- **API**: `GET/POST /api/projects/{id}/monitoring`, `GET .../monitoring/workbook`, `GET/POST .../monitoring/watch`.
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

- Ranking drops (needs the Research module).
- Notifications outside the app (email, chat).
- Structured-data and image changes per page.
