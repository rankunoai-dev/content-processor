# Content Source Tagging and Change-Aware Recrawl

> **Document ID**: `CP-DOC-PROVENANCE-V1` · **Date**: 2026-10-16 · **Branch**: `feature/provenance-recrawl`

## 1. Every block says where it came from and when

Every `ContentBlock` (HTML source, rendered DOM, explored states, PDF, image OCR) carries:

| Field | Meaning | Example |
|---|---|---|
| `source_format` | Artefact the text came from | `html`, `pdf`, `image`, `office_doc`, `spreadsheet`, `presentation`, `text`, `json`, `xml`, `audio`, `video`, `other` |
| `ui_region` | Where it sits on the page or in the document | `main_content`, `header`, `hero`, `nav_menu`, `hamburger_menu`, `mega_menu`, `breadcrumb`, `sidebar`, `footer`, `modal`, `popover`, `tooltip`, `accordion_panel`, `tab_panel`, `carousel`, `card`, `snippet`, `palette`, `search`, `form`, `table`, `cookie_banner`, `chat_widget`, `ad_slot`, `page_metadata`, `document_metadata`, `pdf_page`, `pdf_table`, `pdf_form`, `pdf_annotation`, `image_text`, `unknown` |
| `source_url` | URL it was taken from (after redirects) | `https://site/plans` |
| `extracted_at` | When we extracted it (UTC) | `2026-10-16T09:12:03Z` |
| `page_modified_at`, `page_modified_source` | When the site says the page or file was last updated, and which signal said so | `2026-09-01T10:00:00Z`, `jsonld:dateModified` |
| `reused_from_run_id` | Set when the page was unchanged and the block was carried forward | `run-20261015-ab12cd34` |

`ui_region` is decided in the HTML extractor from the tag, ARIA role and label, and the id/class vocabulary of common themes (Bootstrap, Tailwind, WordPress, AEM, Shopify). The most specific region wins: a link in a hamburger menu inside the header is `hamburger_menu`. It is a strong hint, not a guarantee; custom markup without these cues is `main_content`, the inherited region, or `unknown`. Documents and images have no DOM, so their region comes from the extractor (`pdf_table`, `image_text`, ...).

## 2. Page update dates

Read on every fetch, strongest first: JSON-LD `dateModified`; meta `article:modified_time`, `og:updated_time`, `last-modified`, `dcterms.modified`, `DC.date.modified`, `revised`; microdata `itemprop="dateModified"`; PDF `/ModDate`; HTTP `Last-Modified` unless it equals the fetch time (many CMSs stamp every response "now"). `datePublished` is kept separately. Nothing is invented: no signal means no date. Resources and UrlRecords also keep the HTTP `ETag` and `Last-Modified` validators.

## 3. The next visit: judge, then skip

Run with the previous run's records as the baseline:

```powershell
python -m src.modules.content_processor.cli dryrun --scope s.json --out exports/run1.json
python -m src.modules.content_processor.cli dryrun --scope s.json --out exports/run2.json --baseline exports/run1.records.jsonl
# durable: --baseline-run <previous run id>, or scope.recrawl_of_run_id
```

For each URL with a reusable previous capture the fetch is conditional (`If-None-Match`, `If-Modified-Since`), then:

| Order | Evidence | Verdict |
|---|---|---|
| 1 | Server answers 304 Not Modified | unchanged |
| 2 | Identical bytes | unchanged |
| 3 | Identical visible text (markup, nonces or tracking tokens changed) | unchanged, note `bytes_differ` |
| 4 | Text changed and the site's modified date moved forward | changed |
| 5 | Text changed but the site's date did not move | changed, note `site_modified_date_not_updated` |
| 6 | Anything else that differs | changed |

Content wins over dates: a stale `dateModified` never hides an edit, and a moved date with identical text is reused (noted as `site_modified_date_moved`).

**Unchanged:** blocks, gaps and summaries are carried forward under the new run with their original `extracted_at` and `reused_from_run_id`; no render, state exploration or OCR is queued; the URL keeps its previous verdict; links it pointed to (PDFs, images) are re-queued and each gets its own conditional check. **Changed or new:** the full pipeline runs. The report shows `changes` counts and each record carries `change_status`, `change_basis`, `previous_run_id`.

A capture is reused only if it was acquired (badge VERIFIED_WITHIN_SCOPE, PARTIAL or NEEDS_REVIEW, payload present, not FAILED or BLOCKED); blocked or failed URLs are always retried.

## 4. Measured on the local fixtures (real Chromium)

| Run | Fetched | Rendered | Changes | Wall clock |
|---|---|---|---|---|
| 1, no baseline | 21 | 17 | all `not_compared` | 87 s |
| 2, baseline = run 1 | 21 | 2 | 15 unchanged, 3 new, 3 not compared | 33 s |

No reused page changed its verdict. The 3 `new` are URLs blocked or still running in run 1 (not reusable); the 3 `not_compared` are judged before the change check (policy exclusion, soft 404, truncated body) and are simply fetched again.

## 5. Limits

- The change check for HTML uses the server HTML. A page whose server HTML is identical but whose client-side data changed (pure SPA, API-fed prices) is judged unchanged; such hosts need `--baseline` off or a future render-level fingerprint.
- Sitemap `<lastmod>` is not read yet; it would let a run skip even the conditional request.
- PDF and image dates come from `/ModDate` and HTTP headers only (no XMP or EXIF date yet).
