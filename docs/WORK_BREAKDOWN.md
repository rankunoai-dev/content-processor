# Work Breakdown — Done, Left, and Parallel Lanes

> **Document ID**: `CP-DOC-WBS-V1`
> **Date**: 2026-10-16
> **Basis**: code at commit `f3f32fb` plus untracked files in the working tree, measured against `ARCHITECTURE.md`, `ACCESS_RESILIENCE_PLAYBOOK.md`, `FEASIBILITY_AND_GATED_CONTENT.md`, `STAGE4_ANALYSIS_AND_INTERFACES.md`, the gap audit (cases 1–222) and the decisions recorded in chat.
> **Purpose**: split the remaining work into lanes that several coding sessions can run at the same time without breaking each other.

---

## 0. Read this first: a parallel session is already active

The repository already shows two writers.

| Evidence | What it is | Risk |
|---|---|---|
| Commit `f553778` (author Gaurav Doshi) | A second "Stage 1c" commit touching the same 16 files as `bf1d4f2` | Two implementations of the same stage were merged in sequence; later edits silently overwrote parts of each other |
| `src/modules/content_processor/migrations/001_initial.sql` | A second schema with **different table names** (`host_policies`, `url_records`, `gap_records`, `content_blocks`, `run_events`) | Conflicts with `ledger/schema.sql`, which the code actually uses (`host_policy`, `resources`, `gaps`, `extractions`, `events`). Whichever one runs second creates a split database |
| Untracked `adapters/pdf.py`, `adapters/ocr.py`, `adapters/image.py` (855 lines) | PDF, OCR and image adapters from the other session | Not imported anywhere, not tested, fail `mypy --strict` (9 errors), drop coverage below the 85% gate when included |

This is exactly the breakage the lane plan below is meant to prevent. Section 4 gives the rules; section 5 says what to do with these three items before any lane starts.

---

## 1. What is done (verified in code and tests)

Gate at `f3f32fb`: ruff clean, mypy strict clean on the tracked code, 268 tests passing, 9 skipped (Postgres), coverage 90%.

### 1.1 Foundations

| # | Item | Where |
|---|---|---|
| D1 | Vendored Rankuno core (StrictModel, BaseTool, guardrails, CostLedger, TokenBucket, logger, settings) | `src/core/` |
| D2 | Two-level status taxonomy, gap types, layers, tiers, task kinds | `enums.py` |
| D3 | Contracts: RunScope with policy validators (paid tier needs cost cap, T6 dual sign-off token, robots default by authorization, dev-only private egress, persona rules), Resource, Gap, ContentBlock, UrlRecord, Task, RunEvent, FormPersona | `contracts.py` |
| D4 | Bright Data settings and caps in config, kill switch default off | `src/core/config.py` |

### 1.2 Scope and frontier

| # | Item | Where |
|---|---|---|
| D5 | URL normalization: schedule key, near-duplicate key, tracking-param stripping, fragment routes, IDN, invalid-input reasons (cases 91, 92, 94) | `scope/normalize.py` |
| D6 | SSRF classification incl. metadata, CGNAT, mapped IPv6, rebinding (cases 88, 93) | `scope/ssrf.py` |
| D7 | Frontier: aliases, exclusions with reasons, mandatory priority, egress check, requested-count denominator | `scope/frontier.py`, `planner.py` |

### 1.3 Governor and access

| # | Item | Where |
|---|---|---|
| D8 | Block classifier: Cloudflare, Akamai, HUMAN, DataDome, Imperva, Kasada, AWS WAF, Sucuri, Anubis, CAPTCHA frames, 402, 429, 451, 401, soft 404, wrong payload, login redirect | `governor/block_classifier.py` |
| D9 | Per-host state machine: escalation, ceiling, NEEDS_APPROVAL for paid tiers, circuit breaker, tier decay | `governor/escalation.py` |
| D10 | RFC 9309 robots with respect / exceptions / informational, unreachable treated as transient | `governor/robots.py` |
| D11 | DOM gates: lead-gen form wall, pincode modal, product unavailable, login wall, age gate, newsletter trap | `governor/gates.py` |

### 1.4 Acquisition and extraction

