# Evidence-driven completeness verification and crawler operations

## What can completeness actually mean, and how should it be measured?

### Takeaway
Unconditional 100% extraction of an arbitrary URL cannot be certified by a crawler. Build an auditable, bounded coverage claim for a declared snapshot, state set, resource set, and extraction policy; distinguish accounted-for items from faithfully recovered content.

### Cited Findings
- Modern pages continue fetching data and populating UI after load; Playwright explicitly says there is no universal way to decide a page is loaded. Hydration can make apparently enabled controls ineffective. [Playwright navigation](https://playwright.dev/docs/navigations)
- Playwright documents networkidle as discouraged for readiness and provides request, response, and page event instrumentation. A browser lifecycle event is an observation, not a completeness oracle. [Playwright Page API](https://playwright.dev/docs/api/class-page)
- Faceted navigation can create effectively infinite URL spaces and overcrawling. [Google crawling guidance](https://developers.google.com/crawling/docs/faceted-navigation)

### Inferences
The following is a proposed product contract and schema, not an industry standard or a demonstrated implementation.

**Scope contract before execution:** record input URLs; permitted hosts/subdomains and schemes; one-page-plus-assets versus linked-page crawl; asset recursion depth; page/resource/byte/time/action budgets; timestamp/window; locale; timezone; viewport/device; identity/access role; geographic execution context; consent policy; allowed read-only interactions; media scope (text, image semantics, audio/transcripts, video frames); supported formats; retention and sensitive-data policy. Browser-visible client content is a different scope from server-side source code, databases, drafts, or all possible users' experiences. Client crawling cannot certify the latter.

**Three distinct completeness questions:** discovery coverage (did we identify the relevant things?); acquisition coverage (did we retrieve every identified thing?); extraction fidelity (did we recover its content accurately and preserve relationships?). A fourth question, state coverage, asks which permitted UI variants were actually explored. None can safely substitute for another.

**Manifest model:**

| Entity | Required fields |
|---|---|
| Run | run_id, contract/version/hash, software/model versions, start/end, budgets, root seeds, inventory_revision, discovery_open, termination_reason |
| Resource | resource_id, parent_id, discovered_from evidence, original URL, request URL, final URL, redirect chain, declared and sniffed MIME, HTTP outcome, payload hash/size, acquisition time, scope decision, mandatory flag, expected/observed child counts |
| State | state_id, resource_id, parent_state_id, action and outcome, viewport/locale/role/consent, URL/DOM fingerprint, capture time, stability observations, remaining controls, state budget |
| Region | region_id, resource/state/page/frame, region kind, selector or DOM path and bounding box, expected extraction routes, visibility, source-layer distinction, content-bearing classification, status/reason, evidence links |
| Extraction | extraction_id, region_id, method/version, text/structured output, token or span coordinates, original reading order, confidence signal, competing candidate text, normalization rules, duplicate linkage |
| Evidence | immutable artifact ID/hash, storage URI, type, timestamp, redaction/access policy, relationships to resource/state/region/extraction |
| Event | monotonic sequence, run_id, entity_id, old/new state, inventory revision, timestamp, reason, attempt, evidence IDs |

**Terminal region statuses:** VERIFIED_EXTRACTED (defined checks passed), EXTRACTED_UNVERIFIED (content present but unresolved fidelity), PARTIAL, BLOCKED, FAILED, UNSUPPORTED, BUDGET_EXHAUSTED, EXCLUDED_BY_POLICY, VERIFIED_NONCONTENT. Pending states are DISCOVERED, QUEUED, FETCHING, RENDERING, EXTRACTING, VERIFYING, RETRY_SCHEDULED. Every exclusion needs a reason and evidence. Decorative classification must be inspectable and reversible. Missing or undecodable image is not decorative by default. Terminal means the attempt ended, not the problem disappeared; a later retry creates a new attempt and revision.

**Final status precedence:** return a vector of discovery, acquisition, fidelity, state exploration, and reasons. If a single badge is necessary: RUNNING while work/discovery remains; FAILED when no usable target payload exists due to acquisition or processing failure; BLOCKED when access prevents obtaining target content; PARTIAL if any required region/resource/state is missing, unsupported, budget-stopped, or blocked while other content exists; NEEDS_REVIEW if all identified required items are obtained but fidelity or discovery questions remain; VERIFIED_WITHIN_SCOPE only when all mandatory checks pass and declared bounded discovery is closed. A useful partial extraction with an unavailable attachment must not become COMPLETE merely because the HTML succeeded. An entirely policy-excluded target is EXCLUDED, not successful extraction. Preserve all causes even if the UI shows one badge.

**Defensible metrics, displayed separately:**

- Inventory accounting = terminal manifest items / discovered in-scope manifest items. This can reach 100% while extraction fails; explicitly label it accounting, never content extracted.
- Acquisition = successfully obtained required resources / all discovered required resources, including blocked and failed ones in denominator.
- Region verified coverage = verified required content regions / all discovered required content regions. Label as observed-inventory coverage and show unresolved discovery flags alongside it.
- State exploration = visited required declared states / required declared states, only if the state set is finite and known. Otherwise show discovered/visited/frontier counts and stop reason.
- PDF page processing = processed pages / parser-reported page count; this says nothing by itself about text or image completeness within each page.
- On manually annotated fixtures, text recall = matched ground-truth content units / ground-truth units; precision = correct extracted units / extracted units. Use normalized text metrics plus exact preservation tests for numbers, units, negation, tables, and order. OCR character/word error rates are separate fidelity measures.

Do not compute a single weighted average which hides a missing pricing clause behind 500 recovered navigation labels. Do not count a region twice because DOM and OCR both recovered it. Never remove failed resources from the denominator. Do not derive the denominator solely from successfully extracted content. A DOM-only inventory misses rasterized content; screenshot-only inventory misses hidden states and unrendered data. Byte counts and screenshot area are not semantic content counts. An LLM's self-rating is not a calibrated probability that nothing is missing. A percentage can decrease during discovery as new regions appear; this is honest and should be explained in the UI.

**Live audit loop:** initial fetch emits resource discovery and access classification; DOM, browser network, frame inventory, assets and screenshots emit additional candidate regions; interactions add child states and more regions; extractor workers emit candidate content; verifier compares independent channels, maps extracted spans to regions, and emits gaps; remediation queues targeted rerender/OCR/refetch; closure emits final evidence-backed state. Example: 24 observed regions, 18 verified, 3 pending OCR, 1 blocked iframe, 2 unexplored tabs. Display these counts with live phase and outstanding causes instead of “75% of all content.”

Cross-check raw HTML text vs rendered DOM vs accessibility structure vs visible OCR; compare PDF native text with rendered pages; flag visible text-shaped areas without matched extraction, unloaded content-bearing images, unresolved loaders, clipped or overlapping text, child resources without outcomes, and interactions with no expected state change. Agreement raises confidence but correlated failures remain possible. Never invent absent content to reconcile disagreement.

### Gaps
No cited source establishes a universal quantitative completeness score for arbitrary websites. Universal coverage would require knowing all undiscovered states and inaccessible resources, which a black-box run does not provide. Exact thresholds must be calibrated on the user's target sites and manually reviewed fixtures. Site ownership, supported languages, authentication, and whether visual meaning/audio/video are required remain unspecified.

## How should the crawler handle blocks, URL scope, and unsafe inputs?

### Takeaway
Treat access outcomes as first-class evidence and recover through supported access paths, bounded retries, and owner coordination. Failure to obtain content must remain visible in the audit instead of being masked by aggressive retries or an unrelated HTML response.

### Cited Findings
- Robots rules are crawler instructions, not access authorization. RFC 9309 distinguishes unavailable robots files (for example 4xx) from unreachable files (server/network errors), with complete disallow required for the latter; it also specifies redirects and caching. [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html)
- HTTP 429 indicates rate limiting; Retry-After can provide a delay or HTTP date. [MDN 429](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status/429), [MDN Retry-After](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Retry-After)
- Cloudflare documents cf-mitigated: challenge as the marker on Challenge Page responses and text/html as their content type even when another resource type was requested. [Cloudflare detection](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/)
- Google identifies content-based soft 404s even when HTTP status suggests success. [Google crawling errors](https://developers.google.com/search/docs/crawling-indexing/troubleshoot-crawling-errors)
- Presigned S3 URLs expire at their configured time or earlier if their credentials expire or are revoked; they are bearer tokens. [Amazon S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html)
- OWASP recommends URL/IP validation, network-layer protection and redirect restrictions for SSRF defenses; arbitrary outbound URL handling can expose internal services. [OWASP SSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
- Stored browser authentication state can contain impersonation-capable cookies and headers. Playwright uses isolated browser contexts and warns against committing authentication state. [Playwright authentication](https://playwright.dev/docs/auth)
- Canonical declarations are signals for selecting representative URLs among duplicates; site-preferred and Google-selected canonical URLs can differ. [Google canonicalization](https://developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls)

### Inferences
Proposed access policy: classify DNS/TLS/connection failures, redirect loops, 401/login redirects, 403, 404/410, 429, transient 5xx, captcha/challenge, consent overlay, geographic restriction, session expiry, signed-resource expiry, unexpected MIME, truncated response and soft-200 errors separately. Inspect headers, MIME and body fingerprints before extraction; HTTP 200 is not a pass. A page-sized captcha image is not the user's requested page content.

Use per-origin concurrency and adaptive delay; honor Retry-After, jitter bounded exponential retries for transient failures, cache safely, and stop retry storms. For own/client sites prefer documented APIs, CMS exports, asset inventories, a read-only service account, staging access or narrowly scoped owner-approved allowlisting. For third-party sites record block evidence and use permitted public/API access or seek owner permission. Do not make proxy rotation, identity spoofing or automated CAPTCHA circumvention a prerequisite for the product. An owner-provided content inventory gives a stronger denominator than open-ended crawling. Neither robots allowance nor technical visibility establishes authorization to access private information.

Signed URLs should retain exact request bytes for acquisition; normalize only for separately defined identity/dedup purposes. Reacquire expired asset URLs through the permitted source page/session. Redact signed tokens from reports. Do not strip query parameters indiscriminately: they may select language, page number, product variant or signatures. Maintain original, fetch, normalized identity, declared canonical and content-hash identities independently. A canonical hint must not prevent capturing an explicitly requested URL or prove identical content. Handle fragments used by client-side routers as state identifiers, even though ordinary fragment anchors do not change the HTTP resource.

Bound calendars, search results, facets, pagination, session IDs, repeated path segments, link-generated query permutations, external assets and recursively linked documents. Record budgets and excluded edges in the manifest. Asset scope can differ from crawl navigation scope: a CDN image may be in scope even when crawling the CDN as a website is not. Never label a budget-terminated frontier exhausted.

Isolate browser and parser workers with no host credentials or internal-network access. Enforce egress controls for page subrequests as well as initial requests and redirect destinations; defend against DNS rebinding and private/link-local/loopback addresses. Permit intended schemes only. Bound decompression, file size, pixels, pages, nesting, runtime and memory. Treat HTML/scripts/document macros and extracted instructions as untrusted data. Do not execute a scraped instruction or submit a form because a page tells the system to. Keep navigation read-only by policy: arbitrary buttons can buy, delete, log out, or change account settings.

Separate tenant storage and browser sessions; encrypt sensitive evidence, redact auth headers/query tokens in user-facing logs, apply retention limits, and gate model/OCR uploads by the data policy. Keep raw evidence access-controlled while retaining sanitized review artifacts and hashes.

### Gaps
The target sites' access policies and owner permissions are unknown. Block detection beyond documented vendor indicators is heuristic and should carry evidence plus a confidence level. No guarantee can be made that an allowlisted crawler sees the same variants as every customer.

## What architecture and acceptance tests should precede semantic analysis?

### Takeaway
Build the capture manifest, evidence retention and gap verifier before sophisticated website analysis. Ship a bounded audited extractor for representative formats first, then expand capability using measured failures.

### Cited Findings
- Playwright's network/page instrumentation and browser contexts provide mechanisms for observing events and isolating session state, not a completeness guarantee. [Page API](https://playwright.dev/docs/api/class-page), [authentication](https://playwright.dev/docs/auth)
- Late hydration and post-load content explain why timed waits alone cannot establish reliable captures. [Navigation behavior](https://playwright.dev/docs/navigations)

### Inferences
Proposed architecture: scope and access policy -> URL/resource frontier -> fetch and MIME classifier -> immutable raw evidence store -> browser/format-specific extraction workers -> resource/state/region manifest -> reconciler and gap detection -> targeted remediation queue -> human review -> versioned content graph and semantic-analysis export. A relational manifest plus object storage is sufficient initially; do not require a vector database to solve extraction fidelity. Event-driven updates support live progress, retries and resumable jobs. Keep raw content, normalized text, layout/structure and semantic annotations as separate versioned layers.

Each content unit should carry source URL, parent resource, capture timestamp, locale/role/state, heading ancestry, region/page/frame/bounding box, method/version, confidence, and evidence hash. Preserve main text and boilerplate with labels rather than destructively dropping navigation/footer material. Link identical content units without deleting source provenance. Preserve original punctuation, negation, numbers, units, table relations and footnote attachment. Future contradictions require matching topic, entity, jurisdiction, effective date and audience; old versus new versions or different product variants are not automatically contradictions.

**Phased decision:**
1. Define scope and annotated golden corpus; prototype only the manifest and acceptance criteria. Obtain representative own/client and permitted third-party examples before selecting vendors.
2. Implement raw HTML + rendered browser evidence + native PDF/image handling + conditional OCR, and explicit unsupported/blocked reporting. Every seed receives an outcome; every extracted span retains provenance.
3. Add bounded tabs/accordions/scrolling/frames, resource recursion, structured tables, target languages and targeted remediation. Compare against a manually annotated benchmark and examine false COMPLETE cases.
4. Add difficult formats and site-specific adapters where measured coverage benefit justifies cost. Optimize cost only after fidelity baselines.
5. Enable themes, duplicates, semantic similarity and contradiction candidates over the versioned content graph, with gap-aware filtering and source quotations.

**Golden fixtures and acceptance tests (proposed, not tests executed):**
- Known static page with expected headings, paragraphs, footer, alt text and table relationships; all expected units survive and link to evidence.
- Hydration delayed beyond normal load; content inserted after interaction; ignored click before hydration; verify eventual outcome or an honest unresolved status.
- Nested scroll container and virtualized list which recycles DOM nodes; earlier captured items must not disappear when later nodes replace them.
- One content-bearing image blocked while HTML succeeds; final result must be PARTIAL and image must stay in denominator.
- Tab and accordion content absent until opened; unvisited controls block VERIFIED_WITHIN_SCOPE if in contract.
- OCR fixture with low-resolution numerals, minus signs, negation, multilingual and rotated text; fail fidelity thresholds or queue review instead of guessing.
- Mixed PDF with digital page, scanned page, table, missing font mapping, image-only footnote and corrupted page; preserve per-page outcomes and catch partial parser success.
- HTTP 200 login/challenge/soft-error page; never report the target document as extracted.
- 429 with both Retry-After formats; capped retry and no busy-loop. Persistent 403/auth challenge becomes BLOCKED.
- Expired signed resource, redirect loop, truncated binary, unsupported format; exact cause recorded, no fabricated empty-success content.
- Infinite calendar/facet frontier and action loop; bounded exit with BUDGET_EXHAUSTED and remaining frontier.
- Disagreement between source, rendered text and OCR; preserve alternate readings and region-level review.
- Duplicate or conflicting canonical hints; preserve requested pages and distinct content evidence.
- SSRF initial URL, redirect, iframe and asset cases; egress denial produces evidence without touching prohibited destinations.
- Resume after crash; idempotent work/evidence linkage, no lost failures or inflated duplicate counts.

Primary quality gate: zero false VERIFIED_WITHIN_SCOPE results in the known-missing adversarial suite. This is a release criterion for that suite, not a universal guarantee. Report per-format content-unit recall/precision, layout/table fidelity and OCR error rates on a held-out annotated set; set exact thresholds after baseline measurement. Track false-complete rate, unresolved gaps, cost and latency by extraction route. Test stop/status behavior as rigorously as extraction output. Human review can strengthen observed coverage but cannot prove absence of undiscovered states.

### Gaps
No implementation, benchmark, production sample or timing/cost experiment was performed. Format choice, deployment environment, budgets, language mix and owner/API access will determine the final stack. The most important missing decision is whether the first product extracts supplied URLs and their embedded assets only, or also recursively discovers an entire website.
