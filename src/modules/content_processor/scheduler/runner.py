"""Drive the workers of one run until its queue drains (P-1, Lane L6).

The same engine runs a dry run (in-memory ledger) and a durable run
(Postgres `TaskLedger`): it only talks to the shared `LedgerPort`. Stages run
in rounds (fetch, then render, then documents) because later stages create
work for earlier ones (depth-1 follow-ups found in a rendered page are new
FETCH tasks). A run stops when nothing is left, when a round makes no
progress (everything still pending is held by Retry-After, a host delay or an
open circuit), at the round cap, or when a run budget is reached (K9: runtime,
requests, bytes, evidence bytes; see `scheduler/budget.py`). On a budget stop
the run's open tasks are cancelled and each affected resource ends
`BUDGET_EXHAUSTED` with a BUDGET_STOPPED gap, so its URL reads PARTIAL with
the reason. The stop reason is reported, never hidden.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection, Iterable, Mapping
from typing import Any, Literal, Protocol

from pydantic import Field

from src.core.logger import get_logger
from src.core.schemas import StrictModel
from src.integrations.evidence_store import EvidenceStore
from src.modules.content_processor.contracts import RunEvent, RunScope, Task, UrlRecord
from src.modules.content_processor.enums import EntityStatus, GapType, TaskKind
from src.modules.content_processor.ledger.outcomes import close_out_task_resource
from src.modules.content_processor.scheduler.budget import BudgetMeter, BudgetName
from src.modules.content_processor.scheduler.grouping import RunGroups
from src.modules.content_processor.scope.frontier import FrontierPlan
from src.modules.content_processor.verify.detectors import default_detector
from src.modules.content_processor.verify.record import frames_from_evidence, url_records_for_run
from src.modules.content_processor.workers.ports import LedgerPort

__all__ = [
    "STAGE_KINDS",
    "DurableLedger",
    "LazyRenderStage",
    "RunExecution",
    "RunOptions",
    "Stage",
    "StageName",
    "assemble_run_records",
    "execute_run",
    "foreign_open_tasks",
]

_logger = get_logger("content_processor.scheduler")

StageName = Literal["fetch", "render", "docs"]
StopReason = Literal[
    "drained", "no_progress_tasks_held", "max_rounds", "task_cap", "budget_exhausted"
]
STAGE_ORDER: tuple[StageName, ...] = ("fetch", "render", "docs")
STAGE_KINDS: dict[StageName, tuple[TaskKind, ...]] = {
    "fetch": (TaskKind.FETCH,),
    "render": (TaskKind.RENDER,),
    "docs": (TaskKind.EXTRACT_DOC, TaskKind.EXTRACT_IMAGE),
}


class Stage(Protocol):
    """A worker loop (FetchWorker, RenderWorker, DocWorker, LazyRenderStage)."""

    async def run_until_idle(
        self,
        *,
        max_tasks: int | None = None,
        idle_polls: int = 3,
        poll_s: float = 1.0,
        max_wait_s: float = 300.0,
    ) -> int:
        """Process tasks until idle; return how many were processed."""
        ...


class DurableLedger(LedgerPort, Protocol):
    """What the durable commands need beyond the worker port (`TaskLedger`)."""

    def create_schema(self) -> None:
        """Apply the schema (idempotent)."""
        ...

    def create_run(self, run_id: str, scope: RunScope, tool_versions: dict[str, str]) -> None:
        """Insert the run row (idempotent)."""
        ...

    def enqueue(self, tenant_id: str, tasks: Iterable[Task]) -> int:
        """Insert tasks; duplicates by idempotency key are ignored."""
        ...

    def task_counts(self, run_id: str) -> dict[str, int]:
        """Task counts by state for one run."""
        ...

    def reap(self) -> list[dict[str, Any]]:
        """Reclaim expired leases."""
        ...

    def close(self) -> None:
        """Release the connection."""
        ...


class _Startable(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class RunOptions(StrictModel):
    """How long and how hard to drive one run."""

    stages: list[StageName] = Field(default_factory=lambda: list(STAGE_ORDER))
    max_tasks_per_stage: int | None = Field(
        default=None, ge=1, description="Per stage per round; setting it also caps rounds at 1"
    )
    max_rounds: int = Field(default=10, ge=1, le=100)
    idle_polls: int = Field(default=3, ge=1, le=50)
    poll_s: float = Field(default=0.5, gt=0.0)
    max_wait_s: float = Field(
        default=360.0,
        ge=0.0,
        description=(
            "How long a stage waits for held tasks to become ready. The default covers the "
            "render retry schedule (30, 90, 180 s) so unattended runs do not stop mid-retry."
        ),
    )


class RunExecution(StrictModel):
    """What happened while driving a run."""

    run_id: str
    stages: list[StageName]
    rounds: int = Field(ge=0)
    processed: dict[str, int]
    open_tasks_after: int = Field(ge=0, description="Pending or leased tasks of the run's stages")
    stop_reason: StopReason
    elapsed_s: float = Field(ge=0.0)
    budget_exhausted: BudgetName | None = None
    budget_usage: dict[str, dict[str, float]] | None = None
    budget_closed_out: int = Field(default=0, ge=0, description="Resources ended BUDGET_EXHAUSTED")
    group_stats: list[dict[str, Any]] | None = Field(
        default=None, description="Per template group: members and asset-cache counters"
    )


class LazyRenderStage:
    """Starts the browser only if there is RENDER work, then keeps it for later rounds."""

    def __init__(
        self,
        ledger: LedgerPort,
        renderer: _Startable,
        worker_factory: Callable[[], Stage],
        *,
        run_id: str | None = None,
    ) -> None:
        """Bind the stage; nothing is launched until this run has RENDER work."""
        self._ledger = ledger
        self._run_id = run_id
        self._renderer = renderer
        self._factory = worker_factory
        self._worker: Stage | None = None

    @property
    def started(self) -> bool:
        """True once the browser was launched."""
        return self._worker is not None

    async def run_until_idle(
        self,
        *,
        max_tasks: int | None = None,
        idle_polls: int = 3,
        poll_s: float = 1.0,
        max_wait_s: float = 300.0,
    ) -> int:
        """Render pending pages; a no-op (no browser) when nothing is pending."""
        if self._worker is None:
            if self._ledger.pending_count(STAGE_KINDS["render"], run_id=self._run_id) == 0:
                return 0
            await self._renderer.start()
            self._worker = self._factory()
        return await self._worker.run_until_idle(
            max_tasks=max_tasks, idle_polls=idle_polls, poll_s=poll_s, max_wait_s=max_wait_s
        )

    async def close(self) -> None:
        """Stop the browser if it was started."""
        if self._worker is not None:
            await self._renderer.stop()
            self._worker = None


def _kinds(stages: Iterable[StageName]) -> list[TaskKind]:
    return [k for s in STAGE_ORDER if s in set(stages) for k in STAGE_KINDS[s]]


async def _drive_stage(stage: Stage, opts: RunOptions, meter: BudgetMeter | None) -> int:
    """Run one stage; with a meter, one task at a time so the budget is checked before each."""
    if meter is None:
        return await stage.run_until_idle(
            max_tasks=opts.max_tasks_per_stage,
            idle_polls=opts.idle_polls,
            poll_s=opts.poll_s,
            max_wait_s=opts.max_wait_s,
        )
    done = 0
    while meter.exceeded() is None:
        if opts.max_tasks_per_stage is not None and done >= opts.max_tasks_per_stage:
            break
        got = await stage.run_until_idle(
            max_tasks=1,
            idle_polls=opts.idle_polls,
            poll_s=opts.poll_s,
            max_wait_s=min(opts.max_wait_s, meter.remaining_runtime_s()),
        )
        done += got
        if got == 0:
            break
    return done


def _close_out_for_budget(
    ledger: LedgerPort, *, run_id: str, kinds: list[TaskKind], which: BudgetName
) -> int:
    """Cancel the run's open tasks and end their resources BUDGET_EXHAUSTED; return the count."""
    reason = f"budget_exhausted:{which}"
    closed: set[str] = set()
    for task in ledger.cancel_open_tasks(run_id, kinds, reason):
        resource = close_out_task_resource(
            ledger,
            run_id=run_id,
            host=task.host,
            kind=task.kind,
            payload=task.payload,
            status=EntityStatus.BUDGET_EXHAUSTED,
            gap_type=GapType.BUDGET_STOPPED,
            gap_status=EntityStatus.BUDGET_EXHAUSTED,
            reason=reason,
            attempt=task.attempts,
        )
        if resource is not None:
            closed.add(resource.resource_id)
    ledger.append_event(
        RunEvent(
            run_id=run_id,
            entity_type="run",
            entity_id=run_id,
            old_state=None,
            new_state=EntityStatus.BUDGET_EXHAUSTED.value,
            reason=reason,
        )
    )
    return len(closed)


