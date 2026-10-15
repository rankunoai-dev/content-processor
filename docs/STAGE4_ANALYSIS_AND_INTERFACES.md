# Stage 4 Backlog — Website Analysis Features and Review Interfaces

> **Document ID**: `CP-DOC-STAGE4-BACKLOG-V1`
> **Status**: Backlog specification. Nothing here is built in Stages 0–2; it is recorded so that Stage 1–2 data contracts carry the fields these features need.
> **Date**: 2026-10-14
> **Depends on**: `ARCHITECTURE.md` §9 contracts (`block_key`, `role`, `claim_context`, `template_fingerprint`, versions), §10 status vector, evidence store.

---

## 1. The three analysis features (intended mechanisms)

| Feature | Mechanism | Fields it needs from Stage 1–2 |
|---|---|---|
| **Website theming** | **Template fingerprinting.** Repeated headers, navbars and footers are labelled `BOILERPLATE` (never deleted) using cross-page frequency and the DOM-skeleton fingerprint. What remains is the site's design profile: CSS custom properties, font families, colour palette, spacing scale, layout grid, and the structural hierarchy of page types. Output is a set of clean theme profiles per page-type cluster. | `template_fingerprint`, `design_evidence`, `role` on every block, computed-style samples |
| **Content similarity across the site** | **Content-addressed blocks.** Every block's text is normalized (Unicode NFC, whitespace collapsed, reversible map kept) and hashed (sha256). Identical text across 100 pages is stored once and linked by occurrence edges, so exact-match similarity is a join and semantic similarity embeds each distinct block once. | `block_key`, `normalized_text`, `dedupe_of`, occurrence edges `(block_hash → resource, state, region)` |
| **Contradictory content detection** | **ClaimContext and temporal versioning.** Contradictions require matched context: a 2024 return policy does not contradict a 2026 update; a US price tier does not contradict a UK tier. Each claim-bearing block records capture sequence, publication/effective dates, audience, geography, product/variant and extracted entities, so only comparable claims are compared, and every candidate pair ships with both quotes and their evidence. | `claim_context`, `captured_at`, run version chain, `evidence_ids`, status and layer for gap-aware confidence |

Gap-awareness is non-negotiable across all three: a block from a `PARTIAL` URL, an OCR block below threshold, or a `paywalled_in_source` block carries reduced weight and is labelled as such in every UI and report.

---

## 2. Interfaces and reports (requested 2026-10-14)

All four interfaces share: a run selector; filters by URL badge, layer and role (boilerplate excluded by default, toggle to include); a percentage control where the feature is threshold-based; proof panels that open the evidence (DOM selector, screenshot crop, PDF page/bbox); and a **Download** button producing CSV and XLSX (JSONL for programmatic use). Every row in every report carries the `run_id`, the `block_id`s involved and the evidence ids, so a reviewer can trace any number back to bytes.

### I1 — URL Content Explorer

**Purpose**: after a run completes, show the whole extracted content of each URL and let the user export it.

- **Page view** per URL: badge and status vector; whole content in reading order with layer and role colour-coding; gap list with evidence links; per-state tabs (base state, each explored tab/accordion state); document pages for PDFs with region overlays.
- **"Whole content" definition** (default): concatenation of `rendered_visible` + `collapsed_published` blocks, main role first then other roles, in reading order, with headings preserved. Optional toggles add `accessibility_only`, `metadata`, `hidden`, `source_only`, `third_party_chrome`, `ugc`.
- **Report R1 (URL content)**: one row per requested URL.

| Column | Content |
|---|---|
| `url` | requested URL (aliases in a secondary column) |
| `whole_content` | the content string as defined above |
| `badge` | URL badge |
| `word_count` | of `whole_content` |
| `gap_count` / `gap_types` | summary of gaps |
| `captured_at`, `run_id` | provenance |

XLSX cells cap at 32,767 characters; longer content is split across `whole_content_part_N` columns and flagged, never truncated silently.

### I2 — Exact and overlap similarity groups

**Purpose**: group URLs that share the same content, by (a) regex/keyword matching and (b) content-to-content overlap percentage.

