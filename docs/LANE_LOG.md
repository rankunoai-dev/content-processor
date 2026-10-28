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

## [2026-10-16] Fix session (L0 + L1 items): K16, K17, CR-007, web to ledger end to end

- **Branch / worktree:** `fix/k16-k17-cr007` at `../content-processor-e2e` (based on `7ce6a00`)
- **What landed:**
  - **K17 (L1):** `FetchWorker._accept` hands non-HTML payloads off by sniffed type: `application/pdf` → `EXTRACT_DOC`, `image/*` → `EXTRACT_IMAGE` (payload `resource_id`, `sha256`, keyed per schedule key; the resource stays `IN_PROGRESS acquired:awaiting_extraction` until the doc worker finishes it). Types no adapter reads (zip, Office, …) now end `UNSUPPORTED no_adapter:<type>` instead of sitting in IN_PROGRESS forever. Duplicate payloads are still extracted once. New fixtures `/docs-hub` and `/files/annual-report.pdf` (a hand-built PDF with a real text layer). Verified through the real CLI `dryrun` against a live fixture server: the PDF ends `pdf_extracted` with its text in the content export, and the 1×1 logo ends `VERIFIED_NONCONTENT` (tracker-sized image).
  - **K16 (L0):** two layers. (1) `html_source` block ids carry the channel: `<resource>:src:N` (unchanged), `<resource>:rendered:N`, `<resource>:stateK:N` (`block_channel(state_id)`), so the source and rendered blocks of a page no longer share ids. Only the `block_id` line in `emit()` changed, as agreed with the provenance branch. (2) `TaskLedger.upsert_blocks` namespaces its storage keys (`storage_keys`): the `region_id` column is `<resource>:<channel>:<region>` and `extraction_id` is that plus `|<method>`, the same key the upsert conflicts on. This also fixes a worse case found on the way: PDF and image blocks (`reg-pdf-metadata`, `blk-<hash>`) collided across resources and, lacking the `<resource>:` prefix, were never returned by `get_blocks`. Stored records are unchanged. No schema change. Rows written by an earlier durable run would not be rekeyed, but K1 means no durable run has ever been written.
  - **CR-007 (L0):** `LedgerPort.lease(worker, kinds, run_id=None)` and `pending_count(kinds, run_id=None)` in both ledgers. The SQL adds `(%(run_id)s IS NULL OR run_id = %(run_id)s)` to both lease statements and the pending count. Fetch, render and doc workers pass their bound run id, and so do the scheduler's drain checks and `LazyRenderStage`. Host slots stay shared across runs, so per-host politeness holds for concurrent runs. The CLI's `other_run_active` refusal (exit 3) is gone; durable reports carry `concurrent_open_tasks` instead.
- **Gate:** ruff format and check clean, mypy strict clean (74 files), 501 passed and 11 skipped (Postgres; 2 new integration tests for run-scoped leases and K16 blocks wait for K1), coverage 93.05% (`main` was 92.97%).
- **Coordination:** `feature/provenance-recrawl` (session content-processor-33, commit `2cdd8b6`) touches `html_source.py`, `fetch_worker.py`, `render_worker.py`, `doc_worker.py`, `cli.py` and appends to `fixtures/server.py` and this log. It will rebase onto this branch once merged. Expected conflicts are only the two appends.
- **Open:** K1 (`CP_DATABASE_URL`). The Postgres tests for CR-007 and K16 are written and skip until a disposable database exists.


## [2026-10-16] Feature: content source tagging and change-aware recrawl

- **Branch / worktree:** `feature/provenance-recrawl` at `../content-processor-provenance` (spans L0 contracts, L1 fetch worker, L4 record assembly, L6 CLI; coordinated with the K16/K17/CR-007 session, whose branch should merge first).
- **What landed:** see `docs/PROVENANCE_AND_RECRAWL.md`.
  - Contracts (additive): `SourceFormat`, `UiRegion`, `ChangeStatus` enums; `ContentBlock.source_format/ui_region/source_url/extracted_at/page_modified_at/page_modified_source/reused_from_run_id`; Resource and UrlRecord validators (ETag, Last-Modified), page modified/published dates, content fingerprint, change status/basis, previous and reused-from run ids; UrlRecord also carries schedule key, resource status, payload hash, follow-up refs and summaries so a records file is a complete baseline.
  - `adapters/regions.py`: page-region classifier (hamburger and mega menus, breadcrumb, snippet, palette, card, carousel, modal, popover, tooltip, cookie banner, chat widget, ...). `provenance.stamp_blocks` applied by the fetch, render and doc workers.
  - `recrawl/`: page dates (JSON-LD, meta, microdata, PDF ModDate, non-dynamic HTTP Last-Modified), baseline from a records file or a previous durable run, ordered change decision. Fetchers accept first-hop headers (conditional GET; 304 returned, not followed). Unchanged pages reuse blocks, gaps and summaries with their original dates; no render, exploration or OCR; children re-queued.
  - CLI: `--baseline <records.jsonl>` on dryrun/run/work, `--baseline-run <id>` on run/work (or `scope.recrawl_of_run_id`); reports carry `changes` counts and baseline info.
- **Gate:** ruff, mypy --strict clean (80 files), 534 passed, 9 skipped, coverage 93.10%.
- **Measured:** fixture scope twice with real Chromium: run 1 rendered 17 pages in 87 s; run 2 with the baseline found 15 unchanged, rendered 2, took 33 s; no reused verdict changed.
- **Limits:** HTML change is judged on server HTML (pure-SPA data changes look unchanged); sitemap lastmod and XMP/EXIF dates not read yet.

## [2026-10-19] Lane L0 (Integrator), session 5: merged K16/K17/CR-007 and provenance/recrawl; K18

- **Branch:** `main`
- **What landed:**
  - `fix/k16-k17-cr007` fast-forwarded as `0dc8878` (gate on the branch: 501 passed, 11 skipped, 93.05%). CR-007 marked LANDED.
  - `feature/provenance-recrawl` rebased onto `0dc8878` (conflicts only in this file and `fixtures/server.py`, append blocks, both kept) and fast-forwarded as `4570cc1`. Gate on the merged tree: 552 passed, 11 skipped, 93.18%.
  - K18 found during the post-merge check: PyMuPDF 1.28 prints a `pymupdf_layout` recommendation to stdout on first use, which broke JSON parsing of every CLI run that extracted a PDF. Fixed with `pymupdf.no_recommend_layout()` at import in `adapters/pdf.py`, plus a subprocess regression test asserting empty stdout.
- **Checked end to end:** two dry runs over `/downloads`, `/fresh` and `/pages/regions` with the docs stage: run 1 extracted 2 PDFs via the K17 hand-off; run 2 with `--baseline` judged 6 URLs unchanged (PDFs on identical bytes) and extracted none again. The fixture PDFs are blank pages, so zero text blocks is correct.
- **Gate on `main`:** ruff and mypy --strict clean (80 files), 553 passed, 11 skipped, coverage 93.18%.
- **Still open:** K1 (`CP_DATABASE_URL` for a disposable Postgres; the durable path and the 11 skipped tests). Lane 2 B-8 (scroll) is why every content page still ends PARTIAL under the default policy.

## [2026-10-19] Lane L3 (Documents and OCR), session 3: C-9 Office (DOCX, XLSX, PPTX)

- **Branch / worktree:** `lane/L3-docs` at `../content-processor-lane3`, on `main` f56a95e plus this session
- **What landed:**
  - `adapters/office.py`: pure-Python extraction with python-docx, openpyxl and python-pptx. No LibreOffice, no Docling, no torch.
    - DOCX: body paragraphs and tables in document order; heading path from Heading/Title styles; headers and footers as role `chrome`; comments as `collapsed_published`; `w:vanish` text and tracked deletions as `hidden`; tracked insertions as visible text.
    - XLSX: one table per sheet with a title block; hidden sheets as `hidden`; formulas with no cached value counted (`formula_without_cached_value:N`); row cap 5,000 per sheet, marking the document PARTIAL.
    - PPTX: slide text and tables in order; speaker notes as `source_only`; hidden slides as `hidden`; picture alt text as `accessibility_only`.
  - Safety before parsing: zip member count (5,000), uncompressed total (200 MB) and per-member compression ratio (100x) are checked first. Refusals return a `limit:` reason, mapped to LIMIT_HIT and PARTIAL, so a zip bomb is never retried.
  - `workers/doc_worker.py` dispatches by content, not by the sniffed MIME type (the sniffer reports every OOXML file as `application/zip`): PDF, then OOXML, then legacy OLE (`.doc`/`.xls`/`.ppt`, reported UNSUPPORTED with reason `legacy_office_requires_libreoffice:<ext>`). Parse errors are FAILED, not retried.
  - Tests: `test_office.py` (20 tests, including the zip bomb, tracked changes, hidden text, hidden sheet and slide, and the legacy reason). Fixtures are built in code by `office_builders.py`.
- **Gate:** ruff clean, `mypy --strict src fixtures` clean (81 files), tests pass, coverage 93.1%.
- **Available to other lanes:** `extract_office()`; `OfficeExtraction.unit_count` (sections, sheets or slides). New gap reason `legacy_office_requires_libreoffice:<ext>`.
- **Open contract requests:** CR-008 (revised to python-docx, openpyxl and python-pptx; reviewed on the lane branch).
- **Not done:** LibreOffice path for legacy formats (waiting on operator approval); archives and email (rest of C-9); C-10 audio and video.
- **Environment note:** `python-pptx` 1.0.2 was installed into the machine's global Python for this work. CI will need it from the `office` extra once CR-008 lands.

## [2026-10-19] Lane L5 (Outputs and Stage 4), session 2: O-5 contradiction candidates, report R4

