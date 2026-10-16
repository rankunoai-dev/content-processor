# Lane Activity Log

Shared blackboard for parallel sessions. **Read this file at the start of every session. Append one entry at the end of every session.** Newest entries go at the bottom. Lane definitions and file ownership: `docs/WORK_BREAKDOWN.md` section 4.

Entry template:

```markdown
## [YYYY-MM-DD HH:MM UTC] Lane Lx (name), session <id>
- **Branch / worktree:** `lane/Lx-topic` at `../content-processor-lanex`
- **What landed:** item IDs and one line each
- **Gate:** ruff / mypy / tests (passed, skipped) / coverage
- **Available to other lanes:** new functions or behaviour others can call
- **Open contract requests:** IDs in CONTRACT_REQUESTS.md, or none
- **Blocked on:** anything waiting for another lane or for the operator
```

---

## [2026-10-16] Lane L0 (Integrator), session 1: runway cleanup

- **Branch / worktree:** `main`
- **What landed:**
  - K2: deleted `migrations/001_initial.sql`; `ledger/schema.sql` is the single schema.
  - K3: moved untracked `adapters/pdf.py`, `ocr.py`, `image.py` unchanged to branch `lane/L3-docs` (commit `272a239`), worktree `../content-processor-lane3`.
  - K13: access-failure fixtures (`/challenge-200`, `/challenge-403`, `/rate-limited`, `/pay-per-crawl`, `/gated`) now use host `localhost`; content fixtures stay on `127.0.0.1`, so a tripped circuit on one host cannot starve rendering on the other.
  - Created this log and `CONTRACT_REQUESTS.md`; renumbered lanes in `WORK_BREAKDOWN.md` (L5 = analysis and reports, L6 = platform, deferred).
- **Gate:** ruff clean, mypy strict clean (51 files), tests passing with 9 Postgres tests skipped, coverage 89%.
- **Fixture run after K13:** 23 scheduled, 15 rendered; gates produce the expected statuses. New defect K15: `/pdf-but-html` ends VERIFIED (false positive on a known trap) — assigned L1 + L4, blocks the V-7 release gate.
- **Available to other lanes:** two-host fixture scope `fixtures/scopes/local_fixtures.json`.
- **Open contract requests:** CR-001 (L3 deps), CR-002 (L1 edges), CR-003 (L2/L4 states and regions), all anticipated and OPEN; L0 lands them on request.
- **Blocked on:** K1. Postgres is not reachable from Windows (nothing listening on 5432; WSL `Ubuntu-24.04` stopped). The 9 ledger integration and chaos tests stay skipped until the operator provides `CP_DATABASE_URL` for a disposable database. Do not point it at the production n8n database.

### Worktree commands for the first wave

```powershell
git worktree add ../content-processor-lane1 -b lane/L1-acquisition
git worktree add ../content-processor-lane2 -b lane/L2-explorer
# L3 already exists: ../content-processor-lane3 on lane/L3-docs
git worktree add ../content-processor-lane4 -b lane/L4-verify
```

---

## [2026-10-16] Lane L0 (Integrator), session 2: CR-001 landed

- **Branch / worktree:** `main`
- **What landed:**
  - CR-001: Added `pyproject.toml` mypy overrides for `selectolax.*`, `pytesseract.*`, `pdfplumber.*`, `pypdf.*`, and `pymupdf.*`/`fitz.*` (`disallow_untyped_calls = false`).
- **Gate:** ruff clean, mypy strict clean (51 files), tests green, coverage 89%.
- **Available to other lanes:** Lane 3 can rebase on `main` to inherit clean mypy typing without local `Any` workarounds.
- **Open contract requests:** CR-002 (L1 edges), CR-003 (L2/L4 states and regions).

---

## [2026-10-15 13:05 UTC] Lane L1 (Acquisition and access), session 1: follow-ups, T2, K15

