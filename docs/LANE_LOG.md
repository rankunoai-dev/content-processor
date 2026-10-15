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