- **Branch / worktree:** `lane/L5-outputs` at `../content-processor-lane5` (fast-forwarded to `f56a95e`)
- **What landed:**
  - O-5, I4 contradiction candidates (`analysis/contradictions.py`): claims are paired by a pluggable `CandidateSource`; `LexicalCandidates` (inverted index on shared uncommon content terms) is the only source until I3 exists, and every row records `pair_basis`. Deterministic checks run first and are reported in `deterministic_flags`: numeric disagreement on the same subject and unit, negation flip, effective-date conflict. Context buckets: geography, audience, period or conditions differing → `context_differs` (shown separately, never as a contradiction); entity and product from heading paths are shown but do not decide the bucket (heuristic until O-7). Gap-aware `confidence_band` from badge, block status, layer, method and confidence (OCR lowers it, with the reason quoted). `pair_id` is stable across recrawls and input order; `load_reviewer_decisions` carries `confirmed` / `not_a_contradiction` / `context_differs` forward from an earlier R4 (CSV or JSONL) and rejects unknown values with the line number.
  - Claims (`analysis/claims.py`): claim-bearing sentences of corpus blocks (quantity, date, negation, condition, or a named audience or geography). Each quote is the exact `normalized_text[start:end]` slice of its block. Minimal deterministic `ClaimContext`: effective from/until dates, geography, audience, conditions, entity and product from headings. Quantities normalise currency symbols and codes (USD/GBP/EUR/INR incl. "Rs."), thousands separators and common units.
  - NLI judge (`analysis/nli.py`): `NliJudge` protocol; `DisabledJudge` is the default (`judge_status: disabled(no_local_provider)`, `contradiction_pct` blank). `GatewayJudge` speaks the OpenAI-compatible chat-completions format (Ollama `/v1` or the future gateway), refuses non-local endpoints while the cost ceiling is $0, judges both directions and keeps the higher contradiction probability, sends page text JSON-encoded as data, and records malformed or failed replies as errors instead of guessing. Judge output annotates only.
  - Report R4: all spec columns plus bucket, judge status/model, confidence reason, heading paths, block ids, layers, statuses, badges, capture times. Written by `write_run_reports` as `r4_contradictions.{csv,xlsx,jsonl}` with a `contradictions` section in the manifest (`REPORT_VERSION` l5-v2) and an explorer download link. `report` gains `--contradiction-threshold` (default 70), `--nli-endpoint`, `--nli-model`, `--decisions`.
  - Acceptance: a known-answer site (price, negation and effective-date contradictions; year- and geography-differing pairs; repeated, monthly-vs-annual and unrelated statements) yields exactly the expected pairs and buckets. Release gate "zero fabricated proofs" checked on the synthetic site and on the real fixture suite bundle.
- **Gate:** ruff format and check clean, mypy strict clean (83 files), 602 passed and 11 skipped, coverage 93.53%. New modules: claims 97%, contradictions 98%.
- **Limits, stated:** lexical pairing misses paraphrased contradictions with little shared vocabulary (I3 closes that); claims need a checkable signal, so a bare "includes phone support" sentence is not a claim until O-7 entity extraction; English cue words only for dates, negation and conditions; no NLI judge runs anywhere today.
- **Available to other lanes:** `find_contradictions`, `ContradictionConfig`, `CandidateSource`, `r4_table`, `load_reviewer_decisions` (`analysis/contradictions.py`); `claims_from_records`, `Claim` (`analysis/claims.py`); `GatewayJudge`, `DisabledJudge`, `is_local_endpoint` (`analysis/nli.py`).
- **Open contract requests:** none.
- **Hand-offs, outside L5's paths:**
  - **O-4 (I3, L5 next):** implement `CandidateSource` with `name = "semantic"` and pass it alongside `LexicalCandidates` in `sources=`.
  - **O-7:** populate `ContentBlock.claim_context` in extraction; `analysis/claims.py` should prefer it over its own heuristics once present.
  - **Gateway / A-7:** a paid (OpenRouter) judge needs the cost ledger first; until then `GatewayJudge` stays local-only.
  - **L0 (Postgres views, G-3 durable half):** R4 export view and a reviewer-decisions table keyed by `pair_id`.
- **Blocked on:** nothing. Spend: $0.

## [2026-10-19] Lane L2 (Browser state explorer), session 2: B-8 scroll accumulation and lazy load (+ B-10 nested surfaces)

- **Branch / worktree:** `lane/L2-explorer` at `../content-processor-lane2`, rebased on `f56a95e`.
- **What landed:**
  - `explore/scroll.py`: every scroll surface (the page plus up to 10 nested overflow containers) is a `scroll_surface` control with a terminal outcome. Overlapping 85% steps with a DOM settle after each; at the bottom a grace wait, and if content grew scrolling continues (lazy load). Outcomes: `visited:reached_end[:content_grew]`, `unvisited:scroll_budget_exhausted` (infinite feed, `max_scroll_passes`), `unvisited:time_budget_exhausted`, `unvisited:virtualized_content_recycled:N` (lines seen while scrolling that are gone afterwards: B-9 needed). Page and surfaces reset to the top before capture so viewport-relative visibility annotation does not mark above-the-fold content hidden.
  - `scroll` is now an implemented action: scroll runs first (lazy sections can bring controls), then disclosures and tabs. `ExplorationResult.scroll_surfaces` and the render worker's `explore` summary report passes, heights, growth and recycled lines per surface.
  - Render worker: a collapsed `<details>` block revealed and verified by an explored state is now verified too and points at the state block (`dedupe_of`), so the FAQ case reaches VERIFIED and exports do not repeat the text. This was masked while every page was PARTIAL for missing scroll.
  - Fixtures: `lazy_sections`, `infinite_scroll`, `nested_scroller`, `virtualized_list`.
- **Gate:** ruff, mypy --strict (81 files), 558 passed, 11 skipped, coverage 93.14%; 12 live explorer tests.
- **End to end (30 fixture URLs, default policy, real Chromium):** 15 VERIFIED_WITHIN_SCOPE (0 before B-8), 6 PARTIAL each with a named gap (infinite feed, virtualized list, pincode modal, gated whitepaper, soft 404, truncated), 5 BLOCKED, 2 EXCLUDED, 1 NEEDS_REVIEW (unavailable product), 1 FAILED (rate-limited).
- **Next for L2:** B-9 virtualization (accumulate recycled rows into blocks), B-11 frames, B-12 shadow DOM.

## [2026-10-19] Lane L2 (Browser state explorer), session 3: B-4, B-5, B-6, B-7, B-9, B-11, B-12

- **Branch / worktree:** `lane/L2-explorer` at `../content-processor-lane2`, on top of B-8 (`1940cce`).
- **What landed** (every `AllowedAction` is now implemented; `IMPLEMENTED_ACTIONS` is the full set):
  - **B-9 virtualized lists** (`explore/scroll.py`): rows are collected at every scroll step, before the list recycles them, keyed by `aria-rowindex`/`aria-posinset` or by text, and returned as one synthetic state (`virtualized:<label>`). Visited only when complete: all of `aria-rowcount`/`aria-setsize` when declared (`virtualized_rows_accumulated:300/300`), no holes in row indices when numbered, otherwise `...:total_unknown`. Out of passes: `unvisited:scroll_budget_exhausted:virtualized_rows=182/300`.
  - **B-4 carousels:** the "next" control of each carousel is clicked until the carousel text repeats (`cycled_after:N`) or the declared slide count is reached (`known_total:N`). Uses `select_tab` until CR-009 adds `advance_carousel`.
  - **B-5 hover/focus:** tooltip/popover triggers, hidden `aria-describedby` targets and hover menus in navigation are hovered and un-hovered (`hover_info`; navigation hover menus are excluded as `site_navigation_menu` when the action is not allowed, like nav accordions). `keyboard_traverse` tabs once through the focus order (`focus_cycle_complete:N`, `no_focusable_elements`, `tab_stops_exhausted:N`) and keeps text that only focus reveals.
  - **B-6 dialogs and consent:** informational dialog triggers are opened, captured and closed (Escape, then a close button; `dialog_left_open` if not). A consent overlay is handled first, before anything else, so it cannot intercept clicks: it is answered with "reject non-essential" when `open_informational_dialog` is allowed. The consent POST it fires is blocked by the guard and counted (`consent_requests_blocked=1`). A banner offering only "accept" is left in place (`consent_overlay_left_in_place:only_accept_offered`); we never accept on the site owner's or the user's behalf.
  - **B-7 pagination/load-more:** in-page "Load more"/"Show more" and button-style "Next" are clicked until the control disappears or is disabled (`exhausted_after:N`), stops adding content, or hits 25 clicks (`unvisited:iteration_budget_exhausted`). A POSTing load-more is refused (`unsafe_side_effect:POST ...`). `rel=next` and pager links that are real URLs are `excluded:separate_url_followup` and reported in `ExplorationResult.followup_urls` for the frontier.
  - **B-11 frames** (`explore/compose.py`): every child frame (same-origin, cross-origin, `srcdoc`, nested) is read and annotated inside the frame and becomes a state with `frame_path` = the iframe's resolved `src` (or `srcdoc:<n>`). The render worker stamps that path on the blocks' `provenance.frame_path`, which is what rule R3 matches. Hidden frames are `excluded:hidden_frame`, unreadable ones `unvisited:frame_unreadable:...`.
  - **B-12 shadow DOM:** `innerText` and `page.content()` both stop at shadow roots, so open roots are serialized from the composed tree (slots resolved, visibility marks inside roots; `ANNOTATE_JS` now walks open roots). Closed roots, including declarative `<template shadowrootmode="closed">`, are read through the DevTools protocol (`DOM.getDocument` with `pierce`), with exact text and no page global patched. This replaces screenshot OCR, which would be lossier; OCR of canvas stays with C-8. Without CDP the composition says `closed_roots_via_cdp: false`.
  - Render worker: frame states use their own base URL and `frame_path`. The `explore` summary carries `composition` (shadow roots, frames) and `followup_urls`.
  - Fixtures (new files only): `carousel`, `hover_tooltip`, `focus_reveal`, `modal_info`, `consent_banner`, `consent_accept_only`, `load_more`, `load_more_post`, `js_pagination`, `frames`, `frame_child`, `frame_partner`, `shadow_dom`, `virtualized_grid`.
