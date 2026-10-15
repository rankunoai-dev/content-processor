# Comparison of the two investigations

## What does the new investigation add?

### Takeaway
The pasted investigation is a useful implementation-oriented checklist, but most of its 70 numbered cases overlap the prior 83-case inventory. Its main additions are explicit transport diagnostics and named OCR subcases; its proposed completeness formula is a regression from the prior evidence-based definition. Numbered entries measure editorial granularity, not breadth or completeness.

### Cited Findings
Source key: [NEW](</C:/Users/RankUno/.codex/attachments/ff9e50a3-07c8-4d53-bd22-45eff6a5b784/Pasted text.txt>); [PRIOR](</C:/Users/RankUno/Desktop/content-processor/reports/Content extraction completeness investigation.md>). References below are document-comparison findings, not claims that proposed techniques have been experimentally validated.

| New reference | Prior reference | Classification | Decision and priority |
|---|---|---|---|
| #31 watermarks; #32 handwriting | #47 image degradation, #55 raster OCR | Newly explicit subcases | Adopt into fixtures; high for target corpora containing handwriting or overprinted notices. Handwriting has no explicit prior acceptance case. |
| #59 Accept/content negotiation | #34 locale/context; response bytes discussion | Genuine explicit addition | Adopt; high. Define finite request-header profiles, preserve returned variant and headers, do not request every possible representation. |
| #60 gzip/Brotli/zstd; #61 chunked transfer | #74 partial download/MIME | Newly explicit transport subcases | Adopt as acquisition tests; high. Test truncated decoded bodies, unsupported encoding and decompression limits alongside original evidence. |
| #48 TLS and #56 HTTP/2 fingerprints | #66 transport failures; #68/#71 blocks | Explicit diagnostic detail | Refine; medium. Useful block-cause hypotheses, not established diagnoses or guaranteed remedies. An HTTP/2 client alone is not evidence of fingerprint equivalence. |
| #53 honeypot links | #28 hidden content; #76 scope explosion; #82 safe actions | Newly explicit discovery concern | Refine; high. Hidden links cannot all be assumed traps: retain evidence and apply navigation policy rather than treating CSS visibility as a definitive classifier. |
| #18 metadata; #10 all ARIA attributes; #11 alt/title | Prior layer separation; #30 control labels; browser semantic capture | More explicit extraction inventory | Adopt named metadata/accessible-description checks; medium. Attributes and relationships need interpretation, not indiscriminate concatenation. |
| #52 referrer checks | #73 signed/contextual resources; #77 cross-host assets | Newly explicit request-context detail | Refine; medium. Investigate ordinary source-context requests and preserve failure reason; header spoofing is not a blanket recovery policy. |
| #64 RDFa; #66 RSS/Atom | #37 microdata/JSON; #61 XML | Named extensions of existing structured scope | Adopt explicit fixtures when supported; medium. Feed text is a separate representation, not proof of webpage completeness. |
| #1–9 shadow/frame/template/canvas/SVG/CSS; #12–17 hidden states and JSON-LD | #11–29, #37–38 | Mostly overlap, often less qualified | Retain prior depth: closed roots, srcdoc, composed slots, SVG paths, CSS images, and independent frame outcomes. |
| #20–27 dynamic browser content | #03–10, #14–16, #40 | Overlap | Retain prior accumulation, nested/two-axis scrolling, time windows and explicit unfinished discovery. |
| #29 and #36–43 PDF | #41–54 | Overlap with significant prior omissions in NEW | Retain prior region-level mixed-page and stale-OCR verification. |
| #67–70 Office/media/code | #32, #58–65 | Overlap | Retain prior format internals, media intervals and transcription uncertainty. |
| Sections 4–5, 7 stack/build roadmap | Prior architecture and stages | Premature technology decisions | Defer until consolidated investigation acceptance gates. |