- **Mode A, pattern/keyword**: user supplies regexes or keyword lists; the system reports, per URL, which patterns matched and where (block ids, spans), and groups URLs by identical match sets or by a chosen pattern. Keyword-to-keyword mode compares the keyword sets extracted per URL (user-defined vocabulary or top-n terms) with a Jaccard percentage.
- **Mode B, content overlap**: shingle-based (word 5-grams by default, configurable) **containment** and **Jaccard** between URL contents, computed with MinHash + LSH so 5,000 URLs do not need 12.5 million exact comparisons; exact pairs above the threshold are verified with the real shingle sets. Threshold slider (default 80%). Grouping is connected components over pairs above threshold, with a "strict" option that requires every pair in a group to exceed the threshold.
- **UI**: group list with size and median overlap; pairwise matrix for a group; side-by-side view with overlapping spans highlighted; the shared block hashes listed with their occurrence counts across the site.
- **Report R2a (groups)**: `group_id, url, overlap_to_group_pct, shared_block_count, shared_block_hashes, mode, threshold`.
- **Report R2b (pairs)**: `url_a, url_b, containment_a_in_b_pct, containment_b_in_a_pct, jaccard_pct, shared_spans (first 3 quoted), evidence_ids`.
- **Report R2c (pattern matches)**: `url, pattern, match_count, first_match_quote, block_ids`.

### I3 — Semantic similarity

**Purpose**: the same grouping and reports as I2, but on meaning rather than overlapping text.

- Embeddings per distinct block (local `sentence-transformers`, model pinned per run; multilingual model so Hindi and English pages compare), stored in Qdrant keyed by `block_hash`, filtered by layer, role and status. Page-level vectors are the length-weighted mean of main-role block vectors; block-level search is also available.
- Score = cosine similarity shown as a percentage; threshold slider (default 85% page-level, 90% block-level). Grouping as in I2.
- **Proof panel**: for a URL pair, the top-k nearest block pairs with both quotes and their evidence, so a reviewer sees *why* two pages are considered similar.
- **Reports R3a/R3b**: same columns as R2a/R2b with `semantic_pct` and `top_block_pairs` (quoted) instead of shingle fields; `model_name, model_version` on every row.

### I4 — Contradiction candidates

**Purpose**: surface statements across the site that appear to contradict each other, with the percentage match and the proof.

- **Candidate generation**: claim-bearing blocks (declarative sentences with entities, numbers, dates, conditions or negations) are paired by semantic similarity (I3) **and** matched `claim_context` (same entity/product/variant, overlapping effective dates, same audience and geography). Pairs with mismatched context are shown in a separate "context differs" bucket, not as contradictions.
- **Scoring**: a natural-language-inference judge (local model first via the LLM Gateway; OpenRouter within the run's cost cap) returns entailment / neutral / contradiction probabilities; the UI shows the contradiction probability as a percentage with the threshold slider (default 70%). Deterministic checks run first and are reported separately: numeric disagreement on the same entity and unit, negation flip, date-range conflict.
- **Proof on the UI**: both quotes verbatim, their heading paths, URLs, capture times, layers and statuses, the context fields side by side, and the evidence crops. Gap-aware confidence is displayed next to the score (for example "OCR source, 0.62 confidence" lowers the displayed confidence band).
- **Report R4**: `pair_id, url_a, quote_a, url_b, quote_b, contradiction_pct, deterministic_flags, context_match (fields), confidence_band, evidence_ids_a, evidence_ids_b, reviewer_decision (blank for the reviewer to fill)`.
- Reviewer decisions (confirmed / not a contradiction / context differs) are stored and fed back as labelled examples.

---

## 3. Percentage definitions (so every number in a report means one thing)

| Name | Definition |
|---|---|
| `containment_a_in_b_pct` | shared shingles / shingles in A |
| `jaccard_pct` | shared shingles / union of shingles |
| `keyword_jaccard_pct` | shared keywords / union of keywords |
| `semantic_pct` | cosine similarity × 100 of the chosen vectors |
| `contradiction_pct` | judge's contradiction probability × 100 |
| `overlap_to_group_pct` | containment of the URL in the group's union of shingles |

---

## 4. Prerequisites that Stage 1–2 must already satisfy

- Every block has `block_key`, `normalized_text` with a reversible map, `role`, `layer`, `language`, `evidence_ids`, `status`.
- Occurrence edges from block hash to every (resource, state, region) where it appears.
- `claim_context` populated where detectable (dates, entities, variants, geography, audience, conditions); empty is allowed and shown as "context unknown".
- Runs are versioned so I2–I4 can compare across time as well as across pages.
- Export views exist in Postgres for R1–R4 so n8n or a spreadsheet can pull them without the UI.

---

## 5. Build order (one at a time, as requested)

1. I1 URL Content Explorer and report R1 (needs only Stage 1 output).
2. I2 pattern and overlap grouping with R2a–R2c (shingling and MinHash; no models).
3. I3 semantic similarity with R3a–R3b (Qdrant, local embeddings).
4. I4 contradiction candidates with R4 (claim extraction, NLI judge, reviewer loop).

Each interface ships with the same acceptance style as the extractor: a fixture set where the expected groups and expected contradiction pairs are known, and a release gate of zero fabricated proofs (every displayed quote must exist verbatim in the evidence).
