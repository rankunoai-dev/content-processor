# Consolidated extraction investigation pathway

## What decision should the investigation produce?

### Takeaway

Finish a comparative, evidence-backed investigation before choosing the architecture. Its deliverable is a tested capability boundary, verifier acceptance criteria, failure taxonomy and trade-off evidence—not a promise to extract everything or a shopping list of libraries. The steps below are proposed research work; neither report supplies benchmark results.

### Cited Findings

- The earlier investigation already distinguishes discovery, acquisition, region recovery, state exploration, accounting and fidelity; includes 83 test cases; and proposes provenance and explicit uncertainty. [Earlier investigation](<C:/Users/RankUno/Desktop/content-processor/reports/Content extraction completeness investigation.md>)
- The new investigation makes transport negotiation/compression, handwriting recognition, anti-bot tool candidates, weighted scoring and a candidate stack more explicit. It also proposes a 98% completeness threshold and says reruns always produce at least previous coverage. These are proposals, not demonstrated properties. [Additional investigation](<C:/Users/RankUno/.codex/attachments/ff9e50a3-07c8-4d53-bd22-45eff6a5b784/Pasted text.txt>)

### Inferences

- Preserve the previous report's evidence model, fold in genuinely additional cases, and turn every claimed fallback into a testable hypothesis. Do not simply merge row counts: several entries overlap.
- “Verified within scope” should require all mandatory conditions to pass. Weighted progress can help prioritize work, but must not let successful paragraphs compensate for an unread image disclaimer.
- Supplied URLs plus required embedded assets is a useful proposed baseline. It has not been confirmed as the permanent scope; whole-site discovery and linked downloads need distinct research tracks.
- Research should separate content topic clustering from visual theme/design extraction. The user's “theming” is ambiguous; collect implications for both without committing the first extractor to design reconstruction.

### Gaps

- Neither report contains measured recall, verifier error rates, throughput, costs or public-site access success. The number of listed cases does not establish breadth of tested support.

## What ordered research workstreams should follow?

### Takeaway

Run workstreams 1–3 first to define what success means and construct reliable evidence. Then evaluate capture and fidelity in workstreams 4–7; conclude with operational feasibility and the architecture decision packet. Each workstream must record unsupported and unknown outcomes alongside successes.

### Inferences

All workstreams and exit criteria below are proposed investigation decisions, not externally measured facts.

