# RFC 0002: Page stamping, offering catalog and knowledge nodes

Status: proposed, 2026-10-26. Builds on the extraction engine (UrlRecords with evidence-backed blocks), RFC 0001 (URL grouping and template fingerprints) and STAGE4_ANALYSIS_AND_INTERFACES.md (I2 overlap, I3 semantic similarity, I4 contradictions).

## 1. Goals

1. **Stamp** every URL and every content chunk with metadata (facets) so content can be filtered, grouped and compared without re-reading it.
2. **Offering catalog ("website theming")**: identify, in detail, every service, product, solution, consulting practice, platform, industry and capability a website offers, and map every page and chunk to the offerings it supports.
3. **Knowledge nodes** for retrieval and question answering, with routing that sends a question only to the relevant nodes.
4. **Competitor comparison**: the same offering structure for several sites, aligned to one industry taxonomy, so coverage, depth, claims and presentation can be compared and gaps found.
5. **Robustness first, cost second** (operator decision, 2026-10-26). No cost saving may reduce the evidence a decision sees or remove a check. Within that rule, cost is minimised: deterministic code first, local models (Ollama) for bulk work, Jev for bulk structured decisions on public content, cloud models only for small-volume, high-value steps. Every model call is metered and capped like paid crawling (playbook §10).

Non-goals for this RFC: the chat UI design, Postgres operations (K1), and paid cloud spend beyond approved caps.

## 2. Why L1 sections alone are not enough (measured on John Hancock, 2026-10-19 run)

| Observation | Consequence for L1-only nodes |
|---|---|
| 25 of 39 URLs are under `/individual/`; below it, `help-center` (20) and `insights` (5) | One node mixes e-signature form guides with wellness podcasts |
| `/adobe/…`, `/akam/…`, `/content/dam/…` appear as L1 paths | Images and tracking pixels would become "knowledge" |
| "Vitality" content appears under podcasts, news and product pages | One topic split across three nodes; a router picking one misses the rest |
| Two near-identical URLs for one press release | Duplicate knowledge |
| Title, meta description, Open Graph, canonical, hreflang and JSON-LD are extracted (`html_source._metadata`, `SourceExtraction.structured_data`) but never stored on the record | The cheapest and most reliable labels are lost |

The URL path tells the **section and page type** reliably; it does not tell the **offering or topic**. Both are needed, as separate facets.

## 3. Concepts

| Concept | Definition |
|---|---|
| **Page stamp** | Facts about one URL: structure, metadata, page type, offerings, audience, funnel stage, entities, dates, quality. Each fact records its source (`code`, `jev`, `ollama`, `cloud`, `human`), engine version and confidence. |
| **Chunk** | A section of a page built from its blocks by heading path (about 200 to 500 tokens), keeping the block ids for citations. Chunks are what gets embedded and retrieved. |
| **Offering** | Something the organisation sells or provides: `service`, `product`, `solution`, `consulting_practice`, `platform`, `managed_service`, `industry_focus`, `capability`, `program`. Offerings form a tree (family, offering, sub-offering). |
| **Offering card** | Everything known about one offering: name and aliases, type, parent, description, audiences, industries, geographies, features, deliverables, pricing model and amounts, proof points (case studies, awards, metrics), calls to action, the pages and chunks that evidence each attribute. |
| **Knowledge node** | A retrievable slice of a site: an offering, a section, a topic, a page type, or an audience. Nodes are views over facets, not separate stores; one chunk can belong to several nodes. |
| **Node card** | Summary of a node for routing and site-wide questions: what it covers, key entities and claims, page count, depth, freshness, formats, quality. |
| **Industry taxonomy** | A shared offering tree per industry (for example Procurement software and services; Insurance and wealth), onto which each site's offerings are mapped so competitors line up. |

## 4. Stage A: stamping every URL (deterministic, no model cost)

Input: one `UrlRecord` (blocks with layer, role, ui_region, heading path, evidence ids; page dates; template fingerprint) plus the stored raw HTML from the evidence store.

### 4.1 Page metadata (fix: persist what is already extracted)

Stored on a new `PageStamp` (one per URL per run):

