# Competitor lite scan (gaps without scraping a whole competitor website)

`src/modules/lite_scan/`. A full scrape of a 5,000-page competitor takes hours of polite requests. A lite scan reads the competitor's sitemaps, groups the addresses by section and scrapes a capped sample with plain downloads only; the rest of the sitemap is kept as an inventory that the theme map counts as "inferred" and that every gap finding names as its basis.

## Flow (`service.start_lite_scan`)

1. **Sitemap inventory.** `monitoring.recrawl.read_sitemaps` (robots `Sitemap:` lines, else `/sitemap.xml`, `/sitemap_index.xml`; indexes on the same website only; gzip; DTD/entities refused; at most 25 files, 15 MB, 200,000 addresses). `SitemapIndex.urls` now keeps every same-site `<loc>` (dated or not), first seen first. Files (PDF, images, feeds...) are dropped by `sample.is_page`.
2. **Sample (`sample.choose_sample`, no requests).** Universe = sitemap + the website's stored addresses + its home page + ranking URLs. In order, at most `CP_LITE_SCAN_MAX_PAGES` (150):
   - pinned pages (API body `pinned`);
   - ranking pages from the latest research snapshot (`rankings_for`: keyword rows with a URL on the website, by traffic share then keyword count), up to 40%;
   - core pages (`recrawl.core_pages` with no theme map: pricing/products/solutions/... words, home and one level down), up to 25%;
   - sections (`section_of`: folders up to two deep, numbers and ids folded, `/` for top-level pages), biggest first, in turns, up to `CP_LITE_SCAN_PER_SECTION` (3) each, shallowest pages first; room left goes to the section least sampled for its size, so the sample always fills.
   Deterministic: the same inputs give the same sample.
3. **Queue.** `ScrapeRunner.submit(urls=sample, lite={...})`: `RunState.scan="lite"`, `lite.json` in the run folder; batch scopes use `mode: SUPPLIED_URLS` (no embedded follow-ups) and `access_tier_ceiling: T2_IMPERSONATED_HTTP`; `subprocess_command` adds `--no-render --no-docs`. Cost ceiling stays $0. Report, stamp, catalog and index run as for any scrape, so the theme map reads it.
4. **Kept.** `LiteStore` (`<app data>/lite_scan/<project>/<site>.json`): `LiteInventory` (sitemap files and note, inventory, sections with pages/sampled, the sample with reasons, run id, cap). `basis()`: "based on 150 of about 5,000 pages (lite scan)"; with no sitemap: "based on a sample of N pages (no sitemap was readable)".

Client websites are refused (they get a full scrape). A second scan while a scrape of the website is queued or running is refused by the runner.

## How the other modules treat a lite scan

- **Theme map** (`infer.add_inferred`, called by `theming.service.build_project_themes`): the website whose map run is a lite run gets `ThemeWebsite.scan="lite"`, `inventory`, `basis`. Each unscraped sitemap page is placed by its address words against each theme's terms (a word pair counts 2; plurals folded); placed only when it matches and no other theme matches as well. Counted as `SiteCoverage.inferred`; **ownership is unchanged** (scraped pages only). `stats.inferred_pages`. Workbook: "<website>: inferred from sitemap" columns in the ownership matrix.
- **Gaps**: theme gap / behind details add "(and about N more in a sitemap, judged by their addresses)"; count evidence adds "+ about N in its sitemap"; `Finding.basis` names each lite-scanned competitor's basis; workbook column "Based on"; the Gaps tab shows it as "probable".
- **Monitoring**: a lite run is compared only with an earlier lite run of the same website (`check._site_diffs`); between two samples only pages in both are compared (no new/removed pages, note "lite scans: only pages in both samples are compared"); the first lite scan after full scrapes is kept as a baseline. Recrawl baselines (`recrawl._latest_finished`) ignore lite runs. A scheduled or manual recrawl of a website whose latest finished scrape is a lite scan repeats the lite scan (`monitoring.service.recrawl_project`).

## Settings

| Setting | Default | Range | Meaning |
|---|---|---|---|
| `CP_LITE_SCAN_MAX_PAGES` | 150 | 10-1000 | pages scraped per lite scan (about 3 minutes of polite requests) |
| `CP_LITE_SCAN_PER_SECTION` | 3 | 1-50 | pages per section in the first pass |

## API and app

- `GET /api/projects/{p}/sites/{s}/lite`: the latest scan (`LiteSummary`: inventory count, sampled, cap, basis, sitemap files and note, sections (first 200), sample, the run).
- `POST /api/projects/{p}/sites/{s}/lite` (unlock token; body `{"pinned": [...]}` optional): 401 locked, 409 client website / already queued / no address at all.
- App: on a competitor website's Scrape view, the **Lite scan** card (run, latest basis, sections table); a "Lite scan" tag in the scrape history; the gap card shows "Competitor coverage: ... treat this as probable".

## Not done yet

- The sample is not refreshed page by page between lite scans (a repeat lite scan re-reads the sitemap and draws the sample again; unchanged pages are still reused by the capture history).
- Inferred pages are placed by address words only; addresses with no words (ids, numbers) stay unplaced.
- "Full capture on demand" for one high-priority gap is the existing scrape: add the pages to the website and start a scrape.