| Order | Research questions | Experiments | Evidence output | Exit criterion |
|---|---|---|---|---|
| 1. Scope and meaning | What counts as page content, linked content and site content? Which visitor states, interaction types and file internals are required? What does “real time” mean: first result, gap notification or final verification? | Apply alternative scope contracts to representative supplied-URL, downloadable-document and whole-site tasks. Compare anonymous visitor contexts and content-versus-visual theming requirements. | Scope alternatives; content-layer definitions; format/depth policy; explicit exclusions; provisional workload profiles. | Every proposed success claim names its boundary and capture context; unresolved user choices remain clearly marked assumptions. |
| 2. Consolidated failure inventory | What does the new report add, repeat or oversimplify? How can each failure be observed independently of successful extraction? | Map both catalogs into unique failures, detection hypotheses, fallback hypotheses and residual outcomes. Include mixed native/OCR regions, virtualization, frames, transport variants, hostile inputs and changing content. | Deduplicated traceability matrix with original-report references, severity, test ID and evidence requirement. | Every supported capability and material failure mode has a test or an explicit research gap; no row claims a fallback is guaranteed. |
| 3. Ground truth and verifier evaluation | Can the verifier detect omissions it did not discover itself? How much do fixtures resemble actual public websites? How often does it falsely certify or raise an unnecessary alarm? | Build controlled fixtures with exact expected resources/states/regions and independently annotate held-out public samples. Inject failures at acquisition, discovery, OCR, normalization and export stages. Include no-text images, genuinely blank PDFs and decorative canvas as negative controls. Separate development, calibration and final evaluation sets. | Annotation rules; disagreement log; corpus composition; hidden-test split; failure injection catalog; verifier confusion matrices with denominators and uncertainty. | Known mandatory omissions never yield VERIFIED in the adversarial suite; false-alarm and miss rates are reported by case/language/format, and provisional thresholds are justified by observed error costs. This bounded gate is not a universal accuracy guarantee. |
| 4. Acquisition and public-site feasibility | Which ordinary access paths work on the target population? Are failures transport, policy, session, challenge or content failures? What changes with negotiation and cache state? | Benchmark ordinary HTTP and rendered access on a stratified public-site sample. Test redirects, Content-Type mismatch, Accept/Accept-Language/Vary behavior, compression, interrupted streams, 304 with and without a usable cached representation, and 206/range assembly with missing/overlapping ranges. Distinguish full responses from resumable partial artifacts. Test bounded retries, Retry-After and permitted source exports/APIs. | Per-origin access outcomes; request/response context; response-integrity checks; block taxonomy; repeatability and acquisition cost. | Every requested resource has an honest outcome; challenge/login/partial responses cannot become content success; feasible permitted acquisition options and persistent blockers are quantified. |
| 5. HTML, rendered state and asset discovery | Which content is absent from raw HTML, final DOM, screenshots or network captures? Does changing interaction order alter the result? | Exercise hydration, delayed requests, nested scrollers, two-axis virtualization, tabs, carousels, hover information, frames including srcdoc/blob contexts, open/closed shadow roots, SVG paths, canvas, CSS text and responsive images. Compare cumulative captures against final-only capture. Test browser-delivered data separately from replayed calls; replay only suitable bounded read operations. | State/region discovery recall; missed-state examples; stop reasons; raw/rendered/source-only disagreements; independent visual comparison. | Mandatory known states and regions are accounted for; budget limits and closed/unknown state spaces remain visible; client-source strings are not silently asserted as published claims. |
| 6. Documents, images and media | Which format features can each candidate actually preserve? Does an apparently healthy text layer hide omitted or inaccurate content? | Compare native extraction, rendering and OCR on mixed PDFs, bad OCR, tables, columns, forms, attachments, annotations, optional layers, handwriting, multilingual/rotated text and small qualifiers. For any Office/archive/audio/video formats proposed in scope, test individual features and tracks rather than file extensions alone. Include speech-free audio and unsampled video overlays. | Per-feature capability matrix; OCR error distributions; structure/reading-order errors; unsupported internals; visual/transcription uncertainty. | Support is claimed only for evaluated features and languages; missing regions and inaccessible document parts remain gaps; image descriptions and chart interpretations are labeled inference. |
| 7. Fidelity, time and recapture | Did recovered content retain meaning? Were multiple states captured from a coherent version? Can a retry mix a current page with an old PDF or silently carry forward removed content? | Use exact critical tokens and claims: negations, numbers, units, price conditions, table headers and footnotes. Change page/data/assets mid-run; rerun after genuine additions, removals, locale changes and OCR corrections. Test normalization, deduplication and merging against these cases. | Claim-level fidelity examples; version/snapshot relationships; freshness and temporal ambiguity flags; reversible normalization examples; recapture reconciliation rules. | Changed content is versioned rather than blended; removed text is not silently revived; critical qualifiers trace to exact evidence; retry idempotency is scoped to immutable input and configuration, not “same URL forever.” |
| 8. Operational feasibility and research handoff | What quality/latency/cost frontier is feasible? Which constraints determine architecture? Which outputs safely support later analysis? | Measure acquisition, rendering, OCR, verification and review separately under small/medium/large proposed workloads. Exercise timeout, crash, resume, duplicate delivery, safety limits and retention/deletion. Review candidate licenses, hosting/API terms, maintenance activity, supported platforms and deployment needs as selection inputs. Run small downstream probes for topic clusters, similarity and contradiction candidates using qualified source claims. | Quality/latency/cost curves; review burden; incident recovery results; retention options; dependency decision table; architecture requirements and unresolved choices. | Produce an architecture-ready decision packet with evidence-ranked options, resource budgets and accepted unknowns. Do not select queues, databases or model vendors merely because either report named them. |

### Cited Findings