| Field | Source | Technology |
|---|---|---|
| `title`, `meta_description`, `h1`, `canonical`, `lang`, `hreflang[]`, `og_type`, `og_title`, `twitter_*` | `<head>` | `html_source._metadata` (selectolax), already written; only persistence is new |
| `schema_types[]` and key schema fields (`Service.name`, `Product.name`, `Offer.price`, `FAQPage` questions, `Organization`, `BreadcrumbList`) | JSON-LD, microdata | `SourceExtraction.structured_data` + `json.loads`; typed reader for the common schema.org types |
| `breadcrumbs[]` | `BreadcrumbList` or breadcrumb UI region | existing `ui_region = breadcrumb` blocks |
| `nav_labels[]`, `mega_menu_tree` | header navigation and mega menu regions | existing region classifier; parse the menu's nested lists into a tree of labels and links |
| `path_l1`, `path_l2`, `path_l3`, `path_depth` | URL | `urllib.parse` |
| `template_group`, `template_fingerprint` | RFC 0001 grouping | existing `grouping` package |
| `is_asset`, `is_tracking`, `is_error_page` | media type, path patterns (`/content/dam/`, pixel endpoints), soft 404 verdict | existing sniffing and verdicts; asset and pixel URLs are excluded from knowledge |
| `duplicate_of` | R2 overlap clusters and canonical | existing MinHash overlap (I2) |
| `published_at`, `modified_at`, `change_status` | page dates, recrawl | existing provenance and recrawl |
| `word_count`, `main_word_count`, `reading_level`, `language` | corpus blocks | code; reading level by a standard formula; language by `lingua` or `fasttext` lid |
| `components{}` | counts of hero, card, carousel, accordion, tab, form, table, CTA | existing `ui_region` |
| `ctas[]` | buttons and links with action verbs ("Request a demo", "Contact sales") | link text + region; pattern list |
| `numbers[]` | prices, rates, percentages, durations with units | existing claim extraction (`analysis/claims.py`) |
| `quality` | badge, gap types, verification share | UrlRecord |

### 4.2 Chunking without losing page context

Blocks are grouped by `heading_path` within the main corpus (boilerplate and chrome excluded, using `grouping/boilerplate.py`) into chunks of about 200 to 500 tokens. Each chunk keeps `block_ids[]`, `evidence_ids[]`, `ui_region` mix, `heading_path`, `parent_section_id`, `prev_chunk_id`, `next_chunk_id`, and a `chunk_kind` from code where it is obvious (FAQ question and answer from accordion or `FAQPage`; table; pricing table; form instructions; testimonial region).

Splitting a page can lose context; these rules prevent it:

| Risk | Rule |
|---|---|
| A chunk does not say what page or offering it is about | **Context header**: the text that is embedded and classified is `site › page title › offering (once known) › heading path` plus a one-line page summary, followed by the chunk text. The stored chunk text stays verbatim for citations. |
| References to earlier sections ("the plans above") | Neighbour links (`prev`, `next`) and a small overlap (one sentence) at split points; retrieval always returns neighbours (section 8). |
| An answer spans several sections | **Parent and child**: chunks are the search unit, the parent section (or the whole page when it is short) is the answer unit. Page vectors and page cards are kept alongside chunk vectors. |
| Tables, FAQ pairs, lists, numbered steps cut in half | **Atomic units** are never split; an oversized unit becomes its own chunk. |
| Tiny or huge sections | Sections under about 80 tokens merge with siblings under the same parent; long sections split at paragraph boundaries, never mid-sentence, never across a top-level heading. |
| Chunk vectors miss page-wide meaning | Optional **late chunking**: embed the whole page with a long-context model (`bge-m3`, about 8k tokens) and pool token embeddings per chunk; evaluated against plain chunk embeddings (section 11). |
| Classifying a chunk without its page | **Chunks are never categorised alone.** The page is categorised first with all its evidence; a chunk inherits the page's offerings and gets a different label only on strong, evidenced disagreement (section 6.2). |

### 4.3 Output

`PageStamp` and `Chunk` records (new contracts, CR to L0 for persistence; JSONL alongside `<run>.records.jsonl` until then). Every field carries `source = code`. Measured goal: Stage A runs in seconds per thousand pages with no model calls.

