# Architecture investigation: one platform, many modules, Claude as the analyst

Status: **proposed 2026-10-31**, for the product owner's decisions (section 9). **Update 2026-11-02:** the product owner moved the Senior Analyst Action Card engine and the Claude control plane to step 1; it is built (`MODULE_ANALYST_ACTION_CARDS.md`, which also has the updated roadmap). Builds on `MODULE_01_CONTENT_SCRAPER.md` and `ARCHITECTURE_PROJECTS_AND_SITES.md`. Nothing here is built yet.

## 1. The request (product owner, 2026-10-31)

- Give Claude control of the whole app: generation (writing, analysis) is done by Claude.
- Several modules: **Content Extractor** (built), **Theming**, **Research**, **Monitoring**, **Gap Analysis**, more later.
- On top of them a **bot** that can look up the context of any module: its stored data and what it can do.
- Each module has its own **projects**, and a project's data must be usable **across modules**. Examples:
  - Theming a website uses everything the Content Extractor already holds about it.
  - A Research project on GEP connects to Semrush and tracks GEP's and its competitors' numbers. That project's data is then used by Monitoring.
  - Gap Analysis takes context from any module and any project.
- All of it is combined in one **playground**, where Claude works as an SEO analyst over the data of many projects and modules and the relations between them.

## 2. What exists today

| Piece | State | Becomes |
|---|---|---|
| Scraping, evidence, verdicts, reports (`content_processor`) | Built, live-tested | **Content Extractor** module |
| Projects with client and competitor websites, separate storage per website (`projects/`) | Built inside the content module | Moves to the **platform core** (projects are shared by every module) |
| Unique websites across projects, competitor relations from every project (`projects/websites.py`) | Built | Platform core: the **website graph** |
| Page stamps, chunks, offering catalog (`knowledge/stamp.py`, `catalog.py`) | Built | Catalog = the engine of the **Theming** module |
| SEO/GEO inventory, page outlines and page types (`inventory.py`, `structured_page.py`) | Built | Content Extractor outputs that Theming and Gap Analysis read |
| Vector store, labels, hybrid search, checked answers (`knowledge/`) and search across websites (`federated.py`) | Built (local fallbacks; Ollama and pgvector not installed yet) | The **context layer** the bot searches |
| Semrush | Only an API-key setting | **Research** module |
| Re-crawl history and change detection (`recrawl/`) | Built | Part of **Monitoring** |
| Claude | Not connected; model steps use local Ollama only, by rule | The analyst (section 5) |

Most of what the bot needs already exists. What is missing is a **platform layer above the content module**:
- shared projects
- a shared store
- one way for Claude to reach every module

## 3. The shape: one core, many modules, one tool layer

```
                ┌──────────────────────────────────────────────┐
                │ Claude (the analyst)                         │
                │  in Claude Desktop / Claude Code / claude.ai │
                │  or the app's own chat (later)               │
                └──────────────────┬───────────────────────────┘
                                   │ tools (MCP): read, search, run, write
                ┌──────────────────▼───────────────────────────┐
                │ Rankuno tool layer ("context server")        │
                │  module registry · permissions · data rules  │
                │  spend caps · evidence on every answer       │
                └──┬────────┬─────────┬──────────┬──────────┬──┘
          Content Extractor Theming  Research  Monitoring  Gap Analysis   (+ future modules)
                   │        │         │          │          │
                ┌──▼────────▼─────────▼──────────▼──────────▼──┐
                │ Platform core: projects, websites, relations, │
                │ shared entities, findings, provenance         │
                │ Postgres (schemas per module) + pgvector       │
                │ evidence files and reports on disk            │
                └───────────────────────────────────────────────┘
```

Three rules make the cross-module requirement work:

1. **Shared things live in the core and are referenced, not copied.** A website (by domain), a page (by URL), an offering, a keyword, a competitor relation: each exists once. Modules attach their own data to them. A Research project on `gep.com` and a Content Extractor project on `gep.com` point at the same website record, so either one sees the other's data.
2. **Every record says where it came from.** Each record carries:
   - the module and project that made it
   - its source (scrape evidence id, Semrush report and date, Claude with model and prompt id, a person)
   - when it was observed
   This is how the playground can mix modules and still say what each number or sentence rests on.
3. **Claude reaches data only through tools, never the database directly.** Tools apply permissions, data rules and spend caps the same way every time, and every answer comes back with its evidence.

## 4. The shared data model (the "playground")

### 4.1 Core entities (platform, shared by every module)

| Entity | Key | Notes |
|---|---|---|
| **Project** | id | Has a **home module** (where it was created) and a type. It can **link** to other projects as inputs (section 4.3). |
| **Website** | domain (subdomains merged) | Unique across all projects and modules. Already built as the website graph. |
| **Relation** | (website, website, kind, project) | `competes_with` today; later `parent_of`, `partner_of`. Kept per project and combined across projects. |
| **Page** | normalized URL | Belongs to a website; points to evidence and the latest scrape. |
| **Offering / theme** | id within a website | From Theming (the catalog). Has a parent offering and aliases. |
| **Keyword / topic** | normalized text + market (country, language) | From Research. Shared, so Monitoring and Gap Analysis join on it. |
| **Observation** | (subject, metric, source, time) | One time-series table for every number: Semrush position, traffic, authority, AI visibility, page word count, scrape status. Monitoring is mostly queries over it. |
| **Finding** | id | A conclusion: a gap, an alert, a recommendation. It has evidence links, a status (open, accepted, done) and an author (code, Claude or a person). |
| **Artifact** | id | A report, workbook or Markdown file a module produced. |