async def execute_run(
    ledger: LedgerPort,
    *,
    run_id: str,
    stages: Mapping[StageName, Stage],
    options: RunOptions | None = None,
    reap: Callable[[], object] | None = None,
    meter: BudgetMeter | None = None,
    grouping: RunGroups | None = None,
) -> RunExecution:
    """Drive the given stages in rounds until the run's queue drains, stalls or hits a budget.

    Args:
        ledger: Shared ledger port; this run's `pending_count` decides when to stop.
        run_id: Run being driven; only its open tasks keep the loop going (CR-007).
        stages: Worker per stage name; stages missing here are not run.
        options: Round and wait limits.
        reap: Called before each round (durable ledgers reclaim expired leases).
        meter: Run budgets (K9). With a meter the budget is checked before every
            task; when one is reached the run stops with `budget_exhausted`.
        grouping: Groups fetched pages and gives grouped pages an asset cache.
            Refreshed after each fetch stage.
    """
    opts = options or RunOptions()
    order = [s for s in STAGE_ORDER if s in stages and s in opts.stages]
    kinds = _kinds(order)
    max_rounds = 1 if opts.max_tasks_per_stage is not None else opts.max_rounds
    processed: dict[str, int] = dict.fromkeys(order, 0)
    started = time.perf_counter()
    rounds = 0
    stop: StopReason = "max_rounds"
    exhausted: BudgetName | None = None
    while rounds < max_rounds:
        if meter is not None and (exhausted := meter.exceeded()) is not None:
            stop = "budget_exhausted"
            break
        rounds += 1
        if reap is not None:
            reap()
        progress = 0
        for name in order:
            n = await _drive_stage(stages[name], opts, meter)
            processed[name] += n
            progress += n
            if grouping is not None and name == "fetch":
                grouping.refresh()
            if meter is not None and meter.exceeded() is not None:
                break
        if meter is not None and (exhausted := meter.exceeded()) is not None:
            stop = "budget_exhausted"
            break
        if ledger.pending_count(kinds, run_id=run_id) == 0:
            stop = "drained"
            break
        if opts.max_tasks_per_stage is not None:
            stop = "task_cap"
            break
        if progress == 0:
            stop = "no_progress_tasks_held"
            break
    closed_out = 0
    if exhausted is not None:
        closed_out = _close_out_for_budget(ledger, run_id=run_id, kinds=kinds, which=exhausted)
    open_after = ledger.pending_count(kinds, run_id=run_id) if kinds else 0
    _logger.info(
        "run_execution_finished",
        extra={
            "run_id": run_id,
            "rounds": rounds,
            "stop": stop,
            "open_after": open_after,
            "budget_exhausted": exhausted,
        },
    )
    return RunExecution(
        run_id=run_id,
        stages=order,
        rounds=rounds,
        processed=processed,
        open_tasks_after=open_after,
        stop_reason=stop,
        elapsed_s=round(time.perf_counter() - started, 3),
        budget_exhausted=exhausted,
        budget_usage=meter.usage() if meter is not None else None,
        budget_closed_out=closed_out,
        group_stats=grouping.stats() if grouping is not None else None,
    )


