# Gap Audit and Extended Edge-Case Inventory

> **Document ID**: `CP-RPT-GAP-AUDIT-V1`
> **Status**: Investigation addendum (SDLC Step 1 close-out, feeds Step 2)
> **Date**: 2026-10-14
> **Reviewed inputs**: the original problem statement; the "Final Unified Investigation" (90-case synthesis of Investigations A and B); the primary 83-case report; `browser.md`, `media.md`, `verification.md`; `pathway.md`, `comparison.md`, `technical-audit.md`; Rankuno SDLC standards (Steps 2, 3, 5, 6, 7) and `LOCAL_LLM_GATEWAY_STANDARD.md`.

This addendum does three things. Part A audits the problem statement itself. Part B lists defects inside the unified investigation that must be fixed before it is treated as the contract. Part C adds **124 new edge cases (IDs 91–214)** that none of the prior documents cover, each with detection, strategy and residual outcome. Part D corrects the taxonomies so that the architecture can be typed. Part E is the decision register for the user.

Nothing here was executed against a live site. Every detection method and fallback is a hypothesis that must get a fixture under the Stage 0 release gate.

---

## Part A — Problem-statement audit

| # | Statement in the problem statement | Issue | Decision adopted for architecture (provisional) |
|---|---|---|---|
| A1 | "100 percent from every corner" | Unprovable for an open-ended page (corpus already corrects this). | Claim is **VERIFIED_WITHIN_SCOPE** against a declared, observed inventory; gaps are first-class output. |
| A2 | "scraper must have a brain … at real time" | "Real time" is undefined: first content, first gap event, or terminal verdict? | Three latencies are tracked separately: time-to-first-evidence, time-to-first-gap-event, time-to-terminal-status. Status streams as events, not as a batch report. |
| A3 | "code level extraction and HTML level extraction" | "Code level" could be misread as server source. | Means **client-delivered** HTML/JS/CSS/JSON. JS bundles and i18n dictionaries are a **source-only** layer used for discovery hints, never indexed as published text. |
| A4 | "theming of the website" | Ambiguous: content themes (topics) vs visual theme (design system). | Capture both cheaply now: a per-state **template fingerprint** (DOM skeleton hash + CSS class vocabulary) and computed-style samples. Topic theming is Stage 4. |
| A5 | "semantic similarity … contradictory content across the website" | Requires stable block identity across pages and across time, plus claim context. | Blocks carry a stable `block_key`, heading ancestry, language, temporal and variant context. See cases 209–214. |
| A6 | "website may block us … bypass" | The corpus says blocked outcomes must stay visible; the user wants maximum access. These are compatible: access resilience is a ladder, and every rung still records what it did. | `ACCESS_RESILIENCE_PLAYBOOK.md` defines tiers T0–T6 with an escalation state machine. T0 (owner authorization) is the strongest "bypass" and the default for client sites. T6 is policy-gated and off by default. |
| A7 | "minimum 5000 URLs … should not crash" | No throughput, latency, host distribution or hardware stated. The Rankuno server is shared with Screaming Frog at P0. | Architecture targets 5,000–50,000 URLs per run with crash-only design (lease/heartbeat/reclaim). Capacity math in `ARCHITECTURE.md` §14 uses declared assumptions. |
| A8 | PDF, image "or whatever" | Format list is open-ended. | Capability registry with explicit **UNSUPPORTED** outcome for anything not in the registry; registry grows by fixture. |
| A9 | Not stated: who consumes the output | n8n, Qdrant, Postgres already run on the server. | Export is JSONL + Postgres views; n8n gets terminal-state webhooks; Qdrant is Stage 4 only. |
| A10 | Not stated: multi-client | Rankuno runs this for many clients. | Tenant isolation (storage prefix, IP pools, credentials, retention) is in the base design, not bolted on. |
| A11 | Not stated: authorization model | "Better cause" is not an authorization. | Every run carries an `authorization` block (owner-authorized / public-third-party / contract reference). It changes which access tiers are allowed. |
| A12 | Not stated: side effects on client analytics | A 5,000-URL crawl inflates GA4 pageviews, A/B exposures and heatmaps on a client site. | On authorized sites the crawler identifies itself and blocks analytics/tag-manager requests; see case 205. |

---

## Part B — Defects in the unified investigation

These must be corrected before the unified document is used as a contract.