### 4.2 Module data (each module owns its schema)

| Module | Its own data | Reads from |
|---|---|---|
| Content Extractor | runs, records, evidence, reports, chunks, vectors | core |
| Theming | catalog trees, offering cards, page→offering mapping, theme clusters | Content Extractor (stamps, chunks, page types, inventory) |
| Research | keyword sets, Semrush snapshots (organic keywords, positions, competitors, backlinks, AI visibility), SERP pulls | core websites and relations; Theming (offerings → keyword groups) |
| Monitoring | watch lists, schedules, alerts | Research (keyword sets, competitors), Content Extractor (re-crawls, change history), observations |
| Gap Analysis | gap runs and findings | any of the above, chosen per project |

### 4.3 Projects across modules: links, not copies

A project lists its **inputs**: other projects (any module) whose data it may read. Examples:

- *Theming project "Flynn's catalog"*: input is Content Extractor project "Flynn's + competitors". It reads their scrapes and builds catalogs per website.
- *Research project "GEP visibility"*: websites `gep.com` plus its competitors (from the website graph, or added). It pulls Semrush snapshots on a schedule into observations.
- *Monitoring project "GEP weekly"*: input is the Research project "GEP visibility". It watches the same keywords and competitors, re-crawls GEP's key pages, and raises alerts when rankings or content change.
- *Gap Analysis project "GEP vs Coupa, Q4"*: inputs are the Research, Monitoring and Content Extractor projects for those websites. It writes findings with evidence.

The bot's **scope** for a question is then a set of projects, plus the websites and relations they reach. That is the existing "client plus all competitors from every project" scope, extended from one module to all modules.

## 5. Claude's role

### 5.1 What Claude does, and what stays as code

| Job | Who | Why |
|---|---|---|
| Writing: catalog cards, theme names, summaries, gap write-ups, answers, recommendations | **Claude** | Your requirement; Claude writes better than the local 7–14B models |
| Choosing and running the next step (create a project, start a scrape, pull Semrush, run a gap analysis) | **Claude, through tools with guardrails** (5.3) | "Control of the whole app" |
| Embeddings (vectors for search) | **Local bge-m3 (Ollama)** | Claude has no embedding model; local costs $0 and keeps text in-house |
| Facts: scraping, evidence, verdicts, numbers, dates, prices | **Code** | They must be repeatable and provable |
| Checking Claude's statements against quotes and evidence | **Code** (the stage E check) | "Evidence-backed" stays the rule: a statement without a matching quote is dropped |

### 5.2 How Claude connects: one tool layer (MCP), two ways to use it

**MCP** (Model Context Protocol) is the standard way to give Claude tools. A "Rankuno MCP server" exposes the platform as tools, for example:

- `list_projects`, `get_project`, `list_websites`, `competitive_scope`
- `search_knowledge` (hybrid search across scopes, with evidence), `get_page`, `get_offering_catalog`
- `get_observations` (time series), `get_findings`, `create_finding`
- `start_scrape`, `pull_semrush_snapshot`, `run_gap_analysis` (actions, 5.3)
- `list_modules` (the registry: what each module holds and can do)

New modules register their tools, so the bot learns them without being rebuilt.

| Way to use it | How | Cost | Good for |
|---|---|---|---|
| **A. Claude apps** (Claude Code, Claude Desktop now; claude.ai for the team later) | Connect the MCP server to Claude. Claude also has its own **Semrush connector** (already connected to this account), so it can combine Rankuno data and live Semrush data in one conversation. | Uses the team's Claude seats; no per-token bill | Analysts working with Claude; fastest to deliver |
| **B. Chat inside the app** | The app calls the Claude API (Agent SDK) with the same tools | Per-token API bill; needs caps like paid scraping | Users who never open Claude; scheduled write-ups (weekly monitoring summary) |

**Recommendation: build the tool layer once, use it through A first, add B later.** For claude.ai, which most of the team uses, the server must be reachable over HTTPS from the internet with sign-in. Until the app is deployed, A works locally in Claude Code and Claude Desktop.

### 5.3 Guardrails for "control of the app"

| Tool tier | Examples | Rule |
|---|---|---|
| Read | search, list, get | Free; limited to the user's scope and to the data classes Claude may see (5.4) |
| Compute | create project, build catalog, index, gap analysis | Allowed; logged; undoable |
| Spend | scrape with paid access, Semrush API units, Claude API (way B) | Same spend guard as Bright Data: caps per run, day and month, plus approval |
| Change or delete | delete project, change URLs, change passwords | Needs a person's confirmation in the app |

