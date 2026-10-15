"""In-memory ledger for dry runs and tests.

Implements the same `LedgerPort` the workers use, including per-host
concurrency and politeness delay, so a dry run behaves like a real run except
that nothing survives the process. Never use it for a run whose results matter.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from src.modules.content_processor.contracts import Resource, RunEvent, Task
from src.modules.content_processor.enums import AccessTier, TaskKind, TaskState
from src.modules.content_processor.governor.escalation import CircuitState, HostAccessState
from src.modules.content_processor.ledger.sql import HOST_SLOT_KINDS
from src.modules.content_processor.workers.ports import RobotsCache

__all__ = ["MemoryLedger"]


class MemoryLedger:
    """Process-local ledger with host slots, delays and events."""

    def __init__(
        self,
        *,
        ceiling: AccessTier = AccessTier.T3_FIDELITY_BROWSER,
        base_delay_s: float = 1.0,
        max_concurrency: int = 1,
    ) -> None:
        """Create an empty ledger with the politeness defaults of playbook T1."""
        self._tasks: dict[int, Task] = {}
        self._next_id = 1
        self._ceiling = ceiling
        self._base_delay = base_delay_s
        self._max_concurrency = max_concurrency
        self.resources: dict[str, Resource] = {}
        self.events: list[RunEvent] = []
        self.hosts: dict[str, HostAccessState] = {}
        self.robots: dict[str, RobotsCache] = {}
        self._in_flight: dict[str, int] = {}
        self._next_allowed: dict[str, datetime] = {}
        self._retry_after: dict[str, datetime] = {}
        self._crawl_delay: dict[str, float] = {}

    # -- enqueue ------------------------------------------------------------

    def enqueue(self, tasks: Iterable[Task]) -> int:
        """Add tasks; duplicates by idempotency key are ignored."""
        known = {t.idempotency_key for t in self._tasks.values()}
        added = 0
        for task in tasks:
            if task.idempotency_key in known:
                continue
            stored = task.model_copy(update={"id": self._next_id})
            self._tasks[self._next_id] = stored
            self._next_id += 1
            added += 1
        return added

    def task_counts(self) -> dict[str, int]:
        """Counts by task state."""
        counts: dict[str, int] = {}
        for t in self._tasks.values():
            counts[t.state.value] = counts.get(t.state.value, 0) + 1
        return counts

    # -- LedgerPort ---------------------------------------------------------

    def lease(self, worker: str, kinds: Sequence[TaskKind]) -> Task | None:
        """Lease the next ready task, honouring host slots and delays."""
        now = datetime.now(UTC)
        candidates = sorted(
            (t for t in self._tasks.values() if t.state is TaskState.PENDING and t.kind in kinds),
            key=lambda t: (int(t.priority), t.id or 0),
        )
        for task in candidates:
            if task.not_before and task.not_before > now:
                continue
            host = task.host
            state = self.hosts.get(host)
            if task.kind in HOST_SLOT_KINDS:
                if self._in_flight.get(host, 0) >= self._max_concurrency:
                    continue
                if self._next_allowed.get(host, now) > now:
                    continue
                if self._retry_after.get(host, now) > now:
                    continue
                if state is not None and state.can_attempt(now).kind.value == "CIRCUIT_OPEN":
                    continue
                self._in_flight[host] = self._in_flight.get(host, 0) + 1
                delay = max(self._base_delay, self._crawl_delay.get(host, 0.0))
                self._next_allowed[host] = now + timedelta(seconds=delay)
            task.state = TaskState.LEASED
            task.attempts += 1
            task.lease_owner = worker
            task.lease_expires_at = now + timedelta(seconds=90)
            return task
        return None

    def pending_count(self, kinds: Sequence[TaskKind]) -> int:
        """Non-terminal tasks of the given kinds."""
        return sum(
            1
            for t in self._tasks.values()
            if t.kind in kinds and t.state in (TaskState.PENDING, TaskState.LEASED)
        )

    def heartbeat(self, task_id: int, worker: str) -> bool:
        """Always alive in-process."""
        return self._tasks[task_id].lease_owner == worker

    def _release(self, task: Task) -> None:
        if task.kind in HOST_SLOT_KINDS:
            self._in_flight[task.host] = max(0, self._in_flight.get(task.host, 0) - 1)

    def complete(self, task_id: int, worker: str) -> bool:
        """Mark done and release the slot."""
        task = self._tasks[task_id]
        if task.lease_owner != worker or task.state is not TaskState.LEASED:
            return False
        task.state = TaskState.DONE
        self._release(task)
        return True

    def fail(
        self,
        task_id: int,
        worker: str,
        error: str,
        *,
        retry: bool,
        not_before: datetime | None = None,
    ) -> TaskState | None:
        """Re-queue until max attempts, then FAILED."""
        task = self._tasks[task_id]
        if task.lease_owner != worker:
            return None
        task.last_error = error
        task.lease_owner = None
        self._release(task)
        if retry and task.attempts < task.max_attempts:
            task.state = TaskState.PENDING
            task.not_before = not_before or datetime.now(UTC)
        else:
            task.state = TaskState.FAILED
        return task.state

    def upsert_resource(self, resource: Resource, host: str) -> None:
        """Keep the latest record per schedule key."""
        self.resources[resource.schedule_key] = resource.model_copy(deep=True)

    def append_event(self, event: RunEvent) -> int:
        """Append in order."""
        self.events.append(event)
        return len(self.events)

    def load_host_state(self, tenant_id: str, host: str) -> HostAccessState:
        """Create a default state on first sight."""
        return self.hosts.setdefault(host, HostAccessState(host=host, ceiling=self._ceiling))

    def save_host_state(
        self, tenant_id: str, state: HostAccessState, *, retry_after_until: datetime | None = None
    ) -> None:
        """Persist in memory."""
        self.hosts[state.host] = state
        if retry_after_until is not None:
            self._retry_after[state.host] = retry_after_until

    def get_robots(self, tenant_id: str, host: str) -> RobotsCache | None:
        """Cached robots for the host."""
        return self.robots.get(host)

    def set_robots(self, tenant_id: str, host: str, cache: RobotsCache) -> None:
        """Store robots and derive the crawl-delay for the host."""
        self.robots[host] = cache
        if cache.body:
            for line in cache.body.splitlines():
                key, _, value = line.partition(":")
                if (
                    key.strip().lower() == "crawl-delay"
                    and value.strip().replace(".", "", 1).isdigit()
                ):
                    self._crawl_delay[host] = min(float(value.strip()), 30.0)

    # -- reporting ----------------------------------------------------------

    def report(self) -> dict[str, Any]:
        """Summarise the run for a dry-run report."""
        status_counts: dict[str, int] = {}
        for res in self.resources.values():
            status_counts[res.status.value] = status_counts.get(res.status.value, 0) + 1
        return {
            "tasks": self.task_counts(),
            "resources_by_status": status_counts,
            "hosts": {
                h: {
                    "tier": s.tier.value,
                    "ceiling": s.ceiling.value,
                    "circuit": s.circuit.value
                    if isinstance(s.circuit, CircuitState)
                    else str(s.circuit),
                    "clean_streak": s.clean_streak,
                    "block_score": round(s.block_score, 2),
                    "robots_status": self.robots[h].status if h in self.robots else None,
                    "crawl_delay_s": self._crawl_delay.get(h),
                }
                for h, s in self.hosts.items()
            },
            "resources": [
                {
                    "requested_url": r.requested_url,
                    "final_url": r.final_url,
                    "status": r.status.value,
                    "reason": r.status_reason,
                    "http_status": r.http_status,
                    "sniffed_mime": r.sniffed_mime,
                    "bytes": r.payload_bytes,
                    "hops": [h.kind.value for h in r.redirect_chain],
                    "block": (
                        {"kind": r.block_verdict.kind.value, "vendor": r.block_verdict.vendor}
                        if r.block_verdict
                        else None
                    ),
                    "tier": r.acquired_at_tier.value if r.acquired_at_tier else None,
                    "sha256": r.payload_sha256,
                }
                for r in self.resources.values()
            ],
            "host_events": [
                {"host": e.entity_id, "reason": e.reason, "new_state": e.new_state}
                for e in self.events
                if e.entity_type == "host"
            ],
        }