def assemble_run_records(
    reader: LedgerPort,
    *,
    run_id: str,
    plan: FrontierPlan | None,
    evidence: EvidenceStore,
    stages_that_will_run: Collection[StageName],
    tool_versions: Mapping[str, str] | None = None,
) -> list[UrlRecord]:
    """UrlRecords for a run, counting as outstanding only work that will actually run.

    FETCH work always counts: an unfetched URL is never done. RENDER and
    document work count only when those stages are part of the run, the same
    rule the release gate uses (a skipped render pass is not outstanding work).
    """
    kinds = _kinds({"fetch", *stages_that_will_run})
    outstanding = reader.outstanding_resource_ids(run_id, kinds)
    return url_records_for_run(
        reader,
        run_id=run_id,
        plan=plan,
        frames_for=frames_from_evidence(evidence),
        outstanding=outstanding,
        tool_versions=tool_versions,
        evidence=evidence,
        detector=default_detector(),
    )


def foreign_open_tasks(ledger: DurableLedger, run_id: str) -> int:
    """Open tasks in the database that belong to other runs.

    Informational since CR-007: leases are run-scoped, so concurrent runs no
    longer interfere; durable commands report this count instead of refusing.
    """
    everything = ledger.pending_count(list(TaskKind))
    own = ledger.task_counts(run_id)
    return max(0, everything - own.get("PENDING", 0) - own.get("LEASED", 0))
