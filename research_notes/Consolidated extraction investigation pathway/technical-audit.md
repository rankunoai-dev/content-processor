# Technical audit of the supplied investigation

## Which proposed extraction fixes need correction?

### Takeaway
The supplied catalog identifies useful failure surfaces, but several suggested remedies are incomplete or change the evidence being measured. Treat each as a research hypothesis requiring a fixture and a failure detector, rather than a solved capability.

### Cited Findings
- **Iframe src recursion is insufficient (items 2–3).** Browser automation exposes frame objects for contextual access; frames need not be independently retrievable HTTP documents. Inline srcdoc supplies the document itself, and blob resources have creator/storage-context restrictions. [Playwright frames](https://playwright.dev/docs/frames), [srcdoc](https://developer.mozilla.org/en-US/docs/Web/API/HTMLIFrameElement/srcdoc), [blob URLs](https://developer.mozilla.org/en-US/docs/Web/URI/Reference/Schemes/blob)
- **Recursive shadowRoot traversal does not cover closed roots (item 1).** Playwright explicitly excludes closed-mode roots from locator support. [Playwright shadow DOM](https://playwright.dev/docs/locators#locate-in-shadow-dom)
- **Network idle plus delay is not a completion criterion (item 24).** Playwright discourages networkidle and explains that modern pages continue activity after load; there is no universal loaded instant. [Page API](https://playwright.dev/docs/api/class-page#page-wait-for-load-state), [Navigations](https://playwright.dev/docs/navigations)
- **Disabling service workers can change the page (item 26).** Official documentation explicitly warns that application behavior may differ; workers can supply cached/offline responses. [Playwright service workers](https://playwright.dev/docs/service-workers)
- **Server-side fetching addresses browser CORS enforcement, not authentication or all access failures (item 63).** CORS governs browser-origin access; credentials and cookie policies are separate concerns. [MDN CORS](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CORS)
- **HTTP/2 support is not a browser-fingerprint guarantee (item 56).** HTTPX describes protocol support and negotiation, and notes that enabling it does not even guarantee HTTP/2 will be negotiated. It makes no fingerprint-equivalence claim. [HTTPX HTTP/2](https://www.python-httpx.org/http2/)
- **Interception does not justify replaying all API calls (item 22 and score signals).** HTTP defines safe and idempotent methods differently; PUT and DELETE are idempotent despite changing server state. Non-idempotent requests must not be blindly retried. [RFC 9110, sections 9.2.1–9.2.2](https://httpwg.org/specs/rfc9110.html#idempotent.methods)
- **Text-bearing PDF page counts are insufficient (PDF coverage signal).** OCRmyPDF distinguishes skipping text-bearing pages from detecting image text in mixed pages; OCR processing can time out or skip large images. [OCRmyPDF advanced features](https://ocrmypdf.readthedocs.io/en/latest/advanced.html)
- **Running OCR is not fidelity validation (image/canvas coverage signals).** Tesseract documents quality dependencies including resolution, skew, noise and page segmentation, and difficulty recognizing tables without custom layout analysis. [Tesseract quality guide](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html)

### Inferences
- Preserve original frame context first; classify separately retrieved frame URLs as additional observations. Test srcdoc, blob, nested frames, session-dependent embeds, and frames created after interaction.
- Distinguish discovered/open/traversed shadow roots from inaccessible or unknown components. A count of discovered roots cannot establish the count of all roots.
- Investigate bounded stability windows plus state/resource inventories, and report time-budget exhaustion explicitly. Stability is evidence, not universal proof.
- Keep service-worker-enabled observation as the baseline; any disabled-worker diagnostic run needs its own provenance and a comparison against baseline.
- Separate passive response capture from replay. Classify content-bearing read operations before any controlled replay; exclude mutations and unknown actions. Never use requests-replayed/requests-intercepted as completeness.
- Measure resource processing, text recognition and structural fidelity separately. A screenshot OCR pass cannot establish full chart interpretation or invisible content coverage.

### Gaps
- No test cases, tool versions or empirical results in the supplied document substantiate its remedies or implied capability across arbitrary public websites.
- Whether closed-root visual evidence is sufficient depends on the content and supported browser; no universal complete extraction method is established here.

## Is the proposed completeness and idempotency model defensible?

### Takeaway
The 98% completion threshold is an unsupported project proposal and should not be adopted. The idempotency statement confuses repeatability with monotonically improving coverage.

### Cited Findings
- The supplied investigation sets complete at a weighted score of at least 98%, while its sample reports 98.4 alongside two images and one canvas left unprocessed. This is an internal conflict between the proposed complete label and admitted omissions. [Supplied investigation](C:/Users/RankUno/.codex/attachments/ff9e50a3-07c8-4d53-bd22-45eff6a5b784/Pasted%20text.txt)
- The supplied definition says running extraction again on the same URL always yields greater or equal coverage. HTTP idempotency instead concerns intended server effects of repeated identical requests; it is not a guarantee about returned content or extraction yield. [RFC 9110](https://httpwg.org/specs/rfc9110.html#idempotent.methods)
- Browser pages can load additional data and change after initial load, which makes URL-only comparisons under-specified. [Playwright navigation lifecycle](https://playwright.dev/docs/navigations)

### Inferences
- Arithmetic counterexample: with ten equally weighted signals, nine perfect signals and one at 0.8 yield 98%; the incomplete signal still has a known omission. More weak proxies cannot cancel a critical missing region.
- DOM nodes-with-text divided by all nodes measures markup composition, not extracted-content recall. Browser and extracted word counts can match despite substitutions or missing equal-length passages.
- Track at least discovery status, acquisition coverage, extraction coverage, fidelity assessment, and unresolved gaps. Keep counts tied to a known observed denominator, never implied total website content.
- Recommended labels for investigation: verified within declared scope, partial, blocked, unsupported, review required; final vocabulary should be tested against actual fixtures.
- Same captured bytes plus fixed tool versions/settings can support repeatability testing. New live captures need separate timestamps and versions; new content, expired resources, or network failures can lower coverage. Even an accumulating evidence archive can discover new gaps and reduce its coverage fraction.
- A quality score may support prioritization after calibration; it must not override unresolved critical gaps or masquerade as probability of completeness.

### Gaps
- No ground-truth corpus, sensitivity analysis, calibrated weights, false-complete rate, OCR character error rate, table fidelity metric or human review protocol supports the 98% number.
- No proof of a closed discovery frontier is supplied for infinite lists, continuous streams or arbitrary interaction combinations. These require bounded scope and explicit stop reasons.

## What must be investigated before architecture selection?

### Takeaway
Convert the corrected catalog into an evidence matrix and controlled experiments. Defer stack and infrastructure choices until the resulting failure rates, evidence requirements and resource costs are measured.

### Cited Findings
- Official browser documentation exposes materially different frame, shadow-root, worker and navigation behavior; each needs a dedicated observation strategy rather than one HTML snapshot. [Frames](https://playwright.dev/docs/frames), [Shadow DOM](https://playwright.dev/docs/locators#locate-in-shadow-dom), [Workers](https://playwright.dev/docs/service-workers), [Navigation](https://playwright.dev/docs/navigations)
- OCR tools document limitations that justify testing mixed-content regions and layout fidelity instead of counting processed files. [OCRmyPDF](https://ocrmypdf.readthedocs.io/en/latest/advanced.html), [Tesseract](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html)

### Inferences
1. Define the observation scope: public content, client-delivered source/data, browser session, viewport, language, time window, permitted interactions, linked-resource depth, and supported formats.
2. Build ground-truth fixtures for every high-risk failure: mixed PDF regions, closed roots, context-bound frames, virtualized nested scrolling, delayed content, worker-backed content, mutated live state, and known access blocks.
3. For each case record expected content, discovery method, evidence artifact, extraction method candidates, detection of omissions, semantic fidelity checks, and stop condition.
4. Run failure injection: drop one PDF region, image, frame or late response and require the result to remain partial. Test critical missing words such as negations, units and footnote qualifications.
5. Evaluate access outcomes separately from extraction: transient failure, rate limit, login requirement, challenge, restricted resource, and genuine empty content must not collapse into success/empty text.
6. Retain passive observations and authorized retrieval as the public-site baseline. Escalation routes are documented public APIs, licensed exports, or owner-provided access; evasion tooling is not evidence of guaranteed coverage.
7. Establish architecture entry criteria: measured capability matrix, false-complete test results, provenance needs, bounded runtime/cost observations, and unresolved cases. No stack decision is made in this audit.

### Gaps
- Public target sample, expected scale, runtime budgets, media limits and acceptance tolerances remain unspecified. They should be measured or decided in the investigation rather than silently encoded into an architecture.
