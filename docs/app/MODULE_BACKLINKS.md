# Module: Backlinks (Ahrefs: authority, referring domains, link intersect)

Status: **built 2026-11-26** on `feat/backlinks-ahrefs` (`src/modules/backlinks/`). Two ways in: an **Ahrefs export import** ($0, works today) and an **Ahrefs API v3 pull** (spends units; off until switched on with a monthly cap). It mirrors the Research module (`MODULE_RESEARCH.md`): the same budget, reservations, log and pacer classes (`research/semrush.py`), priced for Ahrefs.

## What it gives

For every website of a project (the client's and the competitors'): Domain Rating (DR), Ahrefs rank, backlink counts (live and all time), its referring domains (DR, dofollow links, links to the website, first and last seen, spam flag) and its pages with the most referring domains. Kept as a **snapshot** per pull or import, with insights worked out by code:

| Insight | How |
|---|---|
| Link intersect | Domains linking to **2 or more competitors and not to the client** (to the one competitor when the project has only one), strongest DR first, up to 200. Spam domains (Ahrefs' `is_spam`) and the project's own websites and their subdomains are left out. Needs the client's referring domains too, else a note says the client's own links could not be left out. |
| Authority gap | Each competitor's DR minus the client's; when either DR is unknown (an export carries none), their referring domain counts (live from the stats, else rows listed). |
| Client top pages | The client's 20 pages with the most referring domains. |
| Lost / new domains | Against the previous snapshot of the same website: domains gone (counted only when the new list was complete, or the domain was stronger than the weakest one listed) and domains new. |

Gap findings (`gaps/build.py::_backlink_findings`, wired through `build_findings(..., backlinks=)` and `gaps/service.py`):
- `link_intersect` (base 35, +4 per domain up to +20): one finding, the top domains as `link` evidence (label "domain (DR n): links to a.com, b.com").
- `authority_gap` (base 30): one per competitor at least **10 DR points** ahead (or, by referring domains, at least **twice** the client's), with the two figures as `count` evidence.

## Safety and cost

| Rule | How |
|---|---|
| Off by default | A pull needs `AHREFS_API_KEY`, `CP_AHREFS_ENABLED=true` and `CP_AHREFS_MONTHLY_UNITS > 0` (`.env`, never in chat). |
| Caps | Monthly cap (`CP_AHREFS_MONTHLY_UNITS`) and per-pull cap (`CP_AHREFS_PULL_UNITS`, default 10,000). Rows per website: `CP_AHREFS_REFDOMAINS_LIMIT` (default 500) and `CP_AHREFS_PAGES_LIMIT` (default 100). |
| Priced before sending | Every call reserves its worst case, **max(50, limit x the sum of its field costs)**, under the shared file lock (`units.lock`, `units.reserved.json`), and is refused when that would cross the pull cap or the month's room (the log plus every running pull's reservations). After the answer, the actual units (rows returned x field costs, at least 50) are logged and the rest of the reservation is let go. A refused or failed call is not charged. |
| Fair split | Websites in turn, client first; each may use an equal share of what is left. When the full lists do not fit its share, both lists shrink by the same proportion (never below 10 referring domains and 5 pages), and the website's note says what was asked. A pull is refused up front when a share could not pay for 220 units (rating, counts, 10 domains, 5 pages). |
| Paced | `AHREFS_PACER` (its own, separate from Semrush's): calls at least `CP_AHREFS_MIN_INTERVAL_MS` apart (default 500). HTTP 429 is retried twice after `Retry-After` (at most 60 s) or 2 s x attempt; the snapshot notes how often. |
| A website fails | Its previous data is kept (note "Kept from the last snapshot") and the others go on; the pull fails only when every website did. |
| Logged | `app_data/backlinks/units.jsonl` (endpoint, target, rows, units). |
| Unlock | Pulling or importing needs the project's unlock token. |
| Client data | Only website addresses are sent to Ahrefs. The key goes in the `Authorization: Bearer` header, never in the address. |
| Where the key goes | `CP_AHREFS_BASE_URL` (default `https://api.ahrefs.com/v3/`) accepts only that host over https, or 127.0.0.1 / localhost / ::1 (the stand-in); anything else refuses the pull before a call (`service.address_refusal`). A pull not from `api.ahrefs.com` carries the "DUMMY DATA" note. |

### Units per call (Ahrefs API v3)

Most fields cost 1 unit a row; `refdomains`, `dofollow_refdomains`, `refdomains_target`, `live_refdomains`, `all_time_refdomains` cost 5; `traffic_domain`, `traffic_page`, `traffic_source` cost 10 (none is asked). At least 50 units a request.

| Call | Fields | Per row | Worst case at the defaults |
|---|---|---|---|
| `site-explorer/domain-rating` | domain_rating, ahrefs_rank | 2 | 50 |
| `site-explorer/backlinks-stats` (`mode=subdomains`) | live, all_time, live_refdomains, all_time_refdomains | 12 | 50 |
| `site-explorer/referring-domains` (`history=live`, `order_by=domain_rating:desc`) | domain, domain_rating, dofollow_links, links_to_target, first_seen, last_seen, is_spam | 7 | 500 rows: 3,500 |
| `site-explorer/pages-by-backlinks` (`order_by=refdomains_target:desc`) | url_to, title_target, refdomains_target, links_to_target, url_rating_target, http_code_target | 10 | 100 rows: 1,000 |

So a website costs at most **4,600 units**, and a client with two competitors at most 13,800 (more than the default pull cap of 10,000: the lists then shrink fairly). Actual use is usually far lower, because a website with 80 referring domains returns 80 rows. Check the prices against your plan (`subscription-info/limits-and-usage` in Ahrefs shows what is left).

## Ahrefs exports ($0)

`POST .../backlinks/import` with `kind`, or `backlinks import --kind`. Columns are found by name (case and spacing aside); comma, tab or semicolon; UTF-8 or UTF-16 with or without a byte order mark (Ahrefs often exports UTF-16 with tabs).

| Export (Site Explorer) | `kind` | Columns read |
|---|---|---|
| Backlink profile -> Referring domains -> Export | `refdomains` | Domain (or Referring domain), Domain rating (or DR), Dofollow links, Links to target (or Backlinks, Links), First seen, Last seen, Is spam. Without a Dofollow column, all links count as dofollow. |
| Pages -> Best by links -> Export | `pages` | Page URL (or Target URL, URL), Title, Referring domains (or Ref. domains, RD), Links to target (or Backlinks), URL Rating (or UR), HTTP code. |

An export of the wrong kind is refused with a message saying which kind it is. An imported referring-domains list counts as complete (`refdomains_asked = 0`). Importing keeps the other websites and the other half of the same website from the latest snapshot.

## Where it lives

- Code: `ahrefs.py` (client, `AhrefsBudget`, prices), `importer.py`, `insights.py`, `models.py`, `store.py` (snapshots, `latest.json`, workbook), `service.py` (`BacklinksSettings.from_env`, `address_refusal`, `pull_project`, `import_csv`, `estimate_units`, `fair_limits`), `api.py`, `commands.py`, `standin.py`.
- Data: `<app data>/backlinks/<project>/backlinks-YYYYMMDD-HHMMSS-mmm.json`, `latest.json`, `<snapshot>_backlinks.xlsx` (sheets Websites, Referring domains, Top pages, Link intersect, Authority gap, How to read); deleted with the project.
- API: `GET /api/backlinks/status`; `GET /api/projects/{id}/backlinks`, `.../backlinks/estimate`, `.../backlinks/workbook`; `POST .../backlinks/pull` (unlock token, switch, caps; 409 with the reason when refused), `POST .../backlinks/import` (`{website, kind: refdomains|pages, file_name, content_base64}`, 20 MB at most; unlock token).
- CLI: `backlinks estimate|pull|import|show|units`, `backlinks-standin make|serve`.
- Claude Desktop / analyst: `get_backlinks` (module scope `research`): every website's DR, rank and counts, the authority gap, the link intersect and the client's top pages, each row with a `fact:backlinks-...` ledger ref; with `website`, that website's strongest referring domains.
- Web UI: the "Backlinks (Ahrefs)" card in the Research tab (`webui/src/routes/BacklinksCard.tsx`, `webui/src/api/backlinks.ts`): import (website + which export), pull with the unit estimate when switched on, websites table, authority gap, link intersect, the client's top pages and the workbook.

## Trying it without Ahrefs: the stand-in (dummy data, $0)

`standin.py` invents link data for a project's websites in Ahrefs' formats. Nothing is real; every pull from it says "DUMMY DATA". It is deterministic (the same project gives the same numbers); referring domains come from one pool of 260 invented websites so the same ones link to several competitors (competitors are made stronger than the client), and a later `--period` loses and gains a few domains.

```
python -m src.modules.content_processor.cli backlinks-standin make --project <id> --out dummy
python -m src.modules.content_processor.cli backlinks-standin serve --out dummy [--port 8791] [--slow-down-every 5]
```

`make` writes `dummy/backlinks-dataset.json` and, per website, `<domain>-ahrefs-refdomains.csv` (UTF-16, tabs, as Ahrefs exports it) and `<domain>-ahrefs-best-by-links.csv` (UTF-8, commas). `serve` answers the four endpoints on 127.0.0.1 only (any path prefix, `/v3/` included), honours `select`, `limit` and `order_by`, answers 401 without a `Bearer` key and, with `--slow-down-every n`, HTTP 429 on every n-th call. Point the app at it with `CP_AHREFS_BASE_URL=http://127.0.0.1:8791/v3/`, any `AHREFS_API_KEY`, `CP_AHREFS_ENABLED=true` and a monthly cap.

**Tried 26 Nov 2026** (temporary folder; a project with John Hancock and two competitors, MetLife and Prudential, no scrapes): worst case 13,800 units against the 10,000 pull cap, so the lists were shrunk fairly; the pull spent **2,216 units** in 12 calls (plus 2 answered 429 and retried); DR 37 against 56 and 53; **14 domains** in the link intersect (strongest DR 94 and 85); a top pages import (UTF-8) of the client on top; the gap report gave 3 findings (1 link intersect, priority 55; 2 authority gaps, 19 and 16 DR points, priority 34).

## Connecting the real Ahrefs

1. An Ahrefs plan with **API access** (API v3; Enterprise, or an API add-on). In Ahrefs: Account settings -> API keys -> create a key. The key is the account's: anyone holding it spends its units.
2. In `.env` (never in chat or a ticket):

```
AHREFS_API_KEY=<the key>
CP_AHREFS_ENABLED=true
CP_AHREFS_MONTHLY_UNITS=50000     # what this app may use a month (0 refuses every pull)
CP_AHREFS_PULL_UNITS=10000        # most one pull may use
CP_AHREFS_REFDOMAINS_LIMIT=500
CP_AHREFS_PAGES_LIMIT=100
CP_AHREFS_MIN_INTERVAL_MS=500
# CP_AHREFS_BASE_URL stays unset (https://api.ahrefs.com/v3/)
```

3. Restart the app; the Research tab's Backlinks card shows the month's units and the worst case of a pull. `backlinks estimate --project <id>` shows the same on the command line.

## Not done

- No live Ahrefs call has been made (no key). The field prices follow Ahrefs' published v3 rules; check them against the plan's usage page after the first pull.
- Not pulled: anchors, individual backlinks, broken backlinks, domain rating history, traffic fields (10 units a row).
- The link intersect works on the lists as pulled (strongest first, capped); domains past the cap are not compared.