| # | Item | Where |
|---|---|---|
| D12 | T1 HTTP fetcher: egress per hop, typed redirect chain (http, meta, canonical hint), body cap, truncation, sniffing | `integrations/http_client.py`, `sniff.py` |
| D13 | Content-addressed local evidence store, per tenant | `integrations/evidence_store.py` |
| D14 | Source-channel HTML extraction into layered blocks with traps (icon fonts, templates, noscript, comments, hidden, widgets, collapsed), links, assets, metadata, template fingerprint, placeholder and needs-render hints | `adapters/html_source.py` |
| D15 | T3 renderer: fresh context, egress-routed subrequests, analytics suppression, quiet-period settle, computed-visibility annotation, screenshot, ARIA snapshot, captured XHR | `integrations/browser_client.py` |
| D16 | Source-vs-render reconciliation: verified, hydrated, source-only, moved, collapsed-pending | `verify/reconcile.py` |

### 1.5 Workers, ledger, outputs

| # | Item | Where |
|---|---|---|
| D17 | Fetch worker: robots, egress, fetch, evidence, classify, governor, source extraction, gates, RENDER follow-up | `workers/fetch_worker.py` |
| D18 | Render worker: render, evidence, classify at T3, rendered extraction, reconcile, gates, status | `workers/render_worker.py` |
| D19 | Ledger port; in-memory ledger with host slots, delays, retry-after, circuit; Postgres ledger with SKIP LOCKED leases, reaper, quarantine, checkpoints, events, resources, blocks, gaps, host state | `workers/ports.py`, `ledger/` |
| D20 | Badge derivation and status vector functions | `verify/status.py` |
| D21 | Gate → typed gap mapping | `verify/gaps.py` |
| D22 | Whole-content text per URL (R1 as JSONL), review-queue JSONL | `export/` |
| D23 | CLI: plan, start, fetch, status, dryrun (fetch + render) | `cli.py` |
| D24 | Fixture server with 22 adversarial fixtures and manifest gate; John Hancock seed and scope; live browser tests | `fixtures/`, `tests/` |

### 1.6 Live evidence

- John Hancock seed: 34 of 34 acquired at T1, rendered and verified at T3, $0, no blocks; two live "Lorem ipsum" pages flagged.
- Fixture run: challenge pages blocked with vendor, paid-tier approval event raised without spend.
- Gated fixtures (2026-10-16, source channel): lead-gen wall, age gate, pincode modal and unavailable product each produced the expected typed gap; the newsletter trap produced none; the review queue listed 9 URLs (4 gated, 3 blocked, 1 excluded, 1 rate-limited). The render pass in the same run rendered 0 pages, see K13.
- After the K13 fix (two fixture hosts): 15 pages rendered; content host circuit closed, access host circuit open as designed; lead-gen and age gates now BLOCKED, pincode PARTIAL, unavailable product EXTRACTED_UNVERIFIED, newsletter trap VERIFIED with no gap. One false verified result found, see K15.

---

## 2. Known defects and gaps in what exists

These are bugs or holes in code that is already there, not new features.