- **Gate:** ruff and mypy --strict clean (83 files), 576 passed, 11 skipped, coverage 93%; 29 live explorer tests in real Chromium.
- **End to end (real Chromium, $0):**
  - B-8 fixture scope, default policy: unchanged at 15 VERIFIED_WITHIN_SCOPE; nothing regressed.
  - 13 new pages with every read-only action allowed: 13 VERIFIED_WITHIN_SCOPE (including the iframe child page). 1 PARTIAL, `load_more_post`, because its only "Show more" is a POST, which the guard refuses.
- **Found on the live John Hancock seed (B-8 code, default policy):** 34 of 34 rendered pages PARTIAL. Scroll is no longer the cause. The unvisited controls are 136 tabs and 134 accordions `never_visible`, 65 accordions `click_failed`, and 1 accordion `unsafe_side_effect:POST`. The `never_visible` controls look like responsive duplicates (mobile and desktop copies of the same tabs); `click_failed` is consistent with an overlay intercepting clicks, which the B-6 consent step now addresses.
- **Proposed:** CR-009 (carousel action, consent choice, default actions: operator decision).
- **Next for L2:** decide how a never-visible duplicate control is accounted for: it is not a gap when an equivalent visible control was visited and its content verified. B-13 canvas, B-14 to B-16.

## [2026-10-19] Lane L0 (Integrator), session 6: merged Lane 2 (B-4 to B-12)

- **Branch:** `main`, fast-forwarded to `f7a8506` from `lane/L2-explorer` (rebased twice, onto L3's C-9 merge and then onto L5's O-5; conflicts only in this file and `CONTRACT_REQUESTS.md`, append blocks, both kept).
- **What is now on `main`:** B-8 scroll and lazy load; B-9 virtualized rows; B-4 carousels; B-5 hover and keyboard; B-6 dialogs and consent (reject non-essential, never accept); B-7 pagination and load-more (GET only, real next URLs as follow-ups); B-11 frames (`frame_path` for R3); B-12 shadow DOM (open roots and closed roots via CDP). Live-site fixes: background telemetry not charged to controls, consent and media-player chrome excluded, intercepted clicks retried once, shadow text from laid-out text nodes.
- **Contract requests:** L2's interaction-policy request renumbered to **CR-009** (CR-008 is L3's Office dependencies). Still PROPOSED: `advance_carousel`, `consent_choice`, and the operator's decision on the default actions.
- **Gate on the merged tree:** ruff and mypy --strict clean (87 files), 661 passed, 11 skipped, coverage 93%.
- **Live John Hancock (all read-only actions, real Chrome, $0):** discovery closes on every rendered page (0 unvisited controls, down from 270 never-visible and 65 failed clicks). The two pages checked after the shadow-text fix reached VERIFIED_WITHIN_SCOPE; images remain PARTIAL until an OCR engine is installed (C-8).
- **Still open:** K1 (`CP_DATABASE_URL`); CR-009 decision; a full 34-URL John Hancock run on the merged code.

## [2026-10-19] Platform hardening (L0 + L6 items), session 1: K7, K8, K9

- **Branch / worktree:** `fix/k7-k8-k9` at `../content-processor-hardening` (based on `8cd65f3`)
- **What landed:**
  - K7, crawl-delay persistence: `governor/robots.py` adds `crawl_delay_for(body, token)` (same parser as `evaluate_robots`, bot group then `*`, capped at `MAX_CRAWL_DELAY_S` = 30 s). `RobotsCache` gains optional `crawl_delay_s`; the fetch worker fills it for `RankunoContentBot`. `TaskLedger.set_robots` now writes `crawl_delay_ms` (the existing SQL raises `base_delay_ms`, which the slot lease uses) and `get_robots` reads it back. `MemoryLedger` drops its own line parser, which ignored user-agent groups, for the shared helper.
  - K8, reaper outcomes: `SQL_REAP` returns payload and last error. `TaskLedger.reap` appends a task event per reclaimed lease (`LEASED → PENDING` or `→ QUARANTINED`) and closes out every quarantined task through `ledger/outcomes.py`: a FETCH/UNLOCK resource still in flight (or never created) becomes `FAILED poison:<kind>:<last error>` with an ACQUISITION_FAILED gap; a later-stage poison (render, docs) keeps the acquired status and records a PARTIAL gap, so the URL reads PARTIAL instead of vanishing.
  - K9, run budgets: `scheduler/budget.py` meters runtime, requests (fetcher and renderer calls, robots included), bytes received (fetch bodies, rendered DOM and screenshots) and new evidence bytes, through wrappers around the fetchers, renderer and evidence store (no worker changes; `headers` pass through for conditional GET). `execute_run(meter=…)` drives each stage one task at a time, checks the budget before every task and caps waits at the remaining runtime. On exhaustion it cancels the run's pending tasks (new port method `cancel_open_tasks`, both ledgers), ends each affected resource `BUDGET_EXHAUSTED` with a BUDGET_STOPPED gap, appends a run event, and reports `stop_reason: budget_exhausted`, `budget_exhausted`, `budget_usage`, `budget_closed_out`. `cli._drive` always meters with the scope's budgets, seeding bytes from the run's recorded resources.
  - `ledger/keys.resource_id_for` is now the one resource-id formula (fetch worker, reaper and budget close-out use it).
- **Verified:** real `dryrun --no-render` on the fixture scope with `max_requests: 5` stopped at exactly 5 requests (2 robots + 3 pages) and closed out the other 20 URLs; verdicts 21 PARTIAL, 2 BLOCKED, none RUNNING.
- **Gate:** ruff format and check clean, mypy strict clean (83 files), 596 passed and 11 skipped, coverage 93.20%. New modules: budget 99%, outcomes 96%, runner 99%.
- **Limits, stated:** the durable reaper and `cancel_open_tasks` SQL are tested with a mocked cursor only (K1: still no reachable Postgres). Budgets apply per invocation of the engine: a resumed durable `work` restarts runtime, request and evidence counts (bytes are seeded from the ledger); cumulative run-level counters would need columns on `runs`. The in-memory ledger has no reaper (in-process leases cannot outlive their worker), so K8 is durable-only. A task in flight can overshoot a byte budget by its own size.
- **Port change (additive):** `LedgerPort.cancel_open_tasks(run_id, kinds, reason) -> list[Task]`, implemented in `MemoryLedger` and `TaskLedger`; `RobotsCache.crawl_delay_s: float | None = None`.
- **Blocked on:** K1 for the live Postgres proof. Spend: $0.

## [2026-10-19] Lane L0 (Integrator), session 7: CR-009 item 3 and two operator decisions

- **Branch:** `main`
- **What landed:**
  - CR-009 item 3 (operator decision): `_default_actions()` now allows `scroll`, `expand_disclosure`, `select_tab`, `hover_info`, `open_informational_dialog` and `paginate_get`; `keyboard_traverse` stays opt-in. All run under the GET-only, no-navigation guard. Side effect, stated: with `open_informational_dialog` allowed, a consent overlay is answered "reject non-essential" by default (never accepted). Two explorer tests that used the default policy to mean "not allowed" now declare `NO_DIALOGS_OR_PAGING` explicitly; a contract test pins the new default.
  - Decision recorded, no code change: bot-sensor and consent beacons stay blocked (every non-GET request, telemetry included). A telemetry exception may only be considered as part of a host's T5 approval under the paid-tier procedure; nothing allows it today. Both decisions are in `ARCHITECTURE.md` §19.
- **Gate:** ruff format and check clean, mypy strict clean (89 files), all tests pass (684 + 1 new, 11 skipped), coverage 92.91%.
- **Still open:** CR-009 items 1 (`advance_carousel`) and 2 (`consent_choice`) for L2 to land with the explorer wiring; K1.

## [2026-10-19] Lane L4 (Verification brain), session 2: R6, R7, R8, R11, R12, R13, V-6, evidence integrity, V-8 inventory

