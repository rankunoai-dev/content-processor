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