All comparisons in this table come from [NEW](</C:/Users/RankUno/.codex/attachments/ff9e50a3-07c8-4d53-bd22-45eff6a5b784/Pasted text.txt>) and [PRIOR](</C:/Users/RankUno/Desktop/content-processor/reports/Content extraction completeness investigation.md>).

### Inferences
- Do not simply union 70 and 83 rows into a claimed 153-case catalogue. Normalize by failure class, separate newly explicit subcases, and link each to an expected detection and result.
- Code-level strings, PDF invisible text, hidden HTML and accessibility descriptions need separate publication/evidence labels before future contradiction comparison.

### Gaps
- Neither document provides measured extractor performance, a tested tool-version matrix or an externally annotated corpus. Tool names are candidates, not validated capabilities.

## What is missing or needs correction in the new investigation?

### Takeaway
The critical issue is false assurance: the new document counts processing activity as completeness and permits a complete result despite missing content. Preserve its useful cases but replace the scoring, success criteria and blanket fallback assumptions.

### Cited Findings
| Issue | Evidence and comparison | Decision |
|---|---|---|
| Weighted score and ≥98% complete | NEW §3/§6 uses text-node ratios, page counts, OCR attempts, scroll depth and replay counts. PRIOR completeness table separates discovery, acquisition, fidelity and accounting. | Reject complete threshold; critical. Unknown denominator and missing critical qualifier cannot be averaged away. |
| Example has known gaps at 98.4 | NEW §8 includes two images not OCR'd and unprocessed canvas. PRIOR status table requires PARTIAL for known missing required content. | Replace with explicit inventory/status vector; critical. |
| Idempotency means coverage always rises | NEW §9 states repeat URL extraction always yields ≥ prior coverage. PRIOR discusses changing state and growing denominator. | Reject definition; high. Define reproducible processing of fixed inputs separately from fresh captures and cumulative evidence. |
| Timer readiness | NEW #24 recommends network idle plus extra delay. PRIOR browser paragraph requires content/readiness evidence and bounded windows. | Refine; high. Timeout means a stopped attempt, not complete. |
| iframe src re-fetch | NEW #2–3 reduces frames to src URLs; PRIOR #21 includes frame contexts, nesting and srcdoc. | Refine; high. Investigate context-preserving frame capture and independent resource fetch as distinct methods. |
| Shadow/custom components | NEW #1/#8 implies recursive traversal/expansion is sufficient; PRIOR #19–20 separates open/closed and slots. | Refine; high. Name inaccessible states and rendering evidence. |
| API replay as verification | NEW #22 and §3 count intercepted vs replayed calls. PRIOR classifies content-bearing responses and safe actions. | Reject as coverage metric; high. A captured response may be sufficient; replay needs scope, method and side-effect review. |
| Service-worker bypass | NEW #26 prescribes bypass; PRIOR #40 records context differences. | Refine; high. Compare relevant contexts when justified, retain actual served version. |
| All hidden text / all attributes | NEW #10/#14/#35 gather indiscriminately; PRIOR preserves visibility and semantic relationships. | Refine; high. Avoid false published claims and duplicate candidates. |
| CAPTCHA/proxy/stealth defaults | NEW #30/#44–56 and phase 1 treat evasion as normal dependencies; PRIOR access section retains blocked outcomes and permitted recovery. | Reject as prerequisite; high. Research diagnosable ordinary access, retries, APIs/feeds and authorized paths first. |
| Password patterns | NEW #37 suggests known patterns; PRIOR #54 retains encrypted outcome. | Refine; high. Use explicitly authorized credentials; inaccessible content remains blocked. |
| CORS blanket instruction | NEW #63 says always server-side; PRIOR has multiple browser/source channels. | Refine; medium. Cross-origin response access, resource authentication, embedding context and browser rendering are separate issues. |