| # | Defect | Severity | Correction |
|---|---|---|---|
| B1 | Section headers say "PDF 47–58", "Office/Media 63–72", "Access 75–90" but the tables run 47–62, 63–74, 75–90. | Low | Renumber headers. |
| B2 | Case 58 (attachments/portfolios) and case 62 (PDF portfolio) are the same case. | Low | Merge; keep 58. |
| B3 | Status table lists 7 badges, but the gap example uses `BUDGET_EXHAUSTED`, and the content block uses `VERIFIED`. Three vocabularies are mixed. | **High** | Two-level taxonomy (Part D): entity-level terminal statuses vs URL/run-level badges. `VERIFIED` alone is not a status. |
| B4 | Stack lists both `httpx` and `curl_cffi` as fetchers, both `pdfminer` and `PyMuPDF`, both `camelot` and `pdfplumber`, both Docling and Tika, with no rule for when the second tool runs. | High | One primary per route, second parser only on disagreement/risk triggers (media.md already says this). Decision in ADR-0003 and `ARCHITECTURE.md` §6. |
| B5 | Stack lists BrightData/Oxylabs and FlareSolverr as baseline components, contradicting the corpus axiom that evasion is not a prerequisite. | High | Replaced by the tiered ladder; vendors are T4/T5 tools with `RiskClass.FINANCIAL` and a per-run `CostLedger` ceiling. |
| B6 | "Celery + Redis" chosen without a durability analysis. Redis as broker can lose acknowledged-but-unprocessed work on crash unless configured carefully; Celery and Playwright's event loop interact poorly. | High | ADR-0001: Postgres-backed task ledger with `FOR UPDATE SKIP LOCKED` leases. Postgres already runs on the server. |
| B7 | Scope schema has no `tenant`, `authorization`, `priority`, `access_tier_ceiling`, `proxy_policy`, `retention_class`, `pii_policy`, `recrawl_of`, `mandatory_urls`, `analytics_suppression`. | High | Added to `RunScope` contract in `ARCHITECTURE.md` §9. |
| B8 | Output schema has no `schema_version`, tool versions, identity triple (requested / final / schedule-key), `block_key`, language, boilerplate role, dedupe linkage, reading-order index, bounding box, page index, attempt number, evidence hashes. | High | Added to `ContentBlock` and `UrlRecord` contracts. |
| B9 | Resource entity has a single `parent_id`. A PDF linked from 200 pages has 200 parents. | High | `resource_edges` table (many-to-many, typed edges: `embeds`, `links_to`, `frames`, `attachment_of`, `redirects_to`). Content-addressed dedupe means it is fetched once. |
| B10 | No per-host governor (concurrency, delay, crawl-delay, circuit breaker, block score). This is the single most important component for both "not getting blocked" and "not crashing a client's server". | **Critical** | `ARCHITECTURE.md` §7. |
| B11 | No backpressure. 5,000 PDFs queued for OCR with unbounded concurrency will exhaust memory. | Critical | Stage-bounded queues with admission control, §8. |
| B12 | No poison-URL handling. A renderer crash on one URL retried forever stalls the run. | Critical | Attempt counter, quarantine, `FAILED(poison)` with evidence. |
| B13 | No URL hygiene for inputs (duplicates, tracking params, scheme/host variants). 5,000 user-supplied URLs routinely contain 10–30% duplicates. | High | Cases 91–92; schedule-key normalization that never discards the requested identity. |
| B14 | No HTTP caching / conditional GET / recrawl mode. Contradiction-over-time needs versioned captures. | High | Cases 101–102, 108. |
| B15 | No transport-integrity cases (compression, truncation, 206/304, 4-way MIME mismatch) even though `comparison.md` flagged them. | High | Cases 103–112. |
| B16 | "Live display format" has no event contract (what an event is, how it is delivered). | Medium | `RunEvent` contract + SSE endpoint, §11. |
| B17 | No observability, SLOs or alerting (block-rate spikes, host error rates, queue depth). | Medium | §13. |
| B18 | No cost governance for cloud OCR / ASR / LLM / unblockers, contrary to Rankuno Step 5. | High | Every paid route is a `FINANCIAL` tool charged to the run's `CostLedger`. |
| B19 | Interaction policy "allowed: scroll, expand_disclosure, select_tab, hover_info" has no side-effect test. A "Show more" that POSTs is not a disclosure. | Medium | Allowed action = reversible, GET-only network effect, no navigation away, no form submission. Case 147. |
| B20 | The fixture suite has no false-positive trap for icon-font ligature text, obfuscated fonts, soft hyphens or third-party widget content, which are the commonest real-world corpus polluters. | High | Cases 113, 114, 118, 122 with fixtures. |
| B21 | Security section omits worker egress for browser subrequests, DNS pinning, and the fact that the Rankuno box hosts other production services. | High | §12. |
| B22 | No statement of resource ceilings on the shared server where Screaming Frog is P0. | High | §14; crawler is P2 (sacrificial) per the LLM gateway standard. |

---

## Part C — Extended edge-case inventory (cases 91–214)

Format matches the prior inventories: each row is a **fixture requirement**, not a claim of support. "Residual" is what the ledger must say when the strategy does not fully succeed.

### C1 — URL identity, input hygiene and scheduling (91–102)

