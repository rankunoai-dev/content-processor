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
- **Change:** new optional extra `office = ["docling-slim[format-office]>=2.140,<3"]`, installed by CI alongside `dev`. Use `docling-slim`, not `docling`: the plain `docling` package always pulls the `standard` extra, which brings torch and model weights. `docling-slim[format-office]` installs the DOCX, PPTX, XLSX, HTML, Markdown and CSV backends (about 242 MB, no torch, measured 2026-10-19 in a scratch venv). No new mypy override is expected; check with `mypy --strict` after install.
- **External binary, not a pip dependency:** `.doc`, `.ppt` and `.xls` need LibreOffice on PATH. Without it Docling reports `LibreOffice is required` and the adapter must emit `UNSUPPORTED_FORMAT` for legacy files. LibreOffice is a system install by the operator; it is not added to `pyproject.toml`.
- **Why:** C-9 (Office formats, cases 63–68, 161–168)
- **Status:** OPEN
