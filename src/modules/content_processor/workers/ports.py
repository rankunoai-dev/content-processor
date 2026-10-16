"""Ledger port used by workers.

Workers depend on this Protocol, not on Postgres, so the loop logic is tested
against an in-memory ledger and the SQL layer is tested on its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from src.core.schemas import StrictModel
from src.modules.content_processor.contracts import (
    ContentBlock,
    Gap,
    Region,
    Resource,
    RunEvent,
    State,
    Task,
)
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

    def pending_count(self, kinds: Sequence[TaskKind]) -> int:
        """Tasks of these kinds not yet terminal (pending or leased), ready or not."""
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

    def upsert_blocks(self, resource_id: str, blocks: Sequence[ContentBlock]) -> None:
        """Insert or update extracted blocks for a resource (keyed by region and method)."""
        ...

    def get_resource(self, resource_id: str) -> Resource | None:
        """Load one resource record."""
        ...

    def get_blocks(self, resource_id: str) -> list[ContentBlock]:
        """All blocks of a resource across states and methods."""
        ...

    def enqueue_followups(self, tasks: Sequence[Task]) -> int:
        """Enqueue tasks discovered while processing (idempotent)."""
        ...

    def record_summary(self, resource_id: str, key: str, value: dict[str, Any]) -> None:
        """Attach a named summary (e.g. render reconciliation) to a resource."""
        ...

    def record_gap(self, gap: Gap) -> None:
        """Insert or update a gap (keyed by gap_id)."""
        ...

    def get_gaps(self, resource_id: str) -> list[Gap]:
        """Gaps recorded for a resource."""
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

    # -- CR-002: Edges and schedule key lookup ------------------------------

    def record_edge(
        self,
        run_id: str,
        parent_id: str,
        child_id: str,
        edge_kind: str,
        evidence_id: str | None = None,
    ) -> None:
        """Record a directed edge between resources (embeds, links_to, frames, etc.)."""
        ...

    def find_resource_by_schedule_key(self, run_id: str, schedule_key: str) -> Resource | None:
        """Find a resource by run_id and schedule_key."""
        ...

    # -- CR-004: Payload hash lookup ---------------------------------------

    def find_resource_by_payload(self, run_id: str, payload_sha256: str) -> Resource | None:
        """Find an already-acquired resource with this payload hash in this run."""
        ...

    # -- CR-003: States and regions persistence -----------------------------

    def upsert_state(self, state: State) -> None:
        """Insert or update a dynamic state."""
        ...

    def upsert_region(self, region: Region) -> None:
        """Insert or update a region."""
        ...

    def get_states(self, run_id: str, resource_id: str) -> list[State]:
        """All dynamic states of a resource."""
        ...

    def get_regions(self, run_id: str, resource_id: str) -> list[Region]:
        """All regions of a resource."""
        ...

    # -- CR-005: Read side for UrlRecord assembly --------------------------

    def list_resources(self, run_id: str) -> list[Resource]:
        """All latest resource records for a run (one per schedule key)."""
        ...

    def get_summaries(self, resource_id: str) -> dict[str, dict[str, Any]]:
        """All named summaries recorded for a resource."""
        ...

    def get_events(self, run_id: str, entity_id: str | None = None) -> list[RunEvent]:
        """Append-only events for a run, optionally filtered by entity_id, ordered by seq."""
        ...

    def outstanding_resource_ids(self, run_id: str, kinds: Sequence[TaskKind]) -> set[str]:
        """Resource IDs with a PENDING or LEASED task of the given kinds."""
        ...
