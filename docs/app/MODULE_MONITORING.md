# Module: Monitoring (changes, prices, theme drift, alerts)

Status: **built 2026-11-02** on `feat/monitoring` (`src/modules/monitoring/`). Step 4 of the roadmap in `MODULE_ANALYST_ACTION_CARDS.md`. A check is offline and $0: it reads the scrapes already made. Only the opt-in scheduled re-scrape contacts websites.

## What a check produces

| Part | What it is |
|---|---|
| Per website | Its latest scrape against the one that was latest at the previous check (first time: the scrape before); new, removed and changed pages; template changes; drift (share of pages that changed) |
| Per changed page | Title/description/H1/canonical rewrites (only when both scrapes have the field), lines added and removed, drift (share of its lines), price changes, new numbers, its theme, whether it is a pillar; main-content words before and after; H2/H3 headings added and removed; terms new to the headings and H1 and terms dropped from them (acronyms and words of 4+ letters, common words left out); tables, pricing blocks (an amount with a period: per month, per user...), FAQ answers and forms added or removed; **meaning** (old against new, 0 to 1, see below); **search-engine facts** (below) |
| Search-engine facts per page | From the page stamps, so every scrape has them, compared only when both scrapes read the stored HTML: structured-data types added and removed, a block that was readable and no longer is, the robots tag starting or stopping `noindex`, a canonical that pointed at the page itself and now points elsewhere, strong calls to action added and removed, and offers in the structured data whose price changed (listed with the price changes). **Images without alt text** (missing or a file name) come from the run's inventory (`<run>.inventory.jsonl`, made on demand by the SEO-issues tool or `cli inventory`), so they are compared only when both scrapes have one; otherwise the check says so |
| Rankings and links | The newest research snapshot (Research) and backlink snapshot (Backlinks) against the ones the previous check read: see "Rankings and links" below |
| Price changes | An amount (currency, or a percentage) in the same wording in both scrapes with a different value (`$25 per month` → `$19 per month`) |
| Template changes | A line added or removed on 3+ pages at once (navigation, "Read more", related-article links): reported once per website, not as a change of every page |
| Theme drift | The project's theme map is rebuilt (ids carried over); new pages are placed in their themes, removed pages in the previous check's map; ownership before and after |
| Competitor moves | `competitor_content_growth` (a page grew by 500+ words; high on a pillar), `competitor_messaging_shift` (new heading terms with 2+ headings added or removed, or a new H1; high on a pillar), `competitor_new_tables` (new tables or pricing blocks; high when pricing), `competitor_topic_drift` (meaning old vs new below `CP_MONITOR_TOPIC_DRIFT`, default 0.85; high on a pillar) |
| SEO and intent moves | `client_seo_regression` (client pages now noindex, canonical moved away, structured data removed or broken, 3+ more images without alt text; high for noindex, a moved canonical, or lost structured data on a pillar), `competitor_schema_added` (rich-result types: Product, Offer, FAQPage, Review, AggregateRating, HowTo, Service, SoftwareApplication, Course, Event, VideoObject, LocalBusiness, JobPosting, QAPage, AggregateOffer), `competitor_intent_shift` (a page with no strong call to action gaining one, "informing to selling"; or one call to action added on 3+ pages at once, reported once for the website) |
| Alerts | high: competitor push into a client theme (>= push threshold new pages, default 3), ownership moving away from the client, competitor price change, client pages removed. medium: competitor building a theme the client lacks, competitor title/H1 rewrites, pillar pages changing >= 30%, client price change. low/info: competitor pages removed, client content changes, ownership moving towards the client. Ranking, link, SEO and intent alerts as described in their rows |

## Rankings and links (Research and Backlinks)

Nothing is pulled by a check: it reads the snapshots the Research and Backlinks modules already pulled or imported (`monitoring/signals.py`). Like scrapes, a check compares the newest snapshot with the one the previous check read (without one: the snapshot before the newest); a snapshot already read is not compared again, so nothing is alerted twice. The check records what it compared (`signals.research_before/after`, `backlinks_before/after`, notes) and the workbook's "Data compared" sheet shows it.