- **Branch / worktree:** `lane/L1-acquisition` at `../content-processor-lane1`
- **What landed:**
  - A-1 / K6: `scope/followups.py` plans depth-1 follow-ups from an extracted page: images, frames and embedded objects, plus links that signal a download (`type=`, `download`, a document extension, or "PDF" in the label). Ordinary page links are not followed. Scope mode, `linked_download_depth`, in-scope hosts, `allowed_mime_families`, hidden and action-like traps, and a 200-per-page cap all apply. Every skip has a reason in the parent's `followups` summary, and each child records a `discovered_from` summary. After a follow-up is fetched, its sniffed type is checked against the scope (`EXCLUDED_BY_POLICY mime_not_allowed:*`), and identical payloads are extracted once (`duplicate_payload_of:<resource_id>`).
  - A-2: `ChainFetcher` in `http_client.py` now holds the shared chain (egress per hop, typed redirects, body cap, meta refresh, canonical hint). `HttpFetcher` (T1) keeps its API. New `integrations/impersonated_client.py` adds `ImpersonatedFetcher` (T2, curl_cffi, lazy import, `trust_env=False`, User-Agent left to the impersonation target). `FetchWorker(escalation_fetchers=[...])` fetches each task with the highest configured HTTP tier at or below the host's tier. A block that escalates the host to such a tier re-queues the URL at that tier (`RETRY_SCHEDULED escalated_to:T2_…`, cooldown honoured) instead of ending BLOCKED.
  - K15 (L1 half): follow-ups carry the type the parent promised (`expected_mime` and `expected_mime_source` in the task payload). The fetch worker passes it to the block classifier, falling back to the Content-Disposition file name, then the URL extension. An HTML login page served for a typed PDF link ends `BLOCKED wrong_payload:…:expected=application/pdf:from=link_type` and gets no RENDER task. A typed follow-up uses its own idempotency key so it cannot collapse onto a supplied URL's task.
  - K12 for T2: real curl_cffi truncation (CURLE_PARTIAL_FILE) is detected and tested against a raw loopback socket.
  - Fixture routes (new only): `/downloads` hub, `/files/brochure.pdf`, `/files/brochure-copy.pdf` (same bytes), `/files/report` (download, no extension), `/files/logo.png`, `/files/hotlink.png` (HTML for an image), `/fingerprint-wall` (403 without `sec-ch-ua`, passes with Chrome impersonation).
- **Gate:** ruff format and check clean, mypy strict clean (53 files), 294 passed and 9 skipped (Postgres), coverage 90.76% (was 89.03%). With curl_cffi absent the two live T2 tests skip and everything else passes.
- **Available to other lanes:** `Fetcher` protocol and `ChainFetcher` base (`http_client.py`); `ImpersonatedFetcher` and `impersonation_available()`; `plan_followups`, `anchor_hints`, `expected_mime_for_name`, `mime_allowed` (`scope/followups.py`); fetch-worker summaries `followups` (parent) and `discovered_from` (child).
- **Open contract requests:** CR-002 (still needed: edges are carried in task payloads and summaries until `record_edge` lands), CR-004 (new: persist `expected_mime` on `Resource`, look up payloads by hash; both are per-process caches in `FetchWorker` today).
- **Hand-offs, outside L1's paths:**
  - **L2 / L4, needed to close K15:** `render_worker.py` reloads the resource and overwrites its status. If a supplied URL is fetched as HTML *before* the page that types it as a PDF, its RENDER task is already queued, and that render turns the fetch worker's `BLOCKED wrong_payload` back into `VERIFIED_EXTRACTED`. The render worker must not upgrade a resource whose stored status is `BLOCKED`, at least when the reason starts with `wrong_payload`. The opposite order (the page first, which the fixture scope gives today) is closed by L1 alone.
  - **Fixture scope owner (L0 or L4):** `fixtures/scopes/local_fixtures.json` supplies `/pdf-but-html` with no linking page, so in the fixture dry run nothing promises a PDF and it still ends VERIFIED. Add `http://127.0.0.1:8765/downloads` to `input_urls`. The manifest's `requested_mime` could also be fed in as a scope-level hint, which would need a contract field.
  - **L6:** wire T2 in `cli.py`: build `ImpersonatedFetcher(host_checker=…, accept_language=…, limits=…)` when `impersonation_available()`, and pass it as `escalation_fetchers=[t2]` in `_fetch_loop` and `_dryrun`. Until then the CLI runs T1 only, and escalated hosts behave as before.
- **Blocked on:** nothing for L1. Spend: $0; no proxy or paid tier was touched. curl_cffi 0.16.3 was installed into the local Python 3.11 for the live tests; it is already declared in the `unblock` extra.

---

## [2026-10-16] Lane L2 (Browser state explorer), session 1

