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