| Alert | Rule |
|---|---|
| `ranking_drop` (client) | A keyword that was in the top 10 and is now below it (or gone), or that fell 5+ places while in the top 20. Keywords under the project's `rank_min_volume` (default 50 searches a month) are left out unless they are buyer searches (commercial or transactional intent). High when it left the top 10 from the top 3, or has 1,000+ searches, or is a buyer search; otherwise medium |
| `competitor_ranking_gain` | A competitor entering the top 3 for a keyword the client ranks (or ranked) for, same volume rule. High when the competitor is now above a client page that is in the top 10 |
| `backlinks_lost` (client) | 5+ referring domains gone, or any of DR 50+; spam domains left out; for a cut list, only domains the new list would have held (the Backlinks module's rule). High with 3+ strong domains or 20+ domains lost |
| `authority_shift` | Domain Rating moving 3+ points; a competitor overtaking the client is high; a client drop of 6+ is high |

**Ranking drops matched with page changes.** For each client drop, the check looks for what a scrape saw change on the page that ranked (removed, title or H1 rewritten, words cut, headings removed, topic changed, structured data removed, noindex), in this check's comparison or an earlier check's whose dates overlap the two snapshots' dates (`page_change`, e.g. "H1 rewritten, words 1,200 → 800"). When Google ranks another client page for the keyword now, it says so ("Google now ranks /x instead"). This is a lead for the analyst, not proof of the cause, and the alert says that.

| Case | Handling |
|---|---|
| No research or backlink data | A note; nothing else |
| Only one snapshot | Kept as the baseline for the next check |
| No new snapshot since the last check | "no new search data since the last check"; no repeated alerts |
| Snapshots from different Semrush databases (us / uk) | Not compared, with a note |
| A keyword missing from a list that was cut at a line limit | Marked "list cut: may rank lower" and never high |
| The ranking URL is different now | Named in the drop ("Google now ranks ... instead") |
| A page change long before the ranking moved | Not named: the scrape window must overlap the snapshots' window |
| A website added to or removed from the project between snapshots | Only websites in both are compared |
| An export compared with an API pull (backlinks) | Compared, with a note that their coverage may differ |
| A backlink list empty in the newer snapshot | Means "not pulled", never "every domain lost" |
| Snapshots made by the Semrush or Ahrefs stand-in | Every ranking and link alert starts with "[dummy data]", and the digest's subject says so |
| A check made before this upgrade (no signals) | The next check compares the two newest snapshots |

## Email digest

Off by default. In the watch settings a project chooses **high** alerts or **high and medium**, and up to 10 addresses. After every check (by hand, automatic, after a scheduled re-scrape) the new alerts at that level are sent in one message through the same mailer as the queue emails: the SMTP server in `.env` (`CP_SMTP_*`), else an `.eml` file in `<app data>/outbox/`. An alert already emailed in the last 30 days is not sent again. A send that fails goes to the outbox and never fails the check. The addresses are kept in `monitoring/<project>/digest.json`, never in `watch.json`; the app shows them partly hidden (`g***@rankuno.com`). A **confidential** project's digest holds only counts by kind and a link to the app: no titles, addresses or changed text. Project names are cleaned of line breaks before they go into a subject.

## Automation

- **Auto-check** (on by default, local only): the app server's watcher (every 5 minutes) runs a check when a website of the project finished a new scrape since the last check.
- **Scheduled re-scrape** (off by default): `rescrape_every_days` (1-90) queues a scrape of every website when the last one is older than that. It contacts the websites (free tiers, polite as always), so it is an opt-in per project, set by someone who unlocked it.
- **Smart recrawl** (`smart_recrawl`, on by default; `monitoring/recrawl.py`): a scheduled pass follows a **recrawl plan**, page by page: never captured -> check; **core** page -> check every pass; the website's sitemap `<lastmod>` later than our last capture -> check; last capture `other_every_days` (default 30) or more ago -> check; otherwise **carry forward** with no request ("sitemap: unchanged since the last capture" or "not a core page; next check by ..."). Core pages (up to `core_limit`, default 50, per website, in this order): `core_urls` pinned by a person, theme pillar pages, header-menu links (`<run>.site.json`), comparison and decision pages (funnel stage), addresses naming pricing/plans/compare/vs/solutions/products/platform/features/services, the home page and pages one level from it. Last capture times come from the latest finished run's `.records.jsonl` (a carried-forward page keeps its original capture time, so it comes due).
- **Sitemaps**: `robots.txt` `Sitemap:` lines, else `/sitemap.xml` and `/sitemap_index.xml`; sitemap indexes followed; only the website's own domain; gzip; at most 25 files, 15 MB each, 200,000 addresses; XML with a DTD or entities refused (stdlib parser). A refusal (401/403/429/503) is said as such ("the website refused the sitemap request"); the plan then relies on core pages and the interval. No browser or fingerprint tricks are used for sitemaps.
- **Carrying forward** (`--skip-fetch` on `cli dryrun`, `skip_fetch.json` in the run folder, `Baseline.plan_skips`): the fetch worker sends no request for a planned skip with a reusable capture and reuses it (`change_basis` `planned_skip;...`, capture time kept). Checked pages still get the conditional request (304 ends it), then bytes and visible-text comparison.
- **Pinned core pages** (`core_urls`): set in the watch settings of the Monitoring tab, one address per line. Only addresses on the project's own websites (client or competitor) are accepted; anything else is refused with the addresses named, and the same page typed twice is kept once.
- **Plans** are kept (`plans/<id>.json`, `plans/latest.json`) for previews (`POST .../monitoring/recrawl`, reads sitemaps only), manual recrawls (`{"queue": true}`, queues one scrape per website with what is due; nothing queued when nothing is due) and scheduled passes; downloadable as the `recrawl` report.

## Where it is used

- **App**: the project's **Monitoring** tab (run a check after unlocking, alerts by severity, websites with expandable changes, prices, template changes, theme drift, watch settings, workbook; **Download** the latest check and **Download history** of every check, each in Excel, CSV, Word, PDF or JSON, see MODULE_DOWNLOADS.md).
- **CLI**: `monitor check | show | watch --project <id> [--auto-check on|off] [--rescrape-every-days N] [--push-threshold N]`.
- **API**: `GET/POST /api/projects/{id}/monitoring`, `GET .../monitoring/workbook`, `GET/POST .../monitoring/watch` (also `smart_recrawl`, `other_every_days`, `use_sitemaps`, `core_urls`), `GET/POST .../monitoring/recrawl` (latest plan; preview, or `{"queue": true}` to recrawl now; unlock token).
- **Action cards**: `get_monitoring_alerts` (latest check, or runs one); `get_changes` now uses the same diff engine and also returns template changes, prices and drift. Competitor Strategy Radar (v2) reads the alerts first; the refresh-queue and executive cards may use them.
- **Storage**: `app_data/monitoring/<project>/check-*.json`, `latest.json`, `watch.json`, `digest.json`, `<check>_monitoring.xlsx` (Alerts, Websites, Changed pages, New and removed pages, Theme drift, Competitor moves, Price changes, Template changes, SEO changes, Rankings, Links, Data compared, How to read). The history download gains SEO regressions, ranking drops and competitor top-3 gains per check.

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

- Intent drift judged by a model (the deterministic rule above uses calls to action only).
- Sitemaps behind bot protection: the plan says the website refused them and works without dates (fetching them with a browser fingerprint is a separate decision).
- Chat notifications (Slack, Teams); email only.
- Images per page on every scrape: the inventory is still made on demand, so alt-text changes need it in both scrapes.
- Pulling fresh research or backlink data on a schedule: a check reads what the modules already hold (paid pulls stay a person's decision, with their caps).

## Lite-scanned competitors

See [MODULE_LITE_SCAN.md](MODULE_LITE_SCAN.md): a lite run is compared only with an earlier lite run (pages in both samples only); recrawl baselines ignore lite runs; a recrawl repeats the lite scan of a website followed by lite scans.
