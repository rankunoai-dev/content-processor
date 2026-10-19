"""Platform hardening: crawl-delay persistence (K7), poison close-out (K8), run budgets (K9)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from fixtures.server import app
from src.integrations.browser_client import RenderResult
from src.integrations.evidence_store import LocalEvidenceStore
from src.integrations.http_client import FetchLimits, FetchResult, HttpFetcher
from src.modules.content_processor.contracts import Budgets, Resource, RunScope, Task
from src.modules.content_processor.enums import (
    EntityStatus,
    GapType,
    PriorityClass,
    TaskKind,
    TaskState,
    UrlBadge,
)
from src.modules.content_processor.governor.robots import MAX_CRAWL_DELAY_S, crawl_delay_for
from src.modules.content_processor.ledger import sql
from src.modules.content_processor.ledger.keys import resource_id_for
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.ledger.outcomes import (
    close_out_task_resource,
    resource_for_task,
)
from src.modules.content_processor.ledger.tasks import TaskLedger
from src.modules.content_processor.planner import fetch_tasks_for
from src.modules.content_processor.scheduler.budget import (
    BudgetMeter,
    MeteredEvidenceStore,
    MeteredFetcher,
    MeteredRenderer,
)
from src.modules.content_processor.scheduler.runner import RunOptions, execute_run
from src.modules.content_processor.scope.frontier import build_frontier
from src.modules.content_processor.verify.record import url_records_for_run
from src.modules.content_processor.workers.fetch_worker import FetchWorker, _resource_id
from src.modules.content_processor.workers.ports import RobotsCache

ROOT = Path(__file__).resolve().parents[3]
RUN = "run-h"
NOW = datetime(2026, 10, 19, tzinfo=UTC)
ROBOTS = (
    "User-agent: RankunoContentBot\nCrawl-delay: 5\nDisallow: /x/\n\n"
    "User-agent: *\nCrawl-delay: 2\n"
)


# -- K7: crawl-delay ------------------------------------------------------------------


def test_crawl_delay_uses_the_bots_group_then_star_and_is_capped():
    assert crawl_delay_for(ROBOTS, "RankunoContentBot") == 5.0
    assert crawl_delay_for(ROBOTS, "OtherBot") == 2.0
    assert crawl_delay_for("User-agent: *\nCrawl-delay: 600\n", "x") == MAX_CRAWL_DELAY_S
    assert crawl_delay_for("User-agent: *\nDisallow: /\n", "x") is None
    assert crawl_delay_for(None, "x") is None and crawl_delay_for("", "x") is None


def test_memory_ledger_prefers_the_cached_delay_and_falls_back_to_star():
    ledger = MemoryLedger()
    ledger.set_robots(
        "t", "a", RobotsCache(body=ROBOTS, status=200, fetched_at=NOW, crawl_delay_s=5)
    )
    ledger.set_robots("t", "b", RobotsCache(body=ROBOTS, status=200, fetched_at=NOW))
    ledger.set_robots("t", "c", RobotsCache(body=None, status=404, fetched_at=NOW))
    assert ledger._crawl_delay == {"a": 5.0, "b": 2.0}  # noqa: SLF001


def test_memory_ledger_spaces_leases_by_crawl_delay():
    ledger = MemoryLedger(base_delay_s=0.0)
    ledger.set_robots("t", "h", RobotsCache(body=None, status=200, fetched_at=NOW, crawl_delay_s=3))
    ledger.enqueue(
        [
            Task(
                run_id=RUN,
                kind=TaskKind.FETCH,
                idempotency_key=f"k{i}",
                host="h",
                priority=PriorityClass.HTML_PAGE,
                payload={"schedule_key": f"s{i}", "requested_url": f"http://h/{i}"},
            )
            for i in range(2)
        ]
    )
    first = ledger.lease("w", [TaskKind.FETCH])
    assert first is not None
    ledger.complete(first.id or 0, "w")
    assert ledger.lease("w", [TaskKind.FETCH]) is None  # held by the 3 s crawl-delay


def _mock_task_ledger() -> tuple[TaskLedger, MagicMock]:
    with patch("psycopg.connect") as connect:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        connect.return_value = conn
        return TaskLedger("postgresql://t:t@localhost/t"), cur


def test_task_ledger_persists_crawl_delay_in_ms():
    ledger, cur = _mock_task_ledger()
    ledger.set_robots(
        "t", "h", RobotsCache(body=ROBOTS, status=200, fetched_at=NOW, crawl_delay_s=5)
    )
    sql_text, params = cur.execute.call_args[0]
    assert sql_text == sql.SQL_SET_ROBOTS and params["crawl_delay_ms"] == 5000
    ledger.set_robots("t", "h", RobotsCache(body=ROBOTS, status=200, fetched_at=NOW))
    assert cur.execute.call_args[0][1]["crawl_delay_ms"] == 2000  # `*` group fallback
    ledger.set_robots("t", "h", RobotsCache(body=None, status=404, fetched_at=NOW))
    assert cur.execute.call_args[0][1]["crawl_delay_ms"] is None
    assert "base_delay_ms = GREATEST(base_delay_ms" in sql.SQL_SET_ROBOTS


def test_task_ledger_reads_crawl_delay_back():
    ledger, cur = _mock_task_ledger()
    cur.fetchone.return_value = {
        "robots_fetched_at": NOW,
        "robots_cache": json.dumps({"body": ROBOTS, "status": 200}),
        "crawl_delay_ms": 1500,
    }
    cache = ledger.get_robots("t", "h")
    assert cache is not None and cache.crawl_delay_s == 1.5
    cur.fetchone.return_value = {**cur.fetchone.return_value, "crawl_delay_ms": None}
    again = ledger.get_robots("t", "h")
    assert again is not None and again.crawl_delay_s is None


def _fixture_scope(**budgets: Any) -> RunScope:
    raw = json.loads((ROOT / "fixtures" / "scopes" / "local_fixtures.json").read_text("utf-8"))
    raw["input_urls"] = [u for u in raw["input_urls"] if "/pages/" in u][:6]
    raw["budgets"] = {**raw["budgets"], **budgets}
    return RunScope.model_validate(raw)


def _fetch_worker(ledger: MemoryLedger, scope: RunScope, tmp: Path, meter: BudgetMeter | None):
    raw = HttpFetcher(
        host_checker=lambda _h: (True, "fixture"),
        user_agent="test/1.0",
        transport=httpx.ASGITransport(app=app),
        limits=FetchLimits(max_bytes=1_000_000, total_timeout_s=5),
    )
    fetcher: Any = MeteredFetcher(raw, meter) if meter is not None else raw
    store = LocalEvidenceStore(tmp, scope.tenant_id)
    evidence: Any = MeteredEvidenceStore(store, meter) if meter is not None else store
    worker = FetchWorker(
        ledger=ledger, fetcher=fetcher, evidence=evidence, scope=scope, run_id=RUN, worker_id="w"
    )
    return worker, fetcher


async def test_fetch_worker_stores_the_fixture_crawl_delay(tmp_path: Path):
    scope = _fixture_scope()
    plan = build_frontier(scope, resolver=lambda _h: ["127.0.0.1"])
    ledger = MemoryLedger(base_delay_s=0.0)
    ledger.enqueue(fetch_tasks_for(RUN, plan)[:1])
    worker, fetcher = _fetch_worker(ledger, scope, tmp_path, None)
    await worker.run_until_idle(max_tasks=1, idle_polls=1, poll_s=0, max_wait_s=0)
    await fetcher.aclose()
    (cache,) = ledger.robots.values()
    assert cache.crawl_delay_s == 1.0  # fixtures/server.py serves Crawl-delay: 1


def test_shared_resource_id_matches_the_fetch_worker():
    assert resource_id_for(RUN, "http://h/a") == _resource_id(RUN, "http://h/a")


# -- K8: poison close-out -----------------------------------------------------------


def _fetch_payload(key: str = "http://h/a") -> dict[str, Any]:
    return {"schedule_key": key, "requested_url": key, "aliases": ["http://h/a?x=1"]}


def test_poisoned_fetch_that_never_ran_gets_a_failed_resource_and_gap():
    ledger = MemoryLedger()
    res = close_out_task_resource(
        ledger,
        run_id=RUN,
        host="h",
        kind=TaskKind.FETCH,
        payload=_fetch_payload(),
        status=EntityStatus.FAILED,
        gap_type=GapType.ACQUISITION_FAILED,
        gap_status=EntityStatus.FAILED,
        reason="poison:FETCH:worker_exception:boom",
        attempt=3,
    )
    assert res is not None and res.resource_id == resource_id_for(RUN, "http://h/a")
    stored = ledger.get_resource(res.resource_id)
    assert stored is not None and stored.status is EntityStatus.FAILED
    assert stored.aliases == ["http://h/a?x=1"]
    (gap,) = ledger.get_gaps(res.resource_id)
    assert (gap.gap_type, gap.status, gap.reason) == (
        GapType.ACQUISITION_FAILED,
        EntityStatus.FAILED,
        "poison:FETCH:worker_exception:boom",
    )
    (event,) = ledger.events
    assert (event.old_state, event.new_state, event.attempt) == ("DISCOVERED", "FAILED", 3)
    (record,) = url_records_for_run(ledger, run_id=RUN, frames_for=lambda _r: [])
    assert record.badge is UrlBadge.FAILED


def test_poisoned_render_keeps_the_acquired_status_and_lowers_the_verdict():
    ledger = MemoryLedger()
    acquired = Resource(
        resource_id="res-1",
        run_id=RUN,
        requested_url="http://h/a",
        final_url="http://h/a",
        schedule_key="http://h/a",
        sniffed_mime="text/html",
        payload_sha256="abc",
        status=EntityStatus.EXTRACTED_UNVERIFIED,
        status_reason="source_extracted",
    )
    ledger.upsert_resource(acquired, "h")
    close_out_task_resource(
        ledger,
        run_id=RUN,
        host="h",
        kind=TaskKind.RENDER,
        payload={"resource_id": "res-1"},
        status=EntityStatus.FAILED,
        gap_type=GapType.ACQUISITION_FAILED,
        gap_status=EntityStatus.PARTIAL,
        reason="poison:RENDER:lease_expired",
    )
    stored = ledger.get_resource("res-1")
    assert stored is not None and stored.status is EntityStatus.EXTRACTED_UNVERIFIED
    (record,) = url_records_for_run(ledger, run_id=RUN, frames_for=lambda _r: [])
    assert record.badge is UrlBadge.PARTIAL
    assert any(g.reason == "poison:RENDER:lease_expired" for g in record.gaps)


def test_resource_for_task_lookups():
    ledger = MemoryLedger()
    assert resource_for_task(ledger, run_id=RUN, kind=TaskKind.RENDER, payload={}) is None
    assert (
        resource_for_task(
            ledger, run_id=RUN, kind=TaskKind.RENDER, payload={"resource_id": "missing"}
        )
        is None
    )
    built = resource_for_task(ledger, run_id=RUN, kind=TaskKind.FETCH, payload=_fetch_payload())
    assert built is not None and built.status is EntityStatus.DISCOVERED
    ledger.upsert_resource(built.model_copy(update={"status": EntityStatus.QUEUED}), "h")
    found = resource_for_task(ledger, run_id=RUN, kind=TaskKind.FETCH, payload=_fetch_payload())
    assert found is not None and found.status is EntityStatus.QUEUED


def test_reaper_emits_task_events_and_closes_out_poison():
    ledger, cur = _mock_task_ledger()
    cur.fetchall.return_value = [
        {
            "id": 7,
            "state": "PENDING",
            "run_id": RUN,
            "kind": "FETCH",
            "host": "h",
            "attempts": 1,
            "max_attempts": 3,
            "payload": _fetch_payload(),
            "last_error": "lease_expired",
        },
        {
            "id": 8,
            "state": "QUARANTINED",
            "run_id": RUN,
            "kind": "FETCH",
            "host": "h",
            "attempts": 3,
            "max_attempts": 3,
            "payload": _fetch_payload("http://h/b"),
            "last_error": None,
        },
    ]
    events: list[Any] = []
    with (
        patch.object(TaskLedger, "append_event", lambda _self, e: events.append(e) or 1),
        patch("src.modules.content_processor.ledger.tasks.close_out_task_resource") as close,
    ):
        rows = ledger.reap()
    assert [r["id"] for r in rows] == [7, 8]
    assert [(e.entity_type, e.entity_id, e.old_state, e.new_state) for e in events] == [
        ("task", "7", "LEASED", "PENDING"),
        ("task", "8", "LEASED", "QUARANTINED"),
    ]
    close.assert_called_once()
    kwargs = close.call_args.kwargs
    assert kwargs["status"] is EntityStatus.FAILED and kwargs["gap_status"] is EntityStatus.FAILED
    assert kwargs["reason"] == "poison:FETCH:lease_expired"
    assert kwargs["payload"]["schedule_key"] == "http://h/b"
    assert "t.payload" in sql.SQL_REAP and "t.last_error" in sql.SQL_REAP


def test_reaper_poison_on_a_later_stage_is_a_partial_gap():
    ledger, _ = _mock_task_ledger()
    with patch("src.modules.content_processor.ledger.tasks.close_out_task_resource") as close:
        ledger._close_out_poison(  # noqa: SLF001
            {
                "run_id": RUN,
                "host": "h",
                "kind": "RENDER",
                "payload": {"resource_id": "res-1"},
                "last_error": "render:timeout",
                "attempts": 2,
            }
        )
    assert close.call_args.kwargs["gap_status"] is EntityStatus.PARTIAL
    assert close.call_args.kwargs["reason"] == "poison:RENDER:render:timeout"


def test_task_ledger_cancel_open_tasks():
    ledger, cur = _mock_task_ledger()
    cur.fetchall.return_value = []
    assert ledger.cancel_open_tasks(RUN, [TaskKind.FETCH, TaskKind.RENDER], "budget") == []
    sql_text, params = cur.execute.call_args[0]
    assert sql_text == sql.SQL_CANCEL_OPEN_TASKS
    assert params == {"run_id": RUN, "kinds": ["FETCH", "RENDER"], "reason": "budget"}


# -- K9: budget meter and wrappers -------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_meter_reports_the_first_exhausted_budget_in_order():
    clock = Clock()
    meter = BudgetMeter(
        Budgets(max_runtime_s=60, max_requests=2, max_bytes=100, max_evidence_bytes=10),
        clock=clock,
    )
    assert meter.exceeded() is None and meter.remaining_runtime_s() == 60
    meter.evidence_bytes = 10
    assert meter.exceeded() == "evidence_bytes"
    meter.bytes_received = 100
    assert meter.exceeded() == "bytes"
    meter.requests = 2
    assert meter.exceeded() == "requests"
    clock.now = 61
    assert meter.exceeded() == "runtime" and meter.remaining_runtime_s() == 0
    assert meter.usage()["requests"] == {"used": 2, "limit": 2}
    seeded = BudgetMeter(Budgets(max_bytes=100), seed_bytes=150)
    assert seeded.exceeded() == "bytes"


class FakeFetcher:
    tier_name = "T1_POLITE_HTTP"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str] | None]] = []
        self.closed = False
        self.extra = "delegated"

    async def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResult:
        self.calls.append((url, headers))
        return FetchResult(requested_url=url, final_url=url, received_bytes=40)

    async def aclose(self) -> None:
        self.closed = True


async def test_metered_fetcher_counts_and_passes_headers_through():
    meter = BudgetMeter(Budgets())
    inner = FakeFetcher()
    fetcher = MeteredFetcher(inner, meter)
    await fetcher.fetch("http://h/a", headers={"If-None-Match": "x"})
    await fetcher.fetch("http://h/b")
    assert inner.calls == [("http://h/a", {"If-None-Match": "x"}), ("http://h/b", None)]
    assert (meter.requests, meter.bytes_received) == (2, 80)
    assert fetcher.tier_name == "T1_POLITE_HTTP" and fetcher.extra == "delegated"
    await fetcher.aclose()
    assert inner.closed


class FakeRenderer:
    tier_name = "T3_FIDELITY_BROWSER"

    def __init__(self) -> None:
        self.started = self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def render(self, url: str, on_page: Any = None) -> RenderResult:
        return RenderResult(
            requested_url=url, final_url=url, dom_html=b"x" * 30, screenshot_jpeg=b"y" * 5
        )


async def test_metered_renderer_and_evidence_store(tmp_path: Path):
    meter = BudgetMeter(Budgets())
    inner = FakeRenderer()
    renderer = MeteredRenderer(inner, meter)
    await renderer.start()
    await renderer.render("http://h/a")
    await renderer.stop()
    assert inner.started and inner.stopped and renderer.tier_name == "T3_FIDELITY_BROWSER"
    assert (meter.requests, meter.bytes_received) == (1, 35)
    store = MeteredEvidenceStore(LocalEvidenceStore(tmp_path, "t"), meter)
    ref = store.put(b"hello", "text/plain")
    store.put(b"hello", "text/plain")  # reused: free
    assert meter.evidence_bytes == 5
    assert store.exists(ref.sha256) and store.get(ref.sha256) == b"hello"


# -- K9: budgets in the run engine -----------------------------------------------------


async def _budget_run(tmp: Path, meter_budgets: Budgets, clock: Clock | None = None):
    scope = _fixture_scope()
    plan = build_frontier(scope, resolver=lambda _h: ["127.0.0.1"])
    ledger = MemoryLedger(base_delay_s=0.0)
    ledger.set_robots(
        scope.tenant_id,
        "127.0.0.1",
        RobotsCache(body="User-agent: *\nDisallow: /private/\n", status=200, fetched_at=NOW),
    )
    ledger.enqueue(fetch_tasks_for(RUN, plan))
    meter = BudgetMeter(meter_budgets, clock=clock) if clock else BudgetMeter(meter_budgets)
    worker, fetcher = _fetch_worker(ledger, scope, tmp, meter)
    try:
        execution = await execute_run(
            ledger,
            run_id=RUN,
            stages={"fetch": worker},
            options=RunOptions(stages=["fetch"], idle_polls=1, poll_s=0.01, max_wait_s=0.5),
            meter=meter,
        )
    finally:
        await fetcher.aclose()
    return ledger, plan, execution


async def test_request_budget_stops_the_run_and_closes_out_the_rest(tmp_path: Path):
    ledger, plan, execution = await _budget_run(tmp_path, Budgets(max_requests=3))
    assert execution.stop_reason == "budget_exhausted"
    assert execution.budget_exhausted == "requests"
    assert execution.processed == {"fetch": 3}
    assert execution.open_tasks_after == 0 and execution.budget_closed_out == 3
    assert execution.budget_usage is not None and execution.budget_usage["requests"]["used"] == 3
    assert ledger.task_counts().get("CANCELLED") == 3
    records = url_records_for_run(ledger, run_id=RUN, plan=plan, frames_for=lambda _r: [])
    assert len(records) == 6
    stopped = [r for r in records if any(g.gap_type is GapType.BUDGET_STOPPED for g in r.gaps)]
    assert len(stopped) == 3
    for record in stopped:
        assert record.badge is UrlBadge.PARTIAL
        assert any(g.reason == "budget_exhausted:requests" for g in record.gaps)
    assert all(r.badge is not UrlBadge.RUNNING for r in records)
    run_events = [e for e in ledger.events if e.entity_type == "run"]
    assert [(e.new_state, e.reason) for e in run_events] == [
        ("BUDGET_EXHAUSTED", "budget_exhausted:requests")
    ]


async def test_runtime_budget_stops_before_the_first_task(tmp_path: Path):
    clock = Clock()
    clock.now = 0.0
    ledger, _, execution = await _budget_run(tmp_path, Budgets(max_runtime_s=60), clock)
    assert execution.stop_reason == "drained"  # the fake clock never advances
    clock2 = Clock()
    meter_budgets = Budgets(max_runtime_s=60)
    clock2.now = 0.0

    async def run_with_expired_clock():
        scope = _fixture_scope()
        plan = build_frontier(scope, resolver=lambda _h: ["127.0.0.1"])
        led = MemoryLedger(base_delay_s=0.0)
        led.enqueue(fetch_tasks_for(RUN, plan))
        meter = BudgetMeter(meter_budgets, clock=clock2)
        clock2.now = 61.0
        worker, fetcher = _fetch_worker(led, scope, tmp_path, meter)
        try:
            return led, await execute_run(
                led, run_id=RUN, stages={"fetch": worker}, options=RunOptions(), meter=meter
            )
        finally:
            await fetcher.aclose()

    led, expired = await run_with_expired_clock()
    assert expired.stop_reason == "budget_exhausted" and expired.budget_exhausted == "runtime"
    assert expired.rounds == 0 and expired.processed == {"fetch": 0}
    assert expired.budget_closed_out == 6
    assert all(r.status is EntityStatus.BUDGET_EXHAUSTED for r in led.resources.values())


async def test_task_cap_still_applies_under_a_meter(tmp_path: Path):
    scope = _fixture_scope()
    plan = build_frontier(scope, resolver=lambda _h: ["127.0.0.1"])
    ledger = MemoryLedger(base_delay_s=0.0)
    ledger.enqueue(fetch_tasks_for(RUN, plan))
    meter = BudgetMeter(Budgets())
    worker, fetcher = _fetch_worker(ledger, scope, tmp_path, meter)
    try:
        execution = await execute_run(
            ledger,
            run_id=RUN,
            stages={"fetch": worker},
            options=RunOptions(max_tasks_per_stage=2, idle_polls=1, poll_s=0.01),
            meter=meter,
        )
    finally:
        await fetcher.aclose()
    assert execution.stop_reason == "task_cap" and execution.processed == {"fetch": 2}
    assert execution.budget_exhausted is None and execution.budget_closed_out == 0
    assert ledger.pending_count([TaskKind.FETCH]) == 4  # the 2 done fetches queued renders


def test_cancel_open_tasks_in_memory_leaves_other_runs_and_kinds():
    ledger = MemoryLedger()

    def task(run: str, kind: TaskKind, key: str) -> Task:
        return Task(
            run_id=run,
            kind=kind,
            idempotency_key=key,
            host="h",
            priority=PriorityClass.HTML_PAGE,
            payload={"schedule_key": key},
        )

    ledger.enqueue(
        [
            task(RUN, TaskKind.FETCH, "a"),
            task(RUN, TaskKind.RENDER, "b"),
            task("other", TaskKind.FETCH, "c"),
        ]
    )
    cancelled = ledger.cancel_open_tasks(RUN, [TaskKind.FETCH], "budget_exhausted:bytes")
    assert [(t.idempotency_key, t.state, t.last_error) for t in cancelled] == [
        ("a", TaskState.CANCELLED, "budget_exhausted:bytes")
    ]
    assert ledger.pending_count([TaskKind.FETCH, TaskKind.RENDER]) == 2


@pytest.mark.parametrize("budget", ["max_requests", "max_bytes", "max_evidence_bytes"])
def test_budgets_require_positive_limits(budget):
    with pytest.raises(ValueError):
        Budgets(**{budget: 0})
