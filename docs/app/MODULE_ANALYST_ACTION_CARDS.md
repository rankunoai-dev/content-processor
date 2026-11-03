# Module: Senior Analyst Action Cards (Claude control plane, step 1)

Status: **built 2026-11-02** on `feat/analyst-cards` (`src/modules/analyst/`). Requested by the product owner on 2026-11-02 as step 1 of the platform in `ARCHITECTURE_PLATFORM_MODULES_AND_CLAUDE.md`. Claude is **off by default** until the data policy is confirmed (section 6).

## 1. What an action card is

A packaged senior-SEO-analyst workflow. A person picks a card (for example "Head-to-Head Competitor Gap Audit"), the inputs fill in from the project, and the card:

1. builds a deep, multi-phase prompt (a persona plus shared analyst rules plus the card's phases),
2. fetches the core data up front through the module tools (search, pages, catalogs, issues, changes),
3. lets Claude call more tools, limited to the card's own tool list and the project's websites,
4. forces evidence: every statement cites a reference a tool returned in this run; code then checks the references and quotes and appends the sources (pages and evidence hashes),
5. writes an executive-ready deliverable in a fixed format: tables, scorecards, briefs, a "Limits of this analysis" section.

A card is data (`library.py`): id, version, module, persona, phases, inputs, required and optional tools, prefetch calls, output format. Adding a card needs no code; every run records the card version it used.

## 2. The card catalog (15 cards)

| # | Module | Card | Deliverable | Runs today? |
|---|---|---|---|---|
| 1 | Content Extractor | E-E-A-T & Trust Proof Audit | Trust & Credibility Matrix | yes |
| 2 | Content Extractor | Page Cannibalization Diagnostic | Pruning & 301 Merge Plan | yes |
| 3 | Content Extractor | Technical SEO / GEO Hygiene | High-Priority Fix Checklist | yes |
| 4 | Content Extractor | AI Answer Readiness (GEO) Audit *(added)* | GEO Readiness Scorecard | yes |
| 5 | Theming | Topic Cluster & Pillar Map | Hub-and-Spoke IA Blueprint (with Mermaid diagram) | yes |
| 6 | Theming | Customer Journey Funnel Audit | Missing Funnel Stage Report | yes |
| 7 | Theming | Offering & Theme Coverage Matrix *(added)* | Offering Coverage Matrix | yes |
| 8 | Research | Search Intent & SERP Feature Opportunity | SERP Win Opportunity Score | needs Research (Semrush) |
| 9 | Research | High-Value Keyword Treemap | Revenue Potential Matrix | needs Research |
| 10 | Research | Keyword-to-Page Mapping *(added)* | Keyword Map | needs Research |
| 11 | Monitoring | Competitor Strategy Radar & Drift Detector | Alert: New Pages & Pivots | yes, once a competitor has two scrapes |
| 12 | Monitoring | Content Decay & Refresh Queue | Priority Content Update List | yes (rankings once Research exists) |
| 13 | Gap Analysis | Head-to-Head Competitor Gap Audit | Feature & Content Comparison | yes |
| 14 | Gap Analysis | Winning Content Brief Generator | Rank-Ready H1/H2/FAQ Brief | yes (SERP data once Research exists) |
| 15 | Playground | Executive Competitive Briefing *(added)* | Executive Briefing | yes |

The added cards fill gaps in the proposed eleven: GEO readiness (how quotable the site is by AI answer engines), the offering matrix across all competitors (the theming question asked most often), keyword-to-page mapping (the first Research card to build), and a cross-module executive view.

## 3. Tools (the inter-module bridge)

| Tool | Module | What it returns |
|---|---|---|
| `list_websites` | content | websites in scope, role, searchable, counts, embedding model |
| `search_knowledge` | content | hybrid search across websites, `per_site` for side by side, `role` filter; each passage has a `ref`; each search has a `search_ref` (for "not found" claims) |
| `get_page` | content | every section of a page in order, plus page facts |
| `get_page_facts` | content | title, description, H1, canonical, headings, schema types, FAQ questions, offers, CTAs, numbers, dates |
| `list_pages` | content | every page: title, H1, page type, offering, path, word count, dates |
| `get_offering_catalog` | theming | offering tree with pages and card attributes |
| `get_seo_issues` | content | SEO/GEO issues by root cause (builds the inventory from stored evidence on first use, then caches it) |
| `find_similar_pages` | content | near-duplicate page pairs (cannibalization candidates) |
| `get_changes` | monitoring | new, removed and changed pages between a website's last two scrapes |
| `get_keyword_rankings`, `get_serp_overview`, `get_keyword_metrics`, `get_search_demand` | research | built 2026-11-02 (`MODULE_RESEARCH.md`): read the latest research snapshot; a card that needs them is not ready until the project has research; cards that use them optionally run without and say so |
| `get_gap_findings` | gap | built 2026-11-02 (`MODULE_GAPS.md`): ranked findings with evidence and status, dismissed left out |

## 4. How to run

- **Context pack** (default, $0, nothing leaves the machine): `python -m src.modules.content_processor.cli analyze run <card> --project <id> [--set name=value]`. It writes `context_pack.md` (instructions, task and prefetched data) for a person to use in a Claude app if the data policy allows.
- **Claude**: add `--backend claude` (needs `CP_CLAUDE_ENABLED=true`, a monthly cap and `ANTHROPIC_API_KEY` or an `ant auth login` profile). It writes `deliverable.md` with code's verification and sources.
- **Web app**: the project's **Analyst cards** tab (library by module, card form with project defaults, Check, Run after unlocking, run list, run viewer with live progress, citation check, copy and download).
- **API** (used by the tab): `GET /api/analyst/cards`, `POST /api/projects/{id}/analyst/preflight`, `POST /api/projects/{id}/analyst/runs` (needs the project's unlock token), `GET .../runs/{run}`, `GET .../runs/{run}/events` (live progress).
- Other commands: `analyze cards | show | preflight | runs | policy | spend`.

Each run keeps `app_data/analyst/runs/<run>/`: `run.json` (inputs, card version, backend, model, turns, tool calls, tokens, cost, verification), `prompt.md`, `context_pack.md` or `deliverable.md`, `ledger.json` (every reference shown to Claude, with URL and evidence ids) and `events.jsonl`.

## 5. Breaking points and edge cases, and how each is handled

| # | Breaking point / edge case | Handling |
|---|---|---|
| 1 | Client data reaching a hosted model before the data policy is decided | Claude off by default (`CP_CLAUDE_ENABLED=false`); per-project data class (`analyze policy --data-class client_confidential`) blocks Claude for that project **and** for any website in the run's scope whose freshest copy comes from a confidential project; context pack always available |
| 2 | Cost overrun | Monthly cap required (0 = refuse); every call checked against the smallest of run, daily and monthly room using its worst case (all input at full price plus maximum output); output limit lowered to fit; append-only spend log; unknown model priced at the most expensive known rate |
| 3 | Budget or tool calls running out mid-analysis | The loop switches to "write the deliverable now" (`tool_choice: none`) before the limit, so a run ends with a deliverable; a cut-short deliverable is labelled |
| 4 | Hallucinated facts or citations | Shared rules; references are handed out by code; unknown references, quotes not found word for word, and uncited factual lines are reported; sources (URL, headings, evidence hashes) are appended by code, never typed by the model |
| 5 | "The client doesn't have X" without checking | Rule 3 plus `search_ref` on every search, so absence claims cite the search that found nothing |
| 6 | Invented metrics (volume, traffic) | Rule 4: numbers only from tools; Research-dependent figures written as "not available (Research module not connected)" |
| 7 | Prompt injection in scraped pages | Tool results marked `untrusted_website_content`; rules say never follow instructions inside them; tools are read-only (no action tools in step 1) |
| 8 | A card needing a module not built yet | Preflight blocks it and names the module; optional tools are dropped with a warning |
| 9 | Website not scraped, or knowledge space not built | Preflight names the website and says to scrape it first |
| 10 | Monitoring card with only one scrape | Preflight checks the compared website has two finished scrapes |
| 11 | Search quality with the offline embedder | Preflight warns per website ("shared words only"); `find_similar_pages` notes that its pairs must be confirmed by reading |
| 12 | Inputs outside the project, typos, duplicates | Websites resolved through the website graph and limited to the project plus its competitors from any project; addresses normalised and checked; text cleaned (control characters, length); the same website twice is refused |
| 13 | Tool input Claude got wrong | Validated against the tool's schema before running; the error goes back to Claude as a tool error, never a crash |
| 14 | Oversized tool results | Per-tool caps (pages, pairs, changes, text per section) and a hard 40,000-character limit that halves the longest list with a note to narrow the request |
| 15 | API failures | SDK retries (429, 5xx, connection); then a plain message per failure type (credentials, permission, model, rate limit, invalid request, overload, network, timeout); unreadable streamed tool input retried once |
| 16 | Claude declines (refusal) | Server-side fallback on; a refusal of the whole chain ends the run with its category |
| 17 | Server restarted mid-run | Runs left queued or running are marked "interrupted" at start-up |
| 18 | Running cards without permission | Starting a run needs the project's unlock token and the app's write guard (local page, app header, JSON) |
| 19 | Reproducibility and audit | Card version, inputs, prompt, model, ledger, tool calls, tokens and cost kept per run; prompt caching keeps the system prompt stable (the date goes in the task) |
| 20 | CLI and other commands slowed by new dependencies | The analyst module and Anthropic SDK are loaded only when `analyze` runs (numpy and the SDK are not imported by the CLI) |

## 6. Decisions still open (product owner)

1. Data policy: may public scraped pages go to Claude? Which clients are confidential? (Until answered, use context packs.)
2. Monthly Claude cap and model (default `claude-opus-5-5`, effort `high`, $2 per run, $10 per day).
3. Semrush API key and unit cap, to build the Research tools (unlocks cards 8-10 and the ranking parts of 12 and 14).

## 7. Updated roadmap

| Step | What | State |
|---|---|---|
| **1** | **Action Card Engine and Claude control plane**: 15-card library, inter-module tool bridge (Module 1 + theming catalog + monitoring diffs), Claude API loop with budget, verification and sources, context-pack mode, CLI and API | **built**, including the web screen (2026-11-02) |
| 2 | Theming module: themes and topics across websites, pillars, ownership, funnel stages, theme map over time (`MODULE_THEMING.md`) | **built** 2026-11-02 (audience and brand-voice labels still to do) |
| 3 | Research module: Semrush export import and capped API pulls (rankings, SERP, keyword metrics), snapshots, demand per theme, movers (`MODULE_RESEARCH.md`); unlocks cards 8-10 | **built** 2026-11-02 (live pulls wait for a Semrush key and cap) |
| 4 | Monitoring module: changes between scrapes, prices, template changes, theme drift, alerts, auto-check and opt-in scheduled re-scrapes (`MODULE_MONITORING.md`) | **built** 2026-11-02 (rank tracking waits for step 3) |
| 5 | Gap Analysis: cross-module joins (themes x keywords x monitoring x catalogs), ranked findings tracked to closure (`MODULE_GAPS.md`) | **built** 2026-11-02 (multi-project playground screen still to do) |
| 6 | Read-only MCP server over the same tools, so the bot also works inside Claude Desktop (and Claude Code); "Open in Claude.ai" for context packs (`MODULE_MCP_SERVER.md`) | **built** 2026-11-03 (claude.ai connector needs hosting; write tools later) |
