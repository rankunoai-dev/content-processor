# Analyst UI plan (Lane L8)

Replaces the I1 static explorer page as the main way to read a run. Built for an
SEO or content analyst who has never seen the engine's internals: every screen
answers "what did we get from this page, what is missing, and what should I do".

## 1. Who it is for and what they ask

| The analyst asks | Where it is answered |
|---|---|
| How did the run go overall? | Run overview |
| Which pages are fully captured, which are not, and why? | Pages table, filtered by capture status |
| What exactly did we capture from this page, and where on the page was it? | Page detail, "Content" tab |
| What is missing from this page and what can I do about it? | Page detail, "Issues" tab |
| Which pages repeat each other? | Duplicate content |
| Where do our pages contradict each other? | Contradictions |
| What needs a person to look at it? | Needs your review |
| Give me the spreadsheet. | Downloads |

Technical names (`NEEDS_REVIEW`, `UNVISITED_STATE`, `collapsed_published`) never
appear as primary text. Each has a plain name, a one-line explanation, and, for
problems, what the analyst can do. A "Show technical names" switch in settings
shows the engine name next to the plain one, for engineers.

## 2. Plain-language vocabulary (single source: `webui/src/vocabulary.ts`)

### Capture status (the URL badge)

| Engine | Shown as | Colour | One-line meaning |
|---|---|---|---|
| VERIFIED_WITHIN_SCOPE | Fully captured | green | Everything we were allowed to open on this page was captured and checked against the live page. |
| PARTIAL | Partly captured | gold | We got some of the page; part of it could not be opened or read. See Issues. |
| NEEDS_REVIEW | Needs a check | blue | We captured the page but something looks unusual; a person should confirm it. |
| BLOCKED | Site blocked us | red | The website refused automated access (bot protection or login). |
| FAILED | Couldn't load | red | The page did not load after retries (timeout, server error, rate limit). |
| EXCLUDED | Skipped by rule | grey | Not captured on purpose: out of scope, robots.txt, or a file type we do not process. |
| RUNNING | In progress | processing | Still being captured. |

### Issue types (GapType): title, why it matters, what to do

| Engine | Title | What to do (short) |
|---|---|---|
| ACCESS_BLOCKED | Blocked by the website | Retry later or ask for an allow-listed access route. |
| ACQUISITION_FAILED | Page didn't load | Retry; check the URL in a browser. |
| UNSUPPORTED_FORMAT | File type we can't read yet | Content is listed but its text is missing (for example images without OCR). |
| BUDGET_STOPPED | Stopped at the run's limit | Re-run with a higher limit if this page matters. |
| DISCOVERY_OPEN | Some page sections weren't opened | Parts behind clicks may be missing. |
| FIDELITY_DISAGREEMENT | Live page differs from its source | Check the page by eye. |
| LOW_CONFIDENCE | Captured, but not confirmed | Spot-check the text. |
| UNVISITED_STATE | A tab, menu or button wasn't opened | Its hidden content may be missing; the reason says why. |
| LIMIT_HIT | Too large, cut off | Only the first part was captured. |
| POLICY_EXCLUDED | Skipped by rule | Nothing to do unless the rule should change. |
| SHADOW_BLOCK_SUSPECTED | May be showing us a different page | Possible bot-detection page; verify by eye. |
| DEGRADED_VARIANT | Looks like a placeholder or reduced page | Content may be a template, not the real page. |
| VOLATILE | Changes every visit | Counters, dates or prices that move; compare with care. |
| SOFT_REDIRECT_SUSPECTED | Possibly redirected elsewhere | The page may be a different page than requested. |
| SOFT_404_SUSPECTED | Looks like a "page not found" | The page may no longer exist. |