| ID | Case | Detection | Strategy | Residual outcome |
|---|---|---|---|---|
| 91 | Input duplicates and near-duplicates: `http`/`https`, `www`/apex, trailing slash, path case, default port, query-param order, tracking params (`utm_*`, `gclid`, `fbclid`, `mc_cid`) | RFC 3986 normalization + tracking-param allowlist produces a `schedule_key` | Fetch once per `schedule_key`; keep every requested URL as an alias with its own output record pointing at the shared capture | Aliases listed; no requested URL is dropped silently; tracking-param stripping is reversible |
| 92 | Unsupported or non-fetchable inputs: `mailto:`, `tel:`, `javascript:`, `data:`, `ftp:`, `file:`, IDN/punycode, unencoded spaces or Unicode, URLs > 2,048 chars | Scheme allowlist + parser validation | `EXCLUDED_BY_POLICY` or `FAILED(invalid_input)` with the raw string preserved | Counted in the denominator; reason visible |
| 93 | Seed resolves to private, loopback, link-local, metadata (169.254.169.254) or rebinding DNS (TTL 0, mixed answers) | Resolve before connect; classify IP; re-resolve on every redirect hop; compare answers | Pin the resolved public IP for the connection; egress proxy allowlist as a second wall | Denied request logged; target never contacted |
| 94 | Fragment routes (`#/pricing`, `#!page`) in SPAs | Fragment present + router signals (`hashchange` listeners, hash-based links) | Treat the fragment as a **state** of the same resource; render with the fragment; one state per distinct fragment | Fragment states enumerated; unvisited ones listed |
| 95 | Mobile (`m.`), AMP (`/amp/`, `rel=amphtml`), print (`?print=1`) variants of one page | `rel=alternate` / `rel=amphtml` / media attributes | Record the relationship edge; capture only declared profiles | Variant relationship recorded; unvisited variants explicit |
| 96 | Locale/geo auto-redirect (server sends `/en-in/` based on `Accept-Language` or IP) | Redirect target differs from request by locale segment; `Vary: Accept-Language`; `hreflang` alternates | Pin locale and geography in scope; follow but flag `served_variant != requested_variant` | Both identities kept; no silent merge of locales |
| 97 | Non-HTTP redirects: `<meta http-equiv=refresh>`, JS `location` assignment, `<link rel=canonical>` to another URL | Meta parse; browser navigation events; canonical compare | Extend the redirect chain with a typed hop (`http`, `meta`, `js`, `canonical_hint`); scope and SSRF check per hop | Chain complete; canonical is a hint, never a substitute for the requested URL |
| 98 | Soft redirect to homepage or category page (HTTP 200, unrelated content) | Template fingerprint equals the site's homepage/category skeleton; title match; high similarity to home | `NEEDS_REVIEW(soft_redirect_suspected)` | Never credited as the target's content |
| 99 | Per-request variance (A/B, rotating recommendations, randomized order) | Double-fetch sample disagrees in specific regions | Mark regions volatile; keep both captures | Volatile regions flagged; not a contradiction candidate by default |
| 100 | Priority ordering: user-declared mandatory URLs (pricing, terms), HTML before assets, sitemaps first | `mandatory_urls` in scope | Priority classes in the task ledger | Budget stop reports unprocessed items by priority class |
| 101 | Recrawl / incremental mode of the same scope | Prior run with identical `scope_hash` | Conditional GET (`If-None-Match`, `If-Modified-Since`); content-hash compare; version chain | `304` without a cached body is `FAILED`, not success; removed text is never carried forward |
| 102 | Cross-run reuse of identical bytes (same PDF hash seen last month) | Content-addressed store hit | Reuse with provenance link and original capture time; re-verify policy by age | Reuse recorded; age visible |

### C2 — Transport and acquisition integrity (103–112)

| ID | Case | Detection | Strategy | Residual outcome |
|---|---|---|---|---|
| 103 | `gzip`/`br`/`zstd` encodings; unsupported encoding; truncated compressed stream; decompression bomb | `Content-Encoding`; decoder error; expansion ratio and absolute caps | Bounded decode to disk; keep raw bytes; retry with `Accept-Encoding: identity` | Truncation = `FAILED` or `PARTIAL` with byte counts; bomb = `LIMIT_HIT` |
| 104 | Chunked body cut short, `Content-Length` mismatch, reset mid-body | Byte count vs header; missing terminal chunk | Retry; use `Range` if `Accept-Ranges: bytes` | Truncated body is never parsed as complete |
| 105 | `206`, `304`, `204`, `1xx`, `3xx` without `Location`, `2xx` with empty body | Status handling table | Explicit per-status behaviour; `206` assembled and verified by total length | Status-specific outcome; no empty success |
| 106 | Four-way type mismatch: `Content-Type` vs URL extension vs magic bytes vs `Content-Disposition` filename | Sniff first 8 KB | Sniffed type wins for parsing; record all four | Mismatch flagged; challenge HTML for a PDF URL = `BLOCKED` |
| 107 | Content negotiation (`Accept`, `Accept-Language`, `Sec-CH-UA-*`, `Vary`) | `Vary` header present | One fixed request-header profile per scope, recorded in the run | Other representations declared unrequested |
| 108 | Stale CDN copy (`Age`, `X-Cache: HIT`, `stale-while-revalidate`) | Cache headers | Record cache evidence; cache-busting only if scope allows (it changes server load) | Freshness-uncertainty flag |
| 109 | HTTP/3-only or `Alt-Svc`, HTTP/1.0 servers, legacy TLS, expired/self-signed certificates, HSTS, mixed content blocked in browser | Transport error classification | Protocol fallback matrix; invalid certs never silently accepted (policy switch per tenant) | Transport cause classified; cert failure = `FAILED(tls)` |
| 110 | Oversized responses (HTML > 10 MB, PDF > 500 MB, images > 50 MP) | Streaming size counter | Caps per scope; stream to disk; stop at cap | `LIMIT_HIT` gap with observed size |
| 111 | Never-ending bodies (SSE at an HTML URL, slow-drip servers) | Read timeout, total-time cap, max bytes | Capture window | `PARTIAL` with window and stop reason |
| 112 | Downloads triggered by JS/click, `Content-Disposition: attachment` on navigation, `blob:`/`data:` URLs, `<a download>` | Playwright download events; href scheme | Capture the download stream as a resource; decode `data:` inline | Each download has an outcome |

### C3 — HTML / DOM rendering (113–132)

