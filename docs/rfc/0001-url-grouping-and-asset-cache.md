# RFC 0001: Template-aware URL grouping and group-scoped asset cache

**Status**: Implemented on `feat/url-grouping` (Bits 1 and 2), not merged to `main` · **Date**: 2026-10-20
**Owners**: Stage 1 acquisition and extraction · **Related**: `WORK_BREAKDOWN.md` 4 (lanes), `STAGE4_ANALYSIS_AND_INTERFACES.md` I2 (overlap)

## 1. Problem

Pages of one site often share a template: the same navigation, the same stylesheets, scripts, fonts and logo, and the same boilerplate blocks. Today every URL is fetched, rendered and extracted independently. A run over a service section therefore repeats the same asset requests to the client site, and repeats the same boilerplate extraction, on every page.

The goal is to reduce both the requests sent to client sites and the extraction work on our side, without weakening per-page evidence. Measured on the John Hancock run, extraction is cheap (about 18 ms per page), so the main lever is requests and browser time. Section 7 records the measurements.

## 2. Non-goals

- **Skipping page fetches.** Every URL is still fetched and gets its own status and evidence. Fewer page fetches would need change detection (conditional requests, content hashes), which is a separate design.
- **Sharing page content between URLs.** Shared boilerplate may be reused as extraction work, but each page keeps its own block records and provenance (axiom 4 in `ARCHITECTURE.md`).
- **Bypassing politeness.** Grouping never changes per-host rate limits, robots decisions or block handling.

## 3. Data contracts

All contracts extend `StrictModel` (unknown fields are rejected). Code: `src/modules/content_processor/grouping/`.

### 3.1 `PageSignal` (input)

| Field | Type | Meaning |
|---|---|---|
| `url` | `str` | Requested URL |
| `http_status` | `int \| None` | Final HTTP status; `None` when no response |
| `media_type` | `str \| None` | Sniffed or declared media type |
| `body` | `str` | HTML text; empty when not fetched |

### 3.2 `UrlGroup` (output)

| Field | Type | Meaning |
|---|---|---|
| `group_id` | `str` | `grp-` + first 12 hex of sha256 over the family and sorted members (stable across runs) |
| `path_prefix` | `str` | Path family, for example `/services` |
| `members` | `list[str]`, min length 1 | URLs in the group, sorted |
| `reason` | `template_match \| singleton \| spa_shell` | Why these URLs are together |
| `template_fingerprint` | `str \| None` | sha256 of the first member's skeleton; `None` for singletons and shells |
| `confidence` | `float` in [0, 1] | Mean pairwise skeleton similarity (1.0 for singletons, 0.0 for shells) |

### 3.3 `ExcludedUrl` (output)

| Field | Type | Meaning |
|---|---|---|
| `url` | `str` | URL kept out of grouping |
| `reason` | `str` | `http_<status>`, `not_html:<type>`, `empty_body`, `no_response`, or `challenge_or_block_page` |

### 3.4 `GroupingResult` (output)

`groups: list[UrlGroup]`, `excluded: list[ExcludedUrl]`, `similarity_threshold: float`. Every input URL appears exactly once, either in a group or in `excluded` (denominator integrity, axiom 1).

### 3.5 Asset cache contracts

`CachedAsset(status, headers, body)`: one stored response, enough to replay it.

`CacheStats`: `origin_requests`, `origin_bytes`, `cache_hits`, `cache_bytes_served`, `not_cacheable`, `stored`, `skipped_too_large`. Reported per group in the run artifact.

`AssetCache(group_id, max_bytes = 32 MiB)`: one store per group. Methods: `lookup(url, resource_type, method)`, `record_origin(url, resource_type, method, status, headers, body)`, `__len__`. Entry point: `install(context, cache)`, which routes a Playwright browser context through the cache.

## 4. Algorithms

### 4.1 Gate (`gate.assess`)

Runs before any fingerprinting, in this order:

1. No response → excluded `no_response`.
2. Status ≥ 400 → excluded `http_<status>`.
3. Media type present and not `text/html` → excluded `not_html:<type>`.
4. Empty body → excluded `empty_body`.
5. A block or challenge marker in the first 4 KiB (`access denied`, `challenge-platform`, `cf-browser-verification`, `captcha`, and similar) → excluded `challenge_or_block_page`. The check applies even when the status is 200, because bot managers sometimes return block pages that way.
6. Body contains `<script` and fewer than **20** visible words in the static HTML → `spa_shell`.
7. Otherwise → `template`.

### 4.2 Path family (`prefix.path_family`)

