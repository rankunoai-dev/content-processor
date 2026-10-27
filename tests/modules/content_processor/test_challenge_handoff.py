"""A bot check met by the download hands the page to the browser instead of ending BLOCKED."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx

from fixtures.server import app
from src.integrations.browser_client import RenderResult
from src.integrations.evidence_store import LocalEvidenceStore
from src.integrations.http_client import FetchLimits, HttpFetcher
from src.modules.content_processor.contracts import Authorization, RunScope, UrlRecord
from src.modules.content_processor.enums import AccessTier, AuthorizationKind, TaskKind, UrlBadge
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.planner import fetch_tasks_for
from src.modules.content_processor.scheduler import RunOptions, assemble_run_records, execute_run
from src.modules.content_processor.scope.frontier import build_frontier
from src.modules.content_processor.workers.fetch_worker import FetchWorker
from src.modules.content_processor.workers.ports import RobotsCache
from src.modules.content_processor.workers.render_worker import RenderWorker

HOST = "127.0.0.1"
URL = f"http://{HOST}:8765/challenge-200"
SCOPE = RunScope(
    tenant_id="t1",
    input_urls=[URL],
    hosts_in_scope=[HOST],
    authorization=Authorization(
        kind=AuthorizationKind.PUBLIC_THIRD_PARTY,
        reference="fixtures",
        approved_by="lead",
        approved_at=datetime(2026, 10, 27, tzinfo=UTC),
        hosts=[HOST],
    ),
    egress_allow_private=True,
)
ARTICLE = (
    b"<!doctype html><html lang='en'><head><title>Print security</title></head><body><main>"
    b"<h1>Print security for financial firms</h1>"
    b"<p>Secure release printing keeps client statements in the tray until the owner"
    b" signs in.</p></main></body></html>"
)
CHALLENGE = (
    b"<!doctype html><html><head><title>Just a moment...</title></head><body>"
    b"<div id='cf-challenge-running'>Checking your browser before accessing the site.</div>"
    b"<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1'></script></body></html>"
)


class Browser:
    """Stands in for real Chrome: either passes the bot check or is challenged too."""

    tier_name = "T3_FIDELITY_BROWSER"

    def __init__(self, html: bytes, text: str, status: int = 200) -> None:
        self.html, self.text, self.status = html, text, status
        self.calls: list[str] = []

    async def render(self, url: str, on_page=None) -> RenderResult:
        self.calls.append(url)
        headers = {"content-type": "text/html"}
        if self.status == 403:
            headers |= {"server": "cloudflare", "cf-mitigated": "challenge"}
        return RenderResult(
            requested_url=url,
            final_url=url,
            http_status=self.status,
            headers=headers,
            dom_html=self.html,
            inner_text=self.text,
            settle={"stable": True},
        )


async def _run(
    tmp: Path, browser: Browser, *, render: bool = True
) -> tuple[MemoryLedger, UrlRecord]:
    plan = build_frontier(SCOPE, resolver=lambda _h: [HOST])
    ledger = MemoryLedger(base_delay_s=0.0)
    ledger.set_robots(
        "t1", HOST, RobotsCache(body="User-agent: *\n", status=200, fetched_at=datetime.now(UTC))
    )
    run_id = "run-c"
    ledger.enqueue(fetch_tasks_for(run_id, plan))
    evidence = LocalEvidenceStore(tmp, "t1")
    fetcher = HttpFetcher(
        host_checker=lambda _h: (True, "fixture"),
        user_agent="challenge-test",
        transport=httpx.ASGITransport(app=app),
        limits=FetchLimits(max_bytes=1_000_000, total_timeout_s=5),
    )
    stages: dict[str, object] = {
        "fetch": FetchWorker(
            ledger=ledger,
            fetcher=fetcher,
            evidence=evidence,
            scope=SCOPE,
            run_id=run_id,
            worker_id="f1",
            browser_handoff=render,
        )
    }
    names = ["fetch"]
    if render:
        stages["render"] = RenderWorker(
            ledger=ledger,
            renderer=browser,
            evidence=evidence,
            scope=SCOPE,
            run_id=run_id,
            worker_id="r1",
            render_retry_delays_s=(),
        )
        names.append("render")
    try:
        await execute_run(
            ledger,
            run_id=run_id,
            stages=stages,  # type: ignore[arg-type]
            options=RunOptions(stages=names, idle_polls=1, poll_s=0.01, max_wait_s=0.5),  # type: ignore[arg-type]
        )
    finally:
        await fetcher.aclose()
    records = assemble_run_records(
        ledger, run_id=run_id, plan=plan, evidence=evidence, stages_that_will_run=names
    )
    return ledger, next(r for r in records if r.requested_url == URL)


async def test_the_browser_gets_through_and_the_page_is_extracted(tmp_path: Path):
    browser = Browser(
        ARTICLE,
        "Print security for financial firms\nSecure release printing keeps client statements"
        " in the tray until the owner signs in.",
    )
    _, record = await _run(tmp_path, browser)
    assert browser.calls == [URL], "the challenged page was handed to the browser"
    assert record.badge is not UrlBadge.BLOCKED
    assert record.tier_reached is AccessTier.T3_FIDELITY_BROWSER
    texts = [b.normalized_text for b in record.blocks]
    assert "Print security for financial firms" in texts


async def test_a_browser_challenged_too_reports_the_block_at_the_browser_tier(tmp_path: Path):
    browser = Browser(CHALLENGE, "Checking your browser before accessing the site.", 403)
    _, record = await _run(tmp_path, browser)
    assert browser.calls == [URL]
    assert record.badge is UrlBadge.BLOCKED
    assert record.resource_status_reason is not None
    assert "fetched_at=T3_FIDELITY_BROWSER" in record.resource_status_reason


async def test_without_a_browser_stage_the_page_is_blocked_at_once(tmp_path: Path):
    browser = Browser(ARTICLE, "")
    ledger, record = await _run(tmp_path, browser, render=False)
    assert browser.calls == []
    assert record.badge is UrlBadge.BLOCKED
    assert not any(t.kind is TaskKind.RENDER for t in ledger._tasks.values())  # noqa: SLF001