| ID | Case | Detection | Strategy | Residual outcome |
|---|---|---|---|---|
| 113 | Icon-font ligature text (Material Icons renders the word `arrow_forward` as an arrow; Font Awesome uses PUA code points via `::before`) | Node font-family is a known icon font; PUA ranges; ligature class names | Classify as `ICON_ARTIFACT`; exclude from corpus; keep `aria-label` | Never indexed as words; fixture is a false-positive trap |
| 114 | Anti-scraping font obfuscation (custom `@font-face` remaps glyphs so DOM text ≠ visible text; common on price and listing pages) | OCR of the region disagrees with DOM text while the screenshot is legible; unusual `unicode-range` | Prefer OCR reading, mark DOM text obfuscated, keep both | `NEEDS_REVIEW` unless OCR confidence passes threshold |
| 115 | CSS transforms of text: `text-transform`, `direction: rtl`, `unicode-bidi`, `writing-mode: vertical-*`, letter-spacing art | Computed styles | Store DOM text and computed visual text separately | Both kept; no silent casing changes |
| 116 | Collapsed-but-published content: `content-visibility: auto`, `hidden="until-found"`, closed `<details>`, `popover`, closed `<dialog>` | Attributes and computed styles | Treat as disclosures: scroll into view / open; label `collapsed_published` | Distinct from `hidden`; unopened items listed |
| 117 | `inert` and `aria-hidden` wrappers over visible text (modal backdrop) | Attribute scan vs rendering | Visibility from rendering; accessibility state separately | Layers kept distinct |
| 118 | Invisible code points: soft hyphen U+00AD, `<wbr>`, zero-width joiner/space, NBSP, ligature code points, homoglyph mixing (Cyrillic `а` in Latin words) | Code-point scan | Reversible normalization map; homoglyph mixing flagged (possible cloaking) | Originals preserved; flag visible |
| 119 | Scheme-dependent variants: `prefers-color-scheme`, `prefers-reduced-motion`, `forced-colors`, `<picture media=…>` | Media-query inventory | Declared profile only; record alternates | Other schemes untested |
| 120 | Math duplicates (MathML + visual spans + `aria-label` TeX from MathJax/KaTeX) | Known containers | Keep one canonical form, link duplicates | Dedupe linkage recorded |
| 121 | Code blocks with line-number gutters, copy buttons, diff markers | `pre` with gutter classes/tables | Role-aware extraction; gutter excluded | Gutter exclusion evidenced |
| 122 | Third-party widgets injecting text (chat bots, cookie banners, review carousels, ad slots, recommendation rails) | Frame/host origin; known vendor selectors | Layer `third_party_chrome`; excluded from page corpus unless scope includes | Classification inspectable and reversible |
| 123 | Never-settling pages (clocks, tickers, auto-rotating carousels, animated counters, typing indicators) | Delta-stability never reached; periodic change localized | Volatility mask; stabilize on the non-volatile set; sample N frames of volatile regions | `VOLATILE` region flag with sample count; not a timeout failure |
| 124 | Huge DOMs (> 300k nodes), extreme nesting, snapshot beyond size limits | Node counter | Streaming DOM walk; chunked snapshots; node cap | `LIMIT_HIT` explicit |
| 125 | Scroll-driven mutation (sticky headers duplicating content, parallax, scrollytelling that swaps text) | Per-step snapshot hashes | Dedupe by hash; map text to scroll position | Scroll-state provenance |
| 126 | Content reachable only by keyboard focus or that vanishes on mouse move | Focusable elements with hidden descendants | Keyboard traversal as an allowed action class | Trigger provenance |
| 127 | Text drawn by WebGL, Three.js, Lottie, or video canvases | Canvas/WebGL contexts | Screenshot OCR on sampled frames | Temporal sampling is partial |
| 128 | Broken images showing `alt` as visible text; low-quality placeholders (LQIP) OCR'd as blur | `naturalWidth == 0`; placeholder classes | Wait for real `src`; alt kept in attribute layer | Unloaded image remains a gap |
| 129 | Declarative shadow DOM in SSR HTML (`<template shadowrootmode>`) | Attribute parse | Parse as composed content in the source channel | Declared |
| 130 | `<slot>` assigned vs fallback nodes; text duplicated across light and shadow trees | Composed-tree walk | Walk the composed tree once | Dedupe map |
| 131 | Web fonts failing or swapping late (`font-display`), altering clipping and OCR | `document.fonts.ready`; failed font requests | Wait for fonts before screenshots | Font failures logged |
| 132 | Client-side i18n dictionaries and route manifests in bundles (`_buildManifest.js`, `__NEXT_DATA__`, `__NUXT__`) | Known globals and manifest paths | Source-only layer; use route lists only as **discovery hints** | Hinted routes reported as frontier candidates, never as content |

### C4 — Third-party embeds and document viewers (133–140)

