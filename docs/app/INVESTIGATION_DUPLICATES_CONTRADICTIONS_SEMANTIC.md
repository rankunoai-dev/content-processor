# Investigation: project-level duplication and contradiction reports, and a Semantic module

Status: 2026-11-03. **Step 1 is built** (`feat/project-reports`, see `MODULE_01_CONTENT_SCRAPER.md`) with decisions 1–3 as proposed: client websites only with competitors optional, news and press figures on their own sheet, the proposed severity levels. Steps 2–6 (the Semantic module) wait for decisions 4–6 in section 7.

## 1. What was asked

1. **Per project**, download a duplication report and a contradiction report like the ones already produced for John Hancock.
2. **Exact (word-level) contradictions**: find every place where the same topic is stated differently on different pages, group them by topic, list the URLs of each version, and show the contradictory snippets. The target format is the one provided:

   | Topic | Version A | Version A URLs | Where A appears | Version B | Where B appears | Version B URLs | Severity | Recommended action | Approved answer (ML to fill) |
   |---|---|---|---|---|---|---|---|---|---|
   | Death claim payout time | 7-10 business days after the claim is in good order | help-center FAQ, life insurance hub | Help center hub; Life insurance FAQ | Typically within 60 days of filing | Death claims page | …/claims/death-claims | High | One approved timeline on all three pages, with its condition | |

3. **A Semantic module**, filled automatically from every finished scrape. It shows each project's content as vectors, and has two reports:
   - duplication judged by **meaning** (not by identical words), with the evidence passages;
   - if possible, contradictions judged by meaning.
4. **Why a separate module:** so an analyst can talk to the raw semantic content through Claude and build any report, without the engine's own processing (theming, gaps and so on) in between.

## 2. What exists today

| Piece | Where | What it does | Limits |
|---|---|---|---|
| **R2 overlap** (duplication) | `analysis/overlap.py`, in every scrape's report bundle (`r2_overlap.xlsx`, `r2a_overlap_groups.csv`, `r2b_overlap_pairs.csv`) | 5-word shingles, MinHash + LSH, each pair verified on the real text. It reports Jaccard and containment % (default threshold 80%) and the shared spans. | Words only. **Per website**, not per project. |
| **R4 contradictions** | `analysis/contradictions.py`, `analysis/claims.py`, `analysis/nli.py`; in the bundle (`r4_contradictions.{csv,xlsx,jsonl}`) | Finds sentences that make a claim and pairs them when they share content words. Three fixed checks: numbers that disagree on the same subject and unit, a negation flip, a conflicting effective date. Context buckets: matched / unknown / differs. An optional NLI judge (local model). Reviewer decisions are carried forward. | **One row per pair of sentences**, not per topic. Per website. The judge is off (no local model). |
| **Project scrapes** | `projects/runner.py` → `cli report` | Each website's scrape writes the full bundle, R2 and R4 included. They can be downloaded from that website's Reports tab. | **No project-level report**: nothing combines the client's websites, which now include its brands. |
| **Knowledge spaces** | `knowledge/` (stamp, catalog, index), filled automatically after each scrape. One space per website per project (`<tenant>.<project>.<site>`): chunk vectors, page vectors, a keyword index, labels. | This is already the "content as vectors, filled automatically" the Semantic module needs. | See section 3: today the vectors are **not semantic**. |
| **Similar pages** | the analyst tool `find_similar_pages` (page-vector cosine ≥ 0.85) | Pairs of near-identical pages. | Its own warning says: "word overlap (offline embedder), not meaning". No report or download. |
| **Claude access** | `src/modules/mcp_server` (read-only MCP server for Claude Desktop): `list_websites`, `search_knowledge`, `get_page`, `list_pages`, `find_similar_pages`, … | Claude can already read the raw passages. | It mixes raw content tools with engine outputs (themes, gaps). It has no "nearest passages", "compare" or "duplicate group" tools. |

## 3. Findings

### 3.1 R4 on John Hancock is noise for this purpose

The 303-URL John Hancock run (`exports/jh_full/reports_updated/jh_full_r4_contradictions.jsonl`):

- **14,679 pairs**, involving 214 URLs.
- **All 14,679** are numeric flags (`numeric:USD:$0.0975!=$0.0825`).
- **All 14,679** are between two **news pages**: mostly monthly fund-distribution press releases from different months and years. These are figures that belong to a period, not contradictions.
- 12,184 are marked `context_differs`; 2,495 are `context_matched`, because the period was not recognised.
- **0 pairs** involve product, help-centre or claims pages.