- **Branch / worktree:** `l4/rules-r6-r13` at `../content-processor-rules2` (based on `10360d1`)
- **What landed:**
  - `verify/rules_extended.py`, rules as in ARCHITECTURE.md §10, each a pure tested function with stable gap ids: **R6** screenshot text regions map to some block of the page (LOW_CONFIDENCE otherwise); **R7** obfuscated-font output (private-use glyphs, case 114) and source main text missing from the render (FIDELITY_DISAGREEMENT; numbers and negations stay with R9 on `lane/L4-rules`); **R8** PDF image regions without OCR (LOW_CONFIDENCE) and native-vs-OCR disagreement (FIDELITY_DISAGREEMENT, case 44); **R11** main-region words far below the host baseline (SHADOW_BLOCK_SUSPECTED; only with ≥10 pages and a median ≥100 words, and only when nothing else explains the page; canary A-9 not built); **R12** page exploration/scroll budgets (LIMIT_HIT); **R13** icon-artifact or third-party-chrome text in the main corpus (FIDELITY_DISAGREEMENT); **V-6** pages that never settled (VOLATILE); **evidence integrity** payload re-hashed and every cited evidence object present (FIDELITY_DISAGREEMENT / LOW_CONFIDENCE).
  - `verify/detectors.py`: `TextRegionDetector` protocol, `OcrRegionDetector` (Tesseract `psm 11` via `adapters.ocr.run_ocr`), `default_detector()`, `screenshot_sha()`, `pdf_regions_from_evidence()`, `evidence_problems()`. Wired through `assemble_url_record` / `url_records_for_run` (new keyword-only inputs) and `assemble_run_records` (passes the evidence store and the default detector).
  - OCR policy (operator decision 2026-10-19, "record, don't block"): no Tesseract on this machine, so R6 and R8 emit no gaps and records carry `r6:detector_unavailable` / `r8:ocr_unavailable:regions=N`; they switch on when an engine is installed (C-1).
  - New gate `soft_paywall` (case 141) in `governor/gates.py`: a subscribe/limit overlay → ACCESS_BLOCKED, PARTIAL (BLOCKED with no teaser).
  - V-8: `fixtures/case_inventory.json` accounts for all 222 cases: 32 by manifest fixture, 79 by test, 75 open (locally testable, not yet covered, each with the engine item it waits for), 32 not locally testable (live vendors, Stage 3 media, policy), 4 source missing (K21). Five new fixtures: malformed markup (01), character references (02), soft redirect to the homepage (98, new `/` and `/discontinued-product` routes), obfuscated font (114), soft paywall (141). Manifest tests now check the inventory covers 1–222 once, cites only existing fixtures and test files, and that every manifest fixture is cited.
- **Live John Hancock run on `main` (10360d1, 2026-10-19, $0, 25 min):** 34 supplied URLs → 32 VERIFIED_WITHIN_SCOPE, 2 NEEDS_REVIEW (`/errors` and `/jhvnewsletter`: live "Lorem ipsum" placeholder text). Before L2's latest work the same seed was 34 of 34 PARTIAL. Depth-1 follow-ups: 4 images PARTIAL (no OCR engine), 1 empty response excluded. New defects from the run: K19 (R4 false positives) and K20 (evidence volume). Outputs in `outputs/jh-2026-10-19/` (git-ignored).
- **Gate:** ruff format and check clean, mypy strict clean (91 files), 714 passed and 11 skipped, coverage 93.31%. Release gate V-7 passes with evidence integrity, R7, R13 and V-6 active and zero false VERIFIED_WITHIN_SCOPE across the 28 fixtures; `test_cli` fixture-scope counts updated (23 → 28 URLs).
- **Hand-offs:** L5 for K19; L2 + L6 for K20; L0 for K21 (add the unified investigation to `reports/`). `lane/L4-rules` (R4, R5, R9) touches `verify/record.py` near the same lines: whichever merges second resolves it.

## [2026-10-19] Lane L4 (Verification brain), session 2: R9 critical tokens (V-4), case 90 extraction fixes, responsive duplicate controls

- **Branch / worktree:** `lane/L4-rules` at `../content-processor-rules` (on `main` `eaeeff3`).
- **What landed:**
  - **R9 / V-4** (`verify/rules.py: rule_r9_critical_tokens`, called by `assemble_url_record`): for every corpus block, the critical tokens (numbers including super/subscript digits, currency signs and codes, negations including `n't`, `% ‰ * † ‡ § ¶ [n]`) of `raw_text` after the allowed normalization (NFC, invisible code points, spacing) must equal those of `normalized_text`. It also flags a number cut across two adjacent blocks (`$1,` then `200 …`, same state and heading). Gaps are FIDELITY_DISAGREEMENT (NEEDS_REVIEW), keyed per block, at most 25 per resource, e.g. `critical_tokens_changed:lost=120¹;added=1201`. It is the safety net for every adapter (PDF, OCR, Office, HTML).
  - **Case 90 extraction fixes** (`adapters/html_source.py`, an L1/extractor file, changed here because the bug is a critical-token loss; integrator please note):
    - `<sup>`/`<sub>` made only of digits and signs become Unicode super/subscripts: `$120<sup>1</sup>` was extracted as `$1201` (the footnote marker merged into the price) and is now `$120¹`; `m²`, `CO₂`. Other `<sup>` text (`TM`, `*`) is unchanged.
    - Inline elements (`span`, `a`, `b`, …) no longer end a block: `walk()` flushed after every element, so `Pay $1<span>,</span>200` became two blocks and every sentence with a link was cut in two. Block elements and icon glyphs still separate blocks.
    - `match_key()` (normalize, fold super/subscripts, casefold) is now the single matcher used by `verify/reconcile.py` and the render worker, so a `$120¹` block still verifies against `innerText` "$1201".
  - **Responsive duplicates** (`explore/model.py: mark_responsive_duplicates`, called at the end of `StateExplorer.explore`; option (a) agreed with the L2 session): a `never_visible` control is `excluded` as `responsive_duplicate_of:<control_id>` when a control of the same kind and normalized non-empty label was visited or initially open. Without such a twin it stays an unvisited gap. Excluded controls are never gaps, so R4 needs no special case. Assumption, not a guarantee: same kind and label is taken to mean the same panel content; a mobile copy can differ. Its text is still extracted as hidden-layer blocks, so nothing is lost, but that panel is not verified as a visible state.
- **R4 to R6, status:** R4 (control coverage) is enforced by the render worker's per-control `UNVISITED_STATE` gaps plus the exclusions above; no separate rule was added, to avoid double gaps. R5 (virtualization reconcile) is enforced by L2's explorer outcomes (`virtualized_rows_accumulated:N/M`, unvisited when incomplete). **R6 (visual text regions) is not implemented**: the specified detector is Tesseract `psm 11`, which is not installed (C-1).
- **Checked:** fixture dry run (`local_fixtures.json`, real Chromium), `main` against this branch: identical badges URL by URL (10 VERIFIED_WITHIN_SCOPE, 5 BLOCKED, 4 PARTIAL, 1 NEEDS_REVIEW, 2 EXCLUDED, 1 FAILED); no R9 gap on any fixture.
- **Gate:** ruff format and check clean, mypy strict clean (87 files), 686 passed and 11 skipped, coverage 93.05%; live explorer tests included.
- **Merge notes:**
  - Block segmentation changes on every HTML page, so `block_key` and the recrawl `content_fingerprint` change too: a recrawl against a baseline from before this change marks every URL `changed` once.
  - Not covered by R9: `<del>`/`<s>` struck-out prices read as current text (`Price $200 $150`); needs a layer or marker decision.
- **Open contract requests:** none.

## [2026-10-20] Fix session: six audit bugs, R9 merged

- **Branch / worktree:** `fix/six-bugs` at `../content-processor-bugfix` (based on `6df45bb`; `lane/L4-rules` with R9 was fast-forwarded into `main` first, after its own full gate: 739 passed, 11 skipped, 93.38%).
- **What landed** (from the 2026-10-20 codebase audit; each has a regression test in `test_bugfixes.py`):
  1. **False "verified no content" on OCR failure:** `adapters/image.py` turned an OCR exception into empty text and then `VERIFIED_NONCONTENT`. It is now PARTIAL with an `ocr_error:…` gap (retry eligible). `adapters/ocr.py` reports confidence 0, not 1, when nothing was read.
  2. **Brotli advertised but not decodable:** T1 sent `Accept-Encoding: gzip, deflate, br` with no brotli decoder installed, so a brotli answer reached sniffing and extraction undecoded. `http_client.accept_encoding()` advertises `br` only when `brotli` or `brotlicffi` is importable.
  3. **Open circuit never closed in durable runs:** the Postgres lease skipped `OPEN` hosts forever and the fetch worker never called `can_attempt()`. The lease now admits a host once `CIRCUIT_OPEN_FOR` (15 min) has passed since it opened (bound parameter `circuit_open_s`), and the fetch worker probes it as HALF_OPEN like the render worker.
  4. **Paid-tier approval bypass:** a scope with a T4/T5 ceiling gave every host that ceiling, so escalation entered a paid tier with no approval. `governor/escalation.initial_host_ceiling()` starts every host at most at T3 (`FREE_TIER_CEILING`) in both ledgers; going past it always returns NEEDS_APPROVAL. Raising one host is the future A-8 approval.
  5. **DNS rebinding window (case 93):** the egress check resolved the name, then httpx resolved it again at connect. `cli.EgressGuard` remembers the addresses each check approved; T1 (`HttpFetcher(pinner=…)`) connects through `PinningBackend` to those addresses only (tried in order; unpinned hosts refused). URL, `Host`, SNI and certificate checks keep the real name. Proven end to end against a local server with a name that resolves nowhere.
  6. **K19 contradiction noise:** "as of <date>" and "year/period … through <date>" are points in time (different snapshots → context differs, no date flag); claims naming disjoint years are different editions; short all-capital codes (tickers, plan codes) are entities, and a claim without one inherits its page's repeated codes. John Hancock re-run on the saved records: **0 contradiction candidates** (was 17 false), 5 pairs in `context_differs`.
- **Limits, stated:** pinning covers T1 only; the T2 fetcher (curl_cffi) and the browser still resolve names themselves and rely on their per-request egress checks. Page-level codes can pick up boilerplate tickers (the Oura pair landed in `context_differs` partly because of footer exchange codes). The circuit fix and host-ceiling change on Postgres are tested with a mocked cursor only (K1).
- **Gate:** ruff format and check clean, mypy strict clean (91 files), 761 passed and 11 skipped, coverage 93.54%.

## [2026-10-20] Lane L8 (Analyst UI), session 1: read-only API and React web app