- **Branch / worktree:** `lane/L2-explorer` at `../content-processor-lane2`
- **What landed:**
  - B-1: `explore/` package. `StateExplorer` discovers closed `<details>`, ARIA accordions, tabs and `hidden="until-found"` regions; clicks only what `interaction_policy` allows; action budget (`max_actions_per_url`) and a 45 s time budget; every discovered control ends visited / initially open / excluded (site navigation menus) / unvisited-with-reason; states captured with fresh visibility annotation.
  - Side-effect guard (case 147): during exploration any non-GET request or main-frame navigation is aborted and the control is marked `unsafe_side_effect:<request>`.
  - B-2 disclosures and accordions, B-3 tabs. `BrowserRenderer.render(url, on_page=hook)` runs the explorer on the live page after the base capture; `RenderResult` unchanged.
  - Render worker: per-state extraction and verification, keeps only newly *visible* text per state, resolves collapsed blocks, one `UNVISITED_STATE` gap per unvisited control (evidence linked), `explore` summary. Discovery verdict: `discovery_closed_within_scope` only when every control is closed and every allowed action kind is implemented; otherwise `discovery_open:unvisited_controls=N` (status PARTIAL) or `discovery_open:not_implemented=scroll` (status not downgraded).
  - New fixtures: `tabs.html`, `accordion.html`, `accordion_side_effect.html`.
- **Gate:** ruff clean, mypy strict (54 files), all tests passing incl. 7 live explorer tests, coverage 89%.
- **End-to-end dry run** (policy `expand_disclosure, select_tab`): tabs, accordion, FAQ and static pages VERIFIED with `discovery_closed_within_scope`; side-effect accordion PARTIAL with gap `unvisited_accordion:unsafe_side_effect:POST .../lead`.
- **Available to other lanes:** `explore.StateExplorer`, `ExplorationResult`; `PageHook` in `browser_client`.
- **For L4:** please add the three new fixtures to `fixtures/manifest.json` (expected: tabs and accordion close discovery; accordion_side_effect PARTIAL). Default scopes allow `scroll`, which is not implemented, so they report `discovery_open:not_implemented=scroll` until B-8 lands.
- **Found for L0 (K16):** block ids from `html_source` restart at `<resource>:src:0` for every extraction, so source and rendered blocks share `extraction_id`/`region_id` and collide in the Postgres `extractions` table (memory ledger unaffected). L2 state blocks use unique ids (`<resource>:stateN:i`).
- **Open contract requests:** none.
- **Next for L2:** B-8 scroll accumulation, B-9 virtualization, B-11 frames, B-12 shadow DOM.

---

## [2026-10-16] Lane L0 (Integrator), session 3: merged Lane 2

- **Branch:** `main`
- **What landed:** `lane/L2-explorer` rebased onto `4040e20` (one conflict, this file, resolved by keeping both entries) and fast-forwarded into `main` as `76ed7bd`.
- **Gate on the merged tree:** ruff format and check clean, mypy --strict clean (54 files), 269 passed, 9 skipped (Postgres), coverage 89%.
- **All lanes:** rebase on `main` now to pick up `explore/`, the `on_page` hook in `BrowserRenderer.render`, and the new `RenderWorker(explorer_factory=...)` parameter. Fakes of `Renderer.render` must accept `on_page=None`.
- **Open:** K16 (block id collisions in the Postgres `extractions` table) is L0's next item; K1 still needs `CP_DATABASE_URL`.
- **Process note:** commit `4040e20` was made on `main` by the L3 session. Lanes other than L0 must not commit to `main`; put the request in `CONTRACT_REQUESTS.md` instead.

---

## [2026-10-16] Lane L3 (Documents and OCR), session 1: adapters typed, doc worker

- **Branch / worktree:** `lane/L3-docs` at `../content-processor-lane3`, rebased on `main` 029205e
- **What landed:**
  - C-2: PDF, OCR and image adapters now pass ruff and `mypy --strict` without touching `pyproject.toml` (untyped PyMuPDF and pytesseract handled locally via `Any`). Two bugs fixed in `pdf.py`: `len(doc)` was called after `doc.close()` (page count would raise), and the bold test used the italic flag bit.
  - C-3: `workers/doc_worker.py`, `DocWorker` for `EXTRACT_DOC` and `EXTRACT_IMAGE`. Extraction runs in a spawned child process with a wall-clock kill (`run_isolated`); timeout becomes PARTIAL plus a LIMIT_HIT gap, crashes retry, oversize payloads are PARTIAL.
  - C-4 (part): scanned PDFs and full-page image regions are OCRed per region even when other regions have native text. Missing Tesseract yields an UNSUPPORTED gap, never a silent empty result.
  - Tests: `test_doc_worker.py` (14), `test_ocr_adapter.py` (5).
