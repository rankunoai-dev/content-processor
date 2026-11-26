# Module: Research (keyword rankings, search demand, movers)

Status: **built 2026-11-02** on `feat/research-gaps` (`src/modules/research/`). Step 3 of the roadmap in `MODULE_ANALYST_ACTION_CARDS.md`. Two ways in: a **Semrush export import** ($0, works today) and a **Semrush API pull** (spends units; off until switched on with a monthly cap).

## Safety and cost

| Rule | How |
|---|---|
| Off by default | A pull needs `SEMRUSH_API_KEY`, `CP_SEMRUSH_ENABLED=true` and `CP_SEMRUSH_MONTHLY_UNITS > 0` (`.env`, never in chat). |
| Caps | Monthly cap (`CP_SEMRUSH_MONTHLY_UNITS`) and per-pull cap (`CP_SEMRUSH_PULL_UNITS`, default 5,000). Keywords per website: `CP_SEMRUSH_KEYWORD_LIMIT` (default 200). Database: `CP_SEMRUSH_DATABASE` (default `us`). |
| Checked before spending | Each call reserves its worst case (lines x units per line) before it is sent and is refused when that would cross the pull cap or the units left this month. A pull is refused up front only when, after the chosen keywords and the competitor report, a website's share could not pay for its overview and 10 keywords. |
| Fair split | `build.pull_snapshot`: websites in turn, client first; each may use an equal share of what is left (`fair_lines`), so the first website cannot use up the others' units and what one does not use passes on. Keyword lines = min(`CP_SEMRUSH_KEYWORD_LIMIT`, (share - 10) / 10); the lines asked are kept per website (`keyword_limit`), so a cut list counts as incomplete in the gap analysis. A note lists the keywords asked per website when the split cut any. |
| Paced | One `Pacer` (`SHARED_PACER`) for every pull of the app server: calls at least `CP_SEMRUSH_MIN_INTERVAL_MS` apart (default 500: two a second). HTTP 429 is retried twice after `Retry-After` (at most 60 s) or 2 s x attempt; a refused call is not charged; the snapshot notes how often Semrush asked to slow down. |
| Fresh data reused | A website pulled from Semrush less than `CP_SEMRUSH_FRESH_DAYS` ago (default 14; 0 = never) is copied from the latest snapshot instead of pulled ("reused from the pull of ..."); the competitor report likewise. `POST .../research/pull` with `"refresh": true` (the tab's checkbox) pulls everything again. |
| Logged | Every call is written to `app_data/research/units.jsonl` with the units actually charged (lines returned x units per line: domain_ranks 10, domain_organic 10, phrase_this 10, phrase_organic 10, phrase_kdi 50; unknown reports 50). |
| Unlock | Pulling or importing needs the project's unlock token. |
| Client data | Only website addresses and keywords are sent to Semrush; no page content. |
| Where the key goes | `CP_SEMRUSH_BASE_URL` (default `https://api.semrush.com/`) accepts only that address or one on this computer (the stand-in); anything else refuses the pull before a call (`service.address_refusal`). A pull from the stand-in carries a "DUMMY DATA" note in its snapshot, the tab and the workbook. |

No live Semrush call has been made yet. The whole module has run end to end against the
**stand-in** below (26 Nov 2026, John Hancock sandbox with two competitors).

## Intent, the page report and Search Console ($0, nothing fetched)

- **Intent** (`research/intent.py`): every keyword is tagged informational, navigational,
  commercial or transactional by rules on its words, while the snapshot is finished (the
  project's domains and names count as brands; a short head term with no cue is
  commercial). Demand per theme gives searches per intent and a weighted total
  (transactional 1.2, commercial 1, informational 0.5, navigational 0.4); keyword gap
  findings say their mix and rank buyer searches higher.
- **Search Console** (`research/gsc.py`): `POST .../research/gsc/import` (or
  `research gsc-import`) reads the Performance export: the zip (`Queries.csv`,
  `Pages.csv`, `Filters.csv`) or one CSV; a table with both a query and a page column is
  read as pairs. Kept as `gsc-latest.json` (one per project). **CTR opportunities**: a
  page-one page or query with 200+ impressions whose CTR is under 60% of the usual for
  its position (the curve of `build.CTR`, between steps); clicks to gain = impressions x
  (usual - real). Google sign-in (the API) is not built; the export is.
- **Page report** (`research/pages.py`, `links.py`, `page_models.py`): for the client's
  website, per page: keywords, searches, estimated visits, average and best position, top
  keyword, commercial searches, links from page text and from menus only, internal
  PageRank (body links, damping 0.85, strongest = 100), words, on-page flags from the
  inventory, Search Console clicks and impressions. Lists: cannibalisation, pages that rank
  for nothing, pages with no or one link from page text, pages stuck at 4-20 with
  problems, link suggestions (same theme, stronger, not linking yet; anchor = the target's
  keyword), CTR opportunities. Built on demand and saved as `pages-latest.json` with
  `pages_report.xlsx`; rebuilt when the research snapshot or the Search Console import
  changes (`GET .../research/pages`, `.../pages/workbook`, `research pages`); the gap
  analysis builds it and turns it into findings. Claude Desktop: `get_page_report`
  (section by section). **Links only count between scraped pages**: on a scrape of a URL
  list (John Hancock: 303 addresses) most pages show no link from page text because the
  pages linking to them were not scraped; the report and its findings say so.
- **Importer**: two rows of one keyword with different pages of the same website are kept
  (the best-placed page is the keyword's page, the others `other_urls`) instead of the
  second being dropped.

## Keyword cannibalisation plan ($0, `research/cannibal.py`)

The rules of the keyword-cannibalisation playbook (v1.0, first proven on a 200-URL B2B
site), applied to data the app already holds instead of Screaming Frog exports and a
hand-written topic map. Built with the page report (`PageReport.plan`).

| Playbook input | Here |
|---|---|
| Topic map and owner pages | The theme map: a search's topic is the theme sharing most of its words; the owner is the client's pillar page of that theme |
| Page types | The page classifier and the theme map (business, article, utility; `/author/`, `/tag/`, sitemap, search and paged listings are utility) |
| Search Console page x query | `Queries by page` pairs of the Search Console import; when they cover under half the queries, the Semrush rows (estimated visits as weight, `other_urls` as second pages) |
| Screaming Frog inlinks | The scrape inventory's links in page text (`ui_region` not menu, header, footer, breadcrumb, cookie banner or menu panel), with anchors and image alt |
| Stored HTML paragraphs | The knowledge chunks of the run (`.chunks.jsonl`), lines of 8+ words |

- **Cleaning:** branded searches (the project's domains and names), searches beyond
  position 20, and likely automated searches (0 clicks, 200+ impressions, 4+ pages of the
  site) are set aside and counted.
- **Funnel:** a question first word or learning wording is TOFU; a model number or buying
  wording BOFU; comparison or a short head term MOFU.
- **Status per search:** aligned (owner), also aligned (TOFU or MOFU on an article of the
  same theme; a page of the theme deeper than the owner), misaligned, no page yet (theme
  without a client page), unplaced (no theme). A model number's owner is the page whose
  address carries it.
- **Page alignment:** aligned weight / top-20 weight; strong 95%+, within tolerance 90-95%,
  needs work under 90%; scored from 50 impressions (or visits).
- **Competing pairs** (misaligned searches on needs-work pages, per page and owner), each
  with one pattern: remove from index, merge and redirect (articles sharing 4+ and half
  their paragraphs), localise, point up to parent, hand off to child (owner under the page,
  or a model search on its series page), separate by intent, answer then link,
  differentiate siblings. Priority High from 25% of the page's weight or 500, Medium from
  10%.
- **Links:** repoint (anchor holds the words of one of the pair's searches but points to the
  competing page), fix anchor (generic text or image without alt to a page of a pair,
  unless the page has a descriptive link there), add link (competing page to owner, only
  when its text holds a search's words; 3 per page at most).
- **Template text:** paragraphs on 5+ pages are left out of shared-content and mention
  checks, and counted.
- **Outputs:** gap findings (one `cannibalisation` finding per pair with its pattern and
  changes), the page report workbook (Plan summary, Consolidation, Link changes, Technical,
  Page alignment, Search detail before the page sheets), `get_page_report` section
  `consolidation`, the Research tab (Cannibalisation plan and Page alignment tabs), and
  the action card `keyword-cannibalisation`, where Claude checks owners and writes the
  copy under the playbook's rules (quote real text, final copy, titles 60 characters or
  fewer, descriptions 145-155 on two lines, change tags only when they pull another page's
  searches, 3 links per page, template text once, no technical hygiene).
- **Not taken from the playbook:** the weekly and last-year Search Console windows, the rank
  tracker file (its keywords look like cannibalisation without it), the near-duplicate
  export (paragraph overlap replaces it), and the written-copy JSON files (the action card
  writes copy instead).

## Trying it without Semrush: the stand-in (dummy data, $0)

`src/modules/research/standin.py` invents search data for a project's websites in Semrush's
own formats, so every part of the module can be seen working before a subscription is
connected. Nothing it makes is real; every snapshot pulled from it says so.

```
python -m src.modules.content_processor.cli research-standin make --project <id> --out dummy [--market insurance|procurement|generic] [--period 1]
python -m src.modules.content_processor.cli research-standin serve --out dummy [--port 8790] [--slow-down-every 7]
```

- `make` also writes the client's **Search Console export** (`<domain>-search-console.zip`:
  `Queries.csv`, `Pages.csv`, `Filters.csv`, and `Queries by page.csv` for keywords two
  pages share) and gives about 15% of the client's keywords a second page (cannibalisation).
- `make` writes `dataset.json` and, per website, the **Organic Positions** export
  (`<domain>-organic-positions.csv`) and, for the client, the **Organic Competitors** export,
  with Semrush's column names. Keywords: three **brand keywords** from each theme of the
  project's theme map, ranking on that theme's real pages, and a **market list** where the
  competitors are made stronger (so gaps appear); the client ranks with its real pages only.
  Volumes use Semrush's volume steps. `--period 1` is a later month (about half the
  positions move), for movers. Deterministic: the same project gives the same numbers.
- `serve` answers the five API reports the module calls (`domain_ranks`, `domain_organic`,
  `phrase_this`, `phrase_organic`, `domain_organic_organic`) on 127.0.0.1 only, as the API
  does: `;`-separated text, header names from `export_columns`, `display_limit`,
  `ERROR 120 :: WRONG KEY - ID PAIR` without a key, `ERROR 50 :: NOTHING FOUND` for an
  unknown domain. `--slow-down-every n` answers every n-th call with HTTP 429.
- Route A (exports): import the CSV files in the Research tab or with `research import`.
  Route B (API): set `CP_SEMRUSH_ENABLED=true`, any `SEMRUSH_API_KEY`, a monthly cap and
  `CP_SEMRUSH_BASE_URL=http://127.0.0.1:8790/`, then pull. Use a copy of the project's data
  (or a test project) so dummy numbers never sit in a real project's research.

What the trial showed (John Hancock sandbox, MetLife and Prudential added, no competitor
scrapes): both routes fill the Research tab, demand per theme, movers, the workbook, the
gap analysis (keyword gaps, page-two keywords, unwatched competitors), the keyword cards'
checks and packs, and the four Claude Desktop search-data tools. The API route adds domain
overviews, the top 10 of chosen keywords and the competitor report in one go; the worst case
(7,160 units) was above the 5,000-unit pull cap, so the fair split asked 128 / 172 / 200
keywords and the pull spent 2,090 units; two HTTP 429 answers were waited out. An API pull
counts as cut short whenever Semrush says the website ranks for more keywords than were
pulled, so its keyword gaps are **probable**, while an imported export counts as complete
(**complete deficit**). The keyword-value-matrix card refuses until every competitor in the
project has a scrape.

## Connecting the real Semrush API

1. **Plan and key.** API access needs a plan that includes it and API units (to our
   knowledge the Business plan, with units bought separately; confirm with Semrush). The
   key is shown in Semrush's subscription settings, next to the API units balance.
2. **`.env`** (never in chat or in a file that is committed):
   `SEMRUSH_API_KEY=...`, `CP_SEMRUSH_ENABLED=true`, `CP_SEMRUSH_MONTHLY_UNITS=` the units
   you allow per month, `CP_SEMRUSH_PULL_UNITS=` the most one pull may use,
   `CP_SEMRUSH_DATABASE=us` (or the country), `CP_SEMRUSH_KEYWORD_LIMIT=` keywords per
   website. Leave `CP_SEMRUSH_BASE_URL` unset (the real address is the default).
3. **Restart the app**, then `research units`: `enabled` and `api_key_set` should be true
   (the key itself is never shown).
4. **First pull small**: `CP_SEMRUSH_PULL_UNITS=1000`, `CP_SEMRUSH_KEYWORD_LIMIT=50`;
   `research estimate --project <id>` shows the worst case; pull from the Research tab
   (needs the project's unlock). Compare `research units` with the balance Semrush shows,
   and check `UNITS_PER_LINE` in `semrush.py` against your plan's prices.
5. **The client's full keyword list**: export its Organic Positions from the Semrush
   website and import it (free, counts as complete); use the API for competitors, chosen
   keywords' top 10 and monthly refreshes (reused for 14 days).

The Semrush connector in Claude (claude.ai) is a different path: it lets Claude read
Semrush directly in a chat. The app does not use it; the app needs the Analytics API key.

## What a snapshot holds

| Part | What it is |
|---|---|
| Websites | For the client's websites and each competitor: source (pull or import), overview (organic keywords, traffic, traffic value, rank; pull only), keywords with position, previous position, searches per month, CPC, competition, difficulty, ranking URL, traffic share, theme |
| Keywords looked up | Searches, CPC, competition, number of results and the top 10 results (pull only, up to 50 keywords) |
| Theme demand | For each theme of the project's theme map: keywords, total monthly searches, keywords in the top 10 and ranked per website, estimated visits per website (CTR curve: 1st 28%, 2nd 15%, 3rd 10% … 10th 1.8%, 11-20th 1%) |
| Movers | Against the previous snapshot: keywords that moved 3+ places, crossed into or out of the top 10, or appeared or disappeared |

Keywords are placed in a theme by their ranking URL when the page is in the theme map, otherwise by sharing at least half their words with the theme's terms.

An import replaces only that website's keywords; the other websites of the previous snapshot are kept.

## Import format

Semrush **Organic Research → Positions** export (CSV, `,` or `;`, with or without BOM). Required columns: Keyword, Position. Read when present: Previous position, Search Volume, Keyword Difficulty, CPC, URL, Traffic (%), Competition. Blank and duplicate keywords are skipped.

## Suggested competitors

`research/competitors.py`. Competitors the search data reveals are kept per project in `app_data/research/<project>/competitors.json` (`CompetitorBook` of `CompetitorSuggestion`: domain, status suggested/added/dismissed, sources, relevance 0-1, common keywords, organic keywords and traffic, search results count, example keywords, first/last seen, times seen, decided at, site id).

- **Sources**: `domain_organic_organic` for the client's main website in every pull (20 lines, priced 40 units a line, in the worst-case estimate; a failure leaves a note and keeps the keyword pull); a Semrush "Organic Competitors" CSV export (`POST .../research/competitors/import`, detected by a Domain column plus Common Keywords or Competitor Relevance; the positions import says where to put it instead); the top 10 of the snapshot's pulled keywords (`from_search_results`, on every saved snapshot).
- **Merge**: figures refreshed (overlap figures only from Semrush competitor rows), sources and example keywords added up, `times_seen` counted, decisions kept. Read against the project as it is now (`with_project_status`): the client's websites (domain overlap, so subdomains too) are dropped, competitors already in the project are `added` with their site id, and an added one whose website was removed is suggested again.
- **Decisions** (unlock token): `POST .../competitors/{domain}/add` adds a competitor website named after the domain with a note saying where it was found (409 when already in), `/dismiss`, `/restore`.
- **Context**: gap finding `unwatched_competitor` (one finding listing suggested competitors with relevance >= 0.3, >= 50 common keywords, or in the top 10 for >= 2 pulled keywords); analyst/MCP tool `get_competitor_suggestions` (research module; each with a fact ref and `in_project`).
- **Project creation**: the New project form asks once when no competitor is named ("Add a competitor" / "Create without competitors").

## Where it is used

- **App**: the project's **Research** tab (import a CSV for a chosen website, pull with budget and worst-case estimate when switched on, suggested competitors with add / dismiss and a competitors-export upload, websites with expandable keywords, theme demand, movers, who ranks for looked-up keywords, workbook).
- **CLI**: `research estimate | pull | import | show | movers | units | pages | gsc-import --project <id> [--keyword ...] [--website ... --file ...]`; `research-standin make | serve` (dummy data, above).
- **API**: `GET /api/research/status`, `GET /api/projects/{id}/research`, `GET .../research/estimate?keywords=N`, `POST .../research/pull`, `POST .../research/import` (base64 body, 20 MB), `GET .../research/movers`, `GET .../research/workbook`, `GET .../research/competitors`, `POST .../research/competitors/import`, `POST .../research/competitors/{domain}/add|dismiss|restore`.
- **Action cards**: `get_keyword_rankings`, `get_serp_overview`, `get_keyword_metrics`, `get_search_demand` are built. Cards 8-10 (v2) prefetch the search data, so even their free context packs carry it, and run once the project has research; without it, their check says "needs search data". Other cards that may use search data run without it and say so.
- **Storage**: `app_data/research/<project>/research-*.json`, `latest.json`, `<snapshot>_research.xlsx` (Websites, Keywords, Theme demand, Movers, Search results, How to read).

## Not done yet

- A live Semrush pull (waits for a key and a monthly cap).
- Backlinks, SERP features and keyword difficulty by API (phrase_kdi is priced but not called).
- Scheduled pulls.