- **Branch / worktree:** `lane/L8-ui` at `../content-processor-ui` (based on `0964a36`)
- **What landed:**
  - `src/modules/content_processor/webapi/` (new): FastAPI app, GET only, bound to 127.0.0.1, Host header limited to 127.0.0.1/localhost, strict CSP. Endpoints: `/api/runs`, `/api/runs/{run}/overview`, `/pages` (server-side paging, filters `status`/`host`/`format`/`issue`/`has_issues`/`q`, sort, facets), `/pages/{page_key}`, `/pages/{page_key}/raw`, `/duplicates`, `/contradictions`, `/review`, `/downloads`, `/downloads/{file}`. A run is `<name>.records.jsonl`, cached by mtime and size; unreadable lines are skipped and counted. R2/R4/review come from `<name>_reports/` when the manifest's run id matches, else they are built with `analysis/overlap`, `analysis/contradictions` and `export/reports` (no reimplementation). Downloads only serve names in the reports folder listing. It also serves the built app with route fallback.
  - `webui/` (new): Vite + React 18 + TypeScript strict + antd 5 + recharts. Screens: all runs, run overview (statistic cards linking to filtered pages, status donut, top reasons with what to do, formats, sites, access, changes), pages table, page detail (Content / Issues / Page facts / Technical), duplicate content (groups and pair drawer), contradictions, needs your review, downloads, dark mode. `vocabulary.ts` holds every label and turns every gap reason found in the six local runs (49 distinct reasons, 29 patterns) into a sentence; engine names show only with "Show technical names". State: TanStack Query (key factory, keepPreviousData, hover prefetch, polling only while pages are RUNNING, no retry on 4xx), URL search params through one typed hook, Zustand persist for preferences only. Every data view goes through `QueryState`; lazy routes in an error boundary; page text rendered as text only (lint rule bans `dangerouslySetInnerHTML`).
  - `docs/UI_PLAN.md`: plan plus section 6, the decisions taken while building (derived "Some text couldn't be double-checked" finding for NEEDS_REVIEW pages with no gap, title rule, page key, saved-vs-computed reports, five content-area toggles, grouped Issues cards, contradiction filter by likelihood).
- **Run it:** `cd webui && npm ci && npm run build`, then from the repo root `python -m src.modules.content_processor.webapi --exports <folder with *.records.jsonl> --port 8780 --static webui/dist` and open http://127.0.0.1:8780/. Development: same API command without `--static`, plus `npm run dev` in `webui/` (proxies `/api`).
- **Gate:** ruff format and check clean, mypy strict clean (97 files), 775 passed and 11 skipped, coverage 93.81% (webapi 95-100%). Frontend: `npm run typecheck`, `npm run lint` clean; `npm test` 46 passed (4 files); `npm run build` ok.
- **Checked end to end:** served against the live John Hancock outputs (`jh`, `jh2`, `jh3`) and the fixture runs; screenshots of every screen, in light and dark, reviewed and fixed (table overflow, contradiction flag splitting, facts layout).
- **Open decisions:** whether the engine should record a gap when fidelity is EXTRACTED_UNVERIFIED (the UI derives one today); whether reviewer decisions should be editable in the UI (needs a write endpoint; the server is read-only by design); antd chunk is 1.1 MB minified (fine on localhost, worth splitting if served remotely).
- **Open contract requests:** none. No file outside the lane was changed.

## [2026-10-20] C-1 Tesseract installed and wired; CI pipeline; R6/R13 calibrated on a live site

- **Branch / worktree:** `feat/ocr-ci` at `../content-processor-ocrci` (based on `0964a36`)
- **Machine setup (operator-approved):** Tesseract 5.4.0 (UB-Mannheim build via winget) in `C:\Program Files\Tesseract-OCR`; the 25 core packs from ARCHITECTURE.md §6.1 (fast variant, 66 MB) in the per-user folder `%LOCALAPPDATA%\content-processor\tessdata` (no admin rights needed). Best-variant packs not installed yet: the second-opinion pass that would use them is not wired (C-7).
- **What landed:**
  - `adapters/ocr.py`: `tesseract_cmd()` (env `CP_TESSERACT_CMD`, PATH, then the standard Windows/Linux/macOS locations; the Windows installer does not add itself to PATH), `tessdata_dir()` (env `CP_TESSDATA_DIR`, then the per-user folder), `--tessdata-dir` passed on every call, `ocr_engine_info()` for run reports.
  - `verify/detectors.py`: Tesseract returns words; `group_lines()` merges them into lines (row bands by vertical centre, split on large gaps) so R6 judges phrases.
  - R6 calibrated on live John Hancock pages: OCR-clipped words match by shared prefix, numbers and short words are ignored, only lines of 3+ words read at confidence ≥ 0.8 are judged, consent-banner text is skipped (the base screenshot is taken before the explorer answers the banner), and a review gap needs 2+ unmapped lines (a single line is a `r6:unmapped_line:` warning). R13's chrome-leak check now looks at main-role blocks only and ignores accessibility hints such as "(opens in a new tab)".
  - Images, PDFs and Office files count as discovery-closed (`static_document`): R1 no longer gives them a permanent DISCOVERY_OPEN gap.
  - CI: `.github/workflows/ci.yml` (GitHub Actions). Job `gate` (required): Tesseract + Chromium + all extras, ruff format/check, mypy --strict, the V-7 release gate as its own step, pytest with the 85% floor. Job `ledger-integration`: the 11 Postgres integration/chaos tests against a `postgres:16` service, `continue-on-error` until seen green once (K1). `pytest-asyncio` added to the `dev` extra (the suite needs it; a clean install would have failed).
- **Live John Hancock run, OCR on, final rules (34 supplied URLs, $0):** 3 VERIFIED_WITHIN_SCOPE, 31 NEEDS_REVIEW. R6 and R13 raise no gaps (one R6 warning). Causes: 21 pages R7 source text not rendered (K23, real capture gaps), 8 pages one unverified glued block (K22, regression from the R9 html_source change), 2 pages live "Lorem ipsum". Follow-up images: OCR now runs; logos and photos give low-confidence readings (PARTIAL with a LOW_CONFIDENCE gap). Before calibration the same run gave 0 of 34 because of R6/R13 noise.
- **Gate:** see the merge commit.
- **Open:** K22 (extractor owner; the R9 session has ended), K23 (L2), push to GitHub so CI runs.

## [2026-10-20] Run-named report files (L5 writer, L6 callers, L8 API and web app)

- **Branch / worktree:** `feat/run-named-reports` at `../content-processor-runnames` (on `lane/L8-ui` `c1412d5`, which `main` now points to).
- **Why:** the user asked for each run's reports to be separate. Each run already had its own `<name>_reports/` folder, but every run's files had the same names (`r1_url_content.xlsx`, …), so downloads from different runs collided and were indistinguishable.
- **What landed:**
  - `export/bundle.py`: every file is `<run>_<report>`, e.g. `jh2_r1_url_content.xlsx`, `jh2_explorer.html`, `jh2_manifest.json` (`bundle_file_name`, `run_file_prefix` keeps letters, digits, `.`, `_`, `-`). The run name comes from `run_name=`, else the folder name without `_reports`, else the run id. The explorer's download links, `manifest.files` and the new `manifest.run_name` use the prefixed names. `REPORT_VERSION` is now `l5-v3`. `report_file(folder, run, base)` finds a report under its prefixed name, else the plain name (folders written before this change).
  - Callers: `dryrun`/`run`/`work` pass their `--out` stem (the name the analyst UI shows). `report` takes the records file's name (`x.records.jsonl` → `x`) or `--run-name`.
  - `webapi`: report readers and the manifest go through `report_file`, so existing exports keep working. A plain-named file from an older run is downloaded as `<run>_<name>`.
  - `webui`: file titles match `<run>_<report>` or `<report>`; the Downloads sort recognises `<run>_manifest.json`.
- **Checked:** two dry runs (`alpha`, `beta`) into one exports folder give two separately named sets; the analyst UI (my build, port 8781) lists `beta_*` files with their titles and serves `content-disposition: filename="beta_r1_url_content.xlsx"`; no page errors.
- **Gate:** ruff and mypy --strict clean (97 files), 778 passed and 11 skipped, coverage 93.82%; web app typecheck, lint, 46 vitest tests and build pass.
- **Note for anyone reading report folders by hand or from scripts:** use `report_file()` or the `manifest.json` `files` list; fixed names only exist in folders written before this change.
- **Not done:** `docs/guide/GUIDE.md` (CLAUDE.md asks for a guide update on user-visible changes) is not on any branch yet, only uncommitted in the main worktree, so this change still needs a line in the guide once it is committed.

## [2026-10-21] Struck-out text kept and marked (operator decision: inline marker)

- **Branch / worktree:** `feat/struck-text` at `../content-processor-strike` (on `main` `ea12e07`, after K22). `adapters/html_source.py` handed over by the Lane 2 session.
- **Decision (operator, 2026-10-21):** `<del>$200</del> $150` is recorded as `Price ~~$200~~ $150`: both amounts kept, the old one marked.
- **What landed:**
  - `adapters/html_source.py`: `<del>`, `<s>`, `<strike>` are walked into a side buffer (so superscripts and nested inline markup keep their handling: `~~$1¹~~`) and appended as `~~…~~`, whitespace kept outside the markers; empty struck tags add nothing. Helpers `STRUCK_MARKER`, `without_struck_markers()` (what the browser shows) and `without_struck_text()` (what the page currently states). `match_key()` drops the markers, so a marked block verifies against `innerText`.
  - `verify/rules_extended.py`: R6/R7/R13 compare through `match_key()` (they used their own `normalize+casefold`, which would not have removed the markers; flagged by the Lane 2 session).
  - `analysis/claims.py`: quantities and negations are read from the sentence without struck passages; the quote stays verbatim. A struck old price no longer produces a numeric contradiction; a struck "not" no longer negates the claim.
