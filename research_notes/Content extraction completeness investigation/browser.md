# Browser and HTML extraction completeness

## What can each extraction channel see, and why is one channel insufficient?

### Takeaway
Treat a URL as a collection of resources and browser states, not a single text string. Completeness can be assessed against a declared, observed inventory; no general browser event proves that every possible state and future response has been discovered.

### Cited Findings
- `textContent` includes script/style text and hidden element text; `innerText` accounts for styling and excludes hidden text. Thus neither is a complete, clean content representation by itself. [MDN textContent](https://developer.mozilla.org/en-US/docs/Web/API/Node/textContent)
- `innerHTML` serialization excludes shadow roots. Playwright locators normally traverse shadow DOM, but XPath does not pierce shadow roots and closed-mode roots are unsupported. [MDN innerHTML](https://developer.mozilla.org/en-US/docs/Web/API/Element/innerHTML), [Playwright locators](https://playwright.dev/docs/locators)
- Iframes have their own locator context, requiring explicit frame traversal. [Playwright FrameLocator](https://playwright.dev/docs/api/class-framelocator)
- CSS `content` can create displayed text, counters, images, and alternative text; generated output needs separate consideration from ordinary DOM text. [MDN CSS content](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/content)
- Canvas is a bitmap without semantic information about drawn objects; canvas content is not exposed like semantic HTML to accessibility tools. [MDN canvas](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/canvas)
- ARIA snapshots represent roles, names, attributes, values, and text in the accessibility tree. This is a complementary semantic view. [Playwright snapshots](https://playwright.dev/docs/aria-snapshots)
- Templates contain fragments that are not initially rendered; distinguish template payload from visible content. [MDN template](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/template)
- Google requires structured data to represent page content and not describe hidden material; this is a publishing guideline, not a guarantee that any site's markup actually agrees with its page. [Google structured data policies](https://developers.google.com/search/docs/appearance/structured-data/sd-policies)

### Inferences
Recommended channels: (1) preserve original HTTP response bytes and headers; (2) parse source HTML; (3) snapshot rendered DOM including accessible shadow trees and frames; (4) capture accessibility semantics; (5) inspect computed presentation and take screenshots; (6) OCR text-bearing visual regions; (7) capture content-bearing responses requested by the page; (8) parse embedded metadata as a separate channel. Store every channel separately before reconciling.

Client-delivered HTML, JS, CSS and JSON can be inspected, but that is not server source code, CMS database access, or proof of all unpublished content. Do not dump arbitrary script strings into the article corpus. Label them source-only candidates, bind them to rendered sections where possible, and preserve conflicts rather than replacing visible wording with metadata.

### Gaps
- No target websites or owner-provided content inventories were provided; actual support, missed section rates, and runtime cannot yet be measured.
- Closed shadow root coverage must be validated with the selected browser/instrumentation. A visible screenshot is evidence of rendered content, not proof that all inaccessible state was extracted.

## Which edge cases should be in the browser extraction test matrix?

### Takeaway
The following matrix is a proposed engineering test inventory, not a claim that these scenarios were observed on a target website. For each applicable case the scraper needs a detection signal, extraction attempt, verification evidence, and explicit unresolved result.

### Cited Findings
- Modern pages can load data and UI after the load event; Playwright explicitly says there is no general way to determine that a page is fully loaded. Poor hydration can also make an apparently available interaction ineffective. [Playwright navigation](https://playwright.dev/docs/navigations)
- Virtualized lists retain a moving subset of items and recycle nodes. Grids can virtualize in two dimensions. Therefore one final DOM snapshot can omit previously visible records even after scrolling. [web.dev virtualization](https://web.dev/articles/virtualize-long-lists-react-window)
- Browser emulation exposes viewport/device, locale, timezone, geolocation, color scheme and other context settings. These are useful explicit capture dimensions. [Playwright emulation](https://playwright.dev/docs/emulation)
- Service Workers can affect visibility of network interception. The documentation describes this limitation and alternative handling. [Playwright network](https://playwright.dev/docs/network)

### Inferences
The matrix below is a proposed validation plan informed by the findings above. Evidence fields are requirements for our scraper, not capabilities automatically guaranteed by a library.

| Case to test | Detection and extraction plan | Required evidence or unresolved outcome |
|---|---|---|
| Plain static HTML, malformed markup, charset/entity errors | Preserve bytes; decode using response evidence; compare parser output with browser repair | Encoding, parse warnings, original and normalized text |
| SSR then hydration, client-only SPA, streaming HTML | Capture response and successive rendered snapshots; wait for meaningful section signals | Content deltas, hydration failures, settling interval |
| Slow fetches, retries, long polling, WebSockets | Track content-bearing responses and DOM changes with time budgets | Pending/failed requests; never equate quiet network with completeness |
| Lazy images and lazy sections | Scroll viewport and nested scroll areas in overlap steps; capture new regions | Per-step region inventory and new content hashes |
| Infinite feed with no terminal marker | Accumulate unique blocks throughout scrolling | Stopped-by-budget status, not completed |
| Virtualized list/table/grid | Collect each visited window, including horizontal columns; use stable item IDs when present | Unique observed count versus trusted declared total, holes and duplicates |
| Nested overflow containers | Enumerate independent scroll surfaces | Each surface's visited range and stop reason |
| FAQ accordions and details | Expand safe disclosure controls; re-extract target panels | Control/panel mapping, pre/post text and expanded state |
| Tabs and mutually exclusive panels | Visit tabs independently and preserve state IDs | All discovered tabs attempted; failed tabs named |
| Carousels and slides | Enumerate slide controls and stable IDs; step until repeat or known total | Unique slides, end/repeat/budget condition |
| Hover/focus tooltips and menus | Exercise identified informational controls with reversible actions | Trigger, text, bounding box, interaction state |
| Modal information dialogs | Open known informational dialogs and close after capture | Dialog content and relationship to triggering block |
| Pagination and load-more | Follow content pagination under explicit scope; avoid URL-loop conflation | Page/cursor lineage, unique item count, terminal condition |
| Selectors for product variant, plan or region | Enumerate allowed finite options; preserve variant identity | Option matrix and unvisited combinations |
| Forms/search/calculators | Capture labels/help/default outputs; only run declared input cases | User-entered scenario, output, untested input space |
| Open/nested/declarative shadow DOM and slots | Traverse composed content; distinguish assigned and fallback nodes | Host/root paths and deduplication provenance |
| Closed shadow DOM | Try supported capture channels; OCR visible text where needed | Explicit channel limitation and unknown hidden states |
| Same-origin/cross-origin/nested/srcdoc iframes | Inventory frame tree; capture each permitted frame separately | Frame URL, owner position, access/load failure |
| Embedded viewers/object/embed | Detect resource MIME and dispatch native parser where available | Viewer content versus underlying file coverage |
| CSS pseudo text/counters/markers | Inspect computed presentation and reconcile screenshot/DOM | Generated-text source and visible placement |
| CSS background text images, sprites, image-set | Inventory computed image resources and rendered regions | OCR attempts, original resource association |
| Canvas/WebGL/charts/maps | Capture image; use available structured chart data plus visual extraction | Chart labels versus inferred interpretation kept separate |
| SVG text versus outlined glyphs | Parse text nodes; render/OCR outlined text and external SVG | Extraction method and unrecognized regions |
| Images, picture/srcset, mobile artwork | Record selected source and alternatives; OCR rendered and original image as needed | Variant/resolution and confidence; alt text separate from image text |
| Animated GIF/video/subtitles/audio | Hand off media assets and time ranges to specialist pipeline | Frames/time intervals inspected, captions/transcript provenance |
| Hidden/offscreen/aria-hidden text | Classify visibility rather than delete immediately | Visible, accessibility-only, hidden, template-only labels |
| Clipped text, line clamp, CSS truncation | Preserve DOM full text and rendered excerpt separately | Full-source versus presented text distinction |
| Input values, placeholders, selected options | Read semantic control values, not only descendant text | Label/value/state and sensitivity handling |
| Tables with merged headers/footnotes | Preserve row/column structure and header associations | Cell coordinates, units, note relationships |
| Math/code/preformatted content | Preserve source representations and whitespace | Math/MathML/LaTeX/rendered fallback; code language |
| Mobile/desktop/responsive content | Capture declared viewport profiles | Profile-specific sections and differences |
| Language/region/currency/timezone/auth variants | Isolated explicit capture contexts | Language, geography, session class, timestamp |
| A/B tests, personalized history, live stock/prices | Preserve actual served variant; avoid merging incompatible runs | Variant identifiers when exposed; otherwise unknown |
| Consent overlays and login walls | Detect obstructed region; use authorized ordinary state choices | Access-limited status and covered/obscured regions |
| JSON-LD, microdata, RDFa, hydration JSON | Parse separately, map claims to visible blocks where possible | Schema/source-only mismatch flags |
| Comments, noscript, templates, bundles | Archive as source; classify candidates before indexing | Source-only evidence; never assert publication from strings alone |
| Sticky overlays and repeated navigation | Capture underlying reachable regions; tag recurring site chrome | Block role and visibility; preserve raw evidence before dedupe |
| Linked downloads and external embeds | Build resource graph with MIME dispatch and scope policy | Discovered, included, excluded, blocked, failed assets separately |
| Print styles and print-only notes | Optional explicit print-state capture | Screen versus print variant distinction |
| Redirects, hash routes, query variants | Preserve requested/final URLs and navigation history | Canonicalization decision and route state identity |
| Resource failure or script exception | Capture HTTP failures, console errors and fallback UI | Failed resource linked to potentially affected region |

### Gaps
- Arbitrary UI input spaces, time-dependent feeds, and personalized variants cannot be exhaustively enumerated without site-specific contracts.
- A finite edge-case list is a maintainable test suite, not proof against every future web implementation.

## What architecture and evidence should support real-time completeness and downstream semantic analysis?

### Takeaway
Use a content-and-state ledger with explicit coverage dimensions. The browser should report what it discovered, attempted, extracted, reconciled, and could not reach; an LLM can suggest missing regions but should not issue an unsupported 100% certificate.

### Cited Findings
- Dynamic loading has no universal finished signal, virtualized DOM nodes can disappear, and ARIA and DOM describe different representations. These properties motivate stateful evidence collection rather than single-pass text extraction. [Playwright navigation](https://playwright.dev/docs/navigations), [web.dev virtualization](https://web.dev/articles/virtualize-long-lists-react-window), [Playwright snapshots](https://playwright.dev/docs/aria-snapshots)

### Inferences
Proposed pipeline:
1. Declare scope: requested URLs, allowed resource relationships, interaction classes, viewport/locale/auth profiles, depth/time/item caps, media coverage policy.
2. Acquire and archive: immutable response bytes, headers, requested/final URL, redirect chain, timestamp, content hash, fetch context, failure evidence.
3. Inventory: sections, frames, controls, scroll surfaces, assets and content-bearing requests. Assign IDs immediately, before extraction success is known.
4. Extract channels: HTML structure, DOM states, accessible semantics, source data, images/OCR, document/media parsers.
5. Explore: bounded queue of safe informational state transitions, snapshots after every meaningful transition, accumulation before virtualized items disappear.
6. Reconcile: align blocks across channels by coordinates, structural ancestry, text and resource IDs. Preserve alternatives, do not average away discrepancies.
7. Verify: reconcile known totals, unexplored controls, failed resources, missing frames and text-bearing visual regions without extracted text. A second detector can identify omissions but remains fallible.
8. Emit continuously: status changes as new controls/resources are discovered. A denominator that grows is legitimate; log its history.

Recommended status vocabulary: pending, observed, extraction-attempted, extracted, verified-against-observed-evidence, partial, access-blocked, failed, unsupported, excluded-by-scope, unknown. Reserve `complete-within-declared-scope` for runs with no outstanding known in-scope gaps; avoid `100% of URL content`.

Report separate metrics: discovered controls exercised; frames processed; observed assets handled; declared pagination/items reconciled; visual text regions accounted for; extraction quality; remaining uncertainty. Ratios must name their denominators. An observed-assets ratio of 100% means all discovered assets were handled, not that discovery was exhaustive.

Interaction guardrail: allow known reversible content disclosures; do not indiscriminately click every button or submit arbitrary forms. Purchase, delete, subscribe, send, account modification, and authentication transitions require their own authorization and are outside ordinary content exploration. A viewport or full-page screenshot records a rendered state; it does not prove hidden tabs, nested scrollers, virtualized rows or future responses were covered.

For each block store: requested/final/canonical URL, capture timestamp, context/state ID, frame/shadow path, source channel, raw text, normalized text, heading ancestry, reading order, coordinates, resource hash, visibility, content role, language, table relationships, OCR confidence, linked footnotes, extractor/version, coverage status and exact evidence pointer. Preserve source HTML and screenshots as replayable evidence.

For site themes and similarity: keep raw corpus plus a separately derived main-content corpus; label boilerplate rather than destroying it. Compare equivalent language/context/time variants and maintain page-level and section-level embeddings separately. For contradiction analysis: retain exact quotations, negation, qualifications, entity, date, audience, geography, units, product/version and links to source evidence. A contradiction should be an inspectable claim pair with context, not just a cosine similarity result. A changed price in two timestamps is not automatically a contradiction.

Test on controlled fixtures with known hidden panels, source-only strings, virtualized totals, OCR ground truth and intentionally failed resources. Measure discovery recall, extraction fidelity, false complete rate, and runtime. Include negative fixtures where the correct answer is unknown or blocked. Human-reviewed live-site samples then test generalization.

### Gaps
- No accuracy percentages, confidence thresholds or throughput estimates are defensible before representative fixtures and target-site evaluation.
- Source agreement can be correlated: DOM and accessibility can share the same omission; OCR can miss low-contrast text. Agreement increases evidence but does not prove universal recall.
