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
    Resource,
    RunEvent,
    RunScope,
    Task,
)
from src.modules.content_processor.enums import AccessTier, TaskKind, TaskState
from src.modules.content_processor.governor.escalation import CircuitState, HostAccessState
from src.modules.content_processor.ledger import sql
from src.modules.content_processor.workers.ports import RobotsCache

__all__ = ["TaskLedger", "new_run_id"]

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
                        "tier_ceiling": scope.access_tier_ceiling.value,
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
                    "tier_ceiling": ceiling.value,
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

    def lease(self, worker: str, kinds: Sequence[TaskKind]) -> Task | None:
        """Lease the next eligible task of the given kinds, or None if nothing is ready."""
        slot_kinds = [k.value for k in kinds if k in sql.HOST_SLOT_KINDS]
        plain_kinds = [k.value for k in kinds if k not in sql.HOST_SLOT_KINDS]
        with self._conn.cursor() as cur:
            row: dict[str, Any] | None = None
            if slot_kinds:
                cur.execute(
                    sql.SQL_LEASE_WITH_SLOT,
                    {"kinds": slot_kinds, "worker": worker, "ttl_s": self._ttl},
                )
                row = cur.fetchone()
            if row is None and plain_kinds:
                cur.execute(
                    sql.SQL_LEASE_PLAIN,
                    {"kinds": plain_kinds, "worker": worker, "ttl_s": self._ttl},
                )
                row = cur.fetchone()
        self._conn.commit()
        return _row_to_task(row) if row else None

    def pending_count(self, kinds: Sequence[TaskKind]) -> int:
        """Non-terminal tasks of the given kinds across all runs."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_PENDING_COUNT, {"kinds": [k.value for k in kinds]})
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
        """Reclaim expired leases. Returns the affected rows for event emission."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL_REAP)
            rows = cur.fetchall()
        self._conn.commit()
        for row in rows:
            _logger.warning("task_reclaimed", extra=dict(row))
        return rows

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
                    "record": resource.model_dump_json(),
                },
            )
        self._conn.commit()

    def upsert_blocks(self, resource_id: str, blocks: Sequence[ContentBlock]) -> None:
        """Insert or update extracted blocks (one row per region and method)."""
        with self._conn.cursor() as cur:
            for block in blocks:
                cur.execute(
                    sql.SQL_UPSERT_BLOCK,
                    {
                        "extraction_id": block.block_id,
                        "run_id": block.state_id.split("|", 1)[0],
                        "region_id": block.region_id,
                        "block_key": block.block_key,
                        "method": block.method,
                        "tool_version": block.tool_version,
                        "record": block.model_dump_json(),
                    },
                )
        self._conn.commit()

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
        return RobotsCache(
            body=payload.get("body"),
            status=payload.get("status"),
            fetched_at=row["robots_fetched_at"],
        )

    def set_robots(self, tenant_id: str, host: str, cache: RobotsCache) -> None:
        """Store the robots.txt fetch result for the host."""
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL_SET_ROBOTS,
                {
                    "tenant_id": tenant_id,
                    "host": host,
                    "robots_cache": json.dumps({"body": cache.body, "status": cache.status}),
                    "fetched_at": cache.fetched_at,
                    "crawl_delay_ms": None,
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


def new_run_id() -> str:
    """Opaque run identifier with a date prefix for humans."""
    return f"run-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