- **Gate:** ruff format and check clean, mypy strict clean (107 files), 843 passed and 11 skipped, coverage 94.07%.
- **Merge note:** `docs/guide/GUIDE.md` and the binary `Content_Processor_Guide.docx` are also changed by `fix/k20-evidence-volume`; whichever merges second keeps both GUIDE.md entries and re-runs `python scripts/build_guide.py` instead of merging the .docx.
- **Not done:** the analyst web app shows `~~` as text; rendering it as a strike-through is a small `webui` follow-up.

## [2026-10-21] K20 evidence volume: packed evidence store

- **Branch / worktree:** `fix/k20-evidence-volume` at `../content-processor-k20` (on `main` `d598764`). Coordinated with the Lane 2 session (content-processor-08), which is doing K22 in `adapters/html_source.py`; no overlap.
- **Measured first:** on the live John Hancock evidence (799 objects, 1,182 MB), HTML snapshots are 98 % of the bytes (440 files, 2.6 MB each). 88 % of a snapshot is `<style>` blocks of base64 `data:` URIs (web fonts, images) that repeat in every explored state and across pages, so per-file gzip gives only 2× while 12 consecutive snapshots compressed together give 37×.
- **What landed:**
  - `integrations/evidence_store.py`: text payloads are packed. `data:` URIs and `<style>` bodies of `CHUNK_MIN_BYTES` (4 KB) or more become chunks stored once per tenant (`<root>/<tenant>/chunks/<aa>/<sha>.z`, zlib); the rest is a zlib container `raw/<aa>/<bb>/<sha>.cpz`. Objects keep their SHA-256 of the exact original bytes and `get()` returns those bytes. Already-compressed media (images, PDF, archives, Office, fonts, audio, video) and anything that would not shrink by 10 % stay raw. Chunks are written before the container, both atomically. Raw objects from earlier versions read as before. `EvidenceRef.stored_bytes` reports bytes added on disk (0 when reused); `LocalEvidenceStore.repack(sha)` converts one raw object after checking its hash and verifying the read-back.
  - `scheduler/budget.py`: `max_evidence_bytes` meters `stored_bytes` (bytes on disk) instead of the raw size.
  - `scripts/repack_evidence.py` (`--root`, `--tenant`, `--dry-run`): repacks existing evidence in place, safe to stop and re-run.
- **Result on the live evidence:** 1,182 MB → 65 MB on disk (18×; HTML about 27×, the rest is JPEG kept as is), 695 objects packed, 45 shared chunks, 75 MB/s write, all 799 objects read back byte-identical. About 1.7 MB per URL instead of 30 MB, so a 5,000-URL run needs roughly 8–9 GB instead of exhausting the 20 GB budget at ~750 URLs.
- **Checked:** fixture dry run with real Chromium on `main` and on this branch: identical badges URL by URL (12 VERIFIED_WITHIN_SCOPE, 3 NEEDS_REVIEW, 5 PARTIAL, 5 BLOCKED, 2 EXCLUDED, 1 FAILED).
- **Gate:** ruff format and check clean, mypy strict clean (106 files), 841 passed and 11 skipped, coverage 94.08% (`evidence_store.py` 96%).
- **Guide:** `docs/guide/GUIDE.md` and `Content_Processor_Guide.docx` updated (compact evidence, plus the missing entry for run-named report files from 2026-10-20).
- **Limits, stated:** deleting evidence (retention, P-5) must now count chunk references, since chunks are shared between objects; there is no garbage collection of unreferenced chunks yet. MinIO/S3 (P-5) needs the same packed layout.
- **Operator action, optional:** `python scripts/repack_evidence.py --root evidence` (and for other evidence roots) reclaims about 94 % of existing evidence disk.

## [2026-10-21] Lane L2 + L0 (Integrator): K22, shadow-DOM controls, live John Hancock fixes, project report

- **Merged to `main` (each fast-forwarded after a full green gate):**
  - `a75f43b` K22: `<br>`, custom elements and missing block tags end a text block (R9 had let inline runs continue across them).
  - `8df82a3`, `d493e93`, `ea12e07` Lane 2: keyboard focus into shadow roots (another session); controls inside open shadow roots discovered and opened (`explore/deep.py` rewrites every page script so document queries and `closest()` cross open shadow roots); frames replaced while being read are read again.
  - `390af86` CSS line breaks (`data-cp-block` on inline-by-default elements laid out as blocks, honoured by `html_source`, agreed with the extractor owner), browser-like shadow text joining, 90 s explore budget, one settle-window extension when the page is already quiet.
  - `ad90402` EgressGuard: a DNS failure for a host approved in the last 5 minutes reuses that approval (a failure returns no address); one resolver hiccup had aborted a navigation, opened the host circuit and held 31 of 34 pages.
  - `817d180` Base DOM and visible text read in one browser call (a chat bubble popped up between two reads); settle counts only text-capable elements (tag managers keep adding scripts and pixels); the explorer gets the page time that is left and records unexpected exceptions instead of losing its controls.
  - `42c8a03` R7: a source block also counts as rendered when ≥ 90% of its 5-word runs appear in the render (render-time insertions such as an injected "(opens in a new tab)").
- **Live John Hancock (34 supplied URLs, every read-only action, `--group-assets`, $0):** 20/34 on `ea12e07` → 26/34 on `ad90402` → **31/34 on `817d180`**. Misses on `817d180`: `/errors` and `/jhvnewsletter` (real "Lorem ipsum" placeholder pages, correctly NEEDS_REVIEW, so 32 is the ceiling) and the Oura press release, fixed by `42c8a03` (R7 on the saved Oura data raises no gap). The confirming run on `42c8a03` was cut short twice by this machine's DNS (`ERR_NAME_NOT_RESOLVED` from Chrome itself) opening the host circuit; all six content pages it rendered verified. Re-run with `--max-wait 300` so a held host resumes.
- **New (this branch):** project report `<run>_project_report.xlsx` in every report bundle (`export/project_report.py`): sheet "Pages" (URL, whole content, fully extracted, extracted % = confirmed words / captured words, confidence % = word-weighted block confidence, plain "what is missing"), sheet "Matching content" (every URL × every other URL sharing ≥ 10% of its text by exact 5-word runs, boilerplate roles excluded, directional containment as in STAGE4 §3, with an example passage), sheet "How to read". Web app Downloads title added. Guide updated and rebuilt.
- **Confirmed:** clean run on `42c8a03` with `--max-wait 300`: **32/34 VERIFIED_WITHIN_SCOPE**, the other 2 the placeholder pages (DEGRADED_VARIANT, NEEDS_REVIEW); drained, circuit closed, 34/34 rendered. Evidence 1,619 MB because that commit predates K20.
- **Open:** this machine's intermittent DNS (use `--max-wait 300` for live runs until it is fixed).

## [2026-10-21] Reviewer decisions saved in the analyst web app (operator decision: local writes)

- **Branch / worktree:** `feat/ui-decisions` at `../content-processor-decisions` (on `main` `4744899`).
- **Decision (operator, 2026-10-21):** reviewers save decisions directly in the web app; the server stays local-only.
- **What landed:**
  - `export/decisions.py`: one append-only log per run, `<run>.decisions.jsonl` next to `<run>.records.jsonl`. Each line: time, reviewer, kind (`contradiction` keyed by R4 `pair_id`, `review` keyed by URL), decision, note. Latest line wins; an empty decision and note clears. Contradiction decisions use the R4 vocabulary (`confirmed`, `not_a_contradiction`, `context_differs`); review actions are `REVIEW_ACTIONS` (resolved, accept_as_is, retry_later, obtain_manually, approve_higher_tier, exclude). Damaged lines are skipped. The log is the audit trail.
  - `webapi`: the server stays read-only except `POST /api/runs/{run}/decisions`, accepted only as JSON with `X-Requested-With: content-processor` (a cross-site page cannot send it without a CORS grant, which is never given) and, when an Origin is sent, only from a local host. The item must exist in the run. `GET /api/runs/{run}/decisions` returns the history, newest first. `GET …/contradictions` and `…/review` overlay saved decisions with `decided_by` / `decided_at`.
  - Carry-forward: `report` reads `<records stem>.decisions.jsonl` (merged with any `--decisions` file, saved decisions winning) and dry runs read `<out stem>.decisions.jsonl`. R4 `reviewer_decision` and the review queue's `operator_action` / `operator_note` come pre-filled, in CSV, XLSX and JSONL.
  - `webui`: "Saving decisions as [name]" field (remembered in the browser; controls are disabled without it), a decision picker on each contradiction, an action picker plus note editor on each review row, "Saved by … , time" under each saved decision.
- **Checked in a real browser** (built app, temporary exports): controls disabled without a name; a contradiction decision and a review action plus note saved, shown with the reviewer and time, still there after reload; three lines in the log.
- **Gate:** ruff format and check clean, mypy strict clean (108 files), 890 passed and 11 skipped, coverage 94.23% (`decisions.py` 98%); web app typecheck, lint, 46 vitest tests and build pass.
- **Limits, stated:** no login: the reviewer name is self-declared (fine for one analyst machine; a shared deployment needs real accounts). Decisions follow the R4 `pair_id` and the page URL; a pair that disappears in a later run keeps its line in the log but is no longer shown.

## [2026-10-22] Paid tiers: Bright Data T4/T5, per-host approvals, spend guard (A-6, A-7, A-8, K10)