- **Gate:** ruff clean, `mypy --strict src fixtures` clean (55 files), tests pass (Postgres tests skipped), coverage 86.3%.
- **Available to other lanes:** `DocWorker(ledger=, evidence=, run_id=, worker_id=).run_until_idle()`. Task payload it expects: `{"resource_id", "sha256"}`.
- **Open contract requests:** CR-001 still OPEN (L3 deps in a CI-installed extra; mypy overrides for `pymupdf.*`, `pytesseract.*` would let me drop the local `Any` shims).
- **Blocked on / needs from others:**
  - L1: `fetch_worker` leaves non-HTML payloads at `IN_PROGRESS acquired:awaiting_extraction`; it must enqueue `EXTRACT_DOC` / `EXTRACT_IMAGE` with the payload above.
  - Operator: Tesseract is not installed on this machine (C-1), so OCR is only tested through an injected runner. Install it with the core language packs to exercise the real engine.
- **Not done yet:** C-1 startup detection, C-5/C-6/C-7, C-9, C-10, `fixtures/docs/`.

---

## [2026-10-16] Lane L3 (Documents and OCR), session 2: C-5, C-6, C-7

- **Branch / worktree:** `lane/L3-docs` at `../content-processor-lane3`, still based on `main` 029205e (CR-001 had not landed when this session ran; rebase pending)
- **What landed:**
  - C-5: new `adapters/pdf_extras.py`. Form field values become labelled blocks (`email: a@b.com`, method `pymupdf/forms`); non-painted annotation comments become `collapsed_published` blocks; embedded files are returned in `PdfExtraction.attachments` (name, size, bytes up to 25 MB) for L1 to schedule as child resources; switched-off optional-content layers and invisible (render mode 3) text are reported as warnings, and invisible text lands in the `hidden` layer instead of the body. Invisible text no longer counts as native text, so a PDF whose only text is a ghost OCR layer is still treated as scanned.
  - C-6: a table that starts in the top 20% of a page, has the same column count, and follows a table that ended in the bottom 20% of the previous page is merged into one block (repeated header row dropped; warning `table_continued:pA-pB`). Also fixed: table blocks used to repeat the header row.
  - C-7: `ocr.route_ocr()` decides script and packs per region (OSD on the crop, else script of nearby native text, else Latin plus locale), limited to scope `ocr_languages` and installed packs. `select_ocr_packs` no longer falls back to English for non-Latin or unknown scripts; the result is an UNSUPPORTED gap `ocr_lang:<SCRIPT>`. OCR below confidence 0.60 raises a retry-eligible LOW_CONFIDENCE gap (second-opinion trigger only; the second engine is not wired).
  - `PdfImageRegion.script_hint`, `DocWorker(osd_runner=, locale=)`, `extract_image(locale=, script_hint=, osd_runner=)`.
- **Gate:** ruff clean, `mypy --strict src fixtures` clean, tests pass, coverage 87%.
- **Available to other lanes:** `PdfExtraction.attachments` (child-resource candidates), new gap reasons `ocr_lang:<SCRIPT>` and `ocr_confidence=<x>`.
- **Open contract requests:** CR-001 (landing on main per L0).
- **Known limits:** the 0.60 confidence floor is provisional (needs fixture calibration); table cells still also appear as native text blocks (pre-existing, for L4 to dedupe); real Tesseract and OSD are untested here because Tesseract is not installed on this machine.

---

## [2026-10-15 13:30 UTC] Lane L4 (Verification brain), session 1: UrlRecord, R1–R3/R10, K14, K15, V-7