| ID | Case | Detection | Strategy | Residual outcome |
|---|---|---|---|---|
| 133 | Video embeds (YouTube, Vimeo, Wistia, Loom) | Known frame origins | oEmbed title/description; captions via platform API where licensed | Transcript provenance; otherwise `UNSUPPORTED(no_caption_access)` |
| 134 | Canvas-rendered document viewers (Scribd, Issuu, SlideShare, FlipHTML5, Calameo, flipbooks) | Viewer origins; canvas-only text | Locate a source download/export URL; else per-page screenshot OCR within budget | Page range processed; viewer UI ≠ document |
| 135 | Google Docs/Sheets/Slides published embeds, Office 365 embed viewer, Box/Dropbox previews | Known URL patterns | Public export endpoints (`/export?format=pdf`) when available | Export vs viewer difference recorded |
| 136 | Dashboards and charts (Tableau, Power BI, Looker, Datawrapper, Flourish, Highcharts, Chart.js) | Vendor scripts/frames | SVG text and exposed data CSV/JSON endpoints; canvas → OCR labels; never infer numbers from pixels | Extracted vs inferred separated |
| 137 | Social embeds (X/Twitter, Instagram, TikTok, LinkedIn, Facebook comments, Disqus) | Frame origins | oEmbed/public API or `BLOCKED` | UGC scope policy decides inclusion |
| 138 | Maps (Google Maps, Mapbox, Leaflet) | Map containers | Address text and DOM popups; tiles are not content | Visual only |
| 139 | Forms-as-a-service (Typeform, HubSpot, Calendly, Jotform) | Frame origins | Labels and help text only; never submit | Declared |
| 140 | Review/rating widgets (Trustpilot, Yotpo, Google reviews) with API pagination | Vendor scripts | Scope decision (UGC in/out); bounded pages; PII policy applies | Pagination frontier recorded |

### C5 — Site behaviour variants and gates (141–150)

| ID | Case | Detection | Strategy | Residual outcome |
|---|---|---|---|---|
| 141 | Paywalls: hard, metered, soft (full text in DOM under an overlay) | `isAccessibleForFree=false`; occlusion; truncated text + "Subscribe" | Hard = `BLOCKED`; soft = text present but labelled `paywalled_in_source`; never bypass | Publication status explicit |
| 142 | Age, region, "healthcare professional" gates | Gate dialog patterns | Only declared ordinary public choices; otherwise `BLOCKED(gate)` | Gate recorded with screenshot |
| 143 | Consent-gated content (video or map loads only after accept) | Consent manager APIs (TCF `__tcfapi`), placeholder overlays | Consent state in scope; capture both states if declared | Consent-gated regions listed |
| 144 | Referrer/UTM/cookie-driven personalization, returning-visitor variants, cart state | Compare fresh-context vs session capture on a sample | Fresh context per URL by default; site-session mode by policy; referrer policy recorded | Variant recorded |
| 145 | Time-based content (countdowns, business hours, "today", scheduled publish/unpublish) | Patterns and `datetime` attributes | Capture timestamp and timezone; mark temporal regions | Temporal flag feeds contradiction logic |
| 146 | Live prices, stock, availability | Volatility class | Timestamp; volatility class | Not a contradiction across time by itself |
| 147 | Server-fetch disclosures ("Read more", "Show phone", "Load reviews") | Click produces XHR | Allowed only if the effect is a GET with no navigation; otherwise unexpanded | Non-GET disclosures listed as unvisited with reason |
| 148 | Parameter explosion (sort, filter, `replytocom`, `page=∞`, session IDs) | Param-signature novelty tracking | Signature dedupe; novelty cutoff | Budget stop ≠ exhausted |
| 149 | Multi-step wizards, quizzes, configurators | Form sequences | Declared scenarios only | Unknown input space |
| 150 | Site deploy mid-run (mixed versions across pages) | Build IDs in asset paths (`/_next/static/<buildId>`), ETag shifts | Record build id per page; flag mixed-version run | Snapshot-coherence flag |

### C6 — PDF internals (151–160)

| ID | Case | Detection | Strategy | Residual outcome |
|---|---|---|---|---|
| 151 | Incremental updates retaining pre-redaction or prior content (`/Prev` xref chain, multiple `%%EOF`) | Trailer inspection | Latest revision = visible; earlier revisions = hidden layer | Never merged into visible |
| 152 | Invisible text: render mode 3, clip mode 7, white-on-white, outside CropBox, < 1 pt fonts | Span attributes and colour vs background | Hidden layer | Visible meaning never altered by hidden text |
| 153 | Type3, CID fonts without `ToUnicode`, symbolic fonts (Wingdings checkmarks) | Font dictionary inspection; garbled output | Glyph-level OCR; symbol-font map | `NEEDS_REVIEW` if unmapped |
| 154 | Page labels vs physical index; `/Rotate`; mixed page sizes | Page dictionaries | Store both indices; rotation-aware OCR | Both indices in citations |
| 155 | Outline/bookmarks and tagged structure tree as reading-order evidence | Structure tree presence | Compare with layout order | Order ambiguity flag |
| 156 | Link annotations (URI, GoToR) as discovery edges; JavaScript/Launch actions | Annotation enumeration | Discovery edges under scope; actions never executed | Edges recorded; actions listed as security findings |
| 157 | XFA dynamic forms rendering blank outside Adobe; `NeedAppearances` | XFA stream present | Extract XFA XML separately; flag | `PARTIAL(xfa)` |
| 158 | Permission flags (no-copy) with empty user password; owner-password only | Encryption dictionary | Policy switch per tenant; flag recorded | Declared |
| 159 | Linearized PDF partially downloaded still renders page 1 | Byte count vs `/L` | Integrity check before parse | Truncated = `PARTIAL` with page range |
| 160 | RichMedia/3D/embedded multimedia objects | Object inventory | Inventory; unsupported explicit | `UNSUPPORTED` per object |

### C7 — Office internals (161–168)