**Causes:**
- Time-bound pages (news, press releases, dated announcements) are not set aside.
- The period of a figure ("monthly distribution declared on …") is not read from the page date.
- Results are listed pair by pair, so one topic with 3 versions on 40 pages becomes hundreds of rows.

### 3.2 The death-claim example cannot be checked on existing data

The pages in the example (`/individual/help-center/life-insurance/...` and `.../claims/death-claims`) were **not among the 303 John Hancock URLs**, and neither timeline appears in the scraped text. They should be scraped and kept as the **reference test case**.

Even then, today's R4 might miss it:
- **Pairing needs shared content words.** "7-10 business days after the claim is in good order" and "typically within 60 days of filing" share few of them.
- **Units differ:** "business days" and "days" are not treated as the same unit.
- **Conditions differ** ("after good order" and "of filing"), so the pair would land in `context_differs`, outside the contradictions. Yet for a customer, this is exactly the conflict that needs one approved answer.

### 3.3 The vectors are not semantic yet

`cli kb-check` on this machine (2026-11-03):

- **Model server (Ollama):** not reachable. `bge-m3` is not available, nor any chat model.
- **Postgres:** configured and **connected, but the pgvector extension is not installed**.
- **Index would use:** SQLite with the **hashing fallback (words only)**.
- **Existing knowledge spaces:**
  - `rankuno-dev`: model `hashing-1024-v1`, 1,308 chunks.
  - The two JH test-project spaces: empty.

**So today, "semantic" duplication would be word overlap under another name.** Real embeddings (Ollama + `bge-m3`, steps B1–B4 of the connection guide) are a hard prerequisite for the Semantic module. With pgvector (steps A3–A5), the spaces can live in Postgres.

## 4. Proposed design: the word-level side (Content Scraper)

### 4.1 Project report download

A **Project reports** button on the project creates one bundle from the **latest finished scrape of each of the client's websites** (the main website and its brands):

- **Duplication (R2)** within each website and **across the client's websites**: the same text on two brands is reported too.
- **Contradictions grouped by topic** (4.2) across all the client's pages.
- **Competitors:** each competitor's own duplication and contradictions as separate sheets, optional. A competitor contradicting the client is not a contradiction, and never belongs in this report.

It is built from the existing records files, so nothing is scraped again. It is written to `app_data/projects/<p>/reports/<stamp>/` and downloaded as a zip and an xlsx.

### 4.2 Topic-grouped contradiction report (the requested format)

1. **Facts, not just sentences.** From each claim-bearing sentence, extract normalised facts:
   - durations ("7-10 business days" → 7–10 business days; "within 60 days" → ≤ 60 calendar days);
   - money, percentages, ages, counts and limits;
   - phone numbers, emails and addresses;
   - yes/no statements ("is / is not covered").

   Each fact keeps its verbatim quote, URL, heading path, block id and evidence id, so every snippet stays traceable.
2. **Topic key.** What the fact is about: the question or heading it sits under (FAQ question, H2/H3), plus the subject words of the sentence. Units are converted (business days ↔ days are marked "comparable with a condition"). Facts are grouped into topics by these keys. In phase 2, grouping by meaning (section 5) merges topics worded differently.
3. **Versions.** Within a topic, each distinct normalised value is one version, with all its URLs and where it appears (page title and section). A topic with two or more versions is one row: Version A, Version B (and C, … in extra columns or rows).
4. **Set aside, not hidden:**
   - **Time-bound pages** (news and press releases by page type or path; figures tied to a date, such as a fund distribution "declared on …"). They go to a separate sheet, "Time-bound figures (not contradictions)".
   - **Different audiences or regions** (state-specific, product-specific). They go to "Differs by condition", with the condition shown. The death-claim case goes to the main sheet with the condition noted: a customer-facing commitment stated two ways is a finding even when the conditions differ.
5. **Severity, by rule:**
   - **High:** customer commitments (payout or processing time, fees, prices, eligibility, coverage, legal or compliance terms).
   - **Medium:** product specifications and contact details.
   - **Low:** marketing figures.
6. **Recommended action:** a fixed, plain sentence per kind ("Agree one approved timeline on all N pages, with its condition"). **Approved answer (ML to fill)** stays empty for the team.
7. **Columns:** exactly those of the example, plus evidence columns at the end (block ids, evidence ids, captured-at) and the reviewer decision, which is carried forward between scrapes as in R4 today.

The existing R4 pair report stays available as "all candidate pairs" for audit.

### 4.3 Cost

$0, all offline: rules and normalisation, no model. On John Hancock-sized runs (~300 pages) it takes seconds.

## 5. Proposed design: the Semantic module

### 5.1 What it is