- **Branch / worktree:** `lane/L4-verify` at `../content-processor-lane4` (based on `4040e20`)
- **What landed:**
  - V-1 / K4: `verify/record.py` assembles one `UrlRecord` per scheduled URL plus one per input excluded at plan, so records = requested URLs. Each carries the status vector, `InventoryCounts` over observed entities only, `DiscoveryState`, every gap (recorded + rule-emitted, deduplicated by id) and the corpus blocks of the best channel. The badge is derived from the gaps, never from the worker's status alone. Entry points: `assemble_url_record`, `url_records_for_run(reader, plan=…, frames_for=…, expectations=…, outstanding=…)`, `excluded_url_record`, `frames_from_evidence(store)`.
  - V-2: `verify/rules.py` has R1 inventory accounting (DISCOVERY_OPEN), R2 acquisition (ACCESS_BLOCKED / ACQUISITION_FAILED / UNSUPPORTED_FORMAT / BUDGET_STOPPED / POLICY_EXCLUDED), R3 frame outcomes (per-frame gap; an unreadable frame inventory is a review gap, not a pass) and R10 payload sanity (wrong payload, block page stored as content, soft 404, soft redirect to root, truncation, placeholder). Pure functions, stable gap ids, unit tested.
  - K14: `verify.gaps.strictest_status` applies every gap's implied status to the resource when the record is built, so a gate the fetch worker found sets the badge in a `--no-render` run (the record notes `status_lowered_by_gaps` and `render_not_run`). `fetch_worker.py` was not touched.
  - K15 (L4 half): R10 blocks HTML where a document or image was expected (explicit expectation, then Content-Disposition, then URL extension) and is **sticky**: if any event ever recorded `BLOCKED wrong_payload…` for the resource, the record stays BLOCKED even after a later status overwrite. New gate in `governor/gates.py`: a tiny page (< 40 main words) whose content is a sign-in prompt is a `login_wall`, so `/pdf-but-html` is BLOCKED even with no linking page.
  - Lead-gen walls now follow the feasibility doc, "PARTIAL (teaser kept) or BLOCKED (nothing else)". Severity is unchanged; `gates.py` adds `teaser_words=N` evidence and `verify.gaps.gate_status` maps ≥ 8 teaser words to PARTIAL. This replaced an uncommitted edit of unknown origin in this worktree that failed ruff and `test_gates.py`; the user approved the replacement.
  - V-7: `test_verify_release_gate.py` runs the full fixture scope in-process (real frontier, fetch worker, render worker with a fixture renderer that refuses script-dependent pages), with and without rendering, judged as recorded and with discovery assumed closed. Zero false VERIFIED_WITHIN_SCOPE; with discovery assumed closed every fixture matches its manifest badge exactly. Mutation-checked: removing the expected type from `/pdf-but-html` still ends BLOCKED (sign-in gate).
  - `fixtures/manifest.json`: every fixture now has an explicit `expect_badge` (or a note why not), `/private/secret` added, `/pdf-but-html` pins `expect_gate: login_wall`. VERIFIED is allowed only where `expect_badge` says so.
