"""Grouping wired into the run: RunGroups, the scheduler, the render worker and the browser."""

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from fixtures.server import app
from src.integrations.browser_client import BrowserRenderer
from src.integrations.evidence_store import LocalEvidenceStore
from src.modules.content_processor.contracts import Authorization, Resource, RunScope
from src.modules.content_processor.enums import AuthorizationKind, EntityStatus
from src.modules.content_processor.grouping.asset_cache import AssetCache
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.scheduler.grouping import RunGroups
from src.modules.content_processor.scheduler.runner import execute_run

RUN = "run-grp"
SERVICES = Path(__file__).resolve().parents[3] / "fixtures" / "services"
SLUGS = ["wealth-planning", "retirement-income", "estate-planning", "tax-strategies"]


def _seed(ledger: MemoryLedger, store: LocalEvidenceStore, pages: dict[str, str]) -> None:
    """Record fetched HTML pages for the run, as the fetch worker would."""
    for url, body in pages.items():
        ref = store.put(body.encode(), "text/html")
        ledger.upsert_resource(
            Resource(
                resource_id=f"res-{abs(hash(url))}",
                run_id=RUN,
                requested_url=url,
                schedule_key=url,
                http_status=200,
                sniffed_mime="text/html",
                payload_sha256=ref.sha256,
                status=EntityStatus.IN_PROGRESS,
            ),
            "h",
        )


def _service_pages() -> dict[str, str]:
    base = "http://h/services/"
    return {base + s: (SERVICES / f"{s}.html").read_text(encoding="utf-8") for s in SLUGS}


# -- RunGroups ----------------------------------------------------------------------


def test_refresh_groups_fetched_html_and_hands_out_one_cache_per_group(tmp_path: Path) -> None:
    ledger = MemoryLedger(base_delay_s=0)
    store = LocalEvidenceStore(tmp_path, "t")
    _seed(ledger, store, _service_pages())
    _seed(ledger, store, {"http://h/other": "<html><body><p>lonely</p></body></html>"})
    groups = RunGroups(ledger, store, run_id=RUN)
    groups.refresh()

    gid = groups.group_of("http://h/services/wealth-planning")
    assert gid is not None and gid.startswith("grp-")
    assert all(groups.group_of(u) == gid for u in _service_pages())
    assert groups.group_of("http://h/other") is None  # singleton: no cache

    first = groups.asset_store_for("http://h/services/wealth-planning")
    second = groups.asset_store_for("http://h/services/tax-strategies")
    assert isinstance(first, AssetCache) and first is second
    assert groups.asset_store_for("http://h/other") is None


def test_group_budget_denies_a_cache_once_the_total_is_spent(tmp_path: Path) -> None:
    ledger = MemoryLedger(base_delay_s=0)
    store = LocalEvidenceStore(tmp_path, "t")
    _seed(ledger, store, _service_pages())
    groups = RunGroups(ledger, store, run_id=RUN, total_bytes=0)
    groups.refresh()
    assert groups.asset_store_for("http://h/services/wealth-planning") is None
    stats = groups.stats()
    assert stats and stats[0]["cache"] == "over_budget"


def test_non_html_and_missing_evidence_are_not_grouped(tmp_path: Path) -> None:
    ledger = MemoryLedger(base_delay_s=0)
    store = LocalEvidenceStore(tmp_path, "t")
    ledger.upsert_resource(
        Resource(
            resource_id="res-pdf",
            run_id=RUN,
            requested_url="http://h/doc.pdf",
            schedule_key="http://h/doc.pdf",
            sniffed_mime="application/pdf",
            payload_sha256="0" * 64,
            status=EntityStatus.IN_PROGRESS,
        ),
        "h",
    )
    groups = RunGroups(ledger, store, run_id=RUN)
    groups.refresh()
    assert groups.stats() == []
    assert groups.group_of("http://h/doc.pdf") is None


# -- scheduler ------------------------------------------------------------------------


class _FetchSeeder:
    """A fetch stage that only records pages, so the scheduler refreshes groups after it."""

    def __init__(
        self, ledger: MemoryLedger, store: LocalEvidenceStore, pages: dict[str, str]
    ) -> None:
        self._ledger = ledger
        self._store = store
        self._pages = pages
        self._done = False

    async def run_until_idle(self, **_: Any) -> int:
        if self._done:
            return 0
        self._done = True
        _seed(self._ledger, self._store, self._pages)
        return 1


async def test_scheduler_refreshes_groups_after_fetch_and_reports_stats(tmp_path: Path) -> None:
    ledger = MemoryLedger(base_delay_s=0)
    store = LocalEvidenceStore(tmp_path, "t")
    groups = RunGroups(ledger, store, run_id=RUN)
    stage = _FetchSeeder(ledger, store, _service_pages())
    execution = await execute_run(
        ledger,
        run_id=RUN,
        stages={"fetch": stage},
        grouping=groups,
    )
    assert execution.group_stats is not None
    assert [row["members"] for row in execution.group_stats] == [4]
    assert groups.group_of("http://h/services/tax-strategies") is not None


async def test_scheduler_without_grouping_is_unchanged(tmp_path: Path) -> None:
    ledger = MemoryLedger(base_delay_s=0)
    execution = await execute_run(ledger, run_id=RUN, stages={})
    assert execution.group_stats is None


# -- render worker and browser ---------------------------------------------------------


class _RecordingRenderer:
    """Renderer fake that records whether an asset store was passed."""

    tier_name = "T3_FIDELITY_BROWSER"

    def __init__(self) -> None:
        self.stores: list[object | None] = []

    async def render(self, url: str, on_page: Any = None, asset_store: Any = None) -> Any:
        self.stores.append(asset_store)
        raise RuntimeError("stop after recording")


def test_render_worker_passes_the_group_store_only_when_asked(tmp_path: Path) -> None:
    from src.modules.content_processor.workers.render_worker import RenderWorker

    ledger = MemoryLedger(base_delay_s=0)
    store = LocalEvidenceStore(tmp_path, "t")
    renderer = _RecordingRenderer()
    worker = RenderWorker(
        ledger=ledger,
        renderer=renderer,  # type: ignore[arg-type]
        evidence=store,
        scope=_scope(),
        run_id=RUN,
        worker_id="w",
        asset_store_for=lambda url: AssetCache(group_id="g") if "services" in url else None,
    )
    assert worker._asset_store_for is not None  # noqa: S101 - wiring check
    assert worker._asset_store_for("http://h/services/a") is not None
    assert worker._asset_store_for("http://h/x") is None


def _scope() -> RunScope:
    return RunScope(
        tenant_id="t1",
        input_urls=["http://h/"],
        hosts_in_scope=["h"],
        authorization=Authorization(
            kind=AuthorizationKind.PUBLIC_THIRD_PARTY,
            reference="public",
            approved_by="lead",
            approved_at=datetime(2026, 10, 15, tzinfo=UTC),
            hosts=["h"],
        ),
    )


@pytest.fixture(scope="module")
def server_url() -> Any:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


async def test_browser_renderer_serves_static_assets_from_the_group_store(server_url: str) -> None:
    renderer = BrowserRenderer(
        user_agent="Mozilla/5.0 Chrome/130.0", host_checker=lambda h: (True, "ok"), channel=None
    )
    await renderer.start()
    cache = AssetCache(group_id="grp-test")
    try:
        for slug in SLUGS:
            result = await renderer.render(f"{server_url}/services/{slug}", asset_store=cache)
            assert result.rendered
    finally:
        await renderer.stop()
    assert cache.stats.cache_hits >= 15  # five static assets served from the store on pages 2-4
    assert len(cache) == 5