- Workstreams 1–3 and 5–8 extend the previous report's scope contract, test-corpus, critical-token, version/context, evidence and operational proposals. [Earlier investigation](<C:/Users/RankUno/Desktop/content-processor/reports/Content extraction completeness investigation.md>)
- Workstreams 4 and 6 incorporate explicit candidates from the additional report, including content negotiation, compression, handwriting and protocol behavior. Its score, stack and rerun claims require evaluation rather than adoption. [Additional investigation](<C:/Users/RankUno/.codex/attachments/ff9e50a3-07c8-4d53-bd22-45eff6a5b784/Pasted text.txt>)

### Gaps

- Detector calibration and ground-truth bias need more emphasis than either report supplies. A fixture suite designed by the extractor author can reward the same assumptions that cause live omissions. Independent annotation and held-out difficult public samples reduce this risk; they do not prove universal recall.
- Temporal coherence and incremental recrawling need explicit experiments. A collection of individually successful captures can still mix different source versions.
- Header negotiation, 304 cache validation and 206 partial responses need a dedicated acquisition-integrity treatment. “Received HTTP success” is too coarse for completeness.
- Public-third-party access feasibility should be measured before a broad product commitment. Anti-bot tool names do not constitute access evidence or guaranteed support. Source-use permission, licensing and maintenance belong in feasibility review; this pathway makes no legal determination.

## What remains unknown, and what should be handed to architecture planning?

### Takeaway

Proceed with the comparative investigation now using declared assumptions. Collect the unresolved requirements in one decision register; ask the user to choose only where the choice materially changes the later architecture or accepted product scope.

### Inferences

The final investigation packet should contain:

1. A concise comparison identifying additions, overlaps, corrections and shared omissions in the two reports.
2. A deduplicated, prioritized test matrix with feature-level evidence and residual outcomes.
3. A scope contract and a completeness decision policy, including mandatory gates, unknown discovery and severity of missing claims.
4. A benchmark design/results boundary: clearly distinguish planned experiments from experiments actually executed.
5. A feasibility table showing quality, false verification, false alarms, latency, cost, review load and support limits by source type.
6. A research decision log: accepted assumptions, evidence-ranked alternatives, unresolved choices and exactly which architecture decisions they affect.

Recommended unresolved requirement register:

| Unknown | Provisional research handling | Later architecture consequence |
|---|---|---|
| URL count, site count and recrawl frequency | Evaluate a range of workloads; avoid invented production throughput claims. | Scheduling, concurrency, incremental reuse and storage growth. |
| Languages/scripts and handwriting frequency | Use a declared initial language sample; record unevaluated scripts. | OCR/ASR/model choices, language routing and review effort. |
| Cost ceiling and acceptable manual review | Report cost and review as distributions and per-source components. | Escalation policy and feasible verification depth. |
| Real-time SLA | Separate first content, first gap event and terminal result; benchmark each. | Execution model, streaming status and time budgets. |
| Retention and source-use terms | Separate original evidence, derived content and logs; collect requirements without asserting a universal legal policy. | Storage, deletion, access control and permissible reuse. |
| Access boundaries | Public third-party sites are confirmed; login, paid sources and bypass capabilities are not automatically part of the baseline. Evaluate access routes explicitly. | Source adapters and blocked-result handling. |
| Whole-site versus supplied URLs; linked download depth | Compare both discovery models; keep the initial URL-plus-assets proposal visibly provisional. | Frontier management, identity, traversal and scope controls. |
| Content themes versus visual themes | Document the additional evidence each would require. | Topic analysis versus CSS/layout/design evidence capture. |
| Risk tolerance for missed critical claims | Separate a missed decorative label from a lost exclusion, numerical value or negation. | Mandatory verification gates and downstream eligibility. |

Architecture planning begins after this packet defines what must be captured, how it will be evaluated, what cannot be promised, and the feasible operating envelope. It may use explicit provisional values for unresolved business choices; it must not hide them inside technical defaults.

### Cited Findings

- The original investigation explicitly states that no implementation or accuracy benchmark was performed and recommends deferring accuracy, throughput and language promises until evaluation. [Earlier investigation](<C:/Users/RankUno/Desktop/content-processor/reports/Content extraction completeness investigation.md>)

### Gaps

- No production URL sample, volume, language mix, budget, latency requirement or retention rule has been provided in the material reviewed. These are later decision inputs, not reasons to stop the requested report comparison.
