"""Drive the workers of one run until its queue drains (P-1, Lane L6).

The same engine runs a dry run (in-memory ledger) and a durable run
(Postgres `TaskLedger`): it only talks to the shared `LedgerPort`. Fetch and
render run in rounds (fetch, then render) because later stages create work for
earlier ones (depth-1 follow-ups found in a rendered page are new FETCH tasks).
Documents and images (EXTRACT_DOC, EXTRACT_IMAGE) send nothing to a site, so by
default they are read in parallel with those rounds, as soon as a fetch hands
them over, instead of waiting for the last render (`RunOptions.overlap_docs`;
False keeps the older fetch -> render -> docs rounds). A run stops when nothing
is left, when a round makes no progress on either chain (everything still
pending is held by Retry-After, a host delay or an open circuit), at the round
cap, or when a run budget is reached (K9: runtime, requests, bytes, evidence
bytes; see `scheduler/budget.py`). On a budget stop the run's open tasks are
cancelled and each affected resource ends `BUDGET_EXHAUSTED` with a
BUDGET_STOPPED gap, so its URL reads PARTIAL with the reason. The stop reason
is reported, never hidden.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from typing import Any, Literal, Protocol

from pydantic import Field

from src.core.config import get_settings
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
RunMode = Literal["sequential", "overlapped"]
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
    overlap_docs: bool = Field(
        default=True,
        description=(
            "Read documents and images while pages are still being fetched and rendered. "
            "Extraction sends nothing to a site, so the per-host pace is unchanged; False "
            "drives the stages one after another (fetch, render, docs) in rounds."
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
    mode: RunMode = Field(
        default="sequential", description="Whether documents were read alongside fetch/render"
    )
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


async def _drive_stage(
    stage: Stage, opts: RunOptions, meter: BudgetMeter | None, *, limit: int | None = None
) -> int:
    """Run one stage until it is idle; return how many tasks it processed.

    `limit` caps this call (the per-stage task cap less what earlier calls did).
    With a meter the stage runs in small steps so the budget is checked between
    them; a step is one task, or `stage.concurrent_tasks` for a worker that
    reads several documents at once (those tasks send nothing to a site, so the
    overshoot a step can cause is local work only).
    """
    cap = opts.max_tasks_per_stage if limit is None else limit
    if meter is None:
        return await stage.run_until_idle(
            max_tasks=cap,
            idle_polls=opts.idle_polls,
            poll_s=opts.poll_s,
            max_wait_s=opts.max_wait_s,
        )
    done = 0
    step = max(1, int(getattr(stage, "concurrent_tasks", 1)))
    while meter.exceeded() is None:
        if cap is not None and done >= cap:
            break
        got = await stage.run_until_idle(
            max_tasks=step if cap is None else min(step, cap - done),
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
    started = time.perf_counter()
    drive = _RunDrive(ledger, run_id=run_id, stages=stages, order=order, opts=opts)
    drive.reap, drive.meter, drive.grouping = reap, meter, grouping
    if opts.overlap_docs and "docs" in order and len(order) > 1:
        mode: RunMode = "overlapped"
        await drive.overlapped()
    else:
        mode = "sequential"
        await drive.sequential()
    closed_out = 0
    if drive.exhausted is not None:
        closed_out = _close_out_for_budget(
            ledger, run_id=run_id, kinds=kinds, which=drive.exhausted
        )
    open_after = ledger.pending_count(kinds, run_id=run_id) if kinds else 0
    _logger.info(
        "run_execution_finished",
        extra={
            "run_id": run_id,
            "rounds": drive.rounds,
            "stop": drive.stop,
            "mode": mode,
            "open_after": open_after,
            "budget_exhausted": drive.exhausted,
        },
    )
    return RunExecution(
        run_id=run_id,
        stages=order,
        rounds=drive.rounds,
        processed=drive.processed,
        open_tasks_after=open_after,
        stop_reason=drive.stop,
        elapsed_s=round(time.perf_counter() - started, 3),
        mode=mode,
        budget_exhausted=drive.exhausted,
        budget_usage=meter.usage() if meter is not None else None,
        budget_closed_out=closed_out,
        group_stats=grouping.stats() if grouping is not None else None,
    )


class _RunDrive:
    """The state of one `execute_run`: rounds, per-stage counts, stop reason, budget."""

    def __init__(
        self,
        ledger: LedgerPort,
        *,
        run_id: str,
        stages: Mapping[StageName, Stage],
        order: list[StageName],
        opts: RunOptions,
    ) -> None:
        self.ledger = ledger
        self.run_id = run_id
        self.stages = stages
        self.order = order
        self.opts = opts
        self.reap: Callable[[], object] | None = None
        self.meter: BudgetMeter | None = None
        self.grouping: RunGroups | None = None
        self.max_rounds = 1 if opts.max_tasks_per_stage is not None else opts.max_rounds
        self.processed: dict[str, int] = dict.fromkeys(order, 0)
        self.rounds = 0
        self.stop: StopReason = "max_rounds"
        self.exhausted: BudgetName | None = None

    def _budget_hit(self) -> bool:
        if self.meter is None:
            return False
        if (which := self.meter.exceeded()) is not None:
            self.exhausted = which
            self.stop = "budget_exhausted"
            return True
        return False

    def _pending(self, names: Iterable[StageName]) -> int:
        kinds = _kinds(names)
        return self.ledger.pending_count(kinds, run_id=self.run_id) if kinds else 0

    def _remaining(self, name: StageName) -> int | None:
        cap = self.opts.max_tasks_per_stage
        return None if cap is None else max(0, cap - self.processed[name])

    async def _round(self, names: list[StageName]) -> int:
        """One pass over `names` in stage order; return the tasks processed."""
        self.rounds += 1
        if self.reap is not None:
            self.reap()
        progress = 0
        for name in names:
            n = await _drive_stage(
                self.stages[name], self.opts, self.meter, limit=self._remaining(name)
            )
            self.processed[name] += n
            progress += n
            if self.grouping is not None and name == "fetch":
                self.grouping.refresh()
            if self.meter is not None and self.meter.exceeded() is not None:
                break
        return progress

    async def sequential(self) -> None:
        """Fetch, render and docs one after another, in rounds (the original engine)."""
        while self.rounds < self.max_rounds:
            if self._budget_hit():
                return
            progress = await self._round(self.order)
            if self._budget_hit():
                return
            if self._pending(self.order) == 0:
                self.stop = "drained"
                return
            if self.opts.max_tasks_per_stage is not None:
                self.stop = "task_cap"
                return
            if progress == 0:
                self.stop = "no_progress_tasks_held"
                return

    async def overlapped(self) -> None:
        """Fetch/render rounds on one chain, documents on another, at the same time.

        The document chain keeps going while the page chain can still hand it
        work; once the page chain has stopped it finishes what is queued. A round
        with no page progress stops the run only when the documents made none
        either in the meantime; the final stop reason covers both chains.
        """
        pages: list[StageName] = [s for s in self.order if s != "docs"]
        page_chain_done = asyncio.Event()
        docs_progress = 0
        docs_stop: StopReason | None = None

        async def page_chain() -> None:
            seen_docs = 0
            try:
                while self.rounds < self.max_rounds:
                    if self._budget_hit():
                        return
                    progress = await self._round(pages)
                    if self._budget_hit():
                        return
                    if self._pending(pages) == 0:
                        self.stop = "drained"
                        return
                    if self.opts.max_tasks_per_stage is not None:
                        self.stop = "task_cap"
                        return
                    if progress == 0 and docs_progress == seen_docs:
                        self.stop = "no_progress_tasks_held"
                        return
                    seen_docs = docs_progress
            finally:
                page_chain_done.set()

        async def docs_chain() -> None:
            nonlocal docs_progress, docs_stop
            passes_after_pages = 0
            while True:
                if self.meter is not None and self.meter.exceeded() is not None:
                    return
                limit = self._remaining("docs")
                if limit is not None and limit <= 0:
                    docs_stop = "task_cap"
                    return
                n = await _drive_stage(self.stages["docs"], self.opts, self.meter, limit=limit)
                self.processed["docs"] += n
                docs_progress += n
                if not page_chain_done.is_set():
                    await asyncio.sleep(self.opts.poll_s)
                    continue
                passes_after_pages += 1
                if self._pending(["docs"]) == 0:
                    return
                if n == 0:
                    docs_stop = "no_progress_tasks_held"  # held by a retry backoff
                    return
                if passes_after_pages >= self.max_rounds:
                    docs_stop = "max_rounds"
                    return

        await asyncio.gather(page_chain(), docs_chain())
        if self.exhausted is None and self.stop == "drained" and docs_stop is not None:
            # The pages finished but documents are still open: say why.
            self.stop = docs_stop


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
        # R6 OCRs every rendered page's screenshot; several at once, same regions (K-perf).
        detect_workers=get_settings().cp_doc_parallel_tasks,
    )


def foreign_open_tasks(ledger: DurableLedger, run_id: str) -> int:
    """Open tasks in the database that belong to other runs.

    Informational since CR-007: leases are run-scoped, so concurrent runs no
    longer interfere; durable commands report this count instead of refusing.
    """
    everything = ledger.pending_count(list(TaskKind))
    own = ledger.task_counts(run_id)
    return max(0, everything - own.get("PENDING", 0) - own.get("LEASED", 0))