| ID | Case | Detection | Strategy | Residual outcome |
|---|---|---|---|---|
| 161 | DOCX text boxes, shapes, SmartArt, WordArt (`w:txbxContent`, `diagrams/data*.xml`) missed by body iteration | Package part inventory | Full part walk, not paragraph iteration | Parts without an adapter are explicit gaps |
| 162 | DOCX hidden text (`w:vanish`), white/tiny fonts, cached field results (TOC, REF, MERGEFIELD), content controls, comments, footnotes/endnotes | Run properties and field codes | Layer labels per part | Hidden vs visible distinct |
| 163 | Embedded OLE (Excel in Word), linked external images, `altChunk` HTML imports | Relationship inspection | Recursive dispatch; external links are gaps | Child outcomes |
| 164 | XLSX very-hidden sheets, hidden rows/columns, number-format masking (`;;;`), white font, conditional formatting, validation lists, named ranges, pivot caches, external links, million-row sheets | Workbook metadata | Read-only streaming; format-aware display value alongside stored value | Display vs stored recorded; memory cap explicit |
| 165 | PPTX inherited placeholder text from layouts/masters, grouped shapes, tables, chart titles/data, SmartArt, hidden slides, sections, animation-staged reveals, embedded media | Part walk | Inventory all parts | Presenter notes ≠ audience content |
| 166 | Legacy binaries (`.doc`, `.xls`, `.ppt`), RTF, ODF, Apple iWork, `.pub`, `.vsd`, `.one` | Magic bytes | Sandboxed LibreOffice headless conversion + Tika | Conversion-fidelity flag |
| 167 | Email (`.eml`, `.msg`, `.mbox`), `.ics` | MIME walk | Attachments recursed; inline images handled | Child outcomes |
| 168 | Macro-enabled files and active content | Package inspection | Never execute; VBA source as source-only layer | Declared |

### C8 — Other media and formats (169–176)

| ID | Case | Detection | Strategy | Residual outcome |
|---|---|---|---|---|
| 169 | HEIC/AVIF/JPEG XL/SVGZ/ICO/PSD | Magic bytes | Codec plugins; unsupported explicit | `UNSUPPORTED` |
| 170 | EXIF/IPTC/XMP text (captions, GPS → PII) | Metadata parse | Metadata layer; PII policy | Declared |
| 171 | Subtitle formats (SRT/VTT/ASS/TTML), multi-language tracks, burned-in subtitles | Track inventory | Native tracks first; burned-in = frame OCR | Temporal sampling partial |
| 172 | HLS/DASH manifests; DRM (Widevine) | Manifest and key-system detection | DRM = `BLOCKED`; never circumvent | Declared |
| 173 | Podcasts/audio with RSS enclosures and chapter metadata | Feed detection | Feed-first acquisition | Declared |
| 174 | Archive hazards: bombs, depth, zip-slip paths, encrypted members, `7z`/`rar` licensing | Container inspection | Bounded safe extraction; path validation | Members without outcome impossible |
| 175 | Markdown/RST/TeX/Jupyter (outputs incl. images), source files (code vs comments) | Extension + sniff | Format adapters; label code vs prose | Declared |
| 176 | Full-content RSS/Atom/JSON Feed and sitemaps | Feed discovery | Separate representation channel aligned to HTML | Feed ≠ page proof |

### C9 — Anti-bot behaviour and block detection (177–190)

| ID | Case | Detection | Strategy | Residual outcome |
|---|---|---|---|---|
| 177 | Vendor challenge signatures (Cloudflare, Akamai Bot Manager, HUMAN/PerimeterX, DataDome, Imperva, Kasada, AWS WAF, F5 Shape, Sucuri, Anubis proof-of-work) | Header/cookie/body fingerprint table (playbook §4) | Classify block type and vendor; escalate per the policy ladder | `BLOCKED` with vendor evidence and tier reached |
| 178 | Shadow blocking / degraded responses (HTTP 200 with empty data arrays, fewer listings, scrambled text, fake prices) | Canary URL with known content; dual-channel compare (HTTP vs browser vs alternate IP class); content length vs baseline | `SHADOW_BLOCK_SUSPECTED` gap; escalate | Never trusted silently |
| 179 | Behavioural traps (metronomic intervals, no asset requests, no pointer events) | Block correlates with pacing | Humanized pacing inside the politeness envelope | Declared |
| 180 | Honeypot links and hidden form fields | Visibility + `robots.txt` disallow patterns | Never follow or fill hidden controls | Recorded |
| 181 | Tarpits / infinite link mazes (anti-AI crawler traps) | Frontier growth with low novelty; slow responses | Novelty-based frontier cutoff; per-host time cap | Budget stop with maze evidence |
| 182 | AI-crawler opt-out signals: `robots.txt` user-agents, `noai`/`noimageai`, TDM Reservation Protocol (`tdmrep.json`), `X-Robots-Tag`, content-signal headers | Directive parsing | Policy engine distinguishes crawl-disallow vs AI-training-disallow vs index-disallow; applies per engagement | `EXCLUDED_BY_POLICY` with directive evidence |
| 183 | HTTP 402 pay-per-crawl responses | Status + pricing headers | Record; `FINANCIAL` policy gate; default `BLOCKED` | Declared |
| 184 | IP reputation (datacenter ASN, shared-proxy denylists), geo mismatch (IP country vs `Accept-Language`/timezone) | Block rate correlates with IP class | IP pool classes; context consistency | Declared |
| 185 | TLS/HTTP-2 fingerprint mismatch (JA3/JA4, SETTINGS order), header order/casing, missing client hints | 403 on HTTP but not browser | Impersonation profile pinned per session | Declared |
| 186 | Automation detection surfaces (`navigator.webdriver`, CDP artifacts, software WebGL renderer, zero outer dimensions, untrusted events) | Challenge appears only in automation | Fidelity browser profile (T3) | `detection_suspected` flag |
| 187 | Clearance cookies bound to IP + UA + TLS (`cf_clearance`), session expiry mid-run | Challenge reappears after identity change | Sticky identity tuple per host session | Declared |
| 188 | Authorized login on client sites | Scope `access_context=authenticated_client` | Encrypted storage state per tenant; never logged or stored as evidence | Declared |
| 189 | Block cascades: one burst bans a /24 used by other clients | Cross-tenant block correlation | Per-tenant IP pools; global per-ASN caps; circuit breaker | Declared |
| 190 | Legal signals (cease-and-desist, ToS clauses, robots as contract in some jurisdictions) | Operator-maintained denylist | Host legal hold → `EXCLUDED` | Declared |

