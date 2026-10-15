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
- **Status:** OPEN

### CR-003: States and regions persistence
- **Lane:** L2 and L4 (anticipated by L0)
- **File(s):** contracts.py, ports.py, ledger/*
- **Change:** `State` and `Region` contracts plus `upsert_state`, `upsert_region`, `get_states`, `get_regions` port methods.
- **Why:** B-1, V-1, L0-6
- **Status:** OPEN

### CR-005: Read side of the ledger for UrlRecord assembly
- **Lane:** L4
- **File(s):** ports.py, ledger/sql.py, ledger/tasks.py, ledger/memory.py
- **Change:** a `RunReader`-compatible read port (see `verify/record.py`), implemented in both ledgers:
  - `list_resources(run_id: str) -> list[Resource]` (latest row per schedule key)
  - `get_summaries(resource_id: str) -> dict[str, dict[str, Any]]` (render, explore, followups, discovered_from)
  - `get_events(run_id: str, entity_id: str | None = None) -> list[RunEvent]` (ordered by `seq`)
  - `outstanding_resource_ids(run_id: str, kinds: Sequence[TaskKind]) -> set[str]` (resources with a PENDING or LEASED task of those kinds)
- **Why:** V-1. `url_records_for_run` reads `MemoryLedger.resources`, `.summaries` and `.events` directly today, and the release-gate test reads the in-memory task queue to find outstanding work. The durable path cannot produce UrlRecords until these exist. Numbered after L1's CR-004 on `lane/L1-acquisition`.
- **Status:** OPEN

### CR-006: Gap type for soft 404s
- **Lane:** L4
- **File(s):** enums.py
- **Change:** `GapType.SOFT_404_SUSPECTED = "SOFT_404_SUSPECTED"`; L4 then adds it to the review gaps in `verify/status.py`.
- **Why:** V-2 (R10). A soft 404 is recorded today as `LOW_CONFIDENCE` with reason `soft_404_suspected` so it routes to NEEDS_REVIEW; a dedicated type lets reports and the review queue filter it without parsing reasons.
- **Status:** OPEN