| # | Defect | Impact | Lane |
|---|---|---|---|
| K1 | Postgres ledger has never run against a database (9 integration and chaos tests skipped) | The durable path is unproven; `get_blocks` SQL in particular is untested | L0 |
| K2 | Two schemas (`ledger/schema.sql` vs `migrations/001_initial.sql`) | Split database | L0 |
| K3 | Untracked PDF/OCR/image adapters fail type checks and are not wired | Cannot merge as is | L3 |
| K4 | Badge and status-vector functions exist but no `UrlRecord` is ever assembled; resource status is used as a stand-in | The honest per-URL verdict from the architecture is not yet produced | L4 |
| K5 | Every rendered page carries `discovery_open` because no state explorer exists, so nothing can reach `VERIFIED_WITHIN_SCOPE` | By design until Stage 2, but must be stated in every report | L2 |
| K6 | Discovered links and assets are recorded but never enqueued (no embedded-asset or depth-1 download following) | PDFs and images linked from pages are not acquired | L1 |
| K7 | Crawl-delay is parsed only in the in-memory ledger; the Postgres `set_robots` passes `None` | Durable runs ignore crawl-delay | L0 |
| K8 | Reaper reclaims leases but emits no events and does not mark the owning resource `FAILED(poison)` | Quarantined URLs vanish from the status stream | L0 |
| K9 | Run-level budgets (runtime, requests, bytes, evidence) are declared in scope but not enforced | A runaway run is only bounded by task attempts | L6 |
| K10 | Cost ledger not wired to any route (no paid route exists yet) | Must exist before T4/T5 is ever enabled | L1 |
| K11 | Dry run `--max-tasks` applies to fetch and render separately; render pass can stall waiting on held tasks | Fixed with a 60 s cap, but the loop model is per-kind and serial | L6 |
| K13 | The `/rate-limited` fixture shares host `127.0.0.1` with all other fixtures; three 429s open the host circuit for 15 min and the render pass gives up after its 60 s wait, so 0 pages render in a full fixture run | Correct governor behaviour on a real host, but it hides render results in the fixture suite. Fix: serve rate-limit and challenge fixtures on a second host alias (e.g. `localhost`) so they get their own circuit | L1 + L4 |
| K14 | Gate gaps found by the fetch worker do not change the resource status (stays `EXTRACTED_UNVERIFIED`); only the render worker applies the implied status | `--no-render` runs under-report gated pages in the badge (the gap is still recorded) | L4 |
| K16 | Block ids from `html_source` collided across channels in the Postgres `extractions` table | Fixed `0dc8878` (channel-specific block ids) | done |
| K17 | Nothing enqueued `EXTRACT_DOC` / `EXTRACT_IMAGE`, so fetched PDFs and images were never extracted | Fixed `0dc8878` (fetch worker hands off documents and images) | done |
| K18 | PyMuPDF printed a `pymupdf_layout` banner to stdout on first use, corrupting the CLI's JSON output | Fixed on `main` 2026-10-19 (`pymupdf.no_recommend_layout()` at import; subprocess regression test) | done |
| K15 | `/pdf-but-html` (an HTML login prompt served where a PDF is expected) ends `VERIFIED_EXTRACTED`. The fetch worker infers the expected type only from the URL extension, and this URL has none, so the wrong-payload check never fires | A known adversarial trap passes as verified: violates the release gate (V-7). Fix: carry the expected MIME from the discovering link (`<a href=… type=…>`, link text, Content-Disposition) or the manifest, and fail closed when the parent said PDF | L1 + L4 |
| K19 | R4 contradiction candidates on the live John Hancock run (2026-10-19) were 17 of 17 false positives: "as of March 31, 2024" vs "as of March 31, 2025" (periodic snapshots) are flagged as date conflicts because `analysis/claims.py` reads "as of" as an effective-from cue, and fund codes (PDT vs HTD) are too short to count as distinguishing terms | R4 is noise on periodic financial notices until fixed: "as of" must make a point-in-time period (different snapshots = context differs), and differing short uppercase codes must count as different entities | L5 |
| K20 | Evidence volume is about 27 MB per page (1 GB for 39 URLs on the live John Hancock run), almost all full-page DOM snapshots of about 3 MB each written per explored state | A 5,000-URL run would hit the default 20 GB `max_evidence_bytes` budget (K9) after about 750 URLs. Fix: compress DOM evidence or store per-state diffs (P-5) | L2 + L6 |
| K21 | Cases 1–90 have two numberings: code and manifest entries cite the unified investigation (90 cases), which is not in the repository; the primary investigation in `reports/` numbers 1–83 differently, and cases 85, 86, 87 and 89 are undefined in the repo | `fixtures/case_inventory.json` titles 1–83 from the primary document and maps coverage by topic; add the unified investigation to `reports/` to reconcile | L0 |
| K12 | Truncated-body fixture cannot be exercised through the ASGI transport (Starlette normalises Content-Length) | Case 104 only unit-tested via a mock transport | L1 |

---

## 3. What is left (complete inventory)

IDs are stable so lanes and sessions can reference them. "Stage" follows `ARCHITECTURE.md` section 18.

### 3.1 Ledger, contracts, durability (Stage 0 remainder)

- L0-1 Pick one schema, delete the other, add a numbered migration runner.
- L0-2 Run the integration and chaos suites against Postgres (WSL2); fix what breaks.
- L0-3 Reaper emits events and sets `FAILED(poison)` on the resource with last error.
- L0-4 Crawl-delay persisted and applied by the Postgres governor (`base_delay_ms`).
- L0-5 `resource_edges` writes (embeds, links_to, frames, attachment_of, redirects_to).
- L0-6 States and regions tables actually populated (today only resources, blocks, gaps).
- L0-7 Contract additions needed by other lanes, made once and announced (see section 4.2).

