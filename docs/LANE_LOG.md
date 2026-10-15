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
