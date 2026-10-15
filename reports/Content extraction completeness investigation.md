# Build Content Extraction That Exposes Gaps

**The right product is an evidence-backed extraction system that reports missing content, not a scraper that promises unconditional 100% coverage.** For public third-party URLs, the system should collect delivered HTML and source data, rendered browser states, embedded resources, PDFs, images and other supported files, then reconcile their content against an independently assembled inventory. It should identify known missing regions in real time and preserve uncertainty about undiscovered content. A URL can expose different content through interactions, languages, device sizes, time and access conditions; no general crawler can enumerate all of those possibilities. The defensible result is “verified within this declared scope and capture context,” accompanied by evidence and explicit gaps. This investigation proposes the architecture, 83-case test inventory, quality gates and staged implementation needed before website theming, semantic similarity and contradiction analysis become trustworthy. **This is research and proposed design; no scraper implementation or accuracy benchmark was performed.**

## Completeness requires a declared boundary and separate measures

The first prototype should target **supplied public third-party URLs and their in-scope embedded resources**, with linked downloads recorded and processed under an explicit recursion policy. This is a proposed starting boundary, not a user-confirmed exclusion of website crawling. Entire-site discovery should be a separate mode: a supplied URL list is not proof of a complete website inventory, and faceted navigation can create unbounded crawl spaces. Record external CDN assets independently from navigation scope so that an embedded image is not lost simply because its hostname differs. ([Google faceted-navigation guidance](https://developers.google.com/crawling/docs/faceted-navigation))

For each run, declare hosts and schemes, input URLs, linked-download policy, recursion depth, file and time limits, allowed interactions, device/viewport, language, timezone, geography, consent state and anonymous access context. Define whether “content” includes visual descriptions, audio speech, video overlays, document comments and hidden revisions. Capture actual served variants and timestamps. A finite viewport/locale configuration cannot represent every visitor experience.

“Code-level extraction” means preserving and inspecting **client-delivered HTML, JavaScript, CSS, JSON and other fetched resources**. It does not give access to server application source, databases, CMS drafts or unpublished content. Strings in a JavaScript bundle, templates or comments are source candidates, not automatically published statements. Keep visible content, accessibility-only text, hidden content, metadata and source-only strings in separately labeled layers.

There is no universal browser event that establishes all page content has arrived. Pages can load data after navigation and fail to respond to early clicks before hydration. DOM text, accessibility representations and screenshots expose different subsets: `textContent` includes scripts and hidden text; `innerHTML` omits shadow roots; canvas pixels lack ordinary semantic HTML structure. These are reasons to combine channels, not evidence that any combination guarantees exhaustive discovery. ([Playwright navigation](https://playwright.dev/docs/navigations), [MDN textContent](https://developer.mozilla.org/en-US/docs/Web/API/Node/textContent), [MDN innerHTML](https://developer.mozilla.org/en-US/docs/Web/API/Element/innerHTML), [MDN canvas](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/canvas))

| Question | Proposed measure | What it cannot prove |
|---|---|---|
| Did we discover everything relevant? | Discovery methods run, remaining frontier, known versus unknown expected totals | Absence of undiscovered states or resources |
| Did we obtain discovered resources? | Successful required resources / discovered required resources | Extraction fidelity or undiscovered resources |
| Did we recover observed regions? | Verified required content regions / discovered required content regions | Universal page content recall |
| Did we explore declared states? | Visited / required states when a finite state set exists; otherwise frontier counts | All possible inputs, users or future states |
| Did we account for the inventory? | Terminal outcomes / discovered items | Success: failed items also have terminal outcomes |
| Was recovered content accurate? | Ground-truth recall/precision, OCR errors, table relations and critical-token checks on annotated fixtures | A universal accuracy rate outside the evaluated corpus |

**Never build the denominator solely from successful extractions or remove failures from it.** “100% of discovered assets handled” can coexist with a blocked PDF and an undiscovered tab. Similarly, all PDF pages processed does not mean every image-only footnote was read. A confidence value from OCR or an LLM is not the probability that nothing is missing. Independent visual detection can find additional regions, but the detector can itself miss low-contrast text. Agreement between channels strengthens evidence while retaining correlated failure risk.

## The 83-case inventory makes silent omissions testable

The following matrix is a **proposed engineering and acceptance-test inventory**, not a claim that the named libraries support every case. It consolidates likely failure classes and should grow with real failures. It is not exhaustive against every future format or website implementation. Each row requires a fixture, a detection attempt, a fallback and an explicit residual outcome. “Review” means fidelity or discovery remains unresolved; “partial” means required content is known to be missing; “unsupported” and “budget stopped” remain visible causes of a partial result when other useful content exists.

Browser-specific design follows documented limitations: Playwright traverses supported shadow DOM but not closed roots, frames require their own contexts, CSS can generate displayed content, and virtualized lists recycle nodes. **Scrolling to the bottom and saving the final DOM loses earlier virtualized records.** The collector must accumulate content during traversal, including horizontally virtualized columns. ([Playwright locators](https://playwright.dev/docs/locators), [Playwright frame locators](https://playwright.dev/docs/api/class-framelocator), [MDN CSS content](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/content), [web.dev virtualization](https://web.dev/articles/virtualize-long-lists-react-window))

| ID | HTML/browser case | Detect and recover | Verification or residual outcome |
|---|---|---|---|
| 01 | Static HTML, malformed markup | Archive bytes; compare parser and browser structure | Encoding/repair warnings; review mismatches |
| 02 | Charset, entities, Unicode normalization | Use response/declaration evidence; retain raw and normalized text | Replacement characters and altered critical tokens flagged |
| 03 | SSR followed by hydration | Capture source and successive DOM states | Content deltas and failed interactions retained |
| 04 | Client-only SPA or streamed HTML | Observe rendered content and content-bearing responses | Timeout is partial, not empty success |
| 05 | Delayed fetch, long polling, WebSockets | Track content deltas under a capture window | Pending streams and stop time explicit |
| 06 | Lazy sections/images | Overlapping scroll steps and resource tracking | Unloaded content-bearing resources remain gaps |
| 07 | Nested overflow containers | Inventory and traverse each scroll surface | Visited ranges and stop reasons per container |
| 08 | Infinite scroll | Accumulate stable IDs/text hashes | No terminal marker means bounded partial capture |
| 09 | Virtualized list | Capture each window before nodes recycle | Reconcile unique IDs against trusted total when available |
| 10 | Virtualized table/grid columns | Traverse both axes; retain cell coordinates | Missing row/column ranges recorded |
| 11 | Accordions/details/FAQs | Expand safe disclosures and capture each panel | Verify expanded state and changed content |
| 12 | Tabs | Enumerate tab-to-panel relationships | Unvisited/failed panels prevent verification |
| 13 | Carousels/slides | Follow controls until known total or repeat | Distinguish repetition from budget exhaustion |
| 14 | Hover/focus help and menus | Exercise identified informational triggers | Capture trigger, state and tooltip relationship |
| 15 | Informational modals | Open and close known reversible dialogs | Capture dialog separately with trigger provenance |
| 16 | Pagination/load-more | Traverse permitted pages/cursors | Terminal condition, unique items and loops recorded |
| 17 | Product/plan/region selectors | Explore finite declared options | Preserve variants; list unvisited combinations |
| 18 | Search/calculators/forms | Capture labels/defaults; run only declared scenarios | Untested input space remains unknown |
| 19 | Open/nested/declarative shadow DOM | Traverse supported roots and composed slots | Host/root paths; avoid slot duplication |
| 20 | Closed shadow roots | Use accessible output and visible-region OCR | Hidden state/access limitation explicit |
| 21 | Nested/cross-origin/srcdoc frames | Inventory frame tree; use browser frame contexts | Every frame has load/extraction outcome |
| 22 | object/embed/document viewers | Identify underlying resource and native format | Viewer UI is not substituted for document content |
| 23 | CSS pseudo-elements/counters/markers | Inspect computed presentation and screenshots | Generated text mapped to visible region |
| 24 | CSS backgrounds/sprites/image-set | Inventory resource and displayed crop; OCR | Missing or ambiguous text image requires review |
| 25 | Canvas/WebGL | Capture pixels; associate available data | Labels versus inferred visual meaning separated |
| 26 | SVG text and outlined glyphs | Parse text; render and OCR paths | External references and unrecognized regions tracked |
| 27 | picture/srcset/responsive artwork | Record selected source and relevant variants | Device-specific content retained separately |
| 28 | Hidden/offscreen/aria-hidden text | Record visibility and accessibility state | Hidden strings not asserted as visible publication |
| 29 | Clamping/clipping/ellipsis | Compare full DOM text and visible excerpt | Preserve both representations |
| 30 | Input values/placeholders/selections | Read control state and semantic labels | Value is linked to state, not treated as paragraph |
| 31 | HTML tables and footnotes | Preserve cell/header/footnote relationships | Text presence alone does not verify table meaning |
| 32 | Math/code/preformatted blocks | Keep MathML/source and exact whitespace | Rendered fallback and interpretation labeled |
| 33 | Responsive/mobile/desktop layout | Capture declared viewport profiles | Differences preserved; other devices untested |
| 34 | Locale/geography/currency/timezone | Isolate context and record actual variant | No silent merging of incompatible contexts |
| 35 | A/B tests/personalization/live values | Timestamp captures and exposed variant IDs | Unknown experiment membership remains unknown |
| 36 | Consent/sticky overlays | Detect occlusion; apply declared ordinary choices | Covered/unreachable regions remain explicit |
| 37 | JSON-LD/microdata/hydration JSON | Parse separately and align with visible claims | Source-visible disagreements retained |
| 38 | noscript/templates/comments/bundles | Archive and classify source-only candidates | Do not index arbitrary code strings as article text |
| 39 | Print-only content | Optional declared print-state capture | Screen/print provenance kept distinct |
| 40 | Script failures/service workers/cache | Record errors and affected resources; test fresh context where appropriate | Capture-context difference and unresolved omissions reported |

PDF and OCR handling must operate at **region level as well as page level**. A page can contain a native-text header and a scanned body; the existence of some native text must not suppress OCR for its image regions. OCRmyPDF's skip mode skips pages already containing text, while redo and force modes have different transformations and limitations. Preserve original bytes before any derived conversion. Native PDF extraction can also lose reading order or fail on font encodings, and ordinary page-text extraction does not cover all form fields, annotations or attachments. ([OCRmyPDF advanced options](https://ocrmypdf.readthedocs.io/en/latest/advanced.html), [PyMuPDF extraction issues](https://pymupdf.readthedocs.io/en/latest/recipes-common-issues-and-their-solutions.html), [pypdf forms](https://pypdf.readthedocs.io/en/stable/user/forms.html), [pypdf annotations](https://pypdf.readthedocs.io/en/stable/user/reading-pdf-annotations.html), [pypdf attachments](https://pypdf.readthedocs.io/en/stable/user/handle-attachments.html))

| ID | Document/media case | Detect and recover | Verification or residual outcome |
|---|---|---|---|
| 41 | Native PDF marginalia/headers/footers | Extract spans with coordinates; render every page | Do not drop marginal text before audit |
| 42 | Scanned PDF | Detect text-like image regions and OCR | Page count plus region coverage and OCR quality |
| 43 | Mixed native/scanned page | Native extraction plus image-region OCR | Native header cannot excuse missed scanned body |
| 44 | Bad/stale/partial existing OCR | Compare rendered text with native layer | Alternative readings retained; duplicates reconciled |
| 45 | Broken fonts/vector text | Flag garbled native output; render/OCR | Unreadable regions remain review items |
| 46 | Columns/sidebars/rotation/RTL/CJK | Layout and language-aware extraction | Block order and ambiguity retained |
| 47 | Tiny/blurred/skewed/low-contrast text | Derived preprocessing and tiled OCR | Original pixels kept; unreadable detail not invented |
| 48 | Merged/multipage tables | Reconstruct cells and continuation links | Headers, units, cell associations and notes verified |
| 49 | Charts/maps/diagrams/equations | Extract labels/data; separate visual interpretation | Do not fabricate numeric data from uncertain pixels |
| 50 | PDF forms/widgets/XFA/checkmarks | Inspect fields, values and appearances | Stored-versus-visible disagreement recorded |
| 51 | Annotations/popups/comments | Enumerate annotation objects | Extract or explicitly exclude each object |
| 52 | Attachments/portfolios | Inventory children and recurse within policy | Every child has an outcome |
| 53 | Layers/clipped/off-page/invisible text | Preserve object and visibility layers | Hidden content cannot silently change visible meaning |
| 54 | Encrypted/corrupt/truncated PDF | Validate, alternate parser/render if useful | Blocked/corrupt/partial; empty output is not blank-page proof |
| 55 | Raster image/screenshot | Inventory pixels and text regions; OCR | Empty OCR does not certify no text |
| 56 | Photos/logos/pictograms | Classify and optionally describe | Generated descriptions explicitly separate from quotations |
| 57 | TIFF/GIF/WebP multiple frames | Count and process frames under policy | Frame ranges and skipped intervals reported |
| 58 | DOCX package parts | Inventory body/tables/headers/notes/comments/drawings/revisions | Hidden/deleted/current text distinct; embedded assets accounted |
| 59 | PPTX shapes/notes/hidden slides | Inventory slides, groups, masters, notes, charts and media | Presenter notes distinct from audience-visible content |
| 60 | XLSX cells/formulas/hidden sheets | Preserve formulas, cached/display values and formatting | No blind recalculation; dates/units/hidden status retained |
| 61 | CSV/TSV/JSON/XML/TXT | Encoding-aware structured parsing | Row/record counts; no silent truncation |
| 62 | EPUB/ZIP/nested packages | Safe container inventory and recursive adapters | Encrypted members and depth/size limits explicit |
| 63 | Audio/captions/multiple languages | Track streams; use native transcript plus ASR where scoped | Segment provenance, overlap and uncertainty retained |
| 64 | Video overlays/subtitles/audio | Inventory tracks; frame/scene extraction and transcription | Sampling is partial temporal inspection, even if audio is complete |
| 65 | Live streams/continuously changing media | Define start/end window | Never claim coverage beyond the observed window |

OCR quality depends on image conditions and segmentation; recognizing table text does not reconstruct table meaning automatically. Audio transcription can hallucinate or repeat text, so fluent output is not verification. Presentation files also contain separate notes, comments, master/layout parts and embedded media that a slide-text-only parser can overlook. These are adapter acceptance requirements, not blanket support promises. ([Tesseract quality guidance](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html), [Whisper model card](https://github.com/openai/whisper/blob/main/model-card.md), [Microsoft PresentationML structure](https://learn.microsoft.com/pl-pl/office/open-xml/presentation/structure-of-a-presentationml-document))

| ID | Access, scope or operational case | Detect and recover | Verification or residual outcome |
|---|---|---|---|
| 66 | DNS/TLS/connection failure | Classify transport cause; bounded appropriate retry | Failed acquisition retained |
| 67 | Redirect chains/loops | Track all destinations and enforce scope/security per hop | Loop or disallowed destination explicit |
| 68 | Login/401/403/geographic block | Detect response and rendered gate | Blocked; seek permitted access path |
| 69 | 429/rate limiting | Respect Retry-After; reduce origin concurrency | Retry scheduled or budget stopped |
| 70 | Transient 5xx | Bounded backoff with jitter | Persistent failure not erased by later partial success |
| 71 | CAPTCHA/challenge with HTTP 200 | Inspect headers/body/visual signals | Challenge is not requested content; blocked |
| 72 | Soft 404/empty shell/maintenance page | Compare status, body, title and expected type | Suspected wrong payload requires review |
| 73 | Expired signed asset URL | Reacquire through permitted source context | Expiry tracked; tokens redacted in reports |
| 74 | Wrong MIME/partial download | Compare headers, signatures and parser validation | Truncation/wrong payload explicit |
| 75 | Query/hash/canonical identity | Preserve exact fetch identity and separate normalized identity | Canonical hints do not justify skipping requested content |
| 76 | Facets/calendars/session URL explosion | Frontier limits and repeated-state detection | Budget termination differs from exhausted discovery |
| 77 | Cross-host CDN/linked download | Relationship-aware asset scope | Exclusion reason visible; no accidental full-site recursion |
| 78 | robots.txt unavailable/unreachable/disallow | Apply documented robots policy before crawl | Policy exclusion distinct from extracted success |
| 79 | Browser/parser crash or timeout | Isolated workers, checkpoints, resumable attempts | Previous evidence and outstanding gaps survive |
| 80 | Archive bomb/huge image/hostile document | Bound decompression, pixels, pages, memory and runtime | Limit stop reported as incomplete processing |
| 81 | SSRF via seed/redirect/frame/asset | Validate destinations and enforce network egress | Denied request logged without fetching internal target |
| 82 | Malicious page instructions or active files | Treat content as data; disable macros and dangerous actions | No arbitrary commands or form submission |
| 83 | Normalization/dedup destroys meaning | Keep original spans and reversible transformations | Critical numbers, negations, units and footnotes preserved |

## A resource, state and region ledger supplies the system's brain

The proposed architecture is a scope/access policy service feeding a URL/resource frontier, a fetch-and-MIME classifier, immutable evidence storage, browser and format-specific workers, a shared manifest, a reconciler, a targeted remediation queue and a versioned content export. A relational database plus object storage is sufficient for the first version. A vector database does not solve content acquisition or completeness verification.

The “brain” should combine deterministic checks, independent extraction channels and bounded model assistance. Deterministic checks reconcile page counts, frame outcomes, declared item totals, failed requests and unvisited controls. Visual detectors and OCR identify text-shaped regions with no extracted counterpart. An LLM can suggest section labels, flag likely inconsistencies and prioritize retries, but must not invent omitted content or self-certify full coverage. Its decisions require evidence pointers and inspectable reasons.

Collect original response bytes and headers before transformations. The browser worker captures rendered DOM, supported shadow roots, frames, accessibility semantics, computed text/image presentation, screenshots and content-bearing network responses. The state explorer traverses permitted disclosures, tabs, nested scrolling and pagination while accumulating observations. Use meaningful DOM/region changes and adapter-specific readiness signals, not a fixed sleep or `networkidle` as a universal completion test. Playwright's documented network and accessibility facilities provide observation mechanisms; service workers can affect network visibility and need explicit handling. ([Playwright Page API](https://playwright.dev/docs/api/class-page), [Playwright ARIA snapshots](https://playwright.dev/docs/aria-snapshots), [Playwright network](https://playwright.dev/docs/network))

The document worker should combine a native PDF inspector/renderer, structured conversion and selective OCR. Candidate tools include PyMuPDF, pypdf, OCRmyPDF/Tesseract, Docling and a general-format adapter such as Tika. Pin versions and benchmark the exact configuration before selecting a stack. Format support lists do not establish fidelity for comments, revisions, chart data or embedded objects. Use alternate parsers when disagreement or risk warrants it rather than automatically processing every clean file twice. ([Docling formats](https://github.com/docling-project/docling/blob/main/docs/usage/supported_formats.md), [Tika formats](https://tika.apache.org/docs/4.0.x/formats.html))

| Ledger entity | Minimum content |
|---|---|
| Run | Scope/version/hash, timestamps, software/model versions, budgets, inventory revision, discovery closure and stop reason |
| Resource | Parent relationship, original/final URL, redirect chain, declared/sniffed MIME, status, payload hash/size, scope decision |
| State | Parent state, action/result, viewport/locale/context, capture time, state fingerprint and remaining frontier |
| Region | Resource/state/page/frame, selector or coordinates, visibility, content classification, status and expected extraction methods |
| Extraction | Raw and normalized text, structure/order, method/version, uncertainty, alternative readings and span coordinates |
| Evidence | Immutable artifact/hash, restricted storage pointer, timestamp and source relationships |
| Event | Monotonic sequence, entity, previous/new state, inventory revision, reason, attempt and evidence references |

The following **illustrative per-URL record** shows an honest partial result. It is a design example, not a measured capture. Resource and region totals use different units. Both include unsuccessful required items; unknown discovery prevents interpreting either as all content on the URL.

```json
{
  "run_id": "example-run-001",
  "requested_url": "https://example.org/product",
  "final_url": "https://example.org/product",
  "captured_at": "2026-10-14T10:00:00Z",
  "scope": {
    "mode": "supplied_url_plus_embedded_assets",
    "access_context": "public_anonymous",
    "viewport": {"width": 1440, "height": 1000},
    "locale": "en-US",
    "allowed_actions": ["scroll", "expand_disclosure", "select_tab"],
    "linked_download_depth": 1,
    "arbitrary_form_inputs": "excluded"
  },
  "status": "PARTIAL",
  "inventory_revision": 12,
  "discovery": {
    "closed_within_scope": false,
    "remaining_controls": 2,
    "unknown_state_space": true,
    "stop_reason": "action_budget_exhausted"
  },
  "coverage": {
    "required_resources_discovered": 7,
    "required_resources_acquired": 6,
    "required_regions_discovered": 24,
    "regions_verified": 18,
    "regions_needing_review": 3,
    "regions_blocked": 1,
    "regions_unvisited": 2,
    "denominator_basis": "observed_inventory_only"
  },
  "gaps": [
    {"id": "frame-2", "status": "BLOCKED", "reason": "embedded_frame_access_denied"},
    {"id": "image-4", "status": "NEEDS_REVIEW", "reason": "small_print_ocr_disagreement"},
    {"id": "tab-3", "status": "BUDGET_EXHAUSTED", "reason": "discovered_not_visited"}
  ],
  "gaps_list_is_abbreviated": true,
  "example_block": {
    "region_id": "region-7",
    "state_id": "state-2",
    "frame_path": "main",
    "heading_path": ["Product", "Eligibility"],
    "raw_text": "Available only for annual plans.",
    "normalized_text": "Available only for annual plans.",
    "visibility": "rendered_visible",
    "method": "rendered_dom",
    "status": "VERIFIED_EXTRACTED",
    "evidence_ids": ["dom-state-2", "screenshot-state-2-region-7"]
  }
}
```

Emit events continuously as resources and states are discovered, extracted, verified and retried. A suitable live display is **“24 observed regions: 18 verified, 3 need review, 1 blocked, 2 unvisited; discovery still open.”** It should link each gap to the screenshot crop, failed request or unvisited control. As new regions appear, the denominator grows and an observed coverage percentage can decrease legitimately. Distinguish a terminal attempt from a successful result: blocked, unsupported and failed are terminal outcomes too.

| Final badge | Proposed decision rule |
|---|---|
| RUNNING | Required work or bounded discovery still executing |
| FAILED | No usable target payload due to acquisition/processing failure |
| BLOCKED | Access prevents obtaining the target content |
| PARTIAL | Useful content exists but at least one required resource/region/state is missing, blocked, unsupported or budget stopped |
| NEEDS_REVIEW | Identified content obtained but fidelity or discovery checks remain unresolved |
| VERIFIED_WITHIN_SCOPE | Declared bounded discovery closed and every required check passed; scope and context displayed |
| EXCLUDED | Target excluded by explicit policy; not an extraction success |

An aggregate badge must not hide the underlying status vector. A missing eligibility footnote outweighs hundreds of recovered navigation labels for downstream decisions. Avoid a weighted “quality score” that obscures that fact. Preserve unresolved gaps even after a fallback produces some text.

## Public third-party access needs honest failure handling

Normal browser rendering can recover content omitted by simple HTTP parsing, but it does not guarantee access through a site's restrictions. For third-party targets, use ordinary permitted public access, documented APIs or licensed feeds where available, bounded retries and owner coordination when necessary. Do not make proxy rotation, identity spoofing or automated CAPTCHA circumvention prerequisites for the product. Persistent access controls produce a blocked or partial result with evidence, rather than a fictitious complete extraction.

Handle robots rules explicitly: RFC 9309 distinguishes an unavailable robots file from an unreachable one and defines their crawler behavior. Robots allowance is not authorization to access private information. Respect rate-limit signals, including `Retry-After`, with per-origin concurrency control and bounded backoff. Challenge pages can return HTML in place of a requested non-HTML resource; Cloudflare documents a `cf-mitigated: challenge` response marker. HTTP 200 alone therefore cannot establish that the target content was acquired. ([RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html), [MDN 429](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status/429), [MDN Retry-After](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Retry-After), [Cloudflare challenge detection](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/))

Preserve exact acquisition URLs, particularly signed links, while keeping normalized identities separately. Signed URLs can expire and can carry bearer credentials; reacquire through the permitted source page when possible and redact tokens in user-facing reports. Do not strip query parameters indiscriminately: they can encode pagination, locale, variants or signatures. Canonical declarations are useful hints, not proof that two requested pages have identical content. ([Amazon S3 presigned URLs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html), [Google canonicalization](https://developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls))

Isolation belongs in the initial architecture because this service processes arbitrary public URLs and files. Enforce outbound network restrictions for seeds, redirects, frames and browser subrequests, including private/link-local destinations and DNS rebinding defenses. Separate browser/parser workers from host credentials, isolate tenants, disable macros and parser network access by default, and bound decompression, pixels, pages, nesting, memory, CPU and time. Preserve sanitized review evidence while restricting raw artifacts and tokens. Tika specifically notes that output limits do not bound all processing resource use, so worker isolation and timeouts are necessary. ([OWASP SSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html), [Tika security](https://tika.apache.org/docs/4.1.x/security.html))

Interaction policies should permit identified, reversible informational controls. Blindly clicking every button is not a content-discovery strategy: actions can purchase, send messages, subscribe or change state. Extracted text and page instructions remain untrusted data, including when a model reviews them. These controls protect both the user and the integrity of the corpus.

## Prove gap detection before building semantic analysis

The implementation sequence should establish evidence and failure accounting before expanding format breadth. The proposed stages below are engineering choices, not measured duration or cost estimates. No credible accuracy, throughput or OCR-language promise can be made until a representative corpus is tested.

| Stage | Deliverable | Exit criteria |
|---|---|---|
| 0: Contract and benchmark | Supported scope, ledger schema, annotated fixtures, public-site sample list | Every supported capability and exclusion has a test and visible outcome |
| 1: Audited capture MVP | Raw HTML, rendered browser, native PDF/image extraction, region OCR and immutable evidence | Every supplied URL has an outcome; every extracted block has provenance; known failures persist |
| 2: Dynamic coverage | Tabs, accordions, nested scrolling, virtualization, frames, linked assets, live gap events | Required state/resource counts reconcile; budget stops remain partial |
| 3: Broader format fidelity | Selected Office/archive/media adapters and language support | Per-feature fixtures demonstrate support; unsupported parts remain visible |
| 4: Website analysis | Main-content views, themes, exact/semantic similarity and contradiction candidates | Every result traces to evidence and reflects capture gaps and context |

Build controlled fixtures with known expected content and independently annotate held-out samples. Include a delayed-hydration page, nested scroller, two-axis virtualized grid, unopened tab, failed text-bearing image, blocked iframe, HTTP-200 challenge, expired asset URL and endless pagination. The document set should include a mixed native-header/scanned-body PDF, bad OCR layer, image-only disclaimer, rotated multilingual text, merged multipage table, damaged page, attachment and genuinely blank page. Include false-positive traps such as a photo without text and code strings that are never displayed. These establish whether the verifier detects missing content as well as whether extraction succeeds.

**The initial release gate should be zero false VERIFIED_WITHIN_SCOPE results on the known-missing adversarial suite.** That is a bounded acceptance criterion, not a universal guarantee. Measure discovery recall and extraction recall separately against ground truth, plus precision, character/word error rates, reading order and table relationships. Test exact preservation of numbers, decimal points, currencies, units, negations and exceptions. Report by format/language/content class rather than one pooled average. Set numeric fidelity thresholds after baseline evaluation and the business cost of errors are understood. Measure runtime, model/OCR cost, retries and unresolved review volume alongside quality.

Resumption and status tests matter as much as content tests: a crash must not lose failure records, a retry must not double-count regions, and a failed attachment must keep the URL partial. Evaluate security denial paths without contacting prohibited targets. Public live-site samples test generalization, but manual review cannot certify every undiscovered state. Keep the fixture suite versioned and add a regression whenever a real omission is found.

For the future analysis layers, retain raw evidence, normalized content, layout structure and semantic annotations separately. Label navigation/footer boilerplate rather than permanently deleting it. Keep page-level and section-level similarity separate, and compare equivalent locale, product, audience and time contexts. Deduplicate repeated content while preserving all source relationships. Theme clustering can tolerate some noise; contradiction detection is much more sensitive to lost qualifiers and OCR errors.

A contradiction candidate should consist of **two inspectable claims with exact source quotations**, entity/topic, product/version, effective dates, audience, geography, conditions, units, negation and linked footnotes. Semantic proximity only helps find candidate pairs; it does not establish contradiction. A changed price across two capture dates or different product variants can be consistent. Hidden revisions, source-only strings, inferred image descriptions and uncertain OCR should not carry the same evidentiary weight as verified visible statements. Gap metadata should let downstream analysis exclude uncertain regions or explicitly downgrade confidence.

The immediate next step is to choose a small, representative public third-party URL set spanning dynamic HTML, text-bearing images and mixed PDFs, then finalize supplied-URL versus recursive-site scope. Build the manifest and adversarial fixtures before selecting converters by convenience. The key product advantage will be the ability to explain **which content was recovered, where it came from, what remains unresolved and why**. That makes future website analysis reviewable even when a particular source cannot be fully acquired.
