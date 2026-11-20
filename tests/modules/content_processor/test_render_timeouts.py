"""The browser's time caps are settings, and a slow page is told apart from a broken site.

Measured on 2026-11-16: with the link at ~30 KB/s, 1 MB GEP pages took 49-80 s to
DOMContentLoaded while `RenderLimits.navigation_timeout_s` was a fixed 30 s. Every
render failed, five in a row opened the host circuit, and each batch waited out its
`--max-wait`. The caps now come from `CP_RENDER_*`, read when a batch starts, and a
render that only ran out of time says `render_timeout:<step>` instead of looking like
a transport error.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.core.config import reset_settings_cache
from src.integrations.browser_client import (
    BrowserRenderer,
    PlaywrightError,
    RenderLimits,
    RenderResult,
    is_render_timeout,
)
from src.integrations.evidence_store import LocalEvidenceStore
from src.modules.content_processor import cli
from src.modules.content_processor.contracts import Authorization, Budgets, Resource, RunScope, Task
from src.modules.content_processor.enums import (
    AccessTier,
    AuthorizationKind,
    EntityStatus,
    PriorityClass,
    TaskKind,
)
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.scheduler.budget import (
    SLEEP_GAP_MARGIN_S,
    SLEEP_GAP_S,
    BudgetMeter,
    sleep_gap_for,
)
from src.modules.content_processor.workers.render_worker import RenderWorker

HOST = "testserver"
URL = "http://testserver/app"


def _set_render_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    reset_settings_cache()


# -- The CLI builds the browser's caps from the settings ---------------------------


def test_defaults_match_the_former_fixed_limits(scope: RunScope) -> None:
    limits = cli.render_limits()
    assert limits == RenderLimits()
    assert (limits.navigation_timeout_s, limits.max_page_time_s, limits.settle_window_s) == (
        30.0,
        150.0,
        12.0,
    )
    assert cli.build_renderer(scope)._limits == RenderLimits()


def test_render_limits_come_from_the_env(scope: RunScope, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_render_env(
        monkeypatch,
        CP_RENDER_NAVIGATION_TIMEOUT_S="120",
        CP_RENDER_MAX_PAGE_TIME_S="300",
        CP_RENDER_SETTLE_WINDOW_S="20",
    )
    limits = cli.render_limits()
    assert limits.navigation_timeout_s == 120.0
    assert limits.max_page_time_s == 300.0
    assert limits.settle_window_s == 20.0
    # Everything the settings do not name keeps its default.
    assert limits.settle_min_quiet_s == RenderLimits().settle_min_quiet_s
    assert limits.max_asset_bytes == RenderLimits().max_asset_bytes
    renderer = cli.build_renderer(scope)
    assert renderer._limits.navigation_timeout_s == 120.0
    assert renderer.limits.max_page_time_s == 300.0, "the render worker reads this per-page cap"


def test_a_low_page_cap_is_raised_above_the_navigation_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_render_env(
        monkeypatch, CP_RENDER_NAVIGATION_TIMEOUT_S="200", CP_RENDER_MAX_PAGE_TIME_S="60"
    )
    limits = cli.render_limits()
    assert limits.navigation_timeout_s == 200.0
    assert limits.max_page_time_s == 230.0


def test_explicit_limits_from_a_caller_are_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_render_env(monkeypatch, CP_RENDER_NAVIGATION_TIMEOUT_S="120")
    mine = RenderLimits(navigation_timeout_s=7.0, max_page_time_s=40.0)
    renderer = BrowserRenderer(user_agent="t", host_checker=lambda _h: (True, ""), limits=mine)
    assert renderer._limits is mine
    assert renderer.limits.navigation_timeout_s == 7.0


# -- The meter's sleep gap grows with the render cap ---------------------------------


def test_sleep_gap_follows_the_render_cap() -> None:
    assert sleep_gap_for(150.0, 360.0) == SLEEP_GAP_S, "the defaults add up to the constant"
    assert sleep_gap_for(600.0, 360.0) == 360.0 + 600.0 + SLEEP_GAP_MARGIN_S
    assert sleep_gap_for(30.0, 10.0) == SLEEP_GAP_S, "never below the constant"


def test_a_long_render_allowed_by_the_cap_is_not_counted_as_sleep() -> None:
    now = [1000.0]
    meter = BudgetMeter(
        Budgets(max_runtime_s=7200), clock=lambda: now[0], sleep_gap_s=sleep_gap_for(600.0)
    )
    now[0] += 900.0  # one 600 s render plus a wait for held tasks: the batch was working
    assert meter.elapsed_s == 900.0
    assert meter.excluded_s == 0.0
    now[0] += 1100.0  # longer than any legitimate wait: asleep
    assert meter.elapsed_s == 900.0
    assert meter.excluded_s == 1100.0


# -- A render that ran out of time says so ---------------------------------------------


def _renderer(limits: RenderLimits) -> BrowserRenderer:
    renderer = BrowserRenderer(user_agent="t", host_checker=lambda _h: (True, ""), limits=limits)

    async def no_recycle() -> None:
        return None

    renderer._recycle_if_needed = no_recycle  # type: ignore[method-assign]
    return renderer


async def test_navigation_timeout_is_a_render_timeout() -> None:
    renderer = _renderer(RenderLimits(navigation_timeout_s=30.0))

    async def slow_goto(*_a: Any, **_k: Any) -> RenderResult:
        msg = "Page.goto: Timeout 30000ms exceeded.\nCall log:\n- navigating"
        raise PlaywrightTimeoutError(msg)

    renderer._render_inner = slow_goto  # type: ignore[method-assign]
    result = await renderer.render(URL)
    assert not result.rendered
    assert result.transport_error == "render_timeout:Page.goto: Timeout 30000ms exceeded."
    assert is_render_timeout(result.transport_error)


async def test_the_whole_page_cap_is_a_render_timeout() -> None:
    renderer = _renderer(RenderLimits(navigation_timeout_s=30.0, max_page_time_s=0.05))

    async def never_finishes(*_a: Any, **_k: Any) -> RenderResult:
        await asyncio.sleep(5)
        raise AssertionError("unreachable")

    renderer._render_inner = never_finishes  # type: ignore[method-assign]
    result = await renderer.render(URL)
    assert result.transport_error == "render_timeout:page_time_0s"
    assert is_render_timeout(result.transport_error)


async def test_a_broken_connection_is_not_a_render_timeout() -> None:
    renderer = _renderer(RenderLimits())

    async def reset(*_a: Any, **_k: Any) -> RenderResult:
        msg = "Page.goto: net::ERR_CONNECTION_RESET at http://testserver/app"
        raise PlaywrightError(msg)

    renderer._render_inner = reset  # type: ignore[method-assign]
    result = await renderer.render(URL)
    assert result.transport_error is not None
    assert result.transport_error.startswith("PlaywrightError:Page.goto: net::ERR_CONNECTION_RESET")
    assert not is_render_timeout(result.transport_error)


# -- The worker carries the distinction into the task and the record -------------------


class FakeRenderer:
    tier_name = "T3_FIDELITY_BROWSER"

    def __init__(self, result: RenderResult) -> None:
        self.result = result

    async def render(self, url: str, on_page: Any = None) -> RenderResult:
        return self.result


def _scope() -> RunScope:
    return RunScope(
        tenant_id="t1",
        input_urls=[URL],
        hosts_in_scope=[HOST],
        authorization=Authorization(
            kind=AuthorizationKind.PUBLIC_THIRD_PARTY,
            reference="public",
            approved_by="lead",
            approved_at=datetime(2026, 10, 15, tzinfo=UTC),
            hosts=[HOST],
        ),
        egress_allow_private=True,
    )


def _seed(ledger: MemoryLedger) -> Resource:
    res = Resource(
        resource_id="res-app",
        run_id="run-t",
        requested_url=URL,
        final_url=URL,
        schedule_key=URL,
        status=EntityStatus.EXTRACTED_UNVERIFIED,
        status_reason="source_extracted;needs_render",
        acquired_at_tier=AccessTier.T1_POLITE_HTTP,
    )
    ledger.upsert_resource(res, HOST)
    ledger.enqueue(
        [
            Task(
                run_id="run-t",
                kind=TaskKind.RENDER,
                idempotency_key="render-1",
                host=HOST,
                priority=PriorityClass.HTML_PAGE,
                max_attempts=1,
                payload={
                    "resource_id": res.resource_id,
                    "requested_url": res.final_url,
                    "schedule_key": res.schedule_key,
                },
            )
        ]
    )
    return res


@pytest.mark.parametrize(
    ("transport_error", "expected"),
    [
        (
            "render_timeout:Page.goto: Timeout 30000ms exceeded.",
            "render_timeout:Page.goto: Timeout 30000ms exceeded.",
        ),
        ("render_timeout:page_time_150s", "render_timeout:page_time_150s"),
        (
            "PlaywrightError:net::ERR_CONNECTION_RESET",
            "transport:PlaywrightError:net::ERR_CONNECTION_RESET",
        ),
        (None, "empty_document"),
    ],
)
async def test_failure_reasons_tell_slow_from_broken(
    tmp_path: Path, transport_error: str | None, expected: str
) -> None:
    ledger = MemoryLedger(base_delay_s=0)
    res = _seed(ledger)
    failing = RenderResult(requested_url=URL, final_url=URL, transport_error=transport_error)
    worker = RenderWorker(
        ledger=ledger,
        renderer=FakeRenderer(failing),
        evidence=LocalEvidenceStore(tmp_path, "t1"),
        scope=_scope(),
        run_id="run-t",
        worker_id="r1",
        render_retry_delays_s=(),
    )
    await worker.run_until_idle(idle_polls=1, poll_s=0.01, max_wait_s=30)
    assert ledger.task_counts() == {"FAILED": 1}
    updated = ledger.get_resource(res.resource_id)
    assert updated is not None and updated.status is EntityStatus.PARTIAL
    assert updated.status_reason == f"render_failed:{expected}"
    (task,) = ledger._tasks.values()
    assert task.last_error == f"render:{expected}"
    # The breaker still counted the failure: a timeout protects the site like any other.
    assert ledger.hosts[HOST].consecutive_failures == 1
