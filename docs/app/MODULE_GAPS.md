# Module: Gap Analysis (what the client is missing, ranked)

Status: **built 2026-11-02** on `feat/research-gaps` (`src/modules/gaps/`). Step 5 of the roadmap in `MODULE_ANALYST_ACTION_CARDS.md`. Offline and $0: it joins what the other modules already stored (theme map, latest research snapshot, latest monitoring check, offering catalogs). A missing input is listed on the report, not an error.

## Findings

| Kind | When | Needs |
|---|---|---|
| `theme_gap` | A theme competitors cover and the client does not (ownership "client gap") | themes |
| `theme_behind` | A theme where a competitor leads | themes |
| `pillar_gap` | A theme the client covers without a pillar page while a competitor has one | themes |
| `funnel_gap` | A funnel stage (learn, compare, decide, onboard, support) competitors cover in a theme and the client does not | themes |
| `keyword_gap` | **Complete deficit**: keywords where a competitor is in the top `competitor_top` (default 10) and the client is not in its rankings at all, while every client website's list is complete (`WebsiteResearch.complete`: not cut at the pull's `keyword_limit`, and not fewer keywords than Semrush's overview counts; an imported export counts as complete). Detail: per competitor, keywords and pages ("coupa.com ranks for 120 of them with 25 page(s); rival.com for 60"), the client's coverage, and the theme's page-two count | research |
| `keyword_unverified` | **Probable gap**: as above, but a client list was cut (or the client has no search data): the detail says how much the data holds ("the top 200 of about 3,400 ranking keywords") and how to confirm | research |
| `keyword_behind` | **Page far behind**: the client ranks 21-100; evidence gives the client's position and page | research |
| `striking_distance` | Client keywords at positions 11-20 (page two), grouped by theme | research |
| `competitor_momentum` | A competitor adding pages in a theme (from monitoring's theme drift) | monitoring |
| `offering_gap` | A competitor catalog offering with no similar client offering (word overlap >= 0.5 or one inside the other); navigation and site sections (Next, Read more, About, Industries we serve…) and names over 6 words are ignored | catalogs |

Each finding has a title, a plain detail, a recommended action, its theme, competitors, monthly searches behind it (when research exists), and evidence items (pages, pillar pages, keywords with positions, changes, offerings, counts) with links.

**Settings** (`GapSettings`, `app_data/gaps/<project>/settings.json`, `GET/POST .../gaps/settings`, kept in each report): `min_volume` (keywords searched less a month are left out of keyword findings and page two; default 0) and `competitor_top` (1-20, default 10).

**Priority** (0-100) = base by kind (theme gap 50, keyword gap 45, theme behind 40, offering gap 40, momentum 35, keyword behind 35, striking distance 30, keyword unverified 30, competitor not in project 30, pillar gap 25, funnel gap 15) + up to 20 for competitor pages (4 each) + up to 30 for demand (6 x log10(searches + 1)). **Severity**: high from 70, medium from 45, otherwise low.

**Tracked to closure.** A finding's id is stable (`gap-` + hash of kind and key), so a status (open, accepted, done, dismissed) and a note recorded by the team carry over to every later build. Findings of the previous report that no longer appear are listed under "No longer a gap since last time".

## Where it is used

- **App**: the project's **Gaps** tab (build after unlocking, filter by kind, hide dismissed, evidence links, recommended next step, status per finding, resolved list, workbook).
- **CLI**: `gaps build | show | status --project <id> [--finding gap-… --set accepted|done|dismissed|open --note …]`.
- **API**: `GET/POST /api/projects/{id}/gaps`, `POST .../gaps/findings/{finding}/status`, `GET .../gaps/workbook`.
- **Action cards**: `get_gap_findings` (latest report or builds one; dismissed findings left out; each finding is a citable fact). Head-to-head gap, content brief and executive briefing cards may use it.
- **Storage**: `app_data/gaps/<project>/gaps-*.json`, `latest.json`, `statuses.json`, `<report>_gaps.xlsx` (Findings, Evidence, Resolved, How to read).

## Measured

John Hancock + Flynn's offline test project (no research yet): 15 findings in under a second, one theme gap (Xerox, security, firms: medium, priority 58) and 14 offering gaps, which is expected for two unrelated companies. Navigation items ("Next post…", "Industries We Serve") were reported as offerings until they were filtered; the latter now shows under resolved, which proved the tracking.

## Not done yet

- A multi-project playground screen (one project at a time for now).
- Ranking drops as findings (monitoring of research movers).
