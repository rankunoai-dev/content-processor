# Module: Research (keyword rankings, search demand, movers)

Status: **built 2026-11-02** on `feat/research-gaps` (`src/modules/research/`). Step 3 of the roadmap in `MODULE_ANALYST_ACTION_CARDS.md`. Two ways in: a **Semrush export import** ($0, works today) and a **Semrush API pull** (spends units; off until switched on with a monthly cap).

## Safety and cost

| Rule | How |
|---|---|
| Off by default | A pull needs `SEMRUSH_API_KEY`, `CP_SEMRUSH_ENABLED=true` and `CP_SEMRUSH_MONTHLY_UNITS > 0` (`.env`, never in chat). |
| Caps | Monthly cap (`CP_SEMRUSH_MONTHLY_UNITS`) and per-pull cap (`CP_SEMRUSH_PULL_UNITS`, default 5,000). Keywords per website: `CP_SEMRUSH_KEYWORD_LIMIT` (default 200). Database: `CP_SEMRUSH_DATABASE` (default `us`). |
| Checked before spending | The worst case of the whole pull (websites x (10 + limit x 10) + keywords x (10 + 100)) is compared with the pull cap and the units left this month before the first call; each call reserves its worst case again before it is sent. |
| Logged | Every call is written to `app_data/research/units.jsonl` with the units actually charged (lines returned x units per line: domain_ranks 10, domain_organic 10, phrase_this 10, phrase_organic 10, phrase_kdi 50; unknown reports 50). |
| Unlock | Pulling or importing needs the project's unlock token. |
| Client data | Only website addresses and keywords are sent to Semrush; no page content. |

No live Semrush call has been made yet: the client is tested against a simulated Semrush server.

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

## Where it is used

- **App**: the project's **Research** tab (import a CSV for a chosen website, pull with budget and worst-case estimate when switched on, websites with expandable keywords, theme demand, movers, who ranks for looked-up keywords, workbook).
- **CLI**: `research estimate | pull | import | show | movers | units --project <id> [--keyword ...] [--website ... --file ...]`.
- **API**: `GET /api/research/status`, `GET /api/projects/{id}/research`, `GET .../research/estimate?keywords=N`, `POST .../research/pull`, `POST .../research/import` (base64 body, 20 MB), `GET .../research/movers`, `GET .../research/workbook`.
- **Action cards**: `get_keyword_rankings`, `get_serp_overview`, `get_keyword_metrics`, `get_search_demand` are built. Cards 8-10 (v2) prefetch the search data, so even their free context packs carry it, and run once the project has research; without it, their check says "needs search data". Other cards that may use search data run without it and say so.
- **Storage**: `app_data/research/<project>/research-*.json`, `latest.json`, `<snapshot>_research.xlsx` (Websites, Keywords, Theme demand, Movers, Search results, How to read).

## Not done yet

- A live Semrush pull (waits for a key and a monthly cap).
- Backlinks, SERP features and keyword difficulty by API (phrase_kdi is priced but not called).
- Scheduled pulls.