- **Gate:** ruff format and check clean, mypy strict clean (53 files), 324 passed and 9 skipped (Postgres), coverage 91.23% (was 89.03%).
- **Available to other lanes:** `url_records_for_run` and `RunReader` (L5 reports and L6 API can build per-URL verdicts from a `MemoryLedger` today); `strictest_status`, `gate_status`, `TEASER_MIN_WORDS` (`verify/gaps.py`); `rule_r1_inventory`, `rule_r2_acquisition`, `rule_r3_frames`, `rule_r10_payload`, `expected_mime_for`, `FrameRef` (`verify/rules.py`). `expected_mime_for` already reads `Resource.expected_mime` when L1's CR-004 lands.
- **Open contract requests:** CR-005 (new: read port for the durable ledger), CR-006 (new: `GapType.SOFT_404_SUSPECTED`); CR-003 still needed for regions; CR-004 (L1) feeds R10.
- **Hand-offs, outside L4's paths:**
  - **L2 (`render_worker.py`):** do not upgrade a resource whose stored status is `BLOCKED` with a reason starting `wrong_payload` (L1's K15 hand-off). The UrlRecord already stays BLOCKED via the event history, but the resource row and review queue still show the overwritten status. Discovery contract L4 reads: the token `discovery_closed_within_scope` in `status_reason`, any `discovery_open[:reason]` token, `UNVISITED_STATE` gaps, and `stop_reason` in the `explore` summary (top level or one level down). Keep those stable. After your merge, `/pages/details_collapsed` should reach VERIFIED in a real run; its manifest `expect_badge` stays NEEDS_REVIEW for the in-process gate (no explorer there) and L4 will revisit it.
  - **L1 (`fetch_worker.py`):** optional, for the resource row and events: apply `verify.gaps.strictest_status(current, [implied…])` to the gate statuses in `_extract_source` (line ~270 drops `gap_for_gate`'s implied status). Badges are already correct without it.
  - **L0 / L6 (`cli.py` dry run):** write UrlRecords with `url_records_for_run(ledger, plan=plan, frames_for=frames_from_evidence(evidence), outstanding=…)`; a `--no-render` run should pass outstanding without RENDER tasks.
  - **Extractor owner (`adapters/html_source.py`):** an `<iframe>` with its own `hidden` attribute or `display:none` is not marked hidden (only a hidden parent is). R3 errs safe (gap instead of skip).
- **Blocked on:** nothing. K5 still keeps every real record discovery-open until L2 lands.

---

## [2026-10-16] Lane L0 (Integrator), session 4: Wave 1 complete (merged L1, L2, L3, L4)

- **Branch:** `main`
- **What landed:**
  - Merged `lane/L1-acquisition` (`d2de673`): depth-1 linked downloads, T2 impersonated fetcher, expected MIME link carrying.
  - Merged `lane/L2-explorer` (`76ed7bd`): state explorer (B-1), disclosures/accordions (B-2), tabs (B-3), side-effect guard.
  - Merged `lane/L3-docs` (`ee9e9b1`): PDF extras (forms, annotations, attachments), table continuation across pages, per-region multilingual OCR routing.
  - Merged `lane/L4-verify` (`3b11f86`): honest `UrlRecord` assembly, deterministic rules R1, R2, R3, R10, gate reporting fix K14, Defect K15 closed, V-7 release gate passing with zero false `VERIFIED_WITHIN_SCOPE`.
- **Gate on the fully merged tree:**
  - `ruff format --check .`: 128 files clean.
  - `ruff check .`: 0 errors.
  - `mypy --strict src fixtures`: 0 errors across 63 source files.
  - `pytest --cov=src`: 399 passed, 9 skipped (Postgres). Total coverage: 90.06% (threshold ≥ 85.0%).
- **Open contract requests:** CR-002, CR-003, CR-004, CR-005, CR-006 filed in `docs/CONTRACT_REQUESTS.md`.
- **Next steps:** Wave 2 (L5 Outputs & Analysis, L6 Platform & Ops CLI wiring, landing CR-002..CR-006).

---

## [2026-10-16] Lane L0 (Integrator), session 5: landed CR-002 through CR-006 on main

- **Branch:** `main`
- **What landed:**
  - **CR-006:** `GapType.SOFT_404_SUSPECTED = "SOFT_404_SUSPECTED"` in `enums.py`, wired into `_REVIEW_GAPS` in `verify/status.py`, and rule `R10:soft_404` in `verify/rules.py` emits the typed gap.
  - **CR-004:** Added `Resource.expected_mime` and `Resource.expected_mime_source` in `contracts.py`, schema columns and indexes in `ledger/schema.sql`, `SQL_UPSERT_RESOURCE` update in `ledger/sql.py`, and `find_resource_by_payload` on `LedgerPort` in both `MemoryLedger` and `TaskLedger`.
  - **CR-003:** Added `State` and `Region` models to `contracts.py`, schema indexes on `states` and `regions`, and port methods `upsert_state`, `upsert_region`, `get_states`, `get_regions` on `LedgerPort` in both `MemoryLedger` and `TaskLedger`.
  - **CR-002:** Added port methods `record_edge` and `find_resource_by_schedule_key` on `LedgerPort` in both `MemoryLedger` and `TaskLedger`, with corresponding SQL statements and schema indexes.
  - **CR-005:** Implemented durable read side for `RunReader` in `verify/record.py`: `list_resources`, `get_summaries`, `get_events`, and `outstanding_resource_ids` on `LedgerPort` in both `MemoryLedger` and `TaskLedger`.
  - Added unit test suite `tests/modules/content_processor/test_task_ledger.py` covering all new `TaskLedger` methods with mock database connections, and expanded `test_memory_ledger.py`.
- **Quality Gate:**
  - `ruff format --check .`: 129 files clean.
  - `ruff check .`: 0 errors.
  - `mypy --strict src fixtures`: 0 errors across 63 source files.
  - `pytest --cov=src`: 423 passed, 9 skipped (Postgres). Total coverage: 90.22% (threshold ≥ 85.0%).
- **Open contract requests:** All open requests (CR-001 through CR-006) are now LANDED.
- **Ready for Wave 2:** Lane 5 (Outputs & Stage 4 Analysis) and Lane 6 (Platform & CLI Wiring) can now branch cleanly from `main`.


## [2026-10-16] Lane L5 (Outputs and Stage 4), session 1: R1 export, review queue, explorer, overlap

- **Branch / worktree:** `lane/L5-outputs` at `../content-processor-lane5` (based on `cdbfb38`)
- **What landed:**
  - O-1, report R1: `export/reports.py` builds one row per requested URL from `UrlRecord`s (badge and gaps from L4's record, never from worker status). Columns: run_id, url, aliases, final_url, badge, word_count, gap_count/gap_types, captured_at, tier_reached, warnings, evidence_ids, block_count, block_ids, whole_content. `export/tabular.py` writes CSV (UTF-8 BOM, formula guard), XLSX (standard library only, no new dependency) and verbatim JSONL. Cells over 32,767 characters, counted in UTF-16 units as Excel counts them, are split into `name`, `name_part_2…N` with `name_parts` and `name_split`, and the parts concatenate back exactly. The XLSX writer refuses any unsplit cell over the limit, so nothing is truncated. Characters XML cannot carry are replaced only in XLSX and counted (`control_chars` column, manifest).
  - G-3 (file half), review queue: `review_rows` keeps FAILED, BLOCKED, PARTIAL and NEEDS_REVIEW records, most severe first, with gap list, suggested action, evidence ids and blank `operator_action` / `operator_note`. Written as CSV, XLSX and JSONL. The old resource-based `export/review_queue.py` is untouched because `cli.py` still calls it.
  - O-2, I1 URL Content Explorer: `ui/explorer.py` (+ `explorer.css`, `explorer.js`) writes one offline HTML file per run: badge chips, URL/text search, boilerplate toggle, per-URL status vector, gaps with evidence ids, and blocks in reading order by heading, coloured by layer and role, with download links to the reports. Scraped text travels as escaped JSON and is only ever set as `textContent`. A CSP with `default-src 'none'` pins the page script by SHA-256. Browser-tested: an injected `</script>` / `onerror` payload renders as text and does not run, with no console or CSP errors and no horizontal scroll at 375 px.
  - O-3, I2 mode B (content overlap): `analysis/overlap.py` uses word 5-gram shingles, exact-duplicate collapse, and one-permutation MinHash with densification (one hash per shingle, so pure Python stays fast) with LSH banding tuned for 99% recall at the candidate floor. Every candidate is verified on the real shingle sets, and only verified numbers are reported (`jaccard_pct`, both containments). Groups are connected components, or strict cliques with `strict=True`. Reports R2a (groups: overlap_to_group_pct, shared_block_hashes) and R2b (pairs: up to 3 shared spans). Every quote is verbatim block text (no R1 heading markers); tested on synthetic data and on the real fixture suite. Measured: recall ≥ 98% against brute force on 400 pages, and 5,000 URLs × 800 words in 53 s with 10,000 candidates checked instead of ~12.5 M pairs.
  - `export/bundle.py`: `write_run_reports(records, out_dir)` writes all of the above plus `manifest.json` (counts, overlap parameters, LSH bands and rows, skipped URLs, XLSX replacements). `export/commands.py`: `register(subparsers)` adds `report --records <UrlRecord JSONL> --out <dir> [--threshold 80 --metric jaccard|containment --strict --overlap-boilerplate]`.
- **Gate:** ruff format and check clean, mypy strict clean (71 files), 461 passed and 9 skipped (Postgres), coverage 91.23% (was 90.22%). openpyxl is used only to read XLSX back in tests (skipped if absent); the writer needs no dependency.
- **Available to other lanes:** `write_run_reports`, `r1_rows`/`r1_table`, `review_rows`/`review_table`, `Table`/`write_csv`/`write_xlsx`/`write_jsonl`/`expand_split_column`/`split_cell` (`export/`), `find_overlaps`/`OverlapConfig`/`r2a_table`/`r2b_table` (`analysis/overlap.py`), `write_explorer` (`ui/explorer.py`).
- **Open contract requests:** none.
- **Hand-offs, outside L5's paths:**
  - **L6 (`cli.py`):** call `export.commands.register(sub)` in `main()`. In `dryrun --out`, write `url_records_for_run(...)` as `<out>.records.jsonl` (one `UrlRecord.model_dump_json()` per line) and optionally call `write_run_reports(records, <out>_reports/)` directly; the resource-based review queue and `content.jsonl` can then be retired in favour of these.
  - **Postgres export views (G-3 durable half, STAGE4 §4):** not built. K1 still has no reachable database, and views are schema (L0).
- **Not done (next for L5):** I2 mode A (pattern/keyword matching, R2c), I3 semantic similarity, I4 contradictions. The last two need new dependencies or models; per the backlog decision they come one at a time.
- **Blocked on:** nothing. Spend: $0.


## [2026-10-16] Lane L6 (Platform & CLI Wiring), session 1: UrlRecord assembly, bundle reports, report subcommand

- **Branch:** `main` (direct platform wiring)
- **What landed:**
  - `cli.py`:
    - Wired `export.commands.register(sub)` into `main()` providing the `report` subcommand (`--records <jsonl> --out <dir> [--threshold 80 --metric jaccard|containment --strict --overlap-boilerplate]`).
    - Wired `url_records_for_run` into `_dryrun`: honest per-URL verdict assembly with denominators preserved, frames read from evidence store, and outstanding task tracking (`TaskKind.FETCH` or both `FETCH` and `RENDER` depending on `--no-render`).
    - Added `verdicts` summary to dry run report.
    - When `--out` is specified in `dryrun`, writes `<out>.records.jsonl` (one `UrlRecord.model_dump_json()` per line) and automatically calls `write_run_reports(records, <out>_reports/)` producing the complete Stage 4 report bundle (`r1_url_content.*`, `review_queue.*`, `r2_overlap.xlsx`, `r2a_overlap_groups.csv`, `r2b_overlap_pairs.csv`, `explorer.html`, `manifest.json`).
    - Added optional `--reports-dir` flag to `dryrun` for explicit report directory targeting.
    - Fully typed `host_checker_for` as `Callable[[str], tuple[bool, str]]` and updated `__all__`.
  - Tests:
    - Added 5 new CLI test cases in `tests/modules/content_processor/test_cli.py`: report help, report execution with synthetic records, dryrun with record assembly and report bundle, dryrun with custom reports dir, and dryrun stdout-only.
- **Gate:** ruff format and check clean, mypy strict clean (71 files), pytest passing with coverage ≥ 85.0%.
- **Open contract requests:** none.
- **Blocked on:** nothing. Spend: $0.

## [2026-10-16] Lane L6 (Platform and CLI wiring), session 1

- **Branch / worktree:** `lane/L6-platform` at `../content-processor-lane6`
- **What landed:**
  - `scheduler/` package. `execute_run` drives fetch, render and docs stages in rounds on any `LedgerPort` until the queue drains, a round makes no progress (held by Retry-After or an open circuit), the task cap, or the round cap; the stop reason is reported. `LazyRenderStage` launches the browser only when RENDER work exists. `assemble_run_records` wraps `url_records_for_run`, counting as outstanding only work of stages that will run (same rule as the V-7 release gate). `write_run_outputs` writes the report plus `.records.jsonl` (full UrlRecords, the input of `cli report`), `.content.jsonl`, `.review_queue.jsonl`, all through the ledger port; every `--out` also writes L5's Stage 4 bundle to `<stem>_reports/` (or `--reports-dir`).
  - `cli.py`: `dryrun` now runs through the engine (fetch, render, docs) and writes UrlRecords with a badge summary; new durable commands `run` (create or resume, drive, report), `work` (drive or resume an existing run, `--stages`), `records` (UrlRecords from Postgres, no workers; named `records` because L5's `report` builds the Stage 4 bundle from a records file); `fetch` is an alias of `work --stages fetch`; `start` and `status` unchanged. All durable commands call the lease reaper each round and are resumable with the same `--run` id. T2 (`ImpersonatedFetcher`) is wired as an escalation fetcher when the ceiling allows and curl_cffi is installed (L1 hand-off done). `DocWorker` is wired as the docs stage.
  - Guard: durable commands refuse (exit 3, `other_run_active`) while another run has open tasks, because Postgres leases are not run-scoped (CR-007).
- **Gate:** ruff clean, mypy --strict clean (66 files), 440 passed, 9 skipped (Postgres), coverage 92.19%.
- **End-to-end dry run** (local fixtures, real Chromium): 25 fetches, 15 renders, drained in 1 round; 23 UrlRecords for 23 requested URLs: 15 PARTIAL, 5 BLOCKED, 2 EXCLUDED, 1 FAILED, 0 VERIFIED. `/pdf-but-html` is BLOCKED (K15 closed end to end). Every content page is PARTIAL only because the default policy allows `scroll`, which is not implemented (B-8, L2).
- **Not verified:** the durable commands against a real Postgres (K1, no `CP_DATABASE_URL`); they are tested against a `TaskLedger`-shaped fake.
- **Found (K17, for L1):** nothing enqueues `EXTRACT_DOC` / `EXTRACT_IMAGE` tasks, so the docs stage processed 0 tasks and fetched PDFs and images are never extracted. The fetch worker should enqueue them for document and image payloads; the engine already runs the stage.
- **Open contract requests:** CR-007 (run-scoped leasing).

## [2026-10-16] Lane L0 (Integrator), session 4: merged Lane 6

- **Branch:** `main`
- **What landed:** `lane/L6-platform` rebased onto `0d266ca` and fast-forwarded into `main` as `21d3e97`.
- **Conflicts:** `docs/LANE_LOG.md` (kept both entries) and `cli.py`. Commit `0d266ca` on `main` had wired a one-pass dryrun with records and L5's bundle; the merged CLI keeps the L6 engine and durable commands and adds everything from `0d266ca`: full `.records.jsonl`, `verdicts`, the Stage 4 bundle in `<out stem>_reports/` or `--reports-dir`, and L5's `report` command. The durable ledger command was renamed from `report` to `records` so both exist; `records --out` output feeds `report --records` directly (tested).
- **Gate on the merged tree:** ruff and mypy --strict clean (74 files), 483 passed, 9 skipped (Postgres), coverage 92.97%.
- **Process note:** `0d266ca` (author unknown) committed Lane 6 work directly to `main` while `lane/L6-platform` existed. Lanes commit only on their branch.
- **Open:** CR-007 (run-scoped leasing), K16, K17, K1 (`CP_DATABASE_URL`).
