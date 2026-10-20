"""Task ledger execution layer (ADR-0001).

Thin wrapper around psycopg that runs the statements in `sql.py` inside short
transactions. Crash-only by design: a worker that dies mid-task leaves a lease
that the reaper reclaims; nothing here needs a cleanup handler to run.

Covered by the integration and chaos suites (need `CP_DATABASE_URL`).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.core.logger import get_logger
from src.modules.content_processor.contracts import (
    ContentBlock,
    Gap,
    Region,
    Resource,
    RunEvent,
    RunScope,
    State,
    Task,
)
from src.modules.content_processor.enums import (
    AccessTier,
    EntityStatus,
    GapType,
    TaskKind,
    TaskState,
)
from src.modules.content_processor.governor.escalation import (
    CIRCUIT_OPEN_FOR,
    CircuitState,
    HostAccessState,
    initial_host_ceiling,
)
from src.modules.content_processor.governor.robots import MAX_CRAWL_DELAY_S, crawl_delay_for
from src.modules.content_processor.ledger import sql
from src.modules.content_processor.ledger.outcomes import (
    ACQUISITION_KINDS,
    close_out_task_resource,
)
from src.modules.content_processor.workers.ports import RobotsCache

__all__ = ["TaskLedger", "new_run_id", "storage_keys"]

_logger = get_logger("content_processor.ledger")
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _row_to_task(row: dict[str, Any]) -> Task:
    return Task(
        id=row["id"],
        run_id=row["run_id"],
        kind=TaskKind(row["kind"]),
        idempotency_key=row["idempotency_key"],
        host=row["host"],
        priority=row["priority"],
        state=TaskState(row["state"]),
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        not_before=row["not_before"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        payload=row["payload"] or {},
        checkpoint=row["checkpoint"],
        last_error=row["last_error"],
    )


def storage_keys(resource_id: str, block: ContentBlock) -> tuple[str, str]:
    """(region_id column, extraction_id) for a block of `resource_id` (K16)."""
    channel = block.state_id.split("|", 1)[1] if "|" in block.state_id else block.state_id
    region_key = f"{resource_id}:{channel}:{block.region_id}"
    return region_key, f"{region_key}|{block.method}"


class TaskLedger:
    """Durable task queue, host slots and events on one Postgres database."""

    def __init__(self, conninfo: str, *, lease_ttl_s: float = 90.0) -> None:
        """Bind to a database. One connection per ledger instance (one per worker)."""
        self._conninfo = conninfo
        self._ttl = lease_ttl_s
        self._conn = psycopg.connect(conninfo, row_factory=dict_row, autocommit=False)

    def close(self) -> None:
        """Close the connection."""
        self._conn.close()

    # -- schema and runs ----------------------------------------------------

    def create_schema(self) -> None:
        """Apply `schema.sql` (idempotent)."""
        with self._conn.cursor() as cur:
            cur.execute(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._conn.commit()

    def create_run(self, run_id: str, scope: RunScope, tool_versions: dict[str, str]) -> None:
        """Insert the run row and the host policy rows for its in-scope hosts."""
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_INSERT_RUN,
                {
                    "run_id": run_id,
                    "tenant_id": scope.tenant_id,
                    "scope_hash": scope.scope_hash(),
                    "scope": scope.model_dump_json(),
                    "profile": scope.profile.value,
                    "tool_versions": json.dumps(tool_versions),
                    "cost_ceiling_usd": scope.budgets.cost_ceiling_usd,
                },
            )
            for host in scope.hosts_in_scope:
                cur.execute(
                    sql.SQL_ENSURE_HOST,
                    {
                        "tenant_id": scope.tenant_id,
                        "host": host.lower(),
                        "tier_ceiling": initial_host_ceiling(scope.access_tier_ceiling).value,
                        "max_concurrency": 1,
                    },
                )
        self._conn.commit()

    def ensure_host(
        self, tenant_id: str, host: str, ceiling: AccessTier, max_concurrency: int = 1
    ) -> None:
        """Create a host policy row if missing (discovered cross-host assets)."""
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_ENSURE_HOST,
                {
                    "tenant_id": tenant_id,
                    "host": host.lower(),
                    "tier_ceiling": initial_host_ceiling(ceiling).value,
                    "max_concurrency": max_concurrency,
                },
            )
        self._conn.commit()

    def cancel_run(self, run_id: str) -> None:
        """Ask every worker to release the leases of this run at the next heartbeat."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_CANCEL_RUN, {"run_id": run_id})
        self._conn.commit()

    # -- tasks --------------------------------------------------------------

    def enqueue(self, tenant_id: str, tasks: Iterable[Task]) -> int:
        """Insert tasks; duplicates by idempotency key are ignored. Returns inserted count."""
        inserted = 0
        with self._conn.cursor() as cur:
            for task in tasks:
                cur.execute(
                    sql.SQL_ENQUEUE,
                    {
                        "run_id": task.run_id,
                        "tenant_id": tenant_id,
                        "kind": task.kind.value,
                        "idempotency_key": task.idempotency_key,
                        "host": task.host,
                        "priority": int(task.priority),
                        "max_attempts": task.max_attempts,
                        "not_before": task.not_before,
                        "payload": json.dumps(task.payload),
                    },
                )
                if cur.fetchone() is not None:
                    inserted += 1
        self._conn.commit()
        return inserted

    def lease(
        self, worker: str, kinds: Sequence[TaskKind], run_id: str | None = None
    ) -> Task | None:
        """Lease the next eligible task of the given kinds, or None if nothing is ready.

        With `run_id`, only that run's tasks are eligible (CR-007): a worker bound
        to one run's scope never processes another run's task. Host slots stay
        shared across runs, so politeness per host holds for concurrent runs.
        """
        slot_kinds = [k.value for k in kinds if k in sql.HOST_SLOT_KINDS]
        plain_kinds = [k.value for k in kinds if k not in sql.HOST_SLOT_KINDS]
        with self._conn.cursor() as cur:
            row: dict[str, Any] | None = None
            if slot_kinds:
                cur.execute(
                    sql.SQL_LEASE_WITH_SLOT,
                    {
                        "kinds": slot_kinds,
                        "worker": worker,
                        "ttl_s": self._ttl,
                        "run_id": run_id,
                        # an open circuit is leased again once its open period ends; the
                        # worker's can_attempt() then probes it as HALF_OPEN
                        "circuit_open_s": CIRCUIT_OPEN_FOR.total_seconds(),
                    },
                )
                row = cur.fetchone()
            if row is None and plain_kinds:
                cur.execute(
                    sql.SQL_LEASE_PLAIN,
                    {"kinds": plain_kinds, "worker": worker, "ttl_s": self._ttl, "run_id": run_id},
                )
                row = cur.fetchone()
        self._conn.commit()
        return _row_to_task(row) if row else None

    def pending_count(self, kinds: Sequence[TaskKind], run_id: str | None = None) -> int:
        """Non-terminal tasks of the given kinds, in one run or (None) across all runs."""
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_PENDING_COUNT, {"kinds": [k.value for k in kinds], "run_id": run_id}
            )
            row = cur.fetchone()
        self._conn.commit()
        return int(row["n"]) if row else 0

    def heartbeat(self, task_id: int, worker: str) -> bool:
        """Extend the lease. False means the lease was lost (reaped or cancelled)."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_HEARTBEAT, {"id": task_id, "worker": worker, "ttl_s": self._ttl})
            alive = cur.rowcount == 1
        self._conn.commit()
        return alive

    def checkpoint(self, task_id: int, worker: str, checkpoint: dict[str, Any]) -> bool:
        """Persist progress so a reclaimed task resumes instead of restarting."""
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_CHECKPOINT,
                {
                    "id": task_id,
                    "worker": worker,
                    "checkpoint": json.dumps(checkpoint),
                    "ttl_s": self._ttl,
                },
            )
            ok = cur.rowcount == 1
        self._conn.commit()
        return ok

    def complete(self, task_id: int, worker: str) -> bool:
        """Mark done and release the host slot. False means the lease was lost."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_RELEASE_SLOT, {"id": task_id})
            cur.execute(sql.SQL_COMPLETE, {"id": task_id, "worker": worker})
            ok = cur.fetchone() is not None
        self._conn.commit()
        return ok

    def fail(
        self,
        task_id: int,
        worker: str,
        error: str,
        *,
        retry: bool,
        not_before: datetime | None = None,
    ) -> TaskState | None:
        """Record a failure; retry re-queues until max_attempts, else FAILED."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_RELEASE_SLOT, {"id": task_id})
            cur.execute(
                sql.SQL_FAIL,
                {
                    "id": task_id,
                    "worker": worker,
                    "error": error[:2000],
                    "retry": retry,
                    "not_before": not_before,
                },
            )
            row = cur.fetchone()
        self._conn.commit()
        return TaskState(row["state"]) if row else None

    def reap(self) -> list[dict[str, Any]]:
        """Reclaim expired leases and make every outcome visible (K8).

        Each reclaimed lease gets a task event (`LEASED → PENDING` for a retry,
        `LEASED → QUARANTINED` at max attempts). A quarantined task is poison:
        its resource is closed out so the URL does not vanish from the status
        stream. A FETCH/UNLOCK resource still in flight becomes
        `FAILED poison:<kind>:<last error>` with an ACQUISITION_FAILED gap; for a
        later stage (render, documents) the already-acquired resource keeps its
        status and a PARTIAL gap records the lost stage.

        Returns:
            The reclaimed rows (id, state, run_id, kind, host, attempts,
            max_attempts, payload, last_error).
        """
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_REAP)
            rows = cur.fetchall()
        self._conn.commit()
        for row in rows:
            _logger.warning(
                "task_reclaimed",
                extra={k: row[k] for k in ("id", "state", "run_id", "kind", "host", "attempts")},
            )
            self.append_event(
                RunEvent(
                    run_id=row["run_id"],
                    entity_type="task",
                    entity_id=str(row["id"]),
                    old_state=TaskState.LEASED.value,
                    new_state=row["state"],
                    reason="lease_expired",
                    attempt=int(row["attempts"]),
                )
            )
            if row["state"] == TaskState.QUARANTINED.value:
                self._close_out_poison(row)
        return rows

    def _close_out_poison(self, row: dict[str, Any]) -> None:
        kind = TaskKind(row["kind"])
        reason = f"poison:{kind.value}:{row['last_error'] or 'lease_expired'}"[:500]
        acquisition = kind in ACQUISITION_KINDS
        close_out_task_resource(
            self,
            run_id=row["run_id"],
            host=row["host"],
            kind=kind,
            payload=row["payload"] or {},
            status=EntityStatus.FAILED,
            gap_type=GapType.ACQUISITION_FAILED,
            gap_status=EntityStatus.FAILED if acquisition else EntityStatus.PARTIAL,
            reason=reason,
            attempt=int(row["attempts"]),
        )

    def cancel_open_tasks(self, run_id: str, kinds: Sequence[TaskKind], reason: str) -> list[Task]:
        """Cancel this run's pending tasks of these kinds; return them (K9).

        Leased tasks finish normally; only work not yet started is cancelled.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_CANCEL_OPEN_TASKS,
                {"run_id": run_id, "kinds": [k.value for k in kinds], "reason": reason[:2000]},
            )
            rows = cur.fetchall()
        self._conn.commit()
        return [_row_to_task(row) for row in rows]

    def task_counts(self, run_id: str) -> dict[str, int]:
        """Counts by state for one run."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_TASK_COUNTS, {"run_id": run_id})
            rows = cur.fetchall()
        self._conn.commit()
        return {row["state"]: int(row["n"]) for row in rows}

    # -- resources and host state ------------------------------------------

    def upsert_resource(self, resource: Resource, host: str) -> None:
        """Insert or update the resource row keyed by (run_id, schedule_key)."""
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_UPSERT_RESOURCE,
                {
                    "resource_id": resource.resource_id,
                    "run_id": resource.run_id,
                    "schedule_key": resource.schedule_key,
                    "requested_url": resource.requested_url,
                    "final_url": resource.final_url,
                    "host": host,
                    "mandatory": resource.mandatory,
                    "status": resource.status.value,
                    "status_reason": resource.status_reason,
                    "payload_sha256": resource.payload_sha256,
                    "acquired_at_tier": (
                        resource.acquired_at_tier.value if resource.acquired_at_tier else None
                    ),
                    "expected_mime": resource.expected_mime,
                    "expected_mime_source": resource.expected_mime_source,
                    "record": resource.model_dump_json(),
                },
            )
        self._conn.commit()

    def upsert_blocks(self, resource_id: str, blocks: Sequence[ContentBlock]) -> None:
        """Insert or update extracted blocks (one row per resource, channel, region, method).

        Storage keys are namespaced here (K16) so adapters need not make their
        ids globally unique: PDF and image adapters emit ids like
        `reg-pdf-metadata`, and before html_source ids carried the channel the
        source and rendered blocks of a page shared `<resource>:src:N`. The
        `region_id` column is `<resource>:<channel>:<region>` (so `get_blocks`
        finds every block by its resource prefix) and `extraction_id` is that
        plus the method, the same key the upsert conflicts on. The stored
        record keeps the block exactly as the adapter produced it.
        """
        with self._conn.cursor() as cur:
            for block in blocks:
                region_key, extraction_id = storage_keys(resource_id, block)
                cur.execute(
                    sql.SQL_UPSERT_BLOCK,
                    {
                        "extraction_id": extraction_id,
                        "run_id": block.state_id.split("|", 1)[0],
                        "region_id": region_key,
                        "block_key": block.block_key,
                        "method": block.method,
                        "tool_version": block.tool_version,
                        "record": block.model_dump_json(),
                    },
                )
        self._conn.commit()

    def get_resource(self, resource_id: str) -> Resource | None:
        """Load one resource record."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_GET_RESOURCE, {"resource_id": resource_id})
            row = cur.fetchone()
        self._conn.commit()
        if row is None:
            return None
        record = row["record"]
        known = set(Resource.model_fields)
        return Resource.model_validate({k: v for k, v in record.items() if k in known})

    def get_blocks(self, resource_id: str) -> list[ContentBlock]:
        """All blocks of a resource."""
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_GET_BLOCKS, {"resource_id": resource_id, "prefix": f"{resource_id}:%"}
            )
            rows = cur.fetchall()
        self._conn.commit()
        return [ContentBlock.model_validate(row["record"]) for row in rows]

    def enqueue_followups(self, tasks: Sequence[Task]) -> int:
        """Enqueue discovered tasks; the tenant is read from the run row."""
        batch = list(tasks)
        if not batch:
            return 0
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_TENANT_OF_RUN, {"run_id": batch[0].run_id})
            row = cur.fetchone()
        self._conn.commit()
        if row is None:
            return 0
        return self.enqueue(str(row["tenant_id"]), batch)

    def record_summary(self, resource_id: str, key: str, value: dict[str, Any]) -> None:
        """Merge a named summary into the resource record."""
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_RECORD_SUMMARY,
                {"resource_id": resource_id, "key": key, "value": json.dumps(value, default=str)},
            )
        self._conn.commit()

    def record_gap(self, gap: Gap) -> None:
        """Insert or update a gap row."""
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_UPSERT_GAP,
                {
                    "gap_id": gap.gap_id,
                    "run_id": gap.run_id,
                    "resource_id": gap.resource_id,
                    "gap_type": gap.gap_type.value,
                    "status": gap.status.value,
                    "record": gap.model_dump_json(),
                },
            )
        self._conn.commit()

    def get_gaps(self, resource_id: str) -> list[Gap]:
        """Gaps for one resource."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_GET_GAPS, {"resource_id": resource_id})
            rows = cur.fetchall()
        self._conn.commit()
        return [Gap.model_validate(row["record"]) for row in rows]

    def resource_counts(self, run_id: str) -> dict[str, int]:
        """Resource counts by status for one run."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_RESOURCE_COUNTS, {"run_id": run_id})
            rows = cur.fetchall()
        self._conn.commit()
        return {row["status"]: int(row["n"]) for row in rows}

    def load_host_state(self, tenant_id: str, host: str) -> HostAccessState:
        """Load the governor state for a host, creating the row if needed."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_LOAD_HOST, {"tenant_id": tenant_id, "host": host})
            row = cur.fetchone()
        self._conn.commit()
        if row is None:
            self.ensure_host(tenant_id, host, AccessTier.T3_FIDELITY_BROWSER)
            return HostAccessState(host=host, ceiling=AccessTier.T3_FIDELITY_BROWSER)
        return HostAccessState(
            host=host,
            ceiling=AccessTier(row["tier_ceiling"]),
            tier=AccessTier(row["access_tier"]),
            circuit=CircuitState(row["circuit_state"]),
            circuit_opened_at=row["circuit_opened_at"],
            consecutive_failures=row["consecutive_failures"],
            clean_streak=row["clean_streak"],
            block_score=row["block_score"],
            session_generation=row["session_generation"],
        )

    def save_host_state(
        self, tenant_id: str, state: HostAccessState, *, retry_after_until: datetime | None = None
    ) -> None:
        """Persist the governor state for a host."""
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_SAVE_HOST,
                {
                    "tenant_id": tenant_id,
                    "host": state.host,
                    "access_tier": state.tier.value,
                    "circuit_state": state.circuit.value,
                    "circuit_opened_at": state.circuit_opened_at,
                    "consecutive_failures": state.consecutive_failures,
                    "clean_streak": state.clean_streak,
                    "block_score": state.block_score,
                    "session_generation": state.session_generation,
                    "retry_after_until": retry_after_until,
                },
            )
        self._conn.commit()

    def get_robots(self, tenant_id: str, host: str) -> RobotsCache | None:
        """Cached robots.txt for the host, if fetched before."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_LOAD_HOST, {"tenant_id": tenant_id, "host": host})
            row = cur.fetchone()
        self._conn.commit()
        if row is None or row["robots_fetched_at"] is None:
            return None
        payload = json.loads(row["robots_cache"] or "{}")
        delay_ms = row["crawl_delay_ms"]
        return RobotsCache(
            body=payload.get("body"),
            status=payload.get("status"),
            fetched_at=row["robots_fetched_at"],
            crawl_delay_s=delay_ms / 1000.0 if delay_ms is not None else None,
        )

    def set_robots(self, tenant_id: str, host: str, cache: RobotsCache) -> None:
        """Store the robots.txt fetch result and its crawl-delay for the host (K7).

        `SQL_SET_ROBOTS` also raises the host's `base_delay_ms` to the crawl-delay,
        which the slot lease uses to space requests to that host.
        """
        delay = cache.crawl_delay_s
        if delay is None:
            delay = crawl_delay_for(cache.body, "*")
        delay_ms = round(min(delay, MAX_CRAWL_DELAY_S) * 1000) if delay is not None else None
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_SET_ROBOTS,
                {
                    "tenant_id": tenant_id,
                    "host": host,
                    "robots_cache": json.dumps({"body": cache.body, "status": cache.status}),
                    "fetched_at": cache.fetched_at,
                    "crawl_delay_ms": delay_ms,
                },
            )
        self._conn.commit()

    # -- events -------------------------------------------------------------

    def append_event(self, event: RunEvent) -> int:
        """Append one event and notify listeners. Returns the sequence number."""
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_APPEND_EVENT,
                {
                    "run_id": event.run_id,
                    "entity_type": event.entity_type,
                    "entity_id": event.entity_id,
                    "old_state": event.old_state,
                    "new_state": event.new_state,
                    "inventory_revision": event.inventory_revision,
                    "reason": event.reason,
                    "attempt": event.attempt,
                    "evidence_ids": json.dumps(event.evidence_ids),
                },
            )
            row = cur.fetchone()
            cur.execute("SELECT pg_notify(%s, %s)", ("run_events", event.run_id))
        self._conn.commit()
        return int(row["seq"]) if row else -1

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
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_RECORD_EDGE,
                {
                    "run_id": run_id,
                    "parent_id": parent_id,
                    "child_id": child_id,
                    "edge_kind": edge_kind,
                    "evidence_id": evidence_id,
                },
            )
        self._conn.commit()

    def find_resource_by_schedule_key(self, run_id: str, schedule_key: str) -> Resource | None:
        """Find a resource by run_id and schedule_key."""
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_FIND_RESOURCE_BY_SCHEDULE_KEY,
                {"run_id": run_id, "schedule_key": schedule_key},
            )
            row = cur.fetchone()
        self._conn.commit()
        if row is None:
            return None
        record = row["record"]
        known = set(Resource.model_fields)
        return Resource.model_validate({k: v for k, v in record.items() if k in known})

    # -- CR-004: Payload hash lookup ---------------------------------------

    def find_resource_by_payload(self, run_id: str, payload_sha256: str) -> Resource | None:
        """Find an already-acquired resource with this payload hash in this run."""
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_FIND_RESOURCE_BY_PAYLOAD,
                {"run_id": run_id, "payload_sha256": payload_sha256},
            )
            row = cur.fetchone()
        self._conn.commit()
        if row is None:
            return None
        record = row["record"]
        known = set(Resource.model_fields)
        return Resource.model_validate({k: v for k, v in record.items() if k in known})

    # -- CR-003: States and regions persistence -----------------------------

    def upsert_state(self, state: State) -> None:
        """Insert or update a dynamic state."""
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_UPSERT_STATE,
                {
                    "state_id": state.state_id,
                    "run_id": state.run_id,
                    "resource_id": state.resource_id,
                    "parent_state_id": state.parent_state_id,
                    "state_key": state.state_key,
                    "status": state.status.value,
                    "record": state.model_dump_json(),
                },
            )
        self._conn.commit()

    def upsert_region(self, region: Region) -> None:
        """Insert or update a region."""
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_UPSERT_REGION,
                {
                    "region_id": region.region_id,
                    "run_id": region.run_id,
                    "resource_id": region.resource_id,
                    "state_id": region.state_id,
                    "region_key": region.region_key,
                    "status": region.status.value,
                    "record": region.model_dump_json(),
                },
            )
        self._conn.commit()

    def get_states(self, run_id: str, resource_id: str) -> list[State]:
        """All dynamic states of a resource."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_GET_STATES, {"run_id": run_id, "resource_id": resource_id})
            rows = cur.fetchall()
        self._conn.commit()
        return [State.model_validate(row["record"]) for row in rows]

    def get_regions(self, run_id: str, resource_id: str) -> list[Region]:
        """All regions of a resource."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_GET_REGIONS, {"run_id": run_id, "resource_id": resource_id})
            rows = cur.fetchall()
        self._conn.commit()
        return [Region.model_validate(row["record"]) for row in rows]

    # -- CR-005: Read side for UrlRecord assembly --------------------------

    def list_resources(self, run_id: str) -> list[Resource]:
        """All latest resource records for a run (one per schedule key)."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_LIST_RESOURCES, {"run_id": run_id})
            rows = cur.fetchall()
        self._conn.commit()
        known = set(Resource.model_fields)
        return [
            Resource.model_validate({k: v for k, v in row["record"].items() if k in known})
            for row in rows
        ]

    def get_summaries(self, resource_id: str) -> dict[str, dict[str, Any]]:
        """All named summaries recorded for a resource."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_GET_RESOURCE, {"resource_id": resource_id})
            row = cur.fetchone()
        self._conn.commit()
        if row is None:
            return {}
        record: dict[str, Any] = row["record"]
        known = set(Resource.model_fields)
        return {k: dict(v) for k, v in record.items() if k not in known and isinstance(v, dict)}

    def get_events(self, run_id: str, entity_id: str | None = None) -> list[RunEvent]:
        """Append-only events for a run, optionally filtered by entity_id, ordered by seq."""
        with self._conn.cursor() as cur:
            if entity_id is not None:
                cur.execute(
                    sql.SQL_GET_EVENTS_FOR_ENTITY,
                    {"run_id": run_id, "entity_id": entity_id},
                )
            else:
                cur.execute(sql.SQL_GET_EVENTS_FOR_RUN, {"run_id": run_id})
            rows = cur.fetchall()
        self._conn.commit()
        events: list[RunEvent] = []
        for r in rows:
            evidence_ids = r["evidence_ids"]
            if isinstance(evidence_ids, str):
                evidence_ids = json.loads(evidence_ids)
            events.append(
                RunEvent(
                    seq=r["seq"],
                    run_id=r["run_id"],
                    entity_type=r["entity_type"],
                    entity_id=r["entity_id"],
                    old_state=r["old_state"],
                    new_state=r["new_state"],
                    inventory_revision=r["inventory_revision"],
                    reason=r["reason"],
                    attempt=r["attempt"],
                    evidence_ids=list(evidence_ids),
                    at=r["at"],
                )
            )
        return events

    def outstanding_resource_ids(self, run_id: str, kinds: Sequence[TaskKind]) -> set[str]:
        """Resource IDs with a PENDING or LEASED task of the given kinds."""
        if not kinds:
            return set()
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_OUTSTANDING_RESOURCE_IDS,
                {"run_id": run_id, "kinds": [k.value for k in kinds]},
            )
            rows = cur.fetchall()
        self._conn.commit()
        return {str(r["res_id"]) for r in rows if r.get("res_id")}


def new_run_id() -> str:
    """Opaque run identifier with a date prefix for humans."""
    return f"run-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