Gap `reason` strings are machine text (`unvisited_tab:policy_disallows:hover_info:Pricing`);
`vocabulary.ts` turns the known patterns into sentences ("The 'Pricing' tab wasn't
opened because hovering is turned off for this run") and shows the raw reason only
under "technical details".

### Where content was found (layer and page area)

Layer (how the text was shown): rendered_visible "Visible on the page";
collapsed_published "Behind a click (FAQ, accordion, tab)"; hidden "Hidden on the
page"; accessibility_only "For screen readers only"; metadata "Page metadata
(title, description)"; source_only "In the page code, not shown"; third_party_chrome
"Third-party widget"; ugc "User comments and reviews"; icon_artifact "Icon text";
paywalled_in_source "Behind a paywall"; uncertain "Unclear"; hidden_revision
"Earlier version (tracked change)"; design_evidence "Design reference".

Page area (UiRegion): main_content "Main content", header "Header", hero "Top
banner", nav_menu "Navigation menu", hamburger_menu "Mobile menu", mega_menu
"Mega menu", breadcrumb "Breadcrumb", sidebar "Sidebar", footer "Footer", modal
"Pop-up", popover "Pop-over", tooltip "Tooltip", accordion_panel "FAQ or accordion",
tab_panel "Tab", carousel "Slider", card "Card", snippet "Snippet", palette "Colour
palette", search "Search", form "Form", table "Table", cookie_banner "Cookie notice",
chat_widget "Chat widget", ad_slot "Ad", document_metadata "Document properties",
page_metadata "Page metadata", pdf_page "PDF page", pdf_table "PDF table", pdf_form
"PDF form", pdf_annotation "PDF comment", image_text "Text in an image", unknown "Other".

Source format: html "Web page", pdf "PDF", image "Image", office_doc "Word document",
spreadsheet "Spreadsheet", presentation "Slides", text/json/xml "Text file",
audio "Audio", video "Video", other "Other".

Access tier: T1 "Standard request", T2 "Browser-like request", T3 "Real browser";
paid tiers "Proxy (paid)". Change status: new "First capture", changed "Changed since
last run", unchanged "Unchanged (reused)", unknown "Unknown".

## 3. Screens and navigation

Ant Design `Layout` with a left `Menu` (collapsible) and a run picker in the header.
Routes (React Router; filters live in the URL so a link reproduces the view):

1. **Runs** `/` - one card per run: name/date, sites, number of pages, a stacked bar
   of capture status, "x% fully captured", issues count. Empty state explains how to
   produce a run (`dryrun` command) and where to point the server.
2. **Run overview** `/runs/:runId` - Statistic cards (Pages, Fully captured, Partly
   captured, Needs a check, Blocked, Couldn't load, Skipped), each clickable to the
   filtered Pages table. A donut of capture status. "Top reasons pages aren't fully
   captured": a ranked list of issue titles with page counts and the what-to-do line.
   "Content found": pages by source format. "Changes since last run" when present.
3. **Pages** `/runs/:runId/pages` - Ant `Table`, server-side paging, sorting and
   filtering (5,000+ rows). Columns: Page (title or path, full URL on hover, host as a
   tag), Capture status (tag + tooltip), Words, Issues, Content type, Site last
   updated, Captured on, Change. Toolbar: status `Segmented` with counts, site
   `Select`, content type, "Only pages with issues" switch, debounced search over URL
   and page text. Row click opens Page detail. Column chooser and density persisted.
4. **Page detail** `/runs/:runId/pages/:pageKey` - header: status tag, title, URL
   (copy button, open-in-new-tab), and one plain sentence summarising the outcome.
   Tabs:
   - **Content**: blocks grouped by page area in reading order, under their headings
     (`heading_path`). Each block shows text plus small tags: page area, how it was
     shown (layer), format, a check icon when verified. Filters: page areas
     (main content selected by default; menus, header, footer, cookie notice off),
     "Include hidden text", search within the page, "Copy all visible text".
     Long pages virtualised.
   - **Issues** (badge with count): one Ant `Alert`/card per gap: title, sentence,
     what to do, "can retry" tag, technical details collapsed.
   - **Page facts**: final URL, other URLs for this page (aliases/redirects), captured
     on, site last updated (and where that date came from), published date, change
     since last run and why, how we accessed it, file type, links found on the page
     (followups).
   - **Technical** (hidden unless "Show technical names" is on): status vector,
     warnings, summaries and the raw record as collapsible JSON.
5. **Duplicate content** `/runs/:runId/duplicates` - overlap groups from R2 (size,
   overlap %, representative page) with an expandable list of pages; a pair view
   that shows shared text side by side.
6. **Contradictions** `/runs/:runId/contradictions` - R4 candidates: the two
   statements side by side with page links and the evidence/proof; filters by
   confidence.
7. **Needs your review** `/runs/:runId/review` - the review queue with plain reasons
   and a link to each page.
8. **Downloads** `/runs/:runId/downloads` - R1/R2/R4/review files with one sentence
   on what each contains.

Responsive down to 1280 px laptops first; usable on a tablet. Light theme by default
with a dark-mode toggle (Ant `ConfigProvider` theme algorithm). Keyboard reachable,
labels on every icon button, colour never the only signal (tags carry text).

## 4. Architecture

```
exports dir (runs)  ->  read-only Python API  ->  React app (Vite build, served by the API)
*.records.jsonl         src/modules/content_processor/webapi/     webui/
*_reports/              FastAPI, 127.0.0.1 only
```

### Backend: `src/modules/content_processor/webapi/` (new package)

- `python -m src.modules.content_processor.webapi --exports <dir> [--port 8780]
  [--static webui/dist]` serves the API and the built app. Binds 127.0.0.1.
- A run is a `<name>.records.jsonl` (plus optional `<name>.json` manifest and
  `<name>_reports/` folder). Records are parsed with `UrlRecord.model_validate` and
  cached per file, keyed on mtime and size, so a growing file is re-read.
- Endpoints (all GET, JSON, pydantic response models, camelCase not required):
  - `/api/runs` - list with counts.
  - `/api/runs/{run}/overview` - badge counts, top issue types, formats, changes.
  - `/api/runs/{run}/pages?status=&host=&format=&has_issues=&q=&sort=&order=&page=&size=`
    - paged rows only (no blocks), with `total`.
  - `/api/runs/{run}/pages/{page_key}` - one full record (blocks, gaps, facts).
    `page_key` is a stable URL-safe hash of `schedule_key` or `requested_url`.
  - `/api/runs/{run}/duplicates`, `/contradictions`, `/review` - read the report files
    if present, else build them with the existing `export/` functions.
  - `/api/runs/{run}/downloads/{file}` - only names listed in the reports folder;
    reject anything else (no path traversal).
- Reuses `export/` and `contracts.py`; changes neither. Tests in
  `tests/modules/content_processor/test_webapi.py` with FastAPI's TestClient against
  small records built in the test.

### Frontend: `webui/`

- Vite + React 18 + TypeScript (strict) + Ant Design 5 + `@ant-design/icons`.
- **Server state: TanStack Query v5.** One query-key factory
  (`['runs']`, `['run', id, 'overview']`, `['run', id, 'pages', filters]`,
  `['run', id, 'page', key]`, ...). `placeholderData: keepPreviousData` for paging,
  sensible `staleTime`, refetch interval only while a run has RUNNING pages,
  prefetch page detail on row hover, retries off for 4xx.
- **Shareable view state: URL search params** (filters, sort, page, tab, selected
  areas) through one typed hook, so back/forward and copied links work.
- **Client preferences: Zustand** with `persist` (theme, density, visible columns,
  show technical names, last opened run). No server data in Zustand.
- **Every data view has four states**: loading (Ant `Skeleton`), empty (Ant `Empty`
  with a sentence on why and what to do), error (`Result` with retry, message from
  the API), and data. One `<QueryState>` component does this.
- Typed API client (`fetch` wrapper) and TypeScript types that mirror the API models.
- An `ErrorBoundary` per route. Code-split routes (`React.lazy`).
- Page text is third-party content: render as text only, never
  `dangerouslySetInnerHTML`.
- Tests: Vitest + React Testing Library for vocabulary mapping, the QueryState
  states, the Pages filters-to-URL round trip and Page detail tabs (with mocked API).

## 5. Done means

- Python gate unchanged and green: `ruff format --check .`, `ruff check .`,
  `mypy --strict src fixtures`, `pytest` with coverage >= 85%.
- Frontend gate green: `npm run typecheck`, `npm run lint`, `npm test`,
  `npm run build` in `webui/`.
- Served against the real John Hancock run outputs: every screen loads, and
  screenshots of Runs, Run overview, Pages, Page detail (Content and Issues),
  Duplicates and Contradictions are saved for review.
- No engine term is the primary label anywhere.

## 6. Decisions taken while building (Lane L8, session 1)

Where the plan left room, these choices were made and can be revisited:

- **Derived issue "Some text couldn't be double-checked" (`UNCONFIRMED_TEXT`).** In the real
  John Hancock run (`jh2`) 34 of 39 pages are NEEDS_REVIEW with no gap at all: the badge comes
  from `status_vector.fidelity = EXTRACTED_UNVERIFIED` (state-revealed text not matched on the
  live page). The API adds one finding per such page (`findings`, counted in `issues` and
  `issue_keys`) so the overview, the pages table and the Issues tab can say why. It is never
  added when a LOW_CONFIDENCE or FIDELITY_DISAGREEMENT gap already explains it.
- **Page title.** The first main-content heading (`heading_path[0]` of a visible block with
  role `main` or area `main_content`/`hero`), otherwise the URL's last segment made readable
  ("how-to-use-esignature" -> "How to use esignature"). Headings are shown as the site wrote
  them (no case changes), because analysts compare them with the live page.
- **`page_key`** = first 16 hex characters of sha256(`schedule_key` or `requested_url`); a
  repeated key in one file gets `-2`, `-3`.
- **Extra endpoints:** `/api/runs/{run}/pages/{key}/raw` (full record for the Technical tab,
  loaded only on request) and `/api/runs/{run}/downloads` (the file list).
- **Saved reports vs computed.** A saved report file is used when it exists and the reports
  folder's `manifest.json` names the same run id; otherwise the view is computed with
  `analysis/overlap`, `analysis/contradictions` (no NLI judge) and `export/reports`. Every
  report screen says which. A saved report more than ten minutes older than the records file
  is flagged as possibly out of date. R4 `deterministic_flags` are split at flag starts, since a
  flag can contain spaces (`numeric:percent:10 percent!=15 percent`).
- **Content tab areas** are five toggles instead of thirty: Main content (default on), Header
  and menus, Footer, Pop-ups and widgets, Page metadata. Blocks without a page area fall back to
  their role. Exact repeats (`dedupe_of`) are left out and counted. Lists over 300 rows are
  virtualised (`rc-virtual-list`, already an antd dependency, pinned explicitly).
- **Issues tab** shows one card per issue type; inside, one sentence per cause (gaps with the
  same type and cause are merged, with the control labels as chips). Raw reasons sit under
  "Technical details".
- **Pages table** hides "Captured on" (identical within a run) and "Change" (empty until a
  recrawl) by default; both are in the column chooser.
- **Contradictions** filter by "How likely" (the R4 context bucket: likely a real conflict,
  worth a look, probably fine) rather than by confidence band; the confidence band is shown on
  each item.
- **Duplicate pairs** open a drawer with both pages, the containment numbers and the shared
  passages R2b quotes; a full side-by-side text diff is not built.
- **Security:** GET/HEAD only (others 405), TrustedHost 127.0.0.1/localhost (DNS-rebinding
  guard), CSP `default-src 'self'` (styles allow inline for antd's CSS-in-JS), downloads matched
  against the reports folder listing, never path-joined.
