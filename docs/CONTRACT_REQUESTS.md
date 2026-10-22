# Contract Requests

Only Lane L0 edits `contracts.py`, `enums.py`, `workers/ports.py`, `ledger/schema.sql`, `ledger/sql.py`, `ledger/tasks.py`, `ledger/memory.py` and `pyproject.toml`. Every other lane that needs a change to these files appends a request here and keeps working against the current contract until L0 lands it.

## Rules

- Requests are **additive**: new optional fields with defaults, new enum members, new port methods, new tables or columns, new dependencies. No renames, no removals.
- L0 lands each request in one commit on `main` that also implements new port methods in **both** ledgers, then marks the request `LANDED <commit>` and notes it in `LANE_LOG.md`.
- Lanes rebase on `main` to pick it up.

## Template

```markdown
### CR-NNN: short title
- **Lane:** Lx
- **File(s):** contracts.py / enums.py / ports.py / schema / pyproject
- **Change:** exact field, member, method or dependency, with type and default
- **Why:** item ID from WORK_BREAKDOWN.md
- **Status:** OPEN | LANDED <commit> | REJECTED (reason)
```

## Requests

### CR-001: Python dependencies for L3 adapters
- **Lane:** L3 (anticipated by L0)
- **File(s):** pyproject.toml
- **Change:** move `pymupdf`, `pypdf`, `pdfplumber`, `pytesseract`, `pillow` from the `docs` extra into an installable extra that CI installs; add mypy overrides for `pymupdf.*` untyped calls if L3 cannot type them locally.
- **Why:** C-2, K3
- **Status:** LANDED

### CR-002: Port methods for embedded assets and edges
- **Lane:** L1 (anticipated by L0)
- **File(s):** ports.py, ledger/sql.py, ledger/tasks.py, ledger/memory.py
- **Change:** `record_edge(run_id, parent_id, child_id, edge_kind, evidence_id=None)` and `find_resource_by_schedule_key(run_id, schedule_key)`.
- **Why:** A-1, L0-5
- **Status:** LANDED