- **Branch / worktree:** `feat/paid-tiers` at `../content-processor-paid` (on `main` `b15fce2`).
- **Decision (operator, 2026-10-21):** build the approval path, paid tiers stay off by default. Built to ARCHITECTURE.md §19; the playbook's referenced §10 (credit governance) did not exist and is now written to match the code.
- **What landed:**
  - `integrations/proxy_provider.py`: `ProxiedFetcher` (T4) runs the shared chain through a Bright Data zone (`brd-customer-<id>-zone-<zone>-country-<cc>-session-<id>` at `brd.superproxy.io:33335`), one client and sticky session per host per run, TLS verification on (optional vendor CA). `worst_case_usd()` / `cost_usd()` price by volume. `integrations/unblocker_provider.py`: `UnlockerFetcher` (T5, Web Unlocker) priced per request; refuses to start without Bright Data's CA certificate.
  - `governor/spend.py`: `Approval` (tenant, host, T4/T5, cap per run, operator, optional expiry, revocation) and `SpendEntry` in append-only logs under `CP_SPEND_DIR` (git-ignored `spend/`); `SpendGuard` refuses, in order, on kill switch, unset monthly cap, missing or narrower approval, then the worst case against run (min of setting and scope `cost_ceiling_usd`), daily, monthly, host and per-URL caps; `GuardedFetcher` asks before (nothing is sent on refusal) and charges after; `apply_approval()` raises one host's ceiling (never past the scope's); `requeue_blocked()` re-queues an approved host's BLOCKED URLs at the approved tier.
  - `governor/paid.py`: `paid_tier_status()` (recorded as `paid_tiers` on dry-run and durable reports), `build_paid_fetchers()` (only when every switch allows), `cli approve`, `cli revoke`, `cli spend`.
  - `workers/fetch_worker.py`: applies the host's approval each task; a re-queued task goes straight to its approved tier; a refused paid request ends `BLOCKED paid_tier_refused:<rule>:fetched_at=<tier>` with a host event. `FetchResult.spend_refused` added. Settings: URL cap, proxy host/port, CA path, prices (deliberately high), paid body cap (5 MB), spend dir.
- **Verified (mock transports only, $0, no call to Bright Data):** proxy URL and session format, one session per host, pricing, every guard refusal and the allowed path, cap arithmetic across run/day/month/host/URL, approvals and expiry/revocation, re-queue, on/off status, the commands, and end to end through the fetch worker: an unapproved host stops at NEEDS_APPROVAL with $0 spent; an approved host escalates T3 → T4, is acquired at T4 and charged; an approved host over its cap is refused and nothing is charged.
- **Gate:** ruff format and check clean, mypy strict clean (113 files), 925 passed and 11 skipped, coverage 94.29%.
- **Not verified:** a live Bright Data request (by policy: $0 until the operator enables it). The first real use should be one approved host with a small cap, then `cli spend` compared with the Bright Data dashboard.
- **Not built:** Scraping Browser (T5 browser), rendering through the paid exit IP, nightly reconciliation against invoices, an API approval endpoint, the before/after comparison report, cross-process cap reservation.

## [2026-10-26] Steps 1–4, matching pairs once, 303-URL John Hancock run, scrolled table columns, spaced render retries

- **Branches (fast-forwarded to `main`):** `fix/step1-coverage` `bf70ef7`, `fix/step2-dedupe` `62cc4df`, `fix/step3-utility-chrome` `5e3c6b8`, `fix/step4-spacing-settle` `dafc7a1`, `fix/matches-once` `a92a4a6`, `fix/scroll-columns-render-retry` (this entry).
- **Steps 1–4 (validation findings):** web-component hosts without a box are not hidden; `VISIBLE_TEXT_NOT_EXTRACTED` coverage check per snapshot (CR-011); dedupe never points at a copy that is not in the output; site utilities (sign-in, region picker, search, webchat) are role chrome and the explorer skips them; bare `hidden` attribute is hidden; inline boxes get word breaks; settle on text length + FNV hash, not element counts.
- **Matching sheet:** each pair once under the URL that sorts first, two directional columns, listed when either direction ≥ 10%; on jh100 4,321 → 2,796 rows, 0 reverse duplicates.
- **Live run `jh_full` (303 supplied URLs, 13 chunks of 25, `--group-assets --max-wait 300`, chunk driver re-runs unfinished URLs only):** 279 VERIFIED_WITHIN_SCOPE, 18 PARTIAL, 6 NEEDS_REVIEW, 0 missing. Chunk 10 ended with 23 URLs RUNNING (host circuit open past the 300 s wait); the driver's second attempt finished all 23. Exports in `exports/jh_full`.
- **Root causes from that run, fixed here:**
  - 15 earnings pages PARTIAL: the table sits in an `overflow-x:auto` box; its last column ("Total Net Assets") starts past `documentElement.scrollWidth`, so ANNOTATE_JS marked it offscreen/hidden. Content reachable by scrolling an `auto|scroll` ancestor is no longer offscreen; text parked before the box start or clipped by `overflow:hidden` stays hidden. Fixture `scroll_table` + live-browser test (fails on the old annotator).
  - 3 pages PARTIAL `render_failed: Page.goto timeout`: render tasks had `max_attempts=2` with a 2 s backoff. Now `RENDER_MAX_ATTEMPTS=4` with waits of 30/90/180 s (`RENDER_RETRY_DELAYS_S`, inside the default 300 s wait); test asserts the spacing.
- **Not fixed (reported):** 4 NEEDS_REVIEW are R6 false alarms: OCR read cookie-banner and webchat fragments ("security of our website, improve your ex…", "assistant. Need help") that `_CONSENT_TEXT` does not match line by line. A circuit opened for 15 min still outlasts the 300 s wait; an in-engine second pass for unfinished pages is not built (the chunk driver does it).

## [2026-10-27] RFC 0002 step 1: page stamps, site menus and context-preserving chunks

- **Branch / worktree:** `feat/step1-stamping` at `../content-processor-stamp` (on `main` `2e640d3`). RFC 0002 itself is still on `docs/rfc-0002-knowledge-nodes` (not merged).
- **What landed:** new package `src/modules/content_processor/knowledge/` and CLI `stamp --records <x.records.jsonl> --tenant <t> [--evidence-root] [--out] [--run-name]`, writing `<run>.stamps.jsonl`, `<run>.chunks.jsonl`, `<run>.site.json`. Deterministic only: no network, no model.
  - `models.py`: `PageStamp`, `Chunk`, `SiteStamp`, `SiteMenu`, `SchemaFacts`, `Cta`.
  - `html_facts.py`: JSON-LD reader (`@graph`, nested types; organisation, services, products, offers, FAQ questions, breadcrumbs, article dates; unreadable blocks counted); menu trees from header/footer/nav with mega-menu panels attached to their tab via `aria-controls`, mobile copies deduped, consent panels skipped, "(opens in a new tab)" stripped; `merge_menus` unites views and pages; content links outside chrome; CTAs (strong/soft); asset and tracking URL tests (documents under `/content/dam/` kept).
  - `stamp.py`: head metadata and JSON-LD from the source payload; menus, H1 and links from the source plus every rendered view (block evidence ids = rendered DOM snapshots). Exclusions with reason: blocked/failed/excluded/running, tracking, asset, soft 404, error title, placeholder text (lorem ipsum ≥ 30% of main words), no main content, duplicate (canonical to another eligible page, or identical text). Near-duplicates (Jaccard ≥ 0.9) are listed, never dropped. Site name: `og:site_name`, else title suffix on ≥ half the pages, else organisation, else host.
  - `chunking.py`: main corpus = visible/collapsed layers, role main/legal (not chrome regions) or unknown in content regions, no `dedupe_of`; base view then states; sections by heading path (empty headings dropped); tables, accordions and forms atomic (oversized flagged, split only past 2,000 tokens); long blocks split between sentences at 500 tokens with one-sentence `overlap_before`; small chunks (< 80) merge with a neighbour under the same top heading (or none), kind of the larger part; context header `site › title › headings` + page summary in `embed_text`, verbatim `text`; prev/next ids, `parent_section_id`, stable chunk ids. Accordions are `faq` only when phrased as questions, else `panel`.
  - `analysis/claims.py`: public `quantities_in`; month abbreviations ("Oct.") no longer end a sentence.
- **Measured on `outputs/jh-2026-10-19` (39 records, evidence `rankuno-dev`), ~9 s:** 12 knowledge pages; excluded 20 duplicate (two identical e-signature page families, one governor page under two slugs), 4 asset, 1 tracking pixel, 1 error page (`/errors`), 1 placeholder (`/jhvnewsletter`). 51 chunks, 6–498 tokens (mean 250, ~155 words), 0 oversized. Header menu 72 entries (Life insurance / Long-term care / Annuities / Retirement / Insights mega-menus with their groups), footer 58. Coverage on HTML pages: title, canonical, og, lang, H1, menus 100%; breadcrumbs 97%; meta description 74%; JSON-LD 21%.
- **Gate:** ruff format and check clean, mypy strict clean (119 files), 983 tests collected, coverage 94%; new modules 91–97%. Two browser tests in `test_explorer.py` (`test_load_more_not_allowed_is_a_gap`, `test_frames_are_read_one_by_one`) failed once under full-suite load and pass in isolation: timing flake, untouched code.
- **Not built (later steps):** offering catalog (step 2), Jev/Ollama decision adapters, embeddings and pgvector, routing, competitor reports. Buttons (not links) are not CTAs yet; per-section menus (e.g. individual vs business headers) are merged into one site menu.

## [2026-10-27] RFC 0002 step 2: offering catalog ("website theming")

