"""Ledger port used by workers.

Workers depend on this Protocol, not on Postgres, so the loop logic is tested
against an in-memory ledger and the SQL layer is tested on its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from src.core.schemas import StrictModel
from src.modules.content_processor.contracts import Resource, RunEvent, Task
from src.modules.content_processor.enums import TaskKind, TaskState
from src.modules.content_processor.governor.escalation import HostAccessState

__all__ = ["LedgerPort", "RobotsCache"]


class RobotsCache(StrictModel):
    """Cached robots.txt fetch for one host."""

    body: str | None
    status: int | None
    fetched_at: datetime


class LedgerPort(Protocol):
    """The subset of the ledger a worker needs."""

    def lease(self, worker: str, kinds: Sequence[TaskKind]) -> Task | None:
        """Lease the next task."""
        ...

    def heartbeat(self, task_id: int, worker: str) -> bool:
        """Extend the lease."""
        ...

    def complete(self, task_id: int, worker: str) -> bool:
        """Mark done."""
        ...

    def fail(
        self,
        task_id: int,
        worker: str,
        error: str,
        *,
        retry: bool,
        not_before: datetime | None = None,
    ) -> TaskState | None:
        """Record a failure."""
        ...

    def upsert_resource(self, resource: Resource, host: str) -> None:
        """Insert or update the resource row keyed by (run_id, schedule_key)."""
        ...

    def append_event(self, event: RunEvent) -> int:
        """Append an event."""
        ...

    def load_host_state(self, tenant_id: str, host: str) -> HostAccessState:
        """Load the governor state for a host (creating a default if missing)."""
        ...

    def save_host_state(
        self, tenant_id: str, state: HostAccessState, *, retry_after_until: datetime | None = None
    ) -> None:
        """Persist the governor state for a host."""
        ...

    def get_robots(self, tenant_id: str, host: str) -> RobotsCache | None:
        """Cached robots.txt for the host, if fetched in this run."""
        ...

    def set_robots(self, tenant_id: str, host: str, cache: RobotsCache) -> None:
        """Store the robots.txt fetch result and crawl-delay for the host."""
        ...
