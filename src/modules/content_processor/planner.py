"""Turn a frontier plan into ledger tasks.

Stage 0 scope: one FETCH task per frontier entry. Later stages add RENDER,
EXTRACT_* and VERIFY tasks as resources are discovered.
"""

from __future__ import annotations

from src.modules.content_processor.contracts import Task
from src.modules.content_processor.enums import TaskKind
from src.modules.content_processor.ledger.keys import task_key
from src.modules.content_processor.scope.frontier import FrontierPlan

__all__ = ["fetch_tasks_for"]


def fetch_tasks_for(run_id: str, plan: FrontierPlan, *, max_attempts: int = 3) -> list[Task]:
    """One FETCH task per scheduled entry, keyed by schedule key."""
    tasks: list[Task] = []
    for entry in plan.entries:
        tasks.append(
            Task(
                run_id=run_id,
                kind=TaskKind.FETCH,
                idempotency_key=task_key(run_id, TaskKind.FETCH, entry.schedule_key),
                host=entry.host,
                priority=entry.priority,
                max_attempts=max_attempts,
                payload={
                    "schedule_key": entry.schedule_key,
                    "requested_url": entry.requested_url,
                    "aliases": entry.aliases,
                    "mandatory": entry.mandatory,
                    "fragment_routes": entry.fragment_routes,
                },
            )
        )
    return tasks
