# PDF, image, document and media extraction completeness

## What can be missed, and how should it be detected?

### Takeaway
A successful download or a nonempty text result does not establish complete extraction. Inventory every page, object, attachment and media stream, then reconcile native extraction with rendered evidence; OCR and visual interpretation require uncertainty labels.

### Cited Findings
- PDF text can be stored in an order different from visual reading order, drawn as vectors rather than fonts, or encoded with unusable character mappings. Native extraction alone can therefore be empty, garbled or misordered. [PyMuPDF](https://pymupdf.readthedocs.io/en/latest/recipes-common-issues-and-their-solutions.html)
- pypdf explicitly does not OCR images or detect errors in a pre-existing OCR layer. Tables, ligatures, formulas, footnotes and floating figures complicate reconstruction. Its broad statement that PDFs lack semantics should not be generalized to tagged PDFs: OCRmyPDF explicitly documents structure trees in tagged PDFs. [pypdf](https://pypdf.readthedocs.io/en/stable/user/extract-text.html), [tagged PDF handling](https://ocrmypdf.readthedocs.io/en/latest/advanced.html)
- OCRmyPDF skip mode skips an entire page containing existing text; redo mode masks visible text and OCRs remaining image content, but cannot distinguish some painted-over OCR from real text. Force mode rasterizes content and flattens interactive objects. Preserve original files separately. [OCRmyPDF](https://ocrmypdf.readthedocs.io/en/latest/advanced.html)
- PDF form values, widget appearances and XFA are distinct structures; fields may have hidden values or repeated widgets. Extracting ordinary page text is insufficient. [pypdf forms](https://pypdf.readthedocs.io/en/stable/user/forms.html)
- PDF attachments and annotations have separate extraction APIs. [Attachments](https://pypdf.readthedocs.io/en/stable/user/handle-attachments.html), [annotations](https://pypdf.readthedocs.io/en/stable/user/reading-pdf-annotations.html)
- Tesseract documentation identifies image quality and page segmentation as material factors and acknowledges table recognition limitations without custom segmentation/layout analysis. It can output TSV and hOCR, allowing geometry-aware results. [Quality](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html), [output formats](https://tesseract-ocr.github.io/tessdoc/FAQ.html)
- PresentationML separates slides, notes, comments, masters and layouts, and supports relationships to images, charts, embedded packages, audio and video. [Microsoft](https://learn.microsoft.com/pl-pl/office/open-xml/presentation/structure-of-a-presentationml-document)
- Whisper's model card explicitly describes hallucinated text, repetition and uneven language/accent performance. A fluent transcript is not evidence that every word was spoken. [Whisper model card](https://github.com/openai/whisper/blob/main/model-card.md)

### Inferences
The following is a proposed engineering test matrix, not a claim that any named tool already handles every row.

| Surface / edge case | Extraction plan | Required completeness evidence / failure status |
|---|---|---|
| Native PDF, headers, footers, marginalia | Text spans with page coordinates plus full render | Every page inspected; retain marginal text before downstream boilerplate filtering |
| Scanned PDF, mixed document, mixed individual page | Classify regions, native text plus OCR of image regions | Page with one native character must not automatically skip image OCR |
| Existing bad/partial OCR layer | Compare native layer against rendered OCR | Record disagreement, stale overlay, duplicate text; do not concatenate duplicates |
| Text drawn as outlines; broken font encoding | Render and OCR fallback | Identify unreadable native spans, retain rendered crop |
| Multiple columns, sidebars, rotated text, RTL/CJK | Layout-aware ordering, script/language-specific OCR | Preserve blocks and candidate order; flag order ambiguity |
| Tiny print, blur, skew, perspective, low contrast, watermarks | Non-destructive preprocessing variants; tiled OCR with overlap | Keep original pixels and preprocessing parameters; unresolved low-quality regions remain gaps |
| Tables with merged cells, multi-page tables, nested headings | Structured table reconstruction with cell coordinates | Match all detected cell text; preserve column/row headers, units, footnotes and continuation relationships |
| Charts, diagrams, maps, equations | Native vectors/data when present; OCR labels; visual description separately | Distinguish extracted labels/data from inferred relationships; no guessed numeric values as verbatim content |
| Forms, checkboxes, radio buttons, signatures | Extract field values and states, widget appearance, XFA detection | Record stored/visible mismatch; signature presence does not establish validity |
| Annotations, comments, popup text, links, attachments | Enumerate objects and recursively dispatch attachments | Each object gets extracted, excluded-by-policy, unsupported or failed status |
| Layers, clipped/off-page text, invisible text | Record source objects and rendered visibility separately | Hidden text must not silently become the visible page's asserted meaning |
| Encrypted, damaged, truncated PDF, renderer mismatch | Validate, attempt alternate parser/render; authorized password when supplied | Explicit blocked/corrupt/partial status; never treat blank output as blank page |
| Raster image and screenshot | Preserve bytes, dimensions; OCR text regions | OCR confidence plus visual region ledger; empty OCR is not proof of no text |
| Logos, pictograms, photos, diagrams without text | Image classification and optional description | Description explicitly machine-generated, never verbatim source quotation |
| SVG with text, paths, external assets | XML text extraction plus safe render/OCR | Compare text elements to rendered appearance; unavailable referenced assets are gaps |
| Multi-frame TIFF/GIF/WebP, animation | Inventory frames/pages and extract according to declared scope | Frame counts, processed ranges, deduplicated frames and skipped intervals |
| DOCX and similar documents | Package inventory, body/tables plus headers, footers, notes, comments, revisions, drawings and embedded assets | Test fixtures for every part; separate accepted, deleted and hidden text |
| PPTX | Shapes, groups, notes, comments, masters, hidden slides, images, chart data and media | Slide/part inventory; distinguish presenter notes from audience-visible assertions |
| XLSX | Sheets, cells, formulas, cached/display values, comments, drawings, hidden rows/sheets | Preserve formula versus value, date/number formats, merged ranges; no blind formula execution |
| CSV/TSV/JSON/XML/TXT | Encoding-aware structured parser | Rows/records/keys counted; preserve nulls, locale decimals and delimiters; reject silent truncation |
| EPUB, ZIP and nested attachments | Container inventory, relationship graph, recursive dispatch | Each member accounted for; encryption, depth/size limits visible as gaps |
| Audio | Stream inventory, native captions/transcripts plus ASR | Time intervals, silence/non-speech handling, overlap/language uncertainty; retain native versus generated text |
| Video | Audio, subtitle tracks, frame/scene text and visual content | Sampling is explicitly partial temporal coverage; brief overlays can fall between frames |
| Live media or unbounded stream | Declare start/end capture window | Coverage only for observed interval; no claim of entire stream |

### Gaps
- No finite list proves coverage of all future formats, custom codecs, malformed files or novel encodings. Treat this as a versioned capability registry with adversarial fixtures.
- No measured OCR accuracy, latency, GPU requirement or language coverage is established for the user's corpus yet. Need representative assets and ground truth.
- Detailed Office revision/chart/cache behavior must be verified per selected parser; listed cases are acceptance-test requirements, not supported-feature promises.

## Which tool combination is suitable, and what are its limitations?

### Takeaway
Use complementary native, layout, OCR and media adapters behind a common evidence schema. An orchestrator should decide when to retry or escalate; one universal converter cannot certify completeness.

### Cited Findings
| Candidate | Useful documented capability | Limitation / selection implication |
|---|---|---|
| PyMuPDF | Native PDF spans, rendering and OCR integration | Reading order can diverge; vector text and broken encodings require fallback. [Documentation](https://pymupdf.readthedocs.io/en/latest/recipes-common-issues-and-their-solutions.html) |
| pypdf | PDF object-level forms, annotations and attachments | No OCR; use as structural inventory/complement, not sole content extractor. [Text extraction](https://pypdf.readthedocs.io/en/stable/user/extract-text.html) |
| OCRmyPDF + Tesseract | PDF OCR with skip/redo/force controls, time and size limits | Skips and timeouts must surface; force transforms source structure. [Options](https://ocrmypdf.readthedocs.io/en/latest/advanced.html) |
| Tesseract directly | TSV/hOCR outputs, segmentation configuration | Layout/table reconstruction is separate; tune for document language and image quality. [Quality guidance](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html) |
| Docling | Structured conversion across PDF, Office, images and other formats | Declared format support does not prove every embedded feature survives. Pin version and test. [Formats](https://github.com/docling-project/docling/blob/main/docs/usage/supported_formats.md) |
| Docling enrichment | Picture classification/description and chart extraction options | These enrichments are model outputs, requiring separate provenance and validation. [Options](https://docling-project.github.io/docling/reference/pipeline_options/) |
| Apache Tika | Broad document format parsing and recursive metadata/content output | Treat successful parsing as evidence, not full recovery; secure process isolation required. [Formats](https://tika.apache.org/docs/4.0.x/formats.html), [recursive output](https://cwiki.apache.org/confluence/spaces/TIKA/pages/148639291/TikaServer) |
| Whisper / Docling media | Timestamped transcription; dedicated video path samples frames and optionally diarizes | ASR can hallucinate; frame sampling does not inspect every frame. [Whisper](https://github.com/openai/whisper/blob/main/model-card.md), [Docling media](https://docling-project.github.io/docling/usage/processing_audio_media/) |

Docling's format summary describes video as audio extraction/transcription, while its dedicated media page documents a newer video pipeline with frame sampling and diarization. Use the specific configured pipeline as the capability contract, rather than assuming the summary table fully describes runtime behavior. [Media documentation](https://docling-project.github.io/docling/usage/processing_audio_media/)

### Inferences
- Recommended prototype stack: native PDF inspector/rendering + structured Docling conversion + selective OCR + recursive general-format adapter. Keep alternate parsers available for disagreement, not mandatory double-processing for every clean asset.
- Preserve immutable original bytes before cleanup/conversion. Work on derived artifacts and link every derivative to its source hash.
- Use independent visual text-region detection as a coverage signal: each expected region must map to a native/OCR block or an explicit reason. A detector can itself miss regions, so coverage of detected regions is not absolute content recall.
- Benchmark per content class and language, including hard negatives: genuinely blank page, photo without text, tiny disclaimer, one native header above a scanned body, and a chart with no paragraph text.
- Test extracted content with character/word error rate against human ground truth, table structural accuracy, critical-number/unit/negation accuracy, page/region recall and false-positive content. Aggregate scores alone can hide missing disclaimers.

### Gaps
- No single winner can be selected responsibly without corpus trials, licensing review and operational constraints. This investigation did not run any tools or download models.

## How should completeness, safety and downstream provenance work?

### Takeaway
Report observable coverage and unresolved gaps separately from recognition confidence. Preserve source evidence so semantic similarity and contradiction findings can be traced to the exact page, region or time interval.

### Cited Findings
- Tika warns that output limits bound produced output, not CPU/memory spent producing it; forked parsing with heap limits and timeouts isolates hangs and crashes. Its server is not itself an authentication/security boundary. [Tika security](https://tika.apache.org/docs/4.1.x/security.html)
- OCRmyPDF supports time and image-size limits, and skipped pages may remain without OCR. This makes parser completion codes insufficient as a content coverage check. [OCRmyPDF advanced features](https://ocrmypdf.readthedocs.io/en/latest/advanced.html)
- Docling's video pipeline supports fixed-interval or scene-change frame sampling, while ASR output carries segment timestamps. [Docling media](https://docling-project.github.io/docling/usage/processing_audio_media/)

### Inferences
Proposed evidence record per asset/block:
- Identity: requested URL, final URL, retrieval timestamp, content hash, detected MIME, declared MIME, byte count, parent asset and embedding/attachment relationship.
- Locator: page and bounding box with coordinate system; PDF object identifier; Office part and shape/cell; image frame; media start/end timestamps.
- Evidence: raw source text, normalized text, original crop/render, extraction method, engine/model version and configuration, language, confidence if available, validation outcomes.
- Semantics: heading hierarchy, table row/column headers, units, caption links, footnote links, speaker, visibility, revision status and whether content is verbatim, OCR, ASR, or inferred description.
- Audit: expected/discovered units, processed units, errors, timeouts, unsupported objects, deliberate exclusions, retries and manual decisions.

Proposed statuses: queued, acquired, inventoried, extracting, validating, passed_defined_checks, partial, blocked, unsupported, failed, needs_review. Never use an unqualified complete=true for uncertain OCR or sampled video.

Proposed real-time dashboard: pages processed/known pages; image regions handled/detected regions; attachments handled/discovered attachments; media time covered/known duration; low-confidence regions; budget-exceeded units; extraction disagreements. Numerators/denominators must be explicit. Unknown denominators remain unknown.

Proposed isolation and budgets: deny file parser network access by default; disable macros and active content; bound compressed and uncompressed bytes, archive member count/depth, image pixels, pages, stream duration, memory, CPU, wall-clock time, temporary disk, OCR/model token costs. Validate member paths before unpacking. Limit hits produce explicit partial results with retained original bytes and reasons. Do not silently follow document external links as parser side effects.

For future contradiction analysis, preserve negation, numbers, currencies, units, product/version, applicability, dates, exceptions and footnotes. Do not compare deleted revisions, OCR guesses, hidden source text and visible published statements as equally authoritative. Keep original and normalized forms so dehyphenation, deduplication and boilerplate removal are reversible. Similarity scores should distinguish exact text overlap from semantic resemblance; contradictions require matched subject and scope plus source quotations.

### Gaps
- A calibrated probability that all content is captured requires corpus-specific labeled validation; model confidence is not a completeness probability.
- Unknown content beyond encrypted/unsupported structures cannot be counted from successful regions. The system can prove what it processed and explain known gaps, but cannot guarantee it discovered every unseen item.
