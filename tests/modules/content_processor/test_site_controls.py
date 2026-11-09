"""Faster scrapes: a site's repeating controls are explored once; images are read in parallel."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from src.integrations.browser_client import BrowserRenderer, RenderLimits
from src.integrations.evidence_store import LocalEvidenceStore
from src.modules.content_processor.contracts import InteractionPolicy, Task
from src.modules.content_processor.enums import PriorityClass, TaskKind
from src.modules.content_processor.explore import ExplorationResult, StateExplorer
from src.modules.content_processor.explore.explorer import _UTILITY_LABEL, SITE_WIDE_REASON
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.scheduler.site_controls import (
    SITE_WIDE_AFTER_PAGES,
    SiteControlMemory,
)
from src.modules.content_processor.workers.doc_worker import DocWorker

# -- the memory ---------------------------------------------------------------


def test_a_control_is_site_wide_only_after_enough_different_pages():
    memory = SiteControlMemory()
    memory.record("https://x.test/1", {"menu": "failed"})
    assert not memory.is_site_wide("menu")
    memory.record("https://x.test/1", {"menu": "failed"})
    assert not memory.is_site_wide("menu"), "the same page twice is one page"
    memory.record("https://x.test/2", {"menu": "failed"})
    assert SITE_WIDE_AFTER_PAGES == 2 and memory.is_site_wide("menu")


def test_a_control_that_behaves_differently_is_never_site_wide():
    memory = SiteControlMemory()
    for n, outcome in enumerate(["a", "b", "a", "a", "a"]):
        memory.record(f"https://x.test/{n}", {"panel": outcome})
    assert not memory.is_site_wide("panel")


def test_the_memory_is_shared_by_batches_and_survives_damage(tmp_path: Path):
    path = tmp_path / "sub" / "memory.state"
    first = SiteControlMemory(path)
    first.record("https://x.test/1", {"menu": "failed"})
    first.record("https://x.test/2", {"menu": "failed"})
    assert SiteControlMemory(path).is_site_wide("menu")
    path.write_text("{not json", encoding="utf-8")
    assert not SiteControlMemory(path).is_site_wide("menu")


def test_controls_seen_once_are_dropped_first_when_the_memory_is_full():
    memory = SiteControlMemory(max_entries=2)
    memory.record("https://x.test/1", {"menu": "failed"})
    memory.record("https://x.test/2", {"menu": "failed", "once-1": "a"})
    memory.record("https://x.test/3", {"once-2": "a"})
    assert memory.is_site_wide("menu")


# -- the explorer on real pages ------------------------------------------------

_SCRIPT = """
<script>
document.querySelectorAll('[data-toggle]').forEach((b) => b.addEventListener('click', () => {
  const p = document.getElementById(b.getAttribute('aria-controls'));
  p.hidden = false; b.setAttribute('aria-expanded', 'true');
}));
const lazy = document.getElementById('lazy');
lazy.addEventListener('click', () => {
  lazy.setAttribute('aria-expanded', 'true');
  document.getElementById('slot').textContent = 'Loaded only for ' + location.pathname;
});
</script>
"""


def _page(n: int) -> str:
    return f"""<!doctype html><html><body>
    <main><h1>Page number {n}</h1><p>Body text that is different on page {n}.</p>
    <button data-toggle aria-expanded="false" aria-controls="menu">Site menu</button>
    <div id="menu" hidden>The same site menu text on every page of the site</div>
    <button data-toggle aria-expanded="false" aria-controls="faq">Details</button>
    <div id="faq" hidden>An answer written only for page {n}</div>
    <section><p>Text around a panel that is filled in after the click</p>
    <button id="lazy" aria-expanded="false">Reviews</button><div id="slot"></div></section>
    </main>{_SCRIPT}</body></html>"""


@pytest.fixture(scope="module")
def site_url():
    site = FastAPI()

    @site.get("/p{number}", response_class=HTMLResponse)
    def page(number: int) -> str:
        return _page(number)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(site, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


async def _explore_one(
    renderer: BrowserRenderer, explorer: StateExplorer, url: str
) -> ExplorationResult:
    holder: dict[str, ExplorationResult] = {}

    async def hook(page: Any) -> None:
        holder["r"] = await explorer.explore(page)

    result = await renderer.render(url, on_page=hook)
    assert result.rendered, result.transport_error
    return holder["r"]


async def _explore_pages(
    site_url: str, memory: SiteControlMemory | None, pages: int
) -> list[ExplorationResult]:
    policy = InteractionPolicy(allowed=["expand_disclosure"], max_actions_per_url=20)
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
    out: list[ExplorationResult] = []
    try:
        for n in range(1, pages + 1):
            explorer = StateExplorer(policy, settle_max_s=0.5, site_memory=memory)
            out.append(await _explore_one(renderer, explorer, f"{site_url}/p{n}"))
    finally:
        await renderer.stop()
    return out


def _control(result: ExplorationResult, label: str) -> tuple[str, str | None]:
    return next((c.status, c.reason) for c in result.controls if c.label == label)


@pytest.mark.browser
async def test_only_what_really_repeats_is_left_alone(site_url: str):
    first, second, third = await _explore_pages(site_url, SiteControlMemory(), 3)

    assert _control(first, "Site menu")[0] == "visited"
    assert _control(second, "Site menu")[0] == "visited"
    assert _control(third, "Site menu") == ("excluded", SITE_WIDE_REASON)
    assert third.site_wide_skipped == 1
    assert third.site_wide_panels == ["The same site menu text on every page of the site"]

    assert _control(third, "Details")[0] == "visited", "this answer differs on every page"
    assert _control(third, "Reviews")[0] == "visited", "this click loads different text per page"
    assert any("written only for page 3" in s.inner_text for s in third.states)
    assert any("Loaded only for /p3" in s.inner_text for s in third.states)
    assert not any("same site menu" in s.inner_text for s in third.states)


@pytest.mark.browser
async def test_without_a_memory_every_page_is_explored_in_full(site_url: str):
    result = (await _explore_pages(site_url, None, 1))[0]
    assert _control(result, "Site menu")[0] == "visited"
    assert result.site_wide_skipped == 0


@pytest.mark.parametrize("label", ["Add Comment +", "Leave a reply", "Post a comment"])
def test_comment_buttons_are_site_tools(label: str):
    assert _UTILITY_LABEL.match(label)


# -- reading images in parallel ------------------------------------------------


class _Env:
    def __init__(self, tmp_path: Path, parallel: int, count: int) -> None:
        self.ledger = MemoryLedger(base_delay_s=0)
        evidence = LocalEvidenceStore(tmp_path, "t")
        self.worker = DocWorker(
            ledger=self.ledger,
            evidence=evidence,
            run_id="run",
            worker_id="w",
            isolate=False,
            parallel_tasks=parallel,
        )
        self.ledger.enqueue(
            Task(
                run_id="run",
                kind=TaskKind.EXTRACT_IMAGE,
                idempotency_key=f"k{i}",
                host="h",
                priority=PriorityClass.HTML_PAGE,
                payload={"resource_id": f"r{i}", "sha256": "0"},
            )
            for i in range(count)
        )
        self.lock = threading.Lock()
        self.now = 0
        self.peak = 0
        self.handled: list[int] = []
        self.worker.process = self._slow  # type: ignore[method-assign]

    def _slow(self, task: Task) -> None:
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        time.sleep(0.05)
        with self.lock:
            self.now -= 1
            self.handled.append(task.id or 0)
        self.worker._ledger.complete(task.id or 0, "w")


async def test_images_are_read_several_at_a_time_and_each_exactly_once(tmp_path: Path):
    env = _Env(tmp_path, parallel=4, count=20)
    processed = await env.worker.run_until_idle(idle_polls=1, poll_s=0.01)
    assert processed == 20
    assert sorted(env.handled) == list(range(1, 21))
    assert env.peak == 4


async def test_parallel_reading_respects_the_task_limit(tmp_path: Path):
    env = _Env(tmp_path, parallel=4, count=20)
    assert await env.worker.run_until_idle(max_tasks=3, idle_polls=1, poll_s=0.01) == 3
    assert len(env.handled) == 3
    assert env.peak <= 3


async def test_one_at_a_time_stays_the_default(tmp_path: Path):
    env = _Env(tmp_path, parallel=1, count=5)
    await asyncio.wait_for(env.worker.run_until_idle(idle_polls=1, poll_s=0.01), timeout=10)
    assert env.peak == 1 and len(env.handled) == 5
