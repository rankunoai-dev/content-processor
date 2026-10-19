"""Close out the resource behind a task that will never run again (K8, K9).

A task can end without its worker writing a final status: the reaper
quarantines it after its last lease expired (poison, K8), or the run engine
cancels it when a run budget is exhausted (K9). Either way the owning resource
must not stay in flight or vanish from the status stream. This module finds
that resource from the task payload (creating the row for a FETCH that never
ran), lowers an in-flight status to the terminal one given, records a typed gap
and appends a resource event. A resource that already reached a terminal
status keeps it; the gap carries the new outcome into its UrlRecord.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from src.modules.content_processor.contracts import Gap, Resource, RunEvent
from src.modules.content_processor.enums import (
    TERMINAL_ENTITY_STATUSES,
    EntityStatus,
    GapType,
    TaskKind,
)
from src.modules.content_processor.ledger.keys import resource_id_for
from src.modules.content_processor.workers.ports import LedgerPort

__all__ = ["ACQUISITION_KINDS", "close_out_task_resource", "resource_for_task"]

# Kinds whose failure means the resource itself was never acquired.
ACQUISITION_KINDS: frozenset[TaskKind] = frozenset({TaskKind.FETCH, TaskKind.UNLOCK})


def resource_for_task(
    ledger: LedgerPort, *, run_id: str, kind: TaskKind, payload: Mapping[str, Any]
) -> Resource | None:
    """The resource a task works on.

    Follow-up stages carry `resource_id`; FETCH tasks carry `schedule_key`.
    A FETCH that never ran has no row yet, so one is built from its payload.
    """
    resource_id = payload.get("resource_id")
    if resource_id:
        found = ledger.get_resource(str(resource_id))
        if found is not None:
            return found
    schedule_key = payload.get("schedule_key")
    if not schedule_key:
        return None
    found = ledger.find_resource_by_schedule_key(run_id, str(schedule_key))
    if found is not None or kind not in ACQUISITION_KINDS:
        return found
    return Resource(
        resource_id=resource_id_for(run_id, str(schedule_key)),
        run_id=run_id,
        requested_url=str(payload.get("requested_url") or schedule_key),
        schedule_key=str(schedule_key),
        aliases=[str(a) for a in payload.get("aliases", [])],
        mandatory=bool(payload.get("mandatory", False)),
    )


def close_out_task_resource(
    ledger: LedgerPort,
    *,
    run_id: str,
    host: str,
    kind: TaskKind,
    payload: Mapping[str, Any],
    status: EntityStatus,
    gap_type: GapType,
    gap_status: EntityStatus,
    reason: str,
    attempt: int = 0,
) -> Resource | None:
    """Make the task's outcome visible on its resource; returns the resource or None.

    Args:
        ledger: Ledger port (in-memory or durable).
        run_id: Run of the task.
        host: Task host (the resource row is keyed with it).
        kind: Task kind.
        payload: Task payload.
        status: Terminal status for a resource that is still in flight.
        gap_type: Gap recorded on the resource.
        gap_status: Status the gap implies for the URL verdict.
        reason: Reason on the status, the gap and the event.
        attempt: Task attempt count, for the event.
    """
    resource = resource_for_task(ledger, run_id=run_id, kind=kind, payload=payload)
    if resource is None:
        return None
    old = resource.status
    if old not in TERMINAL_ENTITY_STATUSES:
        resource.status = status
        resource.status_reason = reason
        ledger.upsert_resource(resource, host)
    digest = hashlib.sha256(
        f"{run_id}|{resource.resource_id}|{gap_type.value}|{kind.value}".encode()
    ).hexdigest()[:16]
    ledger.record_gap(
        Gap(
            gap_id=f"gap-{digest}",
            run_id=run_id,
            resource_id=resource.resource_id,
            gap_type=gap_type,
            status=gap_status,
            reason=reason,
            retry_eligible=gap_type is GapType.BUDGET_STOPPED,
            tier_reached=resource.acquired_at_tier,
        )
    )
    ledger.append_event(
        RunEvent(
            run_id=run_id,
            entity_type="resource",
            entity_id=resource.resource_id,
            old_state=old.value,
            new_state=resource.status.value,
            reason=reason,
            attempt=attempt,
        )
    )
    return resource