- **Branch / worktree:** `feat/step2-catalog` at `../content-processor-catalog`, stacked on `feat/step1-stamping` (`967d0fa`, not yet merged).
- **What landed:** CLI `catalog --stamps <run>.stamps.jsonl [--out] [--embed-endpoint --embed-model] [--llm-endpoint --llm-model]` writing `<run>.catalog.json` and `<run>_offering_catalog.xlsx` (Offerings tree with cards, Page mapping, Other menu entries, Suggestions, How to read).
  - `knowledge/catalog_models.py`: `Catalog`, `Offering` (family / offering / sub_offering, signals, evidence, card), `CardAttribute` (value, source code|ollama, model, evidence ≥ 1), `Evidence` (menu | url | schema | chunk | page_meta), `PageLink`, `MenuEntry` (non-offering entries with the pages filed under them), `Suggestion`.
  - `knowledge/catalog.py` (code only): menu entries classified by label and parent group (group, landing, tool, task, content, corporate, audience, else offering/family); merged by same link (host + path) or same name modulo family words; header placement wins over footer; schema `Service`/`Product` names become offerings under the page's offering (and their pages map to them); a folder under an offering shared by ≥ 2 pages becomes a sub-offering; pages map by longest link prefix, then breadcrumb, then schema name; pages under no offering are filed under the covering non-offering section; cards: description (offering's own page meta), audiences (audience menu link prefix), numbers (`quantities_in` per chunk, with the sentence), questions (schema FAQ + FAQ chunks), strong CTAs, offers, tools/tasks; `merge_offerings`.
  - `knowledge/assist.py` (optional, local/private endpoints only via `is_local_endpoint`, else `LocalOnlyError`): `LocalEmbedder` (`/embeddings`) merges pairs ≥ 0.92 cosine only when the family is the same and names share ≥ 50% words, otherwise a `merge` suggestion; `LocalChat` (`/chat/completions`, JSON, temperature 0) proposes features/deliverables/audiences/pricing with chunk id + exact quote, kept only when `quote_in` finds the quote in that chunk; a proposed offering type is applied only when the offering's schema evidence (Product/Service) agrees, else an `offering_type` suggestion.
- **Step 1 fixes found on the 303-URL run:** menu links that are script buttons (`href="#"`, `javascript:`, `mailto:`, `tel:`, or the page itself) have no destination (a footer "Do Not Sell" button had produced 285 per-page addresses); a single sentence longer than 500 tokens is cut between words (the only remaining > 500-token chunk is a 724-token PDF table kept whole and flagged).
- **Measured, code only, $0:** `exports/jh_full` (303 URLs; evidence in `../content-processor-matches/evidence`): 272 knowledge pages, 1,308 chunks; catalog 5 families (Life insurance › Permanent, Hybrid, Term, Executive benefits, Vitality; Long-term care; Annuities › Individual, Group; Investments; Retirement › IRAs) + 7 footer-only offerings (signal count 1); 63 other entries (28 task, 15 corporate, 12 content, 8 tool); pages: 4 under Individual annuities (url family), 267 in sections (249 News, 17 podcast, 1 docuseries), 1 unmapped (privacy PDF). The run's URLs are mostly news, so cards are thin; Individual annuities carries 4 evidenced numbers and a "Start your form" CTA. `outputs/jh-2026-10-19`: same tree; 1 page mapped, 11 in sections.
- **Gate:** ruff format and check clean, mypy strict clean (123 files), full suite passed (no failures), coverage 94.12%; new modules: catalog 89%, assist 92%, commands 92%, html_facts 92%, stamp 91%, chunking 97%.
- **Not verified:** the Ollama steps against a real server (no Ollama on this machine; tested with mock endpoints). Needs the company server URL and model names (`bge-m3`, an 8–14B instruct model).
- **Not built:** labelling content pages by meaning (Stage C), hub-card and sitemap candidates, reviewer edits of the tree in the web app, offering type from code alone (single signal, left `unspecified`).

## [2026-10-27] Global URL history and recrawl index; report audit columns

- **Branch / worktree:** `feat/url-history` at `../content-processor-history`, stacked on `feat/step2-catalog` (`364e98f`).
- **Already existed (kept):** conditional GET (If-None-Match / If-Modified-Since), reuse on 304, identical bytes or identical visible-text fingerprint, "text changed although the site's date did not" detection (`recrawl/decide.py`), but only against one named run (`--baseline` / `--baseline-run`).
- **What landed:**
  - `recrawl/history.py`: `HistoryIndex` (SQLite at `<evidence root>/<tenant>/history.sqlite`, beside the evidence it points at): `captures` (one row per URL per run: times, verdict, change status/basis, fingerprint, payload hash, site date) and `latest` (URL key -> newest reusable capture, full `UrlRecord` zlib-compressed; superseded blobs pruned). Failed/blocked captures are logged, never replace a good one; an older run imported later never overwrites a newer capture; re-import is a no-op. `HistoryBaseline` (lazy lookups; ignores a capture whose payload evidence is gone).
  - `recrawl/baseline.py`: public `url_key`, `Baseline.reusable`, `extraction_time` (a reused capture ages from its original extraction, read from block `extracted_at`), `reuse_since` + `reuse_allowed()`.
  - `workers/fetch_worker.py`: a capture not allowed for reuse is fetched without validators, compared (status stays truthful), and re-extracted with `change_basis` `...;reuse_not_allowed:capture_too_old`.
  - CLI: `dryrun`/`run`/`work` use the history when no `--baseline`/`--baseline-run` is given and add their records at the end (`history` block in the report); `--no-history`, `--reuse-since <ISO date>`, `--reuse-max-age-days N` (history default 30 days). `history import --records ... --tenant`, `history show --url`, `history stats`.
  - Project report "Pages": `Last scraped` (extraction time, UTC), `Site last modified`, `Scrape action` ("New page", "Reused (unchanged: server said not modified | same file | same visible text)", "Re-scraped (updated: ...; the site's date was not updated)", "Re-scraped (unchanged, but the earlier capture was too old to reuse)", "Scraped (not compared with earlier runs)"); How-to-read notes.
  - Tests: `settings` fixture now keeps `cp_evidence_root` in the test's tmp folder (a history left by one test would otherwise make pages "reused" in another).
- **Measured:** importing `exports/jh_full` then `outputs/jh-2026-10-19` into a scratch history: 357 captures, 16 run ids, 308 reusable URLs, 4.7 MB, 3.7 s; the older run's 38 overlapping URLs kept the newer captures; re-import added nothing. Fixture end to end: run 2 sends run 1's ETag from history and reuses on 304 with no render task; a too-old capture is fetched in full and re-extracted; missing evidence -> new.
- **Gate:** ruff format and check clean, mypy strict clean (125 files), full suite passed (no failures), coverage 94.21%; history 98%, baseline 93%.
- **Not done:** no live recrawl of johnhancock.com (outbound traffic; ready to run on request: import `jh_full` into the real history, then re-run its URL list). The real history file is not created until the first run or `history import`.

## [2026-10-28] RFC 0003 step A: SEO and GEO page inventory

- **Branch / worktree:** `feat/page-inventory` at `../content-processor-inventory`, stacked on `feat/url-history` (`56d2607`). RFC: `docs/rfc/0003-page-inventory-and-structured-page.md` (research and plan, steps A-F).
- **What landed:** CLI `inventory --records <run>.records.jsonl --tenant T [--evidence-root] [--out] [--run-name]` writing `<run>.inventory.jsonl` (one `PageInventory` per URL) and `<run>_seo_inventory.xlsx` (Issue summary, Pages, Issues, Links, Images, Videos and embeds, Structured data, How to read). Code only, no fetch, no model.
  - `knowledge/inventory_models.py`: `PageInventory`, `LinkItem` (kind internal/subdomain/external/same_page/download/mailto/tel/javascript/other, rel flags, new tab, insecure, image alt as anchor), `ImageItem` (alt status present/empty/missing/file_name, caption, size attributes, srcset candidates, loading, link target), `MediaItem` (video/audio/iframe/embed, provider, captions), `HeadFacts` (title and length, title/canonical changed by scripts, description, canonical status, robots per bot, indexable/followable, lang, hreflang, Open Graph, Twitter, favicon, feeds, h1s, heading outline, skipped levels), `StructuredItem` (JSON-LD with the node kept, microdata, RDFa; missing required/recommended per Google rich-result fields, nested items checked), `Issue`.
  - `knowledge/inventory.py`: each view walked once (source `src`, browser `rendered`, explored `stateN`) with `classify_region` and the nearest heading; items merged by (url, anchor) / url and tagged `seen_in` source | rendered | interaction; 34 issue codes in one table; blocked, failed and excluded captures are `not_captured`.
  - `knowledge/stamp.py`: `rendered_views()` (channel, evidence hash) made public and shared.
- **Measured, $0:** `exports/jh_full` (318 records; evidence in `../content-processor-matches/evidence`): 281 pages inventoried in ~70 s (7 not captured, 8 not HTML, 22 without evidence); 15,813 links, 2,139 images, 311 media, 240 structured items. 15,636 links exist only after scripts run (JH sends header/menu/link markup as web components; 169 of 282 server pages contain no `<a>` at all) and 11,722 only after a click (menus and tabs opened by the explorer). 262 titles > 60 chars, 230 without description, 242 with 2+ h1 (chat widget heading), 1,033 images alt="" (decorative), 34 self-hosted videos without captions, 17 noindex.
- **Gate:** ruff format and check clean, mypy strict clean (128 files), full suite passed (no failures), coverage 94.15%; inventory 91%.
- **Not built (next steps of RFC 0003):** B structured page tree and per-page Markdown; C robots.txt AI-bot rules, llms.txt, sitemaps, internal link graph; D e-commerce grids, facets, variants and schema-vs-page check; E GEO readiness checks (answer-first, text needing scripts); F optional broken-link checks. Response headers (X-Robots-Tag etc.) are not stored by the fetch worker yet, so they are not in the inventory.