A module of its own (left menu: **Semantic**). It is a **read layer over the knowledge spaces that already exist**, not a second copy of the data:

- **Fills itself automatically.** Every finished scrape already builds its website's knowledge space. The Semantic module lists every project, its websites and their spaces, with the embedding model, passage and page counts, the date built, and whether the space is semantic (a real model) or words-only (fallback).
- **One project = one semantic view** over its websites' spaces. Each space is searched with its own model; results are combined by rank, as the existing federated search does.
- **No engine interpretation.** No themes, gaps, ownership or funnel: only passages, pages, vectors, neighbours and the evidence behind them.

### 5.2 Semantic duplication report

- **Pages:** pairs or groups with page-vector cosine ≥ 0.92 ("same content") or 0.85–0.92 ("overlapping / competing pages"), within a website and across the client's websites.
- **Evidence for every page pair:** the best-matching passage pairs (chunk-to-chunk cosine), side by side with their URLs, headings and evidence ids. This shows *what* is duplicated, not just a score.
- **Next to the word-level result:** each pair also shows its R2 Jaccard. "Same meaning, different words" (high cosine, low Jaccard) is the new finding R2 cannot give.
- Downloadable as xlsx and csv per project. Thresholds can be changed per report.

### 5.3 Semantic contradiction report (phase 2)

1. **Candidates by meaning:** for every client passage, its nearest passages on other pages (top-k, cosine ≥ 0.75). Same topic, whatever the words: this catches "7-10 business days after good order" against "typically within 60 days of filing".
2. **Filter:** keep pairs whose extracted facts (4.2) differ, or that contain a yes/no or "must/may" statement.
3. **Judge:** decides contradiction / compatible / different conditions, both ways round, with a short reason.
   - **Local option:** Ollama (`qwen2.5`). $0, but slow on a 4 GB GPU and weaker.
   - **Claude option:** better, metered, and subject to the client-data policy decision.
4. **Same output as 4.2:** grouped by topic, versions, URLs, snippets, severity, recommended action, empty approved answer. Each row says whether it was found by words, by meaning, or both.

### 5.4 Claude working on the raw content

The read-only MCP server gets a **Semantic tool set** limited to raw content:

| Tool | What it returns |
|---|---|
| `semantic_spaces` | Projects, websites, spaces: model, counts, freshness. |
| `semantic_search` | Passages by meaning (and words) across a project's websites. |
| `nearest_passages` | The passages closest to a given passage or text, across pages and websites. |
| `compare_passages` | Two or more passages side by side, with their facts and evidence. |
| `duplicate_groups` | Groups of pages and passages with the same meaning. |
| `get_page` / `get_passages` | The verbatim text of pages and passages. |

Each result carries URL, heading, chunk id and evidence ids, so Claude's reports cite their sources exactly as the analyst cards do. Theming, gaps, monitoring and research tools stay in their own modules and are not part of this set.

## 6. Proposed steps

| Step | What | Depends on | Cost |
|---|---|---|---|
| 1 | **Project reports button.** Bundles the latest scrape of every client website (R2 across the client's websites + topic-grouped contradictions 4.2). Time-bound pages set aside. Reference test: scrape the 3 death-claim URLs and confirm the row in the example comes out. | nothing | $0 |
| 2 | **Real embeddings.** Ollama + `bge-m3` (and pgvector if Postgres is to hold the vectors); rebuild the existing spaces. | installs on this machine | $0 |
| 3 | **Semantic module screen:** projects and spaces, status, search, rebuild. | 2 | $0 |
| 4 | **Semantic duplication report** with passage evidence and the R2 comparison. | 2, 3 | $0 |
| 5 | **Semantic contradiction candidates** + judge. | 2, 4, judge decision | $0 local / metered with Claude |
| 6 | **Semantic MCP tools** for Claude. | 2, 3 | $0 (the person's own Claude) |

## 7. Decisions needed

1. **Project report scope.** Client websites only (main + brands), with competitors optional as separate sheets? (Proposed: yes.)
2. **Time-bound content.** Set news and press-release figures aside on their own sheet (proposed), or drop them?
3. **Severity rules.** Are the High / Medium / Low kinds in 4.2 right for your clients (insurance, finance, print)?
4. **Embeddings.** Install Ollama + `bge-m3` on this machine now (needed for every semantic step)? And pgvector in Postgres, or keep the vectors in SQLite for now?
5. **Contradiction judge by meaning.** Local model (free, slower, weaker), or Claude (better, metered, sends client page text to Anthropic, which the data-policy decision has to allow)?
6. **Semantic module access.** Open to read like reports, or behind the project password?
