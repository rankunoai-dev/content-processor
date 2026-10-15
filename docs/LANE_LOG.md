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