The first path segment with a leading slash. `/services/tax` and `/services/estate` share `/services`. `/services-archive/x` gets `/services-archive`. Matching is by whole segment, never by string prefix.

### 4.3 Skeleton and similarity (`skeleton`)

The skeleton is the tag sequence of `<body>` in document order, written as `tag@depth`. Text, attributes, `<head>`, comments and text nodes are excluded.

Similarity is `difflib.SequenceMatcher` ratio over the two token sequences (ordered, no junk heuristic). Identical fingerprints short-circuit to 1.0.

Alternatives measured and rejected:

| Measure | One extra aside (25-token page) | Swapped main and footer | Why rejected |
|---|---|---|---|
| 4-token shingle Jaccard | 0.76 | not measured | Splits real variants on short pages |
| 2-token shingle Jaccard | 0.80 | not measured | Same |
| Bag-of-tags Jaccard | 0.93 | not measured | Ignores order, so a reordered page looks identical |
| Ordered ratio (chosen) | 0.96 | 0.92 | Tolerates small edits and reordered sections, sensitive to real layout change |

### 4.4 Clustering (`cluster.group_urls`)

1. Gate every page; excluded pages go to `excluded`.
2. Bucket `template` pages by path family; `spa_shell` pages form one group per family with no template claim.
3. Within a family, link two pages when their similarity is at least **0.9**. Groups are connected components of that graph.
4. A component of one page is a `singleton`. A larger component is `template_match`, with the fingerprint of its first member and mean pairwise similarity as confidence.

Output is deterministic: groups are sorted by family and first member, and the test suite checks equality under shuffled input.

**Known limit.** Linking is transitive through connected components, so A and C can join through B even when A and C are below the threshold. Confidence exposes this (mean pairwise similarity). Pairwise cost is O(n²) per family. A MinHash prefilter is the planned fix at scale (section 8).

### 4.5 Asset cache (`asset_cache`)

- Cacheable only when the request is `GET` and the Playwright resource type is `stylesheet`, `script`, `font` or `image`.
- Stored only when the response is `200` and has no `Cache-Control: no-store`.
- Served from the store when present; otherwise fetched from the origin with `route.fetch()`, then stored.
- Per-group scope: one `AssetCache` per group, dropped when the group finishes.
- Size bound: 32 MiB per group. A larger asset is passed through and counted in `skipped_too_large`.

## 5. Thresholds

| Name | Value | Basis |
|---|---|---|
| Template similarity | 0.9 | Merges a one-element variant (0.96) and a reordered-section page (0.92); splits layout changes |
| Shell words | 20 | Real articles measured at 145 to 163 words; shells have none |
| Cache size per group | 32 MiB | Provisional; a single group of 5,000 URLs would need calibration |
| Cacheable resource types | stylesheet, script, font, image | Static types only |

Thresholds are provisional until they are calibrated on fixtures (section 8).

## 6. Edge cases and their handling

| Case | Handling | Test |
|---|---|---|
| Prefix collision (`/services` vs `/services-archive`) | Whole-segment families | `test_prefix_collision_archive_page_is_not_merged_into_services` |
| 403 block page | Excluded `http_403` | `test_blocked_error_page_is_excluded_not_grouped` |
| Block page served with 200 | Excluded `challenge_or_block_page` | `test_block_page_served_with_200_is_still_excluded` |
| Client-rendered shell | `spa_shell`, no template claim | `test_client_rendered_shell_is_flagged_not_fingerprinted` |
| Article with heavy markup | Template (word count, not ratio) | `test_markup_heavy_article_is_a_template_page_not_a_shell` |
| Same family, different layout | Separate singleton | `test_different_template_in_same_family_stays_separate` |
| Reordered sections of one template | Still grouped at 0.9 | `test_reordered_sections_of_one_template_still_group` |
| Threshold sensitivity | Verified at 0.85 and 1.0 | `test_threshold_controls_merging` |
| Non-HTML, empty, no response | Excluded with reason | `test_non_html_and_empty_responses_are_excluded_with_reason` |
| Input order | Deterministic output | `test_grouping_is_deterministic_under_input_order` |
| Document and API requests | Never cached | `test_documents_and_api_calls_are_never_cached` |
| `POST` for a static type | Never cached | `test_post_is_never_cached_even_for_a_static_type` |
| `no-store`, error status | Not kept | `test_no_store_and_error_responses_are_not_kept` |
| Oversized asset | Passed through, counted | `test_size_limit_skips_storage_without_failing` |
| Browser end to end | Each static asset fetched from origin once per group | `test_group_cache_fetches_each_static_asset_once` |

**Open edge cases** (not yet handled, recorded from the real run):