### C10 — Operational and scale (191–202)

| ID | Case | Detection | Strategy | Residual outcome |
|---|---|---|---|---|
| 191 | Poison URL (renderer crash, parser segfault, OOM) | Attempt counter; lease reclaim | Isolated subprocess; quarantine after N attempts | `FAILED(poison)` with last error |
| 192 | Browser memory leak, zombie processes, GPU process crash | RSS watchdog; process tree | Recycle browser every K pages or M minutes; kill tree | Declared |
| 193 | Disk exhaustion from evidence (full-page screenshots × states × 5,000) | Quota counters | Per-run evidence budget; JPEG; crops on gaps; tiered retention; stream to object store | Run-level `LIMIT_HIT` |
| 194 | Host clock skew, DST, timezone mismatch with declared scope | NTP check; emulation | UTC everywhere; browser TZ emulation | Declared |
| 195 | Duplicate task delivery (at-least-once) double-counting regions | Idempotency keys | Upsert by `(run, resource, state, region_key)` | Declared |
| 196 | Partial writes on crash (evidence uploaded, ledger not updated) | Orphan sweep | Evidence-first then ledger row referencing hash; sweeper | Declared |
| 197 | Tool-version drift mid-run (deploy during a run) | Version pin per run | Refuse mixed versions; new run instead | Version recorded |
| 198 | Rate-limiter starvation between runs/tenants on the same host | Shared host governor | Governor is DB-backed, global, not per process | Declared |
| 199 | Wall-clock overrun (5,000 slow URLs) | Run budget | Checkpoint, resume, partial export | `PARTIAL` run with frontier |
| 200 | Missing per-host observability | Metrics | Block rate, error rate, latency per host; alerts | Declared |
| 201 | Non-deterministic LLM annotations across reruns | Prompt/version pin | Temperature 0, cached by content hash; never affects status | Declared |
| 202 | Cost blow-up on paid routes (cloud OCR on 5,000 PDFs) | `CostLedger` | `FINANCIAL` tools; per-run ceiling; local-first | `BUDGET_EXHAUSTED` |

### C11 — Privacy, legal and governance (203–208)

| ID | Case | Detection | Strategy | Residual outcome |
|---|---|---|---|---|
| 203 | PII in pages (staff pages, reviews, EXIF GPS) | PII detector tagging | Retention class; redaction in exports | Declared |
| 204 | Copyright/TDM, raw-evidence retention, right-to-delete, cross-client isolation | Tenant policy | Tenant buckets; retention jobs; deletion job | Declared |
| 205 | Crawl pollutes client analytics (GA4, heatmaps, A/B exposure) | Authorized-site flag | Self-identifying UA + GA4 bot filter; block analytics/tag-manager requests when authorized | Declared |
| 206 | Side-effect GET links (add-to-cart, unsubscribe, vote, logout, delete) | URL pattern denylist | Never follow action-like links; GET-only navigation | Declared |
| 207 | Credentials for authorized crawls | `SecretStr`, vault | Never in evidence or logs | Declared |
| 208 | Audit of who authorized what | `scope.authorization` | Per-run authorization record | Declared |

### C12 — Downstream readiness (209–214)

| ID | Case | Detection | Strategy | Residual outcome |
|---|---|---|---|---|
| 209 | Template / page-type fingerprint (for theming, boilerplate and soft-redirect detection) | DOM skeleton hash + CSS class vocabulary | Compute per state; cluster across site | Declared |
| 210 | Stable block identity across recrawls | `block_key = (heading_path, text shingle hash, position bucket)` | Stored on every block | Declared |
| 211 | Boilerplate labelling across the site (nav, footer, cookie text, legal footers) | Cross-page frequency | Label, never delete | Declared |
| 212 | Claim context on blocks (entity, product/variant, effective date, locale, audience, conditions) | Structured fields | Stored on `ContentBlock` | Declared |
| 213 | Language per block; mixed-language pages | `lang` attributes + detector | Stored per block | Declared |
| 214 | Visual-theme evidence (palette, fonts, logo, layout grid) | Computed-style sampling | Separate `design_evidence` channel | Declared |

### C13 — Marketplace and B2B gated content (215–222)