### 3.2 Acquisition and access (Stage 1–2)

- A-1 Follow discovered embedded assets and linked downloads to depth 1, with scope and MIME policy, deduped by content hash.
- A-2 T2 impersonated HTTP fetcher (`curl_cffi`) behind the same fetcher interface; escalation actually switches fetchers by tier (today the host tier rises but T1 keeps fetching).
- A-3 Identity-consistency checklist enforced per session (playbook section 6).
- A-4 Conditional GET and recrawl mode (cases 101, 102).
- A-5 Transport integrity: compression bombs, 206 assembly, 304 without cache, HTTP/3/TLS classification (cases 103–110).
- A-6 Bright Data proxy provider (T4) and Web Unlocker / Scraping Browser (T5) behind provider interfaces.
- A-7 Cost ledger per run, per day, per month, per host, per URL; vendor rate table; nightly reconciliation; kill switch honoured.
- A-8 Operator approval flow for `paid_tier_available`: CLI and API command that raises one host's ceiling with a cap and re-queues its blocked URLs; before/after comparison report.
- A-9 Shadow-block detection: canary URL per host, baseline drift, alternate-class probe (case 178).
- A-10 Sitemap and feed discovery as a cheap first channel; CMS JSON endpoints as a separate representation (playbook section 5).
- A-11 AI opt-out and TDM directive parsing (case 182).

### 3.3 Browser state exploration (Stage 2)

- B-1 State explorer framework: action budget, state fingerprint, child states, discovery closure, stop reasons.
- B-2 Disclosures: `<details>`, accordions, `hidden=until-found`, popovers (cases 37, 116).
- B-3 Tabs and ARIA tablists (case 38).
- B-4 Carousels until repeat or known total (case 39).
- B-5 Hover/focus informational triggers and keyboard traversal (cases 40, 126).
- B-6 Informational modals; consent overlays with declared choices (cases 41, 45, 143).
- B-7 Pagination and load-more, GET-only side-effect rule (cases 42, 147).
- B-8 Scroll accumulation, lazy load, infinite scroll budget (cases 16, 31).
- B-9 Virtualized lists and 2-axis grids with accumulation before recycling (cases 32, 33).
- B-10 Nested scroll surfaces (case 34).
- B-11 Frames: same-origin, cross-origin, srcdoc, per-frame outcome (cases 07, 08).
- B-12 Open shadow DOM traversal into the extractor; closed roots via screenshot OCR (cases 05, 06).
- B-13 CSS pseudo-element text, canvas, SVG text and outlined glyphs (cases 12–15).
- B-14 Variant selectors and pincode/location application from scope (cases 43, 216, 219).
- B-15 Anti-detection browser fork evaluation (`patchright` or `camoufox`) and pinning (ADR-0002).
- B-16 Browser recycling by RSS, zombie process cleanup (case 192).

### 3.4 Documents, images, OCR (Stage 1–3)

- C-1 Install Tesseract and language packs (core set from `ARCHITECTURE.md` 6.1); detect availability at startup.
- C-2 Finish and type the existing PDF, OCR and image adapters; tests and fixtures.
- C-3 EXTRACT_DOC and EXTRACT_IMAGE workers in a resource-limited child process.
- C-4 PDF region-level OCR (native header must not suppress OCR of a scanned body), stale OCR layers, broken fonts (cases 47–53).
- C-5 PDF forms, annotations, attachments, layers, incremental updates, invisible text (cases 56–59, 151–160).
- C-6 PDF tables with continuation (case 54).
- C-7 Multilingual routing per region with script detection and second-opinion trigger (decided policy).
- C-8 Image OCR for text-bearing images found on pages; screenshot OCR for canvas and closed shadow roots (with B-12, B-13).
- C-9 Office formats via Docling, legacy via sandboxed LibreOffice, archives, email (Stage 3; cases 63–68, 161–168).
- C-10 Audio and video: captions first, ASR, frame OCR (Stage 3; cases 72–74, 171–173).

### 3.5 Verification brain (Stage 1–2)