### CR-003: States and regions persistence
- **Lane:** L2 and L4 (anticipated by L0)
- **File(s):** contracts.py, ports.py, ledger/*
- **Change:** `State` and `Region` contracts plus `upsert_state`, `upsert_region`, `get_states`, `get_regions` port methods.
- **Why:** B-1, V-1, L0-6
- **Status:** LANDED

### CR-004: Persist the expected media type and look up payloads by hash
- **Lane:** L1
- **File(s):** contracts.py, ports.py, ledger/sql.py, ledger/tasks.py, ledger/memory.py, schema
- **Change:**
  - `Resource.expected_mime: str | None = None` and `Resource.expected_mime_source: str | None = None` (values used today: `link_type`, `link_text`, `url_extension`, `embed_kind`, `content_disposition`), with matching nullable columns on `resources`.
  - `find_resource_by_payload(run_id: str, payload_sha256: str) -> Resource | None` on `LedgerPort`, in both ledgers (index on `(run_id, payload_sha256)`).
- **Why:** K15 and A-1. `FetchWorker` keeps both in per-process dictionaries today. That is enough for one worker (dry runs, tests), but with several workers a supplied URL fetched by another process never learns that a page promised a PDF, and identical payloads are deduplicated only within one process. With these in place L1 swaps the two dictionaries for ledger reads; nothing else changes.
- **Status:** LANDED

### CR-005: Read side of the ledger for UrlRecord assembly
- **Lane:** L4
- **File(s):** ports.py, ledger/sql.py, ledger/tasks.py, ledger/memory.py
- **Change:** a `RunReader`-compatible read port (see `verify/record.py`), implemented in both ledgers:
  - `list_resources(run_id: str) -> list[Resource]` (latest row per schedule key)
  - `get_summaries(resource_id: str) -> dict[str, dict[str, Any]]` (render, explore, followups, discovered_from)
  - `get_events(run_id: str, entity_id: str | None = None) -> list[RunEvent]` (ordered by `seq`)
  - `outstanding_resource_ids(run_id: str, kinds: Sequence[TaskKind]) -> set[str]` (resources with a PENDING or LEASED task of those kinds)
- **Why:** V-1. `url_records_for_run` reads `MemoryLedger.resources`, `.summaries` and `.events` directly today, and the release-gate test reads the in-memory task queue to find outstanding work. The durable path cannot produce UrlRecords until these exist. Numbered after L1's CR-004 on `lane/L1-acquisition`.
- **Status:** LANDED

### CR-006: Gap type for soft 404s
- **Lane:** L4
- **File(s):** enums.py
- **Change:** `GapType.SOFT_404_SUSPECTED = "SOFT_404_SUSPECTED"`; L4 then adds it to the review gaps in `verify/status.py`.
- **Why:** V-2 (R10). A soft 404 is recorded today as `LOW_CONFIDENCE` with reason `soft_404_suspected` so it routes to NEEDS_REVIEW; a dedicated type lets reports and the review queue filter it without parsing reasons.
- **Status:** LANDED


### CR-007: Run-scoped leasing and pending counts
- **Lane:** L6
- **File(s):** ports.py, ledger/sql.py, ledger/tasks.py, ledger/memory.py
- **Change:** optional `run_id: str | None = None` on `LedgerPort.lease(worker, kinds, run_id=None)` and `pending_count(kinds, run_id=None)`; when set, `SQL_LEASE_PLAIN`, `SQL_LEASE_WITH_SLOT` and `SQL_PENDING_COUNT` add `AND t.run_id = %(run_id)s`. Workers pass their bound `run_id`. Default `None` keeps today's behaviour.
- **Why:** P-1. A worker is bound to one run's scope, but Postgres leases match only the task kind, so with two open runs a worker can lease and process the other run's task under the wrong scope. Until this lands the durable `run`/`work` commands refuse to start while another run has open tasks (`foreign_open_tasks`, exit code 3).
- **Status:** LANDED `0dc8878` (merged to `main` 2026-10-19). Workers, the scheduler's drain checks and `LazyRenderStage` pass their run id; the CLI's `other_run_active` guard is replaced by an informational `concurrent_open_tasks` count.

### CR-008: Office document dependencies for C-9
- **Lane:** L3 (anticipated by WORK_BREAKDOWN.md 3.4)
- **File(s):** pyproject.toml
- **Change:** new optional extra `office = ["python-docx>=1.2,<2", "openpyxl>=3.1,<4", "python-pptx>=1.0,<2"]`, installed by CI alongside `dev`. All three are pure Python with no torch and no external binaries. Revised 2026-10-19: this replaces an earlier proposal for `docling-slim[format-office]`, which was dropped because the adapter now uses these libraries directly. mypy needs `openpyxl.*` to be ignored as untyped (the adapter carries an inline `type: ignore[import-untyped]` until a stub or override is added here; `types-openpyxl` is the stub option).
- **Not requested:** LibreOffice. `.doc`, `.xls` and `.ppt` are reported as `UNSUPPORTED` with reason `legacy_office_requires_libreoffice:<ext>`; LibreOffice is deferred until the modern formats are verified and the operator approves a system install.
- **Why:** C-9 (Office formats, cases 63–68, 161–168)
- **Status:** LANDED (merged to main with lane/L3-docs, 2026-10-19; `office` extra in pyproject.toml)


### CR-009: Interaction policy for the B-4 to B-12 explorer
- **Lane:** L2
- **File(s):** contracts.py (`AllowedAction`, `InteractionPolicy`, `_default_actions`)
- **Change:**
  1. `AllowedAction` gains `"advance_carousel"`. Carousels are clicked under `select_tab` today; L2 switches `_ACTION_FOR_KIND["carousel"]` once it lands.
  2. `InteractionPolicy.consent_choice: Literal["reject_non_essential", "leave_in_place"] = "reject_non_essential"`. Today the explorer answers a consent overlay with "reject non-essential" whenever `open_informational_dialog` is allowed, and leaves it in place otherwise; it never accepts.
  3. Decision needed from the operator: whether `_default_actions()` should add `hover_info`, `open_informational_dialog` and `paginate_get` (all read-only, under the GET-only, no-navigation guard). Under today's default (`scroll`, `expand_disclosure`, `select_tab`), a page with a tooltip, an informational dialog or an in-page "Load more" ends PARTIAL with `policy_disallows:<action>`. That is honest, but it is a policy choice, not a capability limit. `keyboard_traverse` costs up to `4 x max_actions_per_url` key presses per page and should stay opt-in.
- **Why:** B-4, B-6 and the default-policy verdicts. The explorer implements every `AllowedAction` now; these change only how a scope declares them.
- **Status:** item 3 LANDED (operator decision 2026-10-19: hover_info, open_informational_dialog and paginate_get on by default; keyboard_traverse stays opt-in; see ARCHITECTURE.md §19). Items 1 and 2 PROPOSED: they need the matching explorer change, so L2 lands them with its wiring.

### CR-010: Group-ordered leasing for RENDER tasks
- **Lane:** L0 (ledger), requested by the URL grouping work (RFC 0001)
- **File(s):** ports.py, ledger/memory.py, ledger/tasks.py
- **Change:** `lease()` accepts an optional `group_key: Callable[[Task], str | None]` (or a `group_of: Mapping[str, str]` keyed by URL). When given, ready tasks of the same group are preferred over tasks of other groups, so a group's pages render back to back and its asset cache is reused. Additive: the parameter defaults to None and keeps the current enqueue order.
- **Why:** the scheduler's `RunGroups` (scheduler/grouping.py) builds groups after the fetch stage, after RENDER tasks already exist. Without ordering, cache hits depend on how leases happen to interleave. Measured effect on the local services fixture is still to be quantified with interleaved leasing.
- **Status:** OPEN

### CR-011: Gap type for visible text that was not extracted
- **Lane:** L0 (enums), requested by the coverage check (validation of the 98-URL John Hancock run)
- **File(s):** enums.py
- **Change:** `GapType.VISIBLE_TEXT_NOT_EXTRACTED`. The render worker raises it (status PARTIAL) when lines the browser shows (3+ words, at least 4 words in all) are not accounted for by any captured block of the same snapshot; hidden, source-only and metadata blocks do not count. Report wording, review action and the web app vocabulary added.
- **Why:** verification proved only precision (extracted text is on the page), never coverage (page text was extracted); the Latest News headlines were wrongly hidden on every John Hancock news page and the pages still read "Fully captured".
- **Status:** LANDED with the coverage check (2026-10-22).

