"""Run engine and run reports (Lane L6) against the in-memory ledger and the fixture app."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from fixtures.server import app
from src.integrations.evidence_store import LocalEvidenceStore
from src.integrations.http_client import FetchLimits, HttpFetcher
from src.modules.content_processor.contracts import Authorization, RunScope, Task
from src.modules.content_processor.enums import (
    AuthorizationKind,
    PriorityClass,
    TaskKind,
    UrlBadge,
)
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.planner import fetch_tasks_for
from src.modules.content_processor.scheduler import (
    LazyRenderStage,
    RunOptions,
    assemble_run_records,
    execute_run,
    foreign_open_tasks,
    write_run_outputs,
)
from src.modules.content_processor.scope.frontier import build_frontier
from src.modules.content_processor.workers.fetch_worker import FetchWorker
from src.modules.content_processor.workers.ports import RobotsCache

HOST = "127.0.0.1"
RUN = "run-l6"


def _scope(paths: list[str]) -> RunScope:
    return RunScope(
        tenant_id="t1",
        input_urls=[f"http://{HOST}:8765{p}" for p in paths],
        hosts_in_scope=[HOST],
        authorization=Authorization(
            kind=AuthorizationKind.PUBLIC_THIRD_PARTY,
            reference="fixtures",
            approved_by="lead",
            approved_at=datetime(2026, 10, 16, tzinfo=UTC),
            hosts=[HOST],
        ),
        egress_allow_private=True,
    )


def _fetch_stage(ledger: MemoryLedger, scope: RunScope, evidence: LocalEvidenceStore) -> Any:
    fetcher = HttpFetcher(
        host_checker=lambda _h: (True, "fixture"),
        user_agent="l6-test",
        transport=httpx.ASGITransport(app=app),
        limits=FetchLimits(max_bytes=1_000_000, total_timeout_s=5),
    )
    return fetcher, FetchWorker(
        ledger=ledger,
        fetcher=fetcher,
        evidence=evidence,
        scope=scope,
        run_id=RUN,
        worker_id="l6-fetch",
    )


def _ledger(scope: RunScope) -> tuple[MemoryLedger, Any]:
    plan = build_frontier(scope, resolver=lambda _h: [HOST])
    ledger = MemoryLedger(ceiling=scope.access_tier_ceiling, base_delay_s=0.0)
    ledger.set_robots(
        scope.tenant_id,
        HOST,
        RobotsCache(
            body="User-agent: *\nDisallow: /private/\n", status=200, fetched_at=datetime.now(UTC)
        ),
    )
    ledger.enqueue(fetch_tasks_for(RUN, plan))
    return ledger, plan


FAST = RunOptions(stages=["fetch"], idle_polls=1, poll_s=0.01, max_wait_s=0.5)


async def test_fetch_stage_drains_and_records_cover_every_requested_url(tmp_path: Path):
    scope = _scope(["/pages/static_ok", "/downloads", "/private/secret", "/pay-per-crawl"])
    scope = scope.model_copy(update={"input_urls": [*scope.input_urls, "mailto:x@y.z"]})
    ledger, plan = _ledger(scope)
    evidence = LocalEvidenceStore(tmp_path, scope.tenant_id)
    fetcher, worker = _fetch_stage(ledger, scope, evidence)
    try:
        execution = await execute_run(ledger, run_id=RUN, stages={"fetch": worker}, options=FAST)
    finally:
        await fetcher.aclose()
    assert execution.stop_reason == "drained"
    assert execution.open_tasks_after == 0
    # /downloads promises typed follow-ups: depth-1 fetches happen in the same run
    assert execution.processed["fetch"] > 4
    records = assemble_run_records(
        ledger, run_id=RUN, plan=plan, evidence=evidence, stages_that_will_run=["fetch"]
    )
    requested = {r.requested_url for r in records}
    assert set(scope.input_urls) <= requested, "every requested URL has a record"
    by_url = {r.requested_url: r for r in records}
    assert by_url["mailto:x@y.z"].badge is UrlBadge.EXCLUDED
    assert by_url[f"http://{HOST}:8765/private/secret"].badge is UrlBadge.EXCLUDED
    assert by_url[f"http://{HOST}:8765/pay-per-crawl"].badge is UrlBadge.EXCLUDED
    # rendering is not part of this run, so its queued RENDER tasks are not outstanding
    assert by_url[f"http://{HOST}:8765/pages/static_ok"].badge is not UrlBadge.RUNNING


async def test_records_count_render_work_as_outstanding_only_if_render_will_run(tmp_path: Path):
    scope = _scope(["/pages/static_ok"])
    ledger, plan = _ledger(scope)
    evidence = LocalEvidenceStore(tmp_path, scope.tenant_id)
    fetcher, worker = _fetch_stage(ledger, scope, evidence)
    try:
        await execute_run(ledger, run_id=RUN, stages={"fetch": worker}, options=FAST)
    finally:
        await fetcher.aclose()
    assert ledger.pending_count([TaskKind.RENDER]) == 1
    url = f"http://{HOST}:8765/pages/static_ok"
    without = assemble_run_records(
        ledger, run_id=RUN, plan=plan, evidence=evidence, stages_that_will_run=["fetch"]
    )
    with_render = assemble_run_records(
        ledger, run_id=RUN, plan=plan, evidence=evidence, stages_that_will_run=["fetch", "render"]
    )
    assert next(r for r in without if r.requested_url == url).badge is not UrlBadge.RUNNING
    assert next(r for r in with_render if r.requested_url == url).badge is UrlBadge.RUNNING


class _Held:
    """A stage whose work never becomes ready (Retry-After, open circuit)."""

    def __init__(self) -> None:
        self.calls = 0

    async def run_until_idle(self, **_: Any) -> int:
        self.calls += 1
        return 0


async def test_stalled_queue_stops_with_reason_not_a_hang():
    ledger = MemoryLedger(base_delay_s=0.0)
    ledger.enqueue(
        [
            Task(
                run_id=RUN,
                kind=TaskKind.FETCH,
                idempotency_key="held",
                host=HOST,
                priority=PriorityClass.HTML_PAGE,
                payload={"requested_url": "http://x/", "schedule_key": "http://x/"},
            )
        ]
    )
    stage = _Held()
    execution = await execute_run(ledger, run_id=RUN, stages={"fetch": stage})
    assert execution.stop_reason == "no_progress_tasks_held"
    assert execution.rounds == 1 and stage.calls == 1
    assert execution.open_tasks_after == 1


async def test_task_cap_runs_a_single_round():
    ledger = MemoryLedger(base_delay_s=0.0)
    ledger.enqueue(
        [
            Task(
                run_id=RUN,
                kind=TaskKind.FETCH,
                idempotency_key="x",
                host=HOST,
                priority=PriorityClass.HTML_PAGE,
                payload={"requested_url": "http://x/", "schedule_key": "http://x/"},
            )
        ]
    )

    class _One:
        async def run_until_idle(self, **_: Any) -> int:
            return 1

    execution = await execute_run(
        ledger,
        run_id=RUN,
        stages={"fetch": _One()},
        options=RunOptions(max_tasks_per_stage=1),
        reap=lambda: None,
    )
    assert execution.stop_reason == "task_cap" and execution.rounds == 1


class _FakeRenderer:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


async def test_lazy_render_stage_launches_browser_only_when_needed():
    ledger = MemoryLedger(base_delay_s=0.0)
    renderer = _FakeRenderer()
    built: list[int] = []

    class _Worker:
        async def run_until_idle(self, **_: Any) -> int:
            return 7

    def factory() -> Any:
        built.append(1)
        return _Worker()

    stage = LazyRenderStage(ledger, renderer, factory)
    assert await stage.run_until_idle() == 0
    assert renderer.started == 0 and not stage.started
    ledger.enqueue(
        [
            Task(
                run_id=RUN,
                kind=TaskKind.RENDER,
                idempotency_key="r",
                host=HOST,
                priority=PriorityClass.HTML_PAGE,
                payload={"resource_id": "res", "requested_url": "http://x/", "schedule_key": "k"},
            )
        ]
    )
    assert await stage.run_until_idle() == 7
    assert await stage.run_until_idle() == 7
    assert renderer.started == 1 and built == [1]
    await stage.close()
    await stage.close()
    assert renderer.stopped == 1


class _FakeDurable:
    def __init__(self, global_open: int, own: dict[str, int]) -> None:
        self.global_open = global_open
        self.own = own

    def pending_count(self, kinds: Any) -> int:
        return self.global_open

    def task_counts(self, run_id: str) -> dict[str, int]:
        return self.own


@pytest.mark.parametrize(
    ("global_open", "own", "foreign"),
    [(0, {}, 0), (3, {"PENDING": 2, "LEASED": 1}, 0), (5, {"PENDING": 2, "DONE": 9}, 3)],
)
def test_foreign_open_tasks(global_open: int, own: dict[str, int], foreign: int):
    assert foreign_open_tasks(_FakeDurable(global_open, own), "r") == foreign  # type: ignore[arg-type]


async def test_write_run_outputs_writes_four_files(tmp_path: Path):
    scope = _scope(["/pages/static_ok", "/pages/gated_whitepaper"])
    ledger, plan = _ledger(scope)
    evidence = LocalEvidenceStore(tmp_path, scope.tenant_id)
    fetcher, worker = _fetch_stage(ledger, scope, evidence)
    try:
        await execute_run(ledger, run_id=RUN, stages={"fetch": worker}, options=FAST)
    finally:
        await fetcher.aclose()
    records = assemble_run_records(
        ledger, run_id=RUN, plan=plan, evidence=evidence, stages_that_will_run=["fetch"]
    )
    out = tmp_path / "out" / "run.json"
    outputs = write_run_outputs(
        out, reader=ledger, run_id=RUN, records=records, report={"run_id": RUN}
    )
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["url_records"]["urls"] == 2
    assert sum(report["url_records"]["by_badge"].values()) == 2
    rows = [json.loads(line) for line in Path(outputs.records).read_text("utf-8").splitlines()]
    assert {r["requested_url"] for r in rows} == set(scope.input_urls)
    assert all(isinstance(r["blocks"], list) for r in rows), "full records for cli report"
    assert report["records"]["count"] == 2
    content = Path(outputs.content).read_text("utf-8")
    assert "Eligibility applies only to annual plans." in content
    queue = Path(outputs.review_queue).read_text("utf-8")
    assert "lead_gen_form_wall" in queue and outputs.review_items >= 1