- V-1 Assemble `UrlRecord` per URL: status vector, inventory counts with denominators, discovery state, gaps, blocks.
- V-2 Rules R1–R13 as separate, tested functions producing gaps (`ARCHITECTURE.md` section 10).
- V-3 Visual text-region detector on screenshots; every detected region maps to an extraction or a gap (R6).
- V-4 Critical-token checks: numbers, units, currencies, negations, footnote markers (R9, case 90).
- V-5 Soft redirect detection via template fingerprint vs homepage (case 98).
- V-6 Volatility detection for never-settling regions (cases 99, 123).
- V-7 Release gate: zero false `VERIFIED_WITHIN_SCOPE` on the adversarial suite, run in CI.
- V-8 Expand fixtures to the full adversarial list (cases 01–222 that are testable locally).

### 3.6 Platform and operations (Stage 1–2)

- P-1 Long-running worker processes per kind (fetch, render, doc, verify) against Postgres; scheduler with admission and backpressure watermarks.
- P-2 Run budgets enforced (K9) and run lifecycle states.
- P-3 FastAPI service: create run, status vector, SSE events with resume, cancel, export.
- P-4 Egress proxy container with private-range denial; workers use it.
- P-5 MinIO evidence store behind the same interface; retention and deletion jobs per tenant.
- P-6 Docker Compose for the shared server at P2 priority with memory caps.
- P-7 Metrics, structured logs per task, alerts (playbook section 8, architecture section 13).
- P-8 5,000-URL load test on the shared server; capacity numbers replace estimates.
- P-9 Chaos tests: kill worker, double delivery, Postgres restart, disk full.
- P-10 n8n webhook on terminal run state.

### 3.7 Gated content (decided policy)

- G-1 `cli resolve` and API: attach an operator-obtained asset, close the gap, record operator and note.
- G-2 Persona form filler and IMAP listener (only after sign-off; contract already enforces the rules).
- G-3 Review queue in Postgres and in the API (today JSONL from dry runs only).

### 3.8 Outputs and Stage 4

- O-1 Report R1 as CSV and XLSX with the 32,767-character column split.
- O-2 Interface I1, URL content explorer.
- O-3 I2 regex and overlap grouping with MinHash/LSH, reports R2a–R2c.
- O-4 I3 semantic similarity with Qdrant and a pinned multilingual model, R3a–R3b.
- O-5 I4 contradiction candidates with claim context and NLI judge, R4 with reviewer column.
- O-6 Theme profiles from template fingerprints and design evidence.
- O-7 Claim-context extraction on blocks (entities, dates, geography, audience).

---

## 4. Lanes for parallel sessions

### 4.1 The lanes

Each lane owns directories. A session may **read** anything but **write** only in its lane's paths plus new test files under `tests/<lane path>`.

| Lane | Scope (IDs) | Owns (write) | Depends on | Can start now |
|---|---|---|---|---|
| **L0 Integrator** | K1, K2, K7, K8, L0-1…L0-7 | `contracts.py`, `enums.py`, `workers/ports.py`, `ledger/`, `migrations/`, `tests/conftest.py`, `pyproject.toml` | Postgres reachable | Yes, first |
| **L1 Acquisition and access** | A-1…A-11, K6, K10, K12 | `integrations/http_client.py`, new `integrations/impersonated_client.py`, `integrations/proxy_provider.py`, `integrations/unblocker_provider.py`, `governor/` (except `gates.py`), `scope/`, `workers/fetch_worker.py`, `fixtures/server.py` (new routes only) | L0 for edge writes and cost tables | Yes (A-1, A-2, A-5, A-9, A-10 need no contract change) |
| **L2 Browser state explorer** | B-1…B-16, K5 | new `explore/` package, `integrations/browser_client.py`, `workers/render_worker.py`, `fixtures/pages/` (new files only) | L0 for states and regions tables | Yes (B-1 framework against the in-memory ledger) |
| **L3 Documents and OCR** | C-1…C-10, K3 | `adapters/pdf.py`, `adapters/ocr.py`, `adapters/image.py`, new `adapters/office.py`, new `workers/doc_worker.py`, `fixtures/docs/` | L0 for EXTRACT_* task payloads | Yes (C-1, C-2, C-4) |
| **L4 Verification brain** | V-1…V-8, K4 | `verify/`, `governor/gates.py`, `fixtures/manifest.json`, `tests/.../test_fixture_manifest.py` | Reads outputs of L1–L3 | Yes (V-1, V-2, V-4 on existing data) |
| **L6 Platform and ops** (second wave) | P-1…P-10, K9, K11 | new `api/`, new `scheduler/`, `cli.py`, `integrations/evidence_store.py`, `deploy/`, `scripts/` | L0 schema stable | After L0-1, L0-2 |
| **L5 Outputs and Stage 4** | O-1…O-7, G-1, G-3 | `export/`, new `analysis/`, new `ui/` | V-1 for UrlRecord | O-1 now; rest after V-1 |
| **L7 Gated persona** | G-2 | new `integrations/forms/`, new `integrations/mailbox.py` | Your written sign-off | No, blocked on sign-off |