## 5. Stage B: discovering the offering catalog

The aim is the site's own list of offerings, in detail, before any page is labelled.

### 5.1 Candidate mining (code)

Offering candidates are collected from the strongest signals, each with evidence:

1. **Navigation and mega menu trees**: labels under menus such as Solutions, Services, Products, Platform, Industries, Capabilities. The site's own information architecture.
2. **URL families**: second and third path segments under offering-like sections (`/solutions/*`, `/services/*`, `/products/*`, `/industries/*`).
3. **Schema.org**: `Service`, `Product`, `SoftwareApplication`, `Offer`, `OfferCatalog` names.
4. **Page H1 and title** of pages whose template group is an offering-page template (the group whose members are linked from the offering menus).
5. **Headings and cards** on hub pages (a services hub's card titles are its offerings).
6. **Footer sitemap sections**, `sitemap.xml` groupings.

### 5.2 Consolidation (local models)

1. Embed every candidate name with its short context (Ollama embedding model) and cluster near-synonyms ("Procurement Software" and "Procurement Platform").
2. An Ollama 7 to 14B instruct model, with JSON-schema output, proposes the merged catalog tree: families, offerings, sub-offerings, type, parent, aliases.
3. For small sites or low-agreement clusters, one cloud-model call per site reviews the proposed tree (tens of calls per site, capped).
4. A reviewer can correct the tree in the analyst app (same decision log as reviewer decisions).

### 5.3 Offering cards (attribute extraction)

For each offering, its own pages (from 5.1 and Stage C) are read:

- Ollama extracts free-text attributes with JSON schema: description, features, deliverables, audiences, industries, geographies, pricing model, proof points, CTAs.
- Numbers come from code (`claims.py`), never from a model.
- Every attribute keeps the chunk ids that support it; an attribute without evidence is dropped.

## 6. Stage C: categorising every page and chunk

### 6.0 Rules for every decision (robustness first)

1. **Independent signals must agree.** A page-type or offering label is accepted only when at least two independent signal families agree: **structure** (menu placement, URL family, schema.org type, template group), **meaning** (embedding similarity to offering cards) and **a decision model** (Jev for public content, Ollama otherwise). Disagreement escalates (section 6.3); it is never resolved by picking the cheapest answer.
2. **No evidence is dropped to save cost.** Jev's 32 KiB body cap limits a request, not what is examined: a long page is judged section by section in several requests and the results are combined (a primary offering must win on the page card and be supported by its sections). A summary card is never the only input for a long page.
3. **Every label carries its evidence**: which signals, their values and agreement, engine and model version, confidence, timestamp. Labels can be audited, re-run and overridden by a reviewer.
4. **Cost is saved only by skipping escalations when signals agree**, never by skipping signals, sections or checks.

### 6.1 Page-level decisions with Jev

Jev (TypeSafe AI, hosted) returns typed decisions: `choice` (one option, per-option probabilities, confidence), `score` (ordered levels) and `noul` (probability of yes). Many questions are answered in one request over one shared `state`. The request body is capped at 32 KiB, so Jev receives a **page card** built from the stamp, not the whole page:

`state` (about 2 to 6 KB): URL path, title, meta description, H1, breadcrumbs, schema types, top headings, components, CTAs, template group, and the first 300 to 600 words of main content.

One request per page asks, for example:

| Question | Type | Options |
|---|---|---|
| `page_type` | choice | offering_detail, offering_hub, industry_page, case_study, article_or_insight, news_or_press, faq_or_help, form_or_how_to, pricing, legal, careers, about, contact, campaign_landing, investor_relations, other |
| `primary_offering` | choice | the top 8 to 15 candidate offerings for this page (pre-selected by embedding similarity to offering cards) plus `none` |
| `also_about_<offering>` | noul | one per remaining candidate (multi-offering pages) |
| `audience` | choice | per industry list (for example buyer, advisor, employer, individual, investor, job seeker, partner) |
| `funnel_stage` | score | learn, compare, decide, onboard, support |
| `offering_depth` | score | mention, overview, detailed, exhaustive |
| `is_outdated` | noul | |

Large taxonomies are asked in two passes (family, then offering within it) to stay inside the body cap and keep option lists short.

### 6.2 Chunk-level decisions

Code labels the obvious chunks (FAQ, table, form steps, testimonial region). Remaining main-content chunks are labelled in batches (several chunks per request as separate questions where the cap allows), each sent with its context header and neighbours: `chunk_role` (claim, definition, feature, benefit, how_to_step, pricing, proof_point, cta, disclaimer, other) and `offering`, as a choice among the page's offerings with the page's primary offering as the default. A chunk's offering differs from its page's only when the decision is confident and its embedding similarity agrees.

### 6.3 Cascade and confidence

- Accept a decision when its confidence is at or above the threshold set from the evaluation (section 10).
- Below it: ask an Ollama 14B model; still uncertain: one capped cloud-model call; still uncertain: the reviewer queue.
- Content that must not leave Rankuno servers (client confidential, staging, logged-in areas) never goes to Jev or cloud models: the same questions run on Ollama with JSON-schema output.
- Every label stores engine, model version, confidence and timestamp, so labels can be re-run when engines change.

## 7. Stage D: knowledge nodes and storage

- **Store**: Postgres with `pgvector` (one database with the ledger; about 150k chunk vectors for a 5,000-page site) plus Postgres full-text search for exact terms (product names, form codes, fund names). Qdrant only if a tenant grows past several million vectors.
- **Vectors**: one per chunk, one per page (mean of main chunks), one per offering card, one per node card. Embedding model pinned per run and recorded.
- **Nodes**: materialised views over facets: offering nodes (primary), section nodes (L1, L2), page-type nodes, audience nodes. A chunk belongs to every node its facets match.
- **Graph edges** (Postgres tables): page links, shares-template, duplicate-of, contradicts (R4), supports-offering, mentions-entity.
- **Node cards**: generated by Ollama from member chunks (map then reduce), refreshed when member content changes (recrawl change status).

## 8. Stage E: routing questions

1. Read the question's facets: site or competitor, offering, page type, audience, time (rules plus a Jev `choice` over node cards, or Ollama when local-only).
2. Select the top nodes by node-card similarity and the Jev scores.
3. Hybrid retrieval inside those nodes (vector plus full-text), filtered by facets.
4. Re-rank locally (`bge-reranker-v2-m3`).
5. Expand each hit to its neighbours and parent section (or the whole page when short) before answering, so no answer is built from an isolated fragment.
6. Answer with citations to block and evidence ids (Ollama for routine answers, a frontier model for client-facing analysis).
7. Site-wide questions ("what does each competitor offer in spend analytics?") are answered from offering and node cards, not by scanning all chunks.

## 9. Stage F: competitor comparison

1. **Industry taxonomy**: drafted once per industry by a cloud model from the union of the competitors' catalogs, reviewed by a person, versioned.
2. **Alignment**: each site's offerings are mapped to it (embedding similarity proposes, Jev `choice` decides, low confidence goes to review).
3. **Outputs** (reports R5 to R7, new):
   - Offering coverage matrix: industry offering by competitor, with page count, depth score, freshness, formats, proof points.
   - Attribute comparison per offering: features, deliverables, audiences, industries, pricing model and amounts (numbers from code, with context checks as in R4).
   - Presentation comparison: components, templates, CTA language, reading level, schema markup coverage.
   - Gaps and opportunities: offerings, features, FAQs or proof points a competitor has that the client lacks, ranked by competitor depth and recency, each with evidence links.

## 10. Engine allocation (least cost)

| Work | Engine | Cost profile |
|---|---|---|
| Stage A stamping, chunking, candidates from menus, URLs, schema; numbers; duplicates; components | Code | Free |
| Embeddings (chunks, pages, offerings, nodes) | Ollama: `bge-m3` or `qwen3-embedding` (multilingual) | Server compute only |
| Re-ranking | `bge-reranker-v2-m3` (sentence-transformers or a text-embeddings-inference server) | Server compute only |
| Page and chunk decisions on public content | Jev (`/api/v1/decisions`, native state and questions) | Per-call price not published; to be measured |
| Same decisions for confidential content; Jev fallback | Ollama 7 to 14B instruct with JSON-schema output | Server compute only |
| Catalog consolidation, offering card attributes, node cards | Ollama; one capped cloud review call per site | Mostly free; tens of cloud calls per site |
| Industry taxonomy draft; client-facing reports | Frontier model (Claude, GPT, Gemini), batch API and prompt caching | Small number of calls per engagement |
| Chinese API models | Not for client content until a data policy allows; the same open models can run on Ollama | |

Call volume for a 5,000-page site: Stage A none; Jev about 5,000 to 10,000 page requests (one or two passes) plus chunk batches; Ollama embeddings about 150k chunks once (re-embedded only on change); cloud calls in the tens. All model calls go through one `DecisionEngine` interface with the spend guard pattern (approval and caps; $0 cloud spend by default).

### Jev: facts and open points (docs read 2026-10-26: /docs, /jev-api, /mcp)

- Native endpoint `POST https://www.jevai.org/api/v1/decisions` with `state` and named `questions` of type `choice`, `score`, `noul`; bearer key; body cap 32 KiB; stated latency 70 to 500 ms; MCP endpoint available; SDK "coming soon".
- The docs show a second, conflicting API shape (`POST /v1/decisions` with `mode`); we build against the native shape only, behind our interface.
- Not stated: price, rate limits and daily quota, questions or options per request, data retention and training use, region, on-premise option, calibration evidence, non-English support. Until data handling is confirmed, only public content is sent.

## 11. Evaluation (before choosing engines and thresholds)

- **Gold set**: about 100 hand-labelled pages and 300 chunks from John Hancock, plus the same from one competitor site; labels for page type, offerings, audience, funnel stage, chunk role.
- **Metrics**: accuracy and macro-F1 per question; calibration (expected calibration error; do 90 % answers prove right about 90 % of the time); latency; cost per 1,000 pages; agreement between engines.
- **Head-to-head**: Jev against Ollama 7B and 14B on public pages; choose per question, set cascade thresholds from calibration curves.
- **Catalog quality**: precision and recall of discovered offerings against a hand-built catalog for two sites; every offering attribute must cite evidence (release gate: zero attributes without a supporting chunk).
- **Routing**: 30 to 50 real questions with known answer pages; top-3 node recall.
- **Context loss**: questions whose answer needs two or more sections of one page, or a reference to an earlier section; compare answer accuracy for (a) plain chunks, (b) chunks with context headers, (c) parent expansion, (d) late chunking. The chunking used in production must not score below whole-page retrieval on these questions.
- **Release gates (robustness first)**: zero offering attributes without a supporting chunk; per-question accuracy at or above target on the gold set; no loss on the context-loss set. An engine that fails a gate is not used for that task regardless of cost.

## 12. Build plan

| Step | Content | Models | Done when |
|---|---|---|---|
| 1 | Persist page metadata and JSON-LD; `PageStamp`; asset and pixel exclusion; menu trees; chunking | none | Every John Hancock URL has a stamp; zero asset URLs in knowledge |
| 2 | Offering candidates and consolidated catalog; offering cards with evidence | Ollama; capped cloud review | Catalog for John Hancock and one competitor reviewed |
| 3 | Gold set and evaluation harness; Jev and Ollama decision adapters | Jev, Ollama | Head-to-head report; thresholds chosen |
| 4 | Page and chunk categorisation with cascade; reviewer queue for low confidence | as chosen | Gold-set accuracy target met |
| 5 | pgvector store, node cards, routing, retrieval API, chat panel in the analyst app | Ollama, re-ranker | Routing top-3 recall target met |
| 6 | Industry taxonomy, alignment, coverage matrix, gap reports, presentation comparison | Jev, Ollama, frontier for reports | Reports for one client and two competitors |

## 13. Open questions

1. Jev: pricing, limits, data handling, calibration evidence, Hindi (email drafted separately).
2. Ollama server: GPU model and memory (decides 7B vs 14B vs larger for consolidation, and throughput).
3. Who owns each industry taxonomy and approves changes.
4. Data policy for client content with hosted model APIs (Jev, cloud, Chinese providers).
5. Postgres availability for durable storage of stamps, chunks and vectors (K1).
