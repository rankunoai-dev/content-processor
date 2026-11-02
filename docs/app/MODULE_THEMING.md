# Module: Theming (topic clusters, pillars, hierarchy, theme ownership)

Status: **built 2026-11-02** on `feat/theming` (`src/modules/theming/`). Step 2 of the roadmap in `MODULE_ANALYST_ACTION_CARDS.md`. Offline and $0: no model, no API, nothing sent anywhere.

## What it produces

For a project, across the client's website, its competitors in the project and its competitors from other projects (the competitive scope), one **theme map**:

| Part | What it is |
|---|---|
| Themes | Groups of pages about the same subject across all websites, named by their distinctive terms |
| Topics | Narrower groups inside a theme (only when a theme actually splits) |
| Pillar per website | The page that anchors the theme on each website (central, shallow, catalog offering, rich content), with the reasons; the rest are spokes |
| Ownership | `client_owned` (client ≥ 1.5× any competitor's pages), `competitor_led` (a competitor ≥ 1.5× the client's), `contested`, `client_gap` (client has none), `client_only` |
| Funnel stage per page | learn, compare, decide, onboard, support, from page type, address, title and calls to action |
| Offerings | Catalog offerings whose name words are all among a theme's terms |

## How it is built

1. Pages: every knowledge-eligible page of each website's freshest scrape (title, H1, breadcrumbs, headings, description, start of the main text).
2. One TF-IDF space for all websites (words and word pairs; titles ×3, headings ×2), with each website's own template words (on > 85% of its pages, and the word pairs containing them) removed. Embeddings are not used, because websites may have been indexed with different embedding models.
3. Seeded spherical k-means: the densest pages seed groups, pages join the nearest group above a threshold (themes 0.20, topics 0.38), groups are refined three times and merged when their centres are close; groups of one are left unclustered.
4. Theme ids come from the defining terms. When the map is rebuilt, a theme that keeps half its pages or a third of its terms keeps its previous id (`carry_ids`), so Monitoring and Gap Analysis can follow themes over time (`compare_maps`: appeared, disappeared, pages per website, ownership moves).

## Where it is used

- **App**: the project's **Themes** tab (build or rebuild after unlocking, ownership filter, pages per website, expandable theme with pillars, topics, funnel and offerings, funnel per website, workbook download).
- **CLI**: `themes build | show | compare --project <id>`.
- **API**: `GET/POST /api/projects/{id}/themes`, `GET .../themes/workbook`, `GET .../themes/changes`.
- **Action cards**: tool `get_theme_map` (reuses the latest map, builds one if none). Required and prefetched by "Topic Cluster & Pillar Map" (v2); optional for the funnel, offering-coverage and executive-briefing cards.
- **Storage**: `app_data/theming/<project>/<map id>.json`, `latest.json`, `<map id>_themes.xlsx` (Themes, Ownership matrix, Topics, Pages, How to read).

## Measured

Offline test project (John Hancock client, 272 pages; Flynn's competitor, 2 eligible pages): 274 pages in 0.4 s; 9 themes (fund notices 130, monthly distributions 79, earnings and fund releases 32, the "Longer Healthier Better" podcast 18, annuity e-signature forms 4, Apple Watch/Vitality 3, ...), 6 topics, 2 pages unclustered; 8 themes client-only and Flynn's security pages as the one client gap; a rebuild gives the same theme ids.

## Edge cases handled

| Case | Handling |
|---|---|
| A website not scraped or not indexed | Left out of the map and named in `skipped` |
| Fewer than two pages | Empty map, no error |
| A website's brand and template words dominating | Removed per website (> 85% of its pages), including word pairs that contain them |
| One topic dominating the whole corpus | Common-word cutoff at 85% of all pages, so the main subject of a news-heavy site is not discarded |
| Themes whose terms shift as content grows | Ids carried over by shared pages or terms; comparisons match the same way |
| A theme that does not split | No topics listed (instead of one topic repeating the theme) |
| Two builds in the same second | Map ids include milliseconds; older ids without them are still read |
| Large sites | Running group sums and a density order from one matrix-vector product (no page-by-page similarity matrix during seeding) |

## Not done yet

- Audience and brand-voice labels (planned with the local or Claude model, checked like card output).
- Weighting by search demand (needs the Research module).
- Embedding-based similarity when every website shares one real embedding model (bge-m3).