- **Soft 404 with content.** `/errors` on the John Hancock run returns 200 with 1,279 words. Grouping treats it as a singleton. Detection belongs to the Stage 1 soft-404 logic (CR-006), not to grouping.
- **Possible internal split.** The 7 `/about-us/news` pages split 3, 2, and two singletons. This may be real template variation; it has not been checked against the rendered pages.
- **Shared-browser-cache interaction.** A single shared browser context already reuses assets through Chromium's own cache. The group cache matters for isolated contexts. Its value in shared contexts is not yet measured.
- **Cache keys across query strings.** Keys are full URLs, so `app.js?v=1` and `app.js?v=2` are separate entries. Correct, but it lowers hit rates on versioned assets.

## 7. Measurements

**Grouping on real pages** (John Hancock run, 34 stored pages, offline): 8 groups. The 20 help-center pages form one `template_match` group with confidence 0.99. The 5 insights pages form a second `template_match` group with confidence 0.99. No page is excluded.

**Asset cache on the local services fixture** (4 pages, one group, headless Chromium, same machine):

| Scenario | Origin requests | Time |
|---|---|---|
| A: isolated context per page, no cache | 24 | 2.70 s |
| B: isolated context per page, group cache | 9 (4 documents, 5 assets) | 2.63 s |
| C: one shared context (browser cache) | 21 | 2.35 s |

Asset requests fall from 20 to 5 (75%). Total origin requests fall 62%. Wall-clock time barely changes on localhost, where latency is near zero, so the time saving on real sites is unmeasured.

**Extraction on the real run:** 34 pages extract in 0.59 s in total (median 18 ms per page). Blocks repeated across pages of the help-center template carry about 35 distinct texts.

**Live site:** not measured. The John Hancock site blocks headless Chromium with an "Access Denied" page (HTTP 403). The timings above come from the local fixture only.

## 8. Remaining work

1. **Template-aware boilerplate reuse (Bit 3).** For a group, identify blocks shared by most members, extract them once, and record the reuse per page. Per-page records and evidence remain.
2. **Scheduler integration.** Wire grouping and the asset cache into the run scheduler (`runner.py`) so multi-URL runs execute group by group. This crosses lane boundaries and needs a contract request for the run artifact fields.
3. **Scale.** MinHash prefilter before pairwise similarity, for families in the thousands.
4. **Calibration.** Threshold and cache size on a larger fixture set, with a labelled set of template pairs.
5. **Latency measurement.** Re-run section 7 with simulated latency, then on a live site once the blocking question is settled.
6. **Run artifact fields.** `group_id`, `reason`, `template_fingerprint` and per-group cache statistics added to the exported run JSON.

## 9. Decisions

- **D1.** Ordered skeleton similarity over shingles or bags of tags. Chosen by measurement (section 4.3).
- **D2.** Gate before fingerprint. Error and block pages never enter a template.
- **D3.** Word count, not a markup ratio, for shell detection. The ratio misclassified real articles.
- **D4.** Group-scoped cache, not run-scoped. A page never reads an asset that belongs to another family, and memory is released per group.
- **D5.** Grouping does not change fetch, politeness or evidence rules. Its gains are in requests for shared assets and in extraction reuse.

## 10. Addendum: Bit 3, template-aware boilerplate reuse

**Implemented** in `grouping/boilerplate.py`. A block text is shared when at least 80% of a group's pages contain it (and at least two pages). Texts shorter than 20 characters are ignored. Matching is exact on normalized text, so a disclaimer that differs by one date is not shared. `ReuseCache.get_or_compute` computes each distinct text once per group. Each page keeps its own block records; only the computation is reused.

**Measured on the John Hancock help-center group** (20 pages, stored evidence, offline):

- 31 shared block texts; every main-role block occurrence (640 of 640) is shared, and so is every character (73,540).
- The static HTML of these pages is identical in its main text. Page-specific content (for example, the form name in the title) does not appear in the static markup, so it must come from client-side rendering.

**Consequence.** Bit 3 reuses work on static boilerplate, and this group is entirely static. The page-specific content is produced in the browser, so its cost is per page and sits in the render path, where Bit 2's asset cache acts. Reuse ratios on other sites will be lower where pages carry more static unique text.

**Timing.** Static extraction for this group took 0.38 s in one run and 0.89 s in another for 20 pages (variance between runs; measured, not modelled). The saving from reuse is therefore small on this site, and the render path remains the dominant cost.

**Edge cases tested:** navigation and footer shared; unique article text not shared; share threshold (80% strict, 60% loose); near-identical disclaimer not shared; short strings ignored; single-page group shares nothing; reuse cache computes once per distinct text.
