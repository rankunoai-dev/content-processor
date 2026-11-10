"""Images are kept from the browser's own loads instead of being downloaded twice.

A page's images used to be downloaded one by one (one second apart) and then loaded a
second time by the browser that renders the page. Now, when the run renders, the FETCH
tasks of a page's images ride along with its RENDER task; images the browser loaded
become resources from that one visit, and any it did not load is downloaded as before.
"""

from __future__ import annotations

import io
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from PIL import Image

from fixtures.server import app
from src.integrations.browser_client import (
    BrowserRenderer,
    CapturedAsset,
    RenderLimits,
    RenderResult,
)
from src.integrations.evidence_store import LocalEvidenceStore
from src.integrations.http_client import FetchLimits, HttpFetcher
from src.modules.content_processor.contracts import Authorization, Resource, RunScope, Task
from src.modules.content_processor.enums import (
    AccessTier,
    AuthorizationKind,
    EntityStatus,
    PriorityClass,
    TaskKind,
)
from src.modules.content_processor.ledger.keys import resource_id_for, task_key
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.scheduler.capture_journal import CaptureJournal
from src.modules.content_processor.workers.fetch_worker import FetchWorker, is_browser_capturable
from src.modules.content_processor.workers.ports import RobotsCache
from src.modules.content_processor.workers.render_worker import DEFERRED_FETCHES, RenderWorker

HOST = "testserver"
RUN = "run-1p"
PAGE = "http://testserver/downloads"
LOGO = "http://testserver/files/logo.png"
HOTLINK = "http://testserver/files/hotlink.png"


def _png(size: tuple[int, int] = (40, 30), colour: str = "white") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


