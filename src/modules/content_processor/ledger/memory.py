"""In-memory ledger for dry runs and tests.

Implements the same `LedgerPort` the workers use, including per-host
concurrency and politeness delay, so a dry run behaves like a real run except
that nothing survives the process. Never use it for a run whose results matter.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from src.modules.content_processor.contracts import (
    ContentBlock,
    Gap,
    Region,
    Resource,
    RunEvent,
    State,
    Task,
)
from src.modules.content_processor.enums import AccessTier, TaskKind, TaskState
from src.modules.content_processor.governor.escalation import CircuitState, HostAccessState
from src.modules.content_processor.ledger.sql import HOST_SLOT_KINDS
from src.modules.content_processor.workers.ports import RobotsCache

__all__ = ["MemoryLedger"]


def _layer_counts(blocks: Sequence[ContentBlock]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for b in blocks:
        counts[b.layer.value] = counts.get(b.layer.value, 0) + 1
    return counts


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
        self.blocks: dict[str, list[ContentBlock]] = {}
        self.summaries: dict[str, dict[str, dict[str, Any]]] = {}
        self.gaps: dict[str, Gap] = {}
        self.events: list[RunEvent] = []
        self.edges: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.states: dict[tuple[str, str, str], State] = {}
        self.regions: dict[tuple[str, str, str | None, str], Region] = {}
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
            known.add(task.idempotency_key)
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

    def lease(
        self, worker: str, kinds: Sequence[TaskKind], run_id: str | None = None
    ) -> Task | None:
        """Lease the next ready task, honouring host slots and delays (and `run_id`)."""
        now = datetime.now(UTC)
        candidates = sorted(
            (
                t
                for t in self._tasks.values()
                if t.state is TaskState.PENDING
                and t.kind in kinds
                and (run_id is None or t.run_id == run_id)
            ),
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

    def pending_count(self, kinds: Sequence[TaskKind], run_id: str | None = None) -> int:
        """Non-terminal tasks of the given kinds (in one run when `run_id` is given)."""
        return sum(
            1
            for t in self._tasks.values()
            if t.kind in kinds
            and t.state in (TaskState.PENDING, TaskState.LEASED)
            and (run_id is None or t.run_id == run_id)
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

    def get_resource(self, resource_id: str) -> Resource | None:
        """Find a resource by id."""
        for res in self.resources.values():
            if res.resource_id == resource_id:
                return res.model_copy(deep=True)
        return None

    def get_blocks(self, resource_id: str) -> list[ContentBlock]:
        """Blocks of a resource."""
        return [b.model_copy(deep=True) for b in self.blocks.get(resource_id, [])]

    def enqueue_followups(self, tasks: Sequence[Task]) -> int:
        """Same as enqueue."""
        return self.enqueue(tasks)

    def record_summary(self, resource_id: str, key: str, value: dict[str, Any]) -> None:
        """Keep the latest summary per key."""
        self.summaries.setdefault(resource_id, {})[key] = value

    def record_gap(self, gap: Gap) -> None:
        """Keep the latest version of each gap."""
        self.gaps[gap.gap_id] = gap.model_copy(deep=True)

    def get_gaps(self, resource_id: str) -> list[Gap]:
        """Gaps for one resource, in insertion order."""
        return [g.model_copy(deep=True) for g in self.gaps.values() if g.resource_id == resource_id]

    def upsert_blocks(self, resource_id: str, blocks: Sequence[ContentBlock]) -> None:
        """Replace the block set of a resource (same region keys overwrite)."""
        self.blocks[resource_id] = [b.model_copy(deep=True) for b in blocks]

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

    # -- CR-002: Edges and schedule key lookup ------------------------------

    def record_edge(
        self,
        run_id: str,
        parent_id: str,
        child_id: str,
        edge_kind: str,
        evidence_id: str | None = None,
    ) -> None:
        """Record a directed edge between resources."""
        key = (run_id, parent_id, child_id, edge_kind)
        prev = self.edges.get(key)
        ev = evidence_id if evidence_id is not None else (prev.get("evidence_id") if prev else None)
        self.edges[key] = {
            "run_id": run_id,
            "parent_id": parent_id,
            "child_id": child_id,
            "edge_kind": edge_kind,
            "evidence_id": ev,
        }

    def find_resource_by_schedule_key(self, run_id: str, schedule_key: str) -> Resource | None:
        """Find a resource by run_id and schedule_key."""
        res = self.resources.get(schedule_key)
        if res is not None and res.run_id == run_id:
            return res.model_copy(deep=True)
        return None

    # -- CR-004: Payload hash lookup ---------------------------------------

    def find_resource_by_payload(self, run_id: str, payload_sha256: str) -> Resource | None:
        """Find an already-acquired resource with this payload hash in this run."""
        for res in self.resources.values():
            if res.run_id == run_id and res.payload_sha256 == payload_sha256:
                return res.model_copy(deep=True)
        return None

    # -- CR-003: States and regions persistence -----------------------------

    def upsert_state(self, state: State) -> None:
        """Insert or update a dynamic state."""
        self.states[(state.run_id, state.resource_id, state.state_key)] = state.model_copy(
            deep=True
        )

    def upsert_region(self, region: Region) -> None:
        """Insert or update a region."""
        self.regions[(region.run_id, region.resource_id, region.state_id, region.region_key)] = (
            region.model_copy(deep=True)
        )

    def get_states(self, run_id: str, resource_id: str) -> list[State]:
        """All dynamic states of a resource."""
        states = [
            s.model_copy(deep=True)
            for s in self.states.values()
            if s.run_id == run_id and s.resource_id == resource_id
        ]
        return sorted(states, key=lambda s: s.state_id)

    def get_regions(self, run_id: str, resource_id: str) -> list[Region]:
        """All regions of a resource."""
        regions = [
            r.model_copy(deep=True)
            for r in self.regions.values()
            if r.run_id == run_id and r.resource_id == resource_id
        ]
        return sorted(regions, key=lambda r: r.region_id)

    # -- CR-005: Read side for UrlRecord assembly --------------------------

    def list_resources(self, run_id: str) -> list[Resource]:
        """All latest resource records for a run (one per schedule key)."""
        res_list = [r.model_copy(deep=True) for r in self.resources.values() if r.run_id == run_id]
        return sorted(res_list, key=lambda r: r.schedule_key)

    def get_summaries(self, resource_id: str) -> dict[str, dict[str, Any]]:
        """All named summaries recorded for a resource."""
        return {k: dict(v) for k, v in self.summaries.get(resource_id, {}).items()}

    def get_events(self, run_id: str, entity_id: str | None = None) -> list[RunEvent]:
        """Append-only events for a run, optionally filtered by entity_id, ordered by seq."""
        return [
            e.model_copy(deep=True)
            for e in self.events
            if e.run_id == run_id and (entity_id is None or e.entity_id == entity_id)
        ]

    def outstanding_resource_ids(self, run_id: str, kinds: Sequence[TaskKind]) -> set[str]:
        """Resource IDs with a PENDING or LEASED task of the given kinds."""
        kind_set = set(kinds)
        by_key = {
            r.schedule_key: r.resource_id for r in self.resources.values() if r.run_id == run_id
        }
        outstanding: set[str] = set()
        for t in self._tasks.values():
            if (
                t.run_id != run_id
                or t.state not in (TaskState.PENDING, TaskState.LEASED)
                or t.kind not in kind_set
            ):
                continue
            res_id = t.payload.get("resource_id")
            if res_id:
                outstanding.add(str(res_id))
            sched_key = t.payload.get("schedule_key")
            if sched_key and str(sched_key) in by_key:
                outstanding.add(by_key[str(sched_key)])
        return outstanding

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
                    "blocks_by_layer": _layer_counts(self.blocks.get(r.resource_id, [])),
                    "render": self.summaries.get(r.resource_id, {}).get("render"),
                    "gaps": [
                        {
                            "type": g.gap_type.value,
                            "reason": g.reason,
                            "status": g.status.value,
                            "evidence": g.evidence_ids,
                        }
                        for g in self.get_gaps(r.resource_id)
                    ],
                }
                for r in self.resources.values()
            ],
            "host_events": [
                {"host": e.entity_id, "reason": e.reason, "new_state": e.new_state}
                for e in self.events
                if e.entity_type == "host"
            ],
        }
