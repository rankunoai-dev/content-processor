"""Idempotency keys (ARCHITECTURE.md §5).

The same logical unit of work always hashes to the same key, so a re-delivered
or re-planned task collapses onto the existing row instead of duplicating it.
"""

from __future__ import annotations

import hashlib

from src.modules.content_processor.enums import TaskKind

__all__ = ["task_key"]


def task_key(run_id: str, kind: TaskKind, *parts: str) -> str:
    """Build a stable key from the run, the kind and the identifying parts."""
    joined = "\x1f".join((run_id, kind.value, *parts))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