Evidence: [NEW numbered rows and sections](</C:/Users/RankUno/.codex/attachments/ff9e50a3-07c8-4d53-bd22-45eff6a5b784/Pasted text.txt>), [PRIOR corresponding cases and decision rules](</C:/Users/RankUno/Desktop/content-processor/reports/Content extraction completeness investigation.md>).

### Inferences
Important omissions from NEW already addressed by PRIOR: explicit scope/locale/device/time/consent variants; nested and horizontal virtual scrolling; carousels/product selectors/forms; CSS background text and outlined SVG; failed image accounting; mixed native/scanned PDF regions; stale OCR; PDF attachments/layers/XFA; document package internals; image animation frames; transcript hallucination/temporal sampling; HTTP-200 challenge/soft-404 detection; URL identity versus canonical hints; crash recovery; hostile files/SSRF; provenance, immutable evidence and error-preserving statuses; independent ground truth and false-verified tests; exact preservation of units/negation/footnotes; context-aware downstream contradiction requirements.

### Gaps
- A browser-level fidelity experiment is still required for each proposed shortcut. This comparison does not independently validate their current library support.

## What further investigation is needed before architecture planning?

### Takeaway
Both documents need a bounded investigation pathway with research questions, test evidence and explicit architecture-entry gates. The following are proposed research additions, not claims that all listed failures occur on every website.

### Cited Findings
- PRIOR already calls for annotated adversarial fixtures and zero false verified results on known-missing cases, but neither document supplies those results. NEW moves directly from checklist to stack and phases. [PRIOR](</C:/Users/RankUno/Desktop/content-processor/reports/Content extraction completeness investigation.md>); [NEW](</C:/Users/RankUno/.codex/attachments/ff9e50a3-07c8-4d53-bd22-45eff6a5b784/Pasted text.txt>).

### Inferences
1. Freeze the extraction contract: supplied URLs versus website discovery; embedded versus linked assets; finite variants; supported content layers; dangerous interactions and access boundaries. Produce scope scenarios and exclusion semantics.
2. Normalize both case inventories. Add explicit tests for base/relative URL resolution, fragment routes, data/blob-backed downloads, download-only event handlers, content-disposition/MIME mismatch, conditional requests/stale cache, partial-range downloads and compression interruption. These are investigation candidates currently absent or implicit, not established coverage claims.
3. Investigate capture consistency: a page and its PDF/image/API assets may change during collection. Define which versions were captured, whether snapshot consistency is attainable, and how uncertainty propagates. Do not conflate an accumulated multi-time corpus with one coherent page state.
4. Investigate website frontier recall separately: sitemap indexes, internal links, orphan URLs, language alternates, query variants, canonical hints, recursion and unbounded facets. An input list can support complete processing of that list without proving whole-site discovery.
5. Build independent truth and omission tests before tools are chosen: deliberately remove a disclaimer/image/tab/table header, inject a blocked payload, garble a numeral, and assert the verifier refuses VERIFIED. Include meaningful no-text images and truly blank pages to test false positives.
6. Test metadata and relationship preservation: headings/footnotes/table headers, superscript references, numbers/units, hyperlinks/citations, entity/version/date context. Confirm lossless raw evidence and reversible normalization.
7. Test source-only versus actually published versus generated interpretation classification. Investigate whether an image/chart description can be supported as extracted fact or must remain a separately labeled inference.
8. Research operating envelope: URL/day, page sizes, languages, OCR/media mix, capture latency, retry and review budgets, data retention, tenant isolation and raw-evidence access. Numeric targets require user priorities and measured baselines.
9. Produce architecture-entry requirements: supported capabilities with fixtures; unresolved limitations; quality/status rules; access policy; throughput/cost baseline; information/provenance contract. Only then compare stack choices and design the overall architecture.

### Gaps
- Website samples, scale targets, language mix, acceptable review rate and retention needs remain unspecified. A provisional pathway can proceed, but architecture sizing cannot be justified yet.
- “Every possible instance” cannot be a closed catalogue; require an extensible failure taxonomy and regression process for newly discovered omissions.