### 4.2 Shared files and how changes to them land

Five files are touched by almost every lane. Only L0 edits them; other lanes request changes.

| Shared file | Rule |
|---|---|
| `contracts.py`, `enums.py` | Additive only (new optional fields, new enum members). A lane that needs a change writes a short request in `docs/CONTRACT_REQUESTS.md`; L0 lands it in one commit and announces it. No renames, no removals, no new required fields without a default. |
| `workers/ports.py` | New methods only. L0 implements them in both ledgers in the same commit so nothing else breaks. |
| `ledger/schema.sql` | One source of truth. New tables or columns go through a numbered migration owned by L0. |
| `cli.py` | Owned by L6. Other lanes add a subcommand by writing a `register(subparsers)` function in their own module; L6 wires it. |
| `pyproject.toml` | L0 only. Dependency requests go in `docs/CONTRACT_REQUESTS.md`. |

### 4.3 Session rules that keep the build green

1. **One branch per lane**, named `lane/<id>-<topic>`, rebased on `main` at the start of every session. No session commits directly to `main` except L0.
2. **Merge order**: L0 first, then lanes in the order they finish. Each merge must pass the full gate on the merge result, not just on the branch.
3. **Gate on every commit**: `ruff format --check`, `ruff check`, `mypy --strict src fixtures`, `pytest` with coverage ≥ 85%. A lane may not lower coverage; untested code stays untracked or on its branch.
4. **New fixtures are additive**: new page files and new manifest entries only; nobody edits another lane's fixture.
5. **Worktrees**: run each session in its own `git worktree` so two sessions never share a working directory. The untracked adapter files in section 0 are what happens without this.
6. **Hand-off note**: each session ends by appending to `docs/LANE_LOG.md`: lane, branch, what landed, what contract requests are open.

---

## 5. Before any lane starts (one short L0 session) — done 2026-10-16 except item 3, see LANE_LOG.md

1. Decide the fate of `migrations/001_initial.sql`. Recommendation: delete it, keep `ledger/schema.sql` (the code and tests use it), and make it migration `001`.
2. Move the three untracked adapters onto branch `lane/L3-docs` so `main` stays green; L3 finishes them there.
3. Point `CP_DATABASE_URL` at the WSL2 Postgres and run the 9 skipped tests (K1).
4. Create `docs/CONTRACT_REQUESTS.md` and `docs/LANE_LOG.md`.
5. Fix K13 first, then re-run the gated-fixture dry run with rendering and record the result here.

## 6. Suggested first wave (four sessions in parallel after section 5)

| Session | Lane | First deliverable | Done when |
|---|---|---|---|
| 1 | L1 | A-1 embedded assets and depth-1 downloads, A-2 T2 fetcher switching by tier | A PDF linked from a fixture page is acquired and recorded with an edge; a fingerprint block escalates to T2 and the T2 fetcher is the one that runs |
| 2 | L2 | B-1 explorer framework, B-2 disclosures, B-3 tabs | The FAQ and tab fixtures close discovery and reach `VERIFIED_WITHIN_SCOPE`; a page with an unvisited tab stays `PARTIAL` |
| 3 | L3 | C-1 Tesseract install, C-2 typed adapters, C-4 mixed PDF fixture | A native-header plus scanned-body PDF yields both regions; the gate is green with the adapters tracked |
| 4 | L4 | V-1 UrlRecord assembly, V-2 R1–R3 and R10 | Every URL in a dry run has a UrlRecord with denominators; the adversarial suite has zero false verified results |

L5 (analysis and reports) and L6 (platform) join in the second wave once V-1 exists and L0's schema is settled.