### 5.4 Which data Claude may see (decision needed, 9.1)

The standing rule is that no client content goes to hosted AI services until the data policy is confirmed. Claude is a hosted service, so this request needs that policy. Proposal: tag every record with a **data class**, and let the tool layer enforce it.

- `public`: scraped public web pages, public SEO data. Allowed to Claude once you confirm.
- `licensed`: Semrush and other paid third-party data. Allowed if the vendor's terms permit use with AI tools; check each vendor.
- `client_confidential`: logged-in or staging pages, client uploads, client analytics. Stays local (Ollama) unless that client's agreement allows Claude.

## 6. The modules in brief

| Module | First version | Main inputs |
|---|---|---|
| **Theming** | Per website: offering catalog (built), Claude-written cards and theme names, page→theme map, client vs competitor theme matrix | Content Extractor |
| **Research** | Per project: websites and competitors; Semrush pulls (domain overview, organic keywords and positions, competitors, AI visibility); keyword sets grouped by theme; snapshots saved as observations | core, Theming, Semrush API |
| **Monitoring** | Scheduled Semrush pulls and re-crawls; alerts when a position, page or competitor changes; weekly Claude summary with evidence | Research, Content Extractor |
| **Gap Analysis** | Themes or keywords competitors cover and the client doesn't; pages that lost rank after a content change; AI-visibility gaps; each finding with evidence and priority | all |

**Semrush in two places.** Claude's own Semrush connector is right for questions asked in the moment. Tracking over time (Research, then Monitoring) needs the **server** to pull Semrush on a schedule and store snapshots, because a conversation keeps no history. Both use your Semrush subscription; server pulls spend API units, so they get caps.

## 7. Three flows end to end

1. **Theme a website.** Claude calls `get_project`, then `get_offering_catalog` for each site, then `search_knowledge` for the gaps in each card. Claude writes the cards; code checks every quote; the cards are saved as Theming data with evidence.
2. **GEP research to monitoring.** Create Research project "GEP visibility" (`gep.com` + competitors from the website graph or Semrush's competitor list). `pull_semrush_snapshot` stores positions as observations. A Monitoring project with that Research project as input re-pulls weekly and re-crawls the pages behind the main keywords. A change becomes an alert, and Claude writes a short note that cites the Semrush numbers and the page changes.
3. **Gap analysis across modules.** Inputs are the Research, Monitoring, Theming and Content Extractor projects for GEP. Code joins on the shared keys (website, keyword, offering, page). Claude reads the joined table through `search_knowledge` and `get_observations` and writes findings, each with links to Semrush snapshots and page evidence. Findings go into the core, so Monitoring can track whether each is fixed.

## 8. Build plan (one step at a time)

| Step | Delivers | Depends on |
|---|---|---|
| 0. Decisions | Answers to section 9 | you |
| 1. Platform core | Projects, websites, relations, observations, findings, data class and provenance in Postgres (local Postgres 18 is installed; a dedicated database and user, never n8n's); the Content Extractor's projects move there unchanged for users | 0 |
| 2. Rankuno MCP server, read-only | Claude Code/Desktop can list projects and websites, search knowledge across projects with evidence, read catalogs and reports. **The bot exists from here on.** | 1 |
| 3. Install Ollama (bge-m3) and pgvector | Search by meaning instead of word matching; the store moves to Postgres | your machine |
| 4. Research module | Semrush API pulls into observations, keyword sets, MCP tools | 1, Semrush key and unit caps |
| 5. Theming module | Catalog plus Claude-written cards and theme matrix, as projects | 2 |
| 6. Monitoring module | Schedules, alerts, weekly summary | 4 |
| 7. Gap Analysis module | Cross-module joins, findings with evidence | 4–6 |
| 8. Action tools and in-app chat | Claude starts scrapes and pulls within the guardrails (5.3); chat in the app through the Claude API | user accounts (9.4), deployment (9.5) |

Steps 1 and 2 are the foundation and are worth doing before any new module. Every later module then plugs into the same core and becomes visible to the bot as soon as it registers its tools.

## 9. Decisions needed from the product owner

1. **Data policy for Claude.** May scraped public pages and Semrush data go to Claude (Anthropic)? Which clients, if any, are confidential and must stay local? (Proposal in 5.4.)
2. **How Claude is paid for.** Through the team's Claude seats via MCP (way A, no per-token bill), or the Claude API for an in-app chat (way B, needs a monthly cap)? The current rule is $0 by default.
3. **Semrush.** Use the existing API key for scheduled pulls? What monthly unit cap? Which databases (country)?
4. **People and permissions.** One password per project does not work for a bot that reads and acts across projects. Introduce user accounts with roles (viewer, analyst, admin) before Claude gets action tools?
5. **Where the app runs.** A shared server with HTTPS is needed for claude.ai and for the team. Until then it stays local to this machine, with Claude Code and Desktop only.
6. **Order of modules** after the core and the read-only bot: Research → Monitoring → Gap Analysis, with Theming in parallel (its engine exists)? Or another order?
