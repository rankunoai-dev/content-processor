"""Fetch worker, lane L1: depth-1 follow-ups (A-1), expected type (K15), T2 switching (A-2).

Runs the real in-memory ledger against the in-process fixture app. Robots are
pre-cached without a crawl-delay so the suite does not sleep.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from fixtures.server import app
from src.integrations.evidence_store import LocalEvidenceStore
from src.integrations.http_client import FetchLimits, FetchResult, HttpFetcher
from src.modules.content_processor.contracts import Authorization, RunScope, Task
from src.modules.content_processor.enums import (
    AccessTier,
    AuthorizationKind,
    EntityStatus,
    PriorityClass,
    ScopeMode,
    TaskKind,
    TaskState,
)
from src.modules.content_processor.ledger.keys import task_key
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.planner import fetch_tasks_for
from src.modules.content_processor.scope.frontier import build_frontier
from src.modules.content_processor.workers.fetch_worker import FetchWorker
from src.modules.content_processor.workers.ports import RobotsCache

HOST = "testserver"
BASE = f"http://{HOST}"
RUN = "run-l1"


def _scope(urls: list[str], **overrides) -> RunScope:
    data = {
        "tenant_id": "t1",
        "input_urls": urls,
        "hosts_in_scope": [HOST],
        "authorization": Authorization(
            kind=AuthorizationKind.PUBLIC_THIRD_PARTY,
            reference="public",
            approved_by="lead",
            approved_at=datetime(2026, 10, 15, tzinfo=UTC),
            hosts=[HOST],
        ),
        "egress_allow_private": True,
    }
    data.update(overrides)
    return RunScope(**data)


class ClientHintsFetcher(HttpFetcher):
    """Stands in for T2 in-process: same chain, browser client hints added."""

    tier_name = "T2_IMPERSONATED_HTTP"

    def __init__(self) -> None:
        super().__init__(
            host_checker=lambda _h: (True, "test"),
            user_agent="Mozilla/5.0 Chrome/131",
            transport=httpx.ASGITransport(app=app),
        )
        self._client.headers["sec-ch-ua"] = '"Chromium";v="131"'
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        return await super().fetch(url)


class Env:
    def __init__(
        self,
        tmp_path: Path,
        scope: RunScope,
        *,
        t2: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.scope = scope
        self.ledger = MemoryLedger(base_delay_s=0)
        self.ledger.robots[HOST] = RobotsCache(
            body="User-agent: *\nAllow: /\n", status=200, fetched_at=datetime.now(UTC)
        )
        self.t1 = HttpFetcher(
            host_checker=lambda _h: (True, "test"),
            user_agent="test/1.0",
            transport=transport or httpx.ASGITransport(app=app),
            limits=FetchLimits(max_bytes=200_000, total_timeout_s=5),
        )
        self.t2 = ClientHintsFetcher() if t2 else None
        self.worker = FetchWorker(
            ledger=self.ledger,
            fetcher=self.t1,
            evidence=LocalEvidenceStore(tmp_path, "t1"),
            scope=scope,
            run_id=RUN,
            worker_id="w1",
            escalation_fetchers=[self.t2] if self.t2 else (),
        )

    def plan(self) -> None:
        plan = build_frontier(self.scope, resolver=lambda _h: ["93.184.216.34"])
        self.ledger.enqueue(fetch_tasks_for(RUN, plan))

    async def run(self) -> int:
        return await self.worker.run_until_idle(idle_polls=1, poll_s=0, max_wait_s=0)

    def res(self, path: str):
        return self.ledger.resources[BASE + path]

    def fetch_tasks(self) -> list[Task]:
        return [t for t in self.ledger._tasks.values() if t.kind is TaskKind.FETCH]

    async def close(self) -> None:
        await self.t1.aclose()
        if self.t2 is not None:
            await self.t2.aclose()


@pytest.fixture
async def downloads_env(tmp_path: Path):
    env = Env(tmp_path, _scope([f"{BASE}/downloads"]))
    env.plan()
    await env.run()
    yield env
    await env.close()


async def test_downloads_page_follows_assets_and_downloads_to_depth_one(downloads_env):
    env = downloads_env
    fetched = {k.removeprefix(BASE): r.status for k, r in env.ledger.resources.items()}
    assert fetched == {
        "/downloads": EntityStatus.EXTRACTED_UNVERIFIED,
        "/pdf-but-html": EntityStatus.BLOCKED,
        "/files/brochure.pdf": EntityStatus.IN_PROGRESS,
        "/files/brochure-copy.pdf": EntityStatus.EXTRACTED_UNVERIFIED,
        "/files/report": EntityStatus.EXTRACTED_UNVERIFIED,
        "/files/logo.png": EntityStatus.IN_PROGRESS,
        "/files/hotlink.png": EntityStatus.BLOCKED,
        "/pages/static_ok": EntityStatus.EXTRACTED_UNVERIFIED,
    }
    parent = env.res("/downloads")
    summary = env.ledger.summaries[parent.resource_id]["followups"]
    assert summary["skipped_by_reason"] == {
        "hidden": 1,
        "mime_not_allowed:application/zip": 1,
        "out_of_scope_host": 1,
        "mime_not_allowed:video/mp4": 1,
    }
    assert summary["links_not_downloads"] == 1
    child = env.res("/files/logo.png")
    assert env.ledger.summaries[child.resource_id]["discovered_from"] == {
        "parent_resource_id": parent.resource_id,
        "edge_kind": "embeds",
        "depth": 1,
        "expected_mime": "image/png",
        "expected_mime_source": "embed_kind",
    }
    # The frame is depth 1: its own embedded assets are not followed (depth 2).
    frame = env.res("/pages/static_ok")
    assert "followups" not in env.ledger.summaries.get(frame.resource_id, {})
    depths = {int(t.payload.get("depth", 0)) for t in env.fetch_tasks()}
    assert depths == {0, 1}
    # Every HTML page acquired gets its render follow-up; downloads do not.
    renders = [t for t in env.ledger._tasks.values() if t.kind is TaskKind.RENDER]
    assert {t.payload["requested_url"] for t in renders} == {
        f"{BASE}/downloads",
        f"{BASE}/pages/static_ok",
    }


async def test_typed_pdf_link_served_as_html_fails_closed(downloads_env):
    res = downloads_env.res("/pdf-but-html")
    assert res.status is EntityStatus.BLOCKED
    assert res.status_reason.startswith("wrong_payload:generic:fetched_at=T1_POLITE_HTTP")
    assert res.status_reason.endswith(":expected=application/pdf:from=link_type")
    assert res.sniffed_mime == "text/html"
    # Never handed to the renderer, so it cannot be verified as a page.
    render_keys = [
        t.idempotency_key for t in downloads_env.ledger._tasks.values() if t.kind is TaskKind.RENDER
    ]
    assert task_key(RUN, TaskKind.RENDER, res.schedule_key) not in render_keys


async def test_image_answered_with_html_fails_closed(downloads_env):
    res = downloads_env.res("/files/hotlink.png")
    assert res.status is EntityStatus.BLOCKED
    assert res.status_reason.endswith(":expected=image/png:from=embed_kind")


async def test_identical_payloads_are_extracted_once(downloads_env):
    env = downloads_env
    canonical = env.res("/files/brochure.pdf")
    for path in ("/files/brochure-copy.pdf", "/files/report"):
        dup = env.res(path)
        assert dup.payload_sha256 == canonical.payload_sha256
        assert dup.status_reason == f"duplicate_payload_of:{canonical.resource_id}"
    assert canonical.status_reason == "acquired:awaiting_extraction"


async def test_seed_fetched_after_its_typed_link_fails_closed(tmp_path: Path):
    # Both supplied; the page sorts first, so the seed learns the promise from it.
    env = Env(tmp_path, _scope([f"{BASE}/pdf-but-html", f"{BASE}/downloads"]))
    env.plan()
    try:
        await env.run()
    finally:
        await env.close()
    res = env.res("/pdf-but-html")
    assert res.status is EntityStatus.BLOCKED
    assert res.status_reason.endswith(":expected=application/pdf:from=link_type")
    keys = {t.idempotency_key for t in env.fetch_tasks()}
    # The typed follow-up did not collapse onto the supplied URL's task.
    assert task_key(RUN, TaskKind.FETCH, res.schedule_key) in keys
    assert task_key(RUN, TaskKind.FETCH, res.schedule_key, "expect=application/pdf") in keys


async def test_seed_fetched_before_its_typed_link_is_downgraded_by_the_typed_fetch(
    tmp_path: Path,
):
    # Mandatory sorts the bare seed first: it is extracted as HTML, then the typed
    # follow-up re-fetches it and fails it closed. (The RENDER queued by the first
    # fetch must not upgrade it again; see LANE_LOG hand-off to L2/L4.)
    env = Env(
        tmp_path,
        _scope(
            [f"{BASE}/downloads", f"{BASE}/pdf-but-html"],
            mandatory_urls=[f"{BASE}/pdf-but-html"],
        ),
    )
    env.plan()
    try:
        await env.run()
    finally:
        await env.close()
    res = env.res("/pdf-but-html")
    assert res.status is EntityStatus.BLOCKED
    assert res.status_reason.endswith(":expected=application/pdf:from=link_type")


async def test_content_disposition_name_sets_the_expectation(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            html="<html><body>Please sign in</body></html>",
            headers={"content-disposition": 'attachment; filename="rates.pdf"'},
        )

    env = Env(tmp_path, _scope([f"{BASE}/rates"]), transport=httpx.MockTransport(handler))
    env.plan()
    try:
        await env.run()
    finally:
        await env.close()
    assert env.res("/rates").status_reason.endswith(
        ":expected=application/pdf:from=content_disposition"
    )


async def test_followup_with_disallowed_sniffed_type_is_excluded(tmp_path: Path):
    env = Env(
        tmp_path,
        _scope([f"{BASE}/files/logo.png"], allowed_mime_families=["text/html", "application/pdf"]),
    )
    env.ledger.enqueue(
        [
            Task(
                run_id=RUN,
                kind=TaskKind.FETCH,
                idempotency_key="child",
                host=HOST,
                priority=PriorityClass.EMBEDDED_ASSET,
                payload={
                    "schedule_key": f"{BASE}/files/logo.png",
                    "requested_url": f"{BASE}/files/logo.png",
                    "depth": 1,
                    "parent_resource_id": "res-parent",
                    "edge_kind": "embeds",
                },
            )
        ]
    )
    try:
        await env.run()
    finally:
        await env.close()
    res = env.res("/files/logo.png")
    assert res.status is EntityStatus.EXCLUDED_BY_POLICY
    assert res.status_reason == "mime_not_allowed:image/png"
    assert res.scope_decision == "excluded_by_policy"


async def test_supplied_urls_mode_follows_nothing(tmp_path: Path):
    env = Env(tmp_path, _scope([f"{BASE}/downloads"], mode=ScopeMode.SUPPLIED_URLS))
    env.plan()
    try:
        await env.run()
    finally:
        await env.close()
    assert list(env.ledger.resources) == [f"{BASE}/downloads"]


# -- A-2: the tier the host reached is the tier that fetches -------------------


async def test_fingerprint_block_escalates_and_the_t2_fetcher_runs(tmp_path: Path):
    env = Env(tmp_path, _scope([f"{BASE}/fingerprint-wall"]), t2=True)
    env.plan()
    try:
        await env.run()
        first = env.res("/fingerprint-wall")
        assert first.status is EntityStatus.RETRY_SCHEDULED
        assert first.status_reason.startswith("escalated_to:T2_IMPERSONATED_HTTP:fingerprint")
        assert first.acquired_at_tier is AccessTier.T1_POLITE_HTTP
        assert env.ledger.hosts[HOST].tier is AccessTier.T2_IMPERSONATED_HTTP
        retry = [t for t in env.fetch_tasks() if t.state is TaskState.PENDING]
        assert len(retry) == 1 and retry[0].payload["escalated_to"] == "T2_IMPERSONATED_HTTP"
        assert retry[0].not_before is not None  # escalation cooldown honoured
        retry[0].not_before = None
        await env.run()
    finally:
        await env.close()
    res = env.res("/fingerprint-wall")
    assert res.status is EntityStatus.EXTRACTED_UNVERIFIED
    assert res.acquired_at_tier is AccessTier.T2_IMPERSONATED_HTTP
    assert env.t2 is not None and env.t2.calls == [f"{BASE}/fingerprint-wall"]
    assert any(
        e.entity_type == "host" and e.reason == "escalate_on:FINGERPRINT" for e in env.ledger.events
    )


async def test_host_already_at_t2_uses_t2_for_robots_and_pages(tmp_path: Path):
    env = Env(tmp_path, _scope([f"{BASE}/pages/static_ok"]), t2=True)
    del env.ledger.robots[HOST]
    env.ledger.load_host_state("t1", HOST).tier = AccessTier.T2_IMPERSONATED_HTTP
    env.plan()
    try:
        await env.run()
    finally:
        await env.close()
    assert env.t2 is not None
    assert env.t2.calls == [f"{BASE}/robots.txt", f"{BASE}/pages/static_ok"]
    assert env.res("/pages/static_ok").acquired_at_tier is AccessTier.T2_IMPERSONATED_HTTP


async def test_without_a_t2_fetcher_the_escalated_url_stays_blocked(tmp_path: Path):
    env = Env(tmp_path, _scope([f"{BASE}/fingerprint-wall"]))
    env.plan()
    try:
        await env.run()
    finally:
        await env.close()
    res = env.res("/fingerprint-wall")
    assert res.status is EntityStatus.BLOCKED
    assert res.status_reason.endswith("host_tier=T2_IMPERSONATED_HTTP")
    assert [t.state for t in env.fetch_tasks()] == [TaskState.DONE]


async def test_t2_block_that_needs_the_browser_ends_blocked_at_t2(tmp_path: Path):
    env = Env(tmp_path, _scope([f"{BASE}/challenge-200"]), t2=True)
    env.ledger.load_host_state("t1", HOST).tier = AccessTier.T2_IMPERSONATED_HTTP
    env.plan()
    try:
        await env.run()
    finally:
        await env.close()
    res = env.res("/challenge-200")
    # JS challenge wants T3 (browser): no HTTP fetcher there, so no HTTP retry.
    assert res.status is EntityStatus.BLOCKED
    assert res.acquired_at_tier is AccessTier.T2_IMPERSONATED_HTTP
    assert env.ledger.hosts[HOST].tier is AccessTier.T3_FIDELITY_BROWSER