Added 2026-10-16; the full rows live in `docs/FEASIBILITY_AND_GATED_CONTENT.md` section 5: lead-generation form walls (215), delivery pincode modals (216), unavailable products with HTTP 200 (217), sponsored interleaving (218), price-changing variant selectors (219), reviews and Q&A behind load-more and login (220), prices as images or obfuscated fonts (221), marketplace identity explosion (222).

---

## Part D — Corrected taxonomies

### D1 Two-level status model

**Entity level** (resource, state, region). Pending: `DISCOVERED`, `QUEUED`, `IN_PROGRESS`, `RETRY_SCHEDULED`. Terminal: `VERIFIED_EXTRACTED`, `EXTRACTED_UNVERIFIED`, `PARTIAL`, `BLOCKED`, `FAILED`, `UNSUPPORTED`, `BUDGET_EXHAUSTED`, `EXCLUDED_BY_POLICY`, `VERIFIED_NONCONTENT`.

**URL and run level** (badge derived by precedence, never hiding the vector): `RUNNING` → `FAILED` → `BLOCKED` → `PARTIAL` → `NEEDS_REVIEW` → `VERIFIED_WITHIN_SCOPE` → `EXCLUDED`.

### D2 Gap types

`ACCESS_BLOCKED`, `ACQUISITION_FAILED`, `UNSUPPORTED_FORMAT`, `BUDGET_STOPPED`, `DISCOVERY_OPEN`, `FIDELITY_DISAGREEMENT`, `LOW_CONFIDENCE`, `UNVISITED_STATE`, `LIMIT_HIT`, `POLICY_EXCLUDED`, `SHADOW_BLOCK_SUSPECTED`, `DEGRADED_VARIANT`, `VOLATILE`, `SOFT_REDIRECT_SUSPECTED`.

### D3 Content layers (additions in bold)

`rendered_visible`, `collapsed_published`, `accessibility_only`, `hidden`, `metadata`, `source_only`, `uncertain` (OCR/ASR/LLM), **`third_party_chrome`**, **`ugc`**, **`icon_artifact`**, **`paywalled_in_source`**, **`hidden_revision`**, **`design_evidence`**.

### D4 Access tiers

`T0_AUTHORIZED`, `T1_POLITE_HTTP`, `T2_IMPERSONATED_HTTP`, `T3_FIDELITY_BROWSER`, `T4_PROXIED`, `T5_MANAGED_UNBLOCKER`, `T6_POLICY_GATED`. Each run declares an `access_tier_ceiling`; each resource records the tier at which it was acquired.

---

## Part E — Decision register (user input required)

| # | Decision | Default assumed by the architecture if unanswered |
|---|---|---|
| E1 | Scope mode for the first release: supplied list only, list plus embedded assets, or recursive site crawl? | Supplied list + embedded assets + linked downloads depth 1. Recursive crawl is a flag, not a separate system. |
| E2 | Which hosts are **owner-authorized** (client sites with written permission) vs **public third-party**? | Per run, in `scope.authorization`. Unspecified = public third-party, ceiling `T3`. |
| E3 | Access-tier ceiling per engagement: may the system use proxies (T4) and managed unblockers (T5)? Who signs off on T6? | **DECIDED 2026-10-15 (final)**: start at **$0 with ceiling T3** (polite HTTP, impersonated HTTP, local Playwright) in every profile. Paid tiers are an escalation ladder only: when a host blocks at T3, the governor returns `NEEDS_APPROVAL` with the proposed tier, the host stays `BLOCKED`, and the operator (Gaurav) is asked. On approval the ceiling is raised **for that host only**, with a small per-run cap (default $5) drawn from the Bright Data subscription and the day/month caps in the playbook. T6 stays off behind the dual sign-off. |
| E4 | Robots policy per engagement: respect, respect-with-exceptions, or informational only? | **DECIDED 2026-10-14 (recommended path)**: `respect` on `public_third_party`; `informational` on `owner_authorized` (the owner's written permission supersedes their robots file, and the directive is still recorded on every resource). `respect_with_exceptions` requires a listed path allowlist in the authorization record and is never the default. |
| E5 | Is UGC (reviews, comments) in scope? | Out by default. |
| E6 | Are soft-paywalled texts (present in DOM) publishable content? | Captured, labelled `paywalled_in_source`, excluded from the main corpus. |
| E7 | Evidence retention and raw-bytes retention period per tenant? | 90 days raw, 1 year derived, hashes forever. |
| E8 | Where does the system run: the shared Rankuno server (WSL2 Docker, P2 priority) or a dedicated crawl node? | Shared server for Stage 0–1 at reduced concurrency; dedicated node recommended from Stage 2. |
| E9 | Target wall-clock for a 5,000-URL single-host run? | ≤ 12 h on the shared server; ≤ 4 h on a dedicated node. |
| E10 | "Theming" = topics, design, or both? | Both captured cheaply; topic analysis Stage 4. |
| E11 | Languages/scripts for OCR in the first release? | **DECIDED 2026-10-14**: full multilingual routing from Stage 1, local engines only (Tesseract packs, PaddleOCR evaluated as second opinion). Script detection routes each region to a language pack; scripts without an installed pack are `UNSUPPORTED(ocr_lang:<script>)`, never silently OCR'd as English. Routing design in `ARCHITECTURE.md` §6.1. |
| E12 | Acceptable manual-review volume per 1,000 URLs? | Unknown; measured in Stage 1. |