def _scope() -> RunScope:
    return RunScope(
        tenant_id="t1",
        input_urls=[PAGE],
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


def _tasks(ledger: MemoryLedger) -> list[Task]:
    return list(ledger._tasks.values())  # noqa: SLF001 - the in-memory ledger's own table


# -- the download pass hands images to the render -----------------------------------


async def _fetch_downloads(tmp_path: Path, *, browser_handoff: bool) -> MemoryLedger:
    ledger = MemoryLedger(base_delay_s=0)
    fetcher = HttpFetcher(
        host_checker=lambda _h: (True, "test"),
        user_agent="test/1.0",
        transport=httpx.ASGITransport(app=app),
        limits=FetchLimits(max_bytes=100_000, total_timeout_s=5),
    )
    worker = FetchWorker(
        ledger=ledger,
        fetcher=fetcher,
        evidence=LocalEvidenceStore(tmp_path, "t1"),
        scope=_scope(),
        run_id=RUN,
        worker_id="w1",
        browser_handoff=browser_handoff,
    )
    ledger.enqueue(
        [
            Task(
                run_id=RUN,
                kind=TaskKind.FETCH,
                idempotency_key="seed",
                host=HOST,
                priority=PriorityClass.HTML_PAGE,
                payload={
                    "requested_url": PAGE,
                    "schedule_key": PAGE,
                    "aliases": [],
                    "mandatory": False,
                },
            )
        ]
    )
    try:
        await worker.run_until_idle(max_tasks=1, idle_polls=1, poll_s=0, max_wait_s=0)
    finally:
        await fetcher.aclose()
    return ledger


async def test_images_ride_along_with_the_render_when_the_run_renders(tmp_path: Path):
    ledger = await _fetch_downloads(tmp_path, browser_handoff=True)
    tasks = _tasks(ledger)
    render = next(t for t in tasks if t.kind is TaskKind.RENDER)
    deferred = [Task.model_validate(d) for d in render.payload[DEFERRED_FETCHES]]
    assert {t.payload["requested_url"] for t in deferred} == {LOGO, HOTLINK}
    assert all(is_browser_capturable(t) for t in deferred)
    fetched = {
        t.payload["requested_url"]
        for t in tasks
        if t.kind is TaskKind.FETCH and t.payload.get("depth") == 1
    }
    assert not any(u.endswith(".png") for u in fetched), "no separate download of the images"
    assert any(u.endswith("brochure.pdf") for u in fetched), "documents still download as before"


async def test_without_a_browser_images_are_downloaded_as_before(tmp_path: Path):
    ledger = await _fetch_downloads(tmp_path, browser_handoff=False)
    tasks = _tasks(ledger)
    render = next(t for t in tasks if t.kind is TaskKind.RENDER)
    assert DEFERRED_FETCHES not in render.payload
    fetched = {t.payload["requested_url"] for t in tasks if t.kind is TaskKind.FETCH}
    assert {LOGO, HOTLINK} <= fetched


# -- the render turns loaded images into resources ------------------------------------


class FakeRenderer:
    tier_name = "T3_FIDELITY_BROWSER"

    def __init__(self, result: RenderResult) -> None:
        self.result = result

    async def render(self, url: str, on_page: Any = None) -> RenderResult:
        return self.result


def _fetch_task(url: str) -> Task:
    return Task(
        run_id=RUN,
        kind=TaskKind.FETCH,
        idempotency_key=task_key(RUN, TaskKind.FETCH, url, "expect=image/png"),
        host=HOST,
        priority=PriorityClass.EMBEDDED_ASSET,
        max_attempts=3,
        payload={
            "schedule_key": url,
            "requested_url": url,
            "aliases": [],
            "mandatory": False,
            "depth": 1,
            "parent_resource_id": "res-page",
            "edge_kind": "embeds",
            "expected_mime": "image/png",
            "expected_mime_source": "url_extension",
        },
    )


def _page_render(
    result: RenderResult,
    deferred: list[Task],
    *,
    robots: str | None = "User-agent: *\nAllow: /",
    max_attempts: int = 2,
):
    ledger = MemoryLedger(base_delay_s=0)
    page = Resource(
        resource_id="res-page",
        run_id=RUN,
        requested_url=PAGE,
        final_url=PAGE,
        schedule_key=PAGE,
        status=EntityStatus.EXTRACTED_UNVERIFIED,
        status_reason="source_extracted",
        acquired_at_tier=AccessTier.T1_POLITE_HTTP,
    )
    ledger.upsert_resource(page, HOST)
    if robots is not None:
        ledger.set_robots(
            "t1", HOST, RobotsCache(body=robots, status=200, fetched_at=datetime.now(UTC))
        )
    ledger.enqueue(
        [
            Task(
                run_id=RUN,
                kind=TaskKind.RENDER,
                idempotency_key="render-1",
                host=HOST,
                priority=PriorityClass.HTML_PAGE,
                max_attempts=max_attempts,
                payload={
                    "resource_id": page.resource_id,
                    "requested_url": PAGE,
                    "schedule_key": PAGE,
                    DEFERRED_FETCHES: [t.model_dump(mode="json") for t in deferred],
                },
            )
        ]
    )
    return ledger, FakeRenderer(result)


def _rendered(assets: list[CapturedAsset]) -> RenderResult:
    return RenderResult(
        requested_url=PAGE,
        final_url=PAGE,
        http_status=200,
        headers={"content-type": "text/html"},
        dom_html=b"<html><body><main><p>Downloads page.</p></main></body></html>",
        inner_text="Downloads page.",
        screenshot_jpeg=b"\xff\xd8\xff\xd9",
        settle={"stable": True, "polls": 2},
        captured_assets=assets,
    )


def _asset(url: str, body: bytes, content_type: str = "image/png", **changes: Any) -> CapturedAsset:
    base: dict[str, Any] = {
        "requested_url": url,
        "final_url": url,
        "status": 200,
        "content_type": content_type,
        "body": body,
    }
    base.update(changes)
    return CapturedAsset(**base)


async def _run(ledger: MemoryLedger, renderer: FakeRenderer, tmp_path: Path) -> LocalEvidenceStore:
    store = LocalEvidenceStore(tmp_path, "t1")
    worker = RenderWorker(
        ledger=ledger, renderer=renderer, evidence=store, scope=_scope(), run_id=RUN, worker_id="r1"
    )
    await worker.run_until_idle(idle_polls=1, poll_s=0, max_wait_s=0)
    return store


async def test_an_image_the_browser_loaded_becomes_a_resource_and_the_rest_downloads(
    tmp_path: Path,
):
    png = _png()
    ledger, renderer = _page_render(
        _rendered([_asset(LOGO, png)]), [_fetch_task(LOGO), _fetch_task(HOTLINK)]
    )
    store = await _run(ledger, renderer, tmp_path)

    logo = ledger.get_resource(resource_id_for(RUN, LOGO))
    assert logo is not None
    assert logo.acquired_at_tier is AccessTier.T3_FIDELITY_BROWSER
    assert logo.sniffed_mime == "image/png" and logo.payload_bytes == len(png)
    assert logo.payload_sha256 and store.get(logo.payload_sha256) == png
    assert logo.status is EntityStatus.IN_PROGRESS and "from_browser" in (logo.status_reason or "")
    assert ledger.summaries[logo.resource_id]["discovered_from"]["parent_resource_id"] == "res-page"
    assert ledger.summaries[logo.resource_id]["discovered_from"]["edge_kind"] == "embeds"

    tasks = _tasks(ledger)
    extract = [t for t in tasks if t.kind is TaskKind.EXTRACT_IMAGE]
    assert len(extract) == 1 and extract[0].payload["sha256"] == logo.payload_sha256
    fallback = [t for t in tasks if t.kind is TaskKind.FETCH]
    assert [t.payload["requested_url"] for t in fallback] == [HOTLINK], (
        "not loaded: downloaded as before"
    )
    assert ledger.get_resource(resource_id_for(RUN, HOTLINK)) is None
    assert ledger.summaries["res-page"]["browser_assets"] == {
        "captured": 1,
        "downloaded_instead": 1,
        "excluded": 0,
        "already_known": 0,
    }


async def test_robots_rules_still_apply_to_a_loaded_image(tmp_path: Path):
    ledger, renderer = _page_render(
        _rendered([_asset(LOGO, _png())]),
        [_fetch_task(LOGO)],
        robots="User-agent: *\nDisallow: /files/",
    )
    await _run(ledger, renderer, tmp_path)
    logo = ledger.get_resource(resource_id_for(RUN, LOGO))
    assert logo is not None and logo.status is EntityStatus.EXCLUDED_BY_POLICY
    assert logo.payload_sha256 is None, "disallowed bytes are not kept"
    assert not [t for t in _tasks(ledger) if t.kind in (TaskKind.FETCH, TaskKind.EXTRACT_IMAGE)]


async def test_without_a_robots_answer_the_download_path_judges(tmp_path: Path):
    ledger, renderer = _page_render(
        _rendered([_asset(LOGO, _png())]), [_fetch_task(LOGO)], robots=None
    )
    await _run(ledger, renderer, tmp_path)
    assert ledger.get_resource(resource_id_for(RUN, LOGO)) is None
    assert [t.payload["requested_url"] for t in _tasks(ledger) if t.kind is TaskKind.FETCH] == [
        LOGO
    ]


@pytest.mark.parametrize(
    "asset",
    [
        _asset(LOGO, b"<html>not an image</html>", content_type="text/html"),
        _asset(LOGO, _png(), truncated=True),
        _asset(LOGO, _png(), status=304),
    ],
    ids=["html_for_an_image", "cut_short", "not_a_2xx"],
)
async def test_an_answer_that_is_not_a_whole_image_falls_back_to_a_download(
    tmp_path: Path, asset: CapturedAsset
):
    ledger, renderer = _page_render(_rendered([asset]), [_fetch_task(LOGO)])
    await _run(ledger, renderer, tmp_path)
    assert ledger.get_resource(resource_id_for(RUN, LOGO)) is None
    assert [t.payload["requested_url"] for t in _tasks(ledger) if t.kind is TaskKind.FETCH] == [
        LOGO
    ]


async def test_a_render_that_is_given_up_downloads_every_image_as_before(tmp_path: Path):
    failing = RenderResult(requested_url=PAGE, final_url=PAGE, transport_error="navigation_timeout")
    ledger, renderer = _page_render(
        failing, [_fetch_task(LOGO), _fetch_task(HOTLINK)], max_attempts=1
    )
    await _run(ledger, renderer, tmp_path)
    fallback = sorted(
        t.payload["requested_url"] for t in _tasks(ledger) if t.kind is TaskKind.FETCH
    )
    assert fallback == sorted([HOTLINK, LOGO])


async def test_an_image_another_page_already_brought_in_is_not_recorded_twice(tmp_path: Path):
    png = _png()
    ledger, renderer = _page_render(_rendered([_asset(LOGO, png)]), [_fetch_task(LOGO)])
    earlier = Resource(
        resource_id=resource_id_for(RUN, LOGO),
        run_id=RUN,
        requested_url=LOGO,
        schedule_key=LOGO,
        status=EntityStatus.VERIFIED_NONCONTENT,
        status_reason="image_extracted",
        acquired_at_tier=AccessTier.T3_FIDELITY_BROWSER,
    )
    ledger.upsert_resource(earlier, HOST)
    await _run(ledger, renderer, tmp_path)
    kept = ledger.get_resource(resource_id_for(RUN, LOGO))
    assert kept is not None and kept.status is EntityStatus.VERIFIED_NONCONTENT
    assert not [t for t in _tasks(ledger) if t.kind in (TaskKind.FETCH, TaskKind.EXTRACT_IMAGE)]
    assert ledger.summaries["res-page"]["browser_assets"]["already_known"] == 1


async def test_the_same_bytes_under_two_addresses_are_extracted_once(tmp_path: Path):
    png = _png()
    other = "http://testserver/files/logo-copy.png"
    ledger, renderer = _page_render(
        _rendered([_asset(LOGO, png), _asset(other, png)]), [_fetch_task(LOGO), _fetch_task(other)]
    )
    await _run(ledger, renderer, tmp_path)
    copy = ledger.get_resource(resource_id_for(RUN, other))
    assert copy is not None and copy.status is EntityStatus.EXTRACTED_UNVERIFIED
    assert (copy.status_reason or "").startswith("duplicate_payload_of:")
    assert len([t for t in _tasks(ledger) if t.kind is TaskKind.EXTRACT_IMAGE]) == 1


def test_a_saved_render_keeps_the_images_it_loaded(tmp_path: Path):
    store = LocalEvidenceStore(tmp_path, "t1")
    journal = CaptureJournal(tmp_path / "captures", store)
    png = _png((8, 8), "blue")
    journal.save_render(_rendered([_asset(LOGO, png)]), None, "T3_FIDELITY_BROWSER")
    saved = journal.find_render(PAGE, "T3_FIDELITY_BROWSER")
    assert saved is not None
    assert [a.body for a in saved.result.captured_assets] == [png]
    assert saved.result.captured_assets[0].requested_url == LOGO


# -- the real browser hands over what it loaded -----------------------------------------


@pytest.fixture(scope="module")
def server_url():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.mark.browser
async def test_the_browser_keeps_the_images_it_downloaded(server_url: str):
    renderer = BrowserRenderer(
        user_agent="test-agent/1.0",
        host_checker=lambda _h: (True, "test"),
        limits=RenderLimits(
            settle_window_s=4, settle_stable_polls=2, settle_min_quiet_s=0.3, poll_interval_s=0.2
        ),
        channel="chrome",  # what scrapes use; bundled Chromium cannot click on every machine
    )
    try:
        await renderer.start()
    except Exception as exc:  # noqa: BLE001 - environment without a browser
        pytest.skip(f"browser unavailable: {exc}")
    try:
        result = await renderer.render(f"{server_url}/downloads")
    finally:
        await renderer.stop()
    assert result.rendered, result.transport_error
    by_url = {a.requested_url: a for a in result.captured_assets}
    logo = by_url[f"{server_url}/files/logo.png"]
    async with httpx.AsyncClient() as client:
        served = (await client.get(f"{server_url}/files/logo.png")).content
    assert logo.body == served and logo.status == 200 and not logo.truncated
    assert logo.content_type.startswith("image/png")
    assert len([a for a in result.captured_assets if a.requested_url.endswith("logo.png")]) == 1
