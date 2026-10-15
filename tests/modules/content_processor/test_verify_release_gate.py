"""Release gate V-7: zero false VERIFIED_WITHIN_SCOPE on the adversarial fixture suite.

The full local fixture scope runs end to end in-process (no sockets, no
network): real frontier, real fetch worker against the fixture app over the
ASGI transport, real render worker with a renderer that serves the fixture
HTML and computes visible text, then `url_records_for_run`. Every record is
checked against `fixtures/manifest.json`.

The fake renderer cannot run scripts, so for fixtures the manifest marks
`needs_render` it reports a render failure instead of pretending; the gate
must never be passed by a renderer that sees less than a browser would.

Each run is judged twice: as recorded, and with discovery assumed closed.
Until the state explorer lands (K5) every page is discovery-open, so "zero
false verified" as recorded is true by construction; the counterfactual pass
proves each trap fails on its own evidence and that the right pages *can*
reach VERIFIED_WITHIN_SCOPE once discovery closes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from selectolax.parser import HTMLParser

from fixtures.server import app
from src.integrations.browser_client import RenderResult
from src.integrations.evidence_store import LocalEvidenceStore
from src.integrations.http_client import FetchLimits, HttpFetcher
from src.modules.content_processor.contracts import RunScope, UrlRecord
from src.modules.content_processor.enums import BADGE_PRECEDENCE, TaskState, UrlBadge
from src.modules.content_processor.export.content import whole_content
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.planner import fetch_tasks_for
from src.modules.content_processor.scope.frontier import build_frontier
from src.modules.content_processor.verify.record import (
    COUNTERFACTUAL_WARNING,
    frames_from_evidence,
    url_records_for_run,
)
from src.modules.content_processor.workers.fetch_worker import FetchWorker, user_agent_for
from src.modules.content_processor.workers.ports import RobotsCache
from src.modules.content_processor.workers.render_worker import RenderWorker

ROOT = Path(__file__).resolve().parents[3]
MANIFEST: dict[str, Any] = json.loads(
    (ROOT / "fixtures" / "manifest.json").read_text(encoding="utf-8")
)
BY_PATH: dict[str, dict[str, Any]] = {f["path"]: f for f in MANIFEST["fixtures"]}
SCOPE = RunScope.model_validate(
    json.loads((ROOT / "fixtures" / "scopes" / "local_fixtures.json").read_text(encoding="utf-8"))
)
RUN_ID = "run-v7"
# robots.txt as served, minus Crawl-delay so the suite does not sleep 1 s per URL.
ROBOTS = "User-agent: *\nDisallow: /private/\n"
_INVISIBLE_TAGS = ("head", "script", "style", "template", "noscript")


def visible_text(html: bytes) -> str:
    """Approximate `innerText`: drop non-rendered and hidden subtrees."""
    tree = HTMLParser(html)
    for tag in _INVISIBLE_TAGS:
        for node in tree.css(tag):
            node.decompose()
    for node in tree.css("[hidden], [aria-hidden=true]"):
        node.decompose()
    for node in tree.css("[style]"):
        style = str(node.attributes.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "left:-9999px" in style:
            node.decompose()
    for details in tree.css("details:not([open])"):
        for child in details.iter():
            if child.tag != "summary":
                child.decompose()
    body = tree.body
    return body.text(separator="\n") if body is not None else ""


class FixtureRenderer:
    """Serves fixture HTML as the 'rendered' DOM; refuses pages that need scripts."""

    tier_name = "T3_FIDELITY_BROWSER"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), follow_redirects=True
        )

    async def render(self, url: str, on_page: Any = None) -> RenderResult:
        path = urlsplit(url).path
        if BY_PATH.get(path, {}).get("needs_render"):
            return RenderResult(
                requested_url=url,
                final_url=url,
                transport_error="fixture_renderer_cannot_run_scripts",
            )
        response = await self._client.get(url)
        return RenderResult(
            requested_url=url,
            final_url=str(response.url),
            http_status=response.status_code,
            headers=dict(response.headers),
            dom_html=response.content,
            inner_text=visible_text(response.content),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


async def _run_suite(tmp: Path, *, render: bool) -> dict[str, list[UrlRecord]]:
    plan = build_frontier(SCOPE, resolver=lambda _h: ["127.0.0.1"])
    ledger = MemoryLedger(ceiling=SCOPE.access_tier_ceiling, base_delay_s=0.0)
    for host in plan.hosts:
        ledger.set_robots(
            SCOPE.tenant_id,
            host,
            RobotsCache(body=ROBOTS, status=200, fetched_at=datetime.now(UTC)),
        )
    ledger.enqueue(fetch_tasks_for(RUN_ID, plan))
    evidence = LocalEvidenceStore(tmp, SCOPE.tenant_id)
    fetcher = HttpFetcher(
        host_checker=lambda _h: (True, "fixture"),
        user_agent=user_agent_for(SCOPE),
        transport=httpx.ASGITransport(app=app),
        limits=FetchLimits(max_bytes=1_000_000, total_timeout_s=5),
    )
    worker = FetchWorker(
        ledger=ledger,
        fetcher=fetcher,
        evidence=evidence,
        scope=SCOPE,
        run_id=RUN_ID,
        worker_id="v7-fetch",
    )
    try:
        await worker.run_until_idle(idle_polls=1, poll_s=0.05, max_wait_s=1.0)
    finally:
        await fetcher.aclose()
    if render:
        renderer = FixtureRenderer()
        render_worker = RenderWorker(
            ledger=ledger,
            renderer=renderer,
            evidence=evidence,
            scope=SCOPE,
            run_id=RUN_ID,
            worker_id="v7-render",
        )
        try:
            await render_worker.run_until_idle(idle_polls=1, poll_s=0.1, max_wait_s=4.0)
        finally:
            await renderer.aclose()
    # Tasks that will still run. A skipped render pass is not outstanding work.
    kinds = {"FETCH", "RENDER"} if render else {"FETCH"}
    tasks = ledger._tasks.values()  # noqa: SLF001 - test reads the in-memory queue
    outstanding = {
        str(t.payload.get("resource_id") or "")
        for t in tasks
        if t.state in (TaskState.PENDING, TaskState.LEASED) and t.kind.value in kinds
    }
    by_key = {r.schedule_key: r.resource_id for r in ledger.resources.values()}
    outstanding |= {
        by_key[str(t.payload["schedule_key"])]
        for t in tasks
        if t.kind.value == "FETCH"
        and t.state in (TaskState.PENDING, TaskState.LEASED)
        and str(t.payload["schedule_key"]) in by_key
    }
    expectations = {
        url: str(BY_PATH[urlsplit(url).path]["requested_mime"])
        for url in SCOPE.input_urls
        if "requested_mime" in BY_PATH.get(urlsplit(url).path, {})
    }
    common: dict[str, Any] = {
        "run_id": RUN_ID,
        "plan": plan,
        "frames_for": frames_from_evidence(evidence),
        "expectations": expectations,
        "outstanding": outstanding,
    }
    return {
        "recorded": url_records_for_run(ledger, **common),
        "closed": url_records_for_run(ledger, **common, assume_discovery_closed=True),
    }


@pytest.fixture(scope="module")
def suites(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, list[UrlRecord]]]:
    return {
        "render": asyncio.run(_run_suite(tmp_path_factory.mktemp("render"), render=True)),
        "no_render": asyncio.run(_run_suite(tmp_path_factory.mktemp("norender"), render=False)),
    }


def _path(record: UrlRecord) -> str:
    return urlsplit(record.requested_url).path


def _rank(badge: UrlBadge) -> int:
    return BADGE_PRECEDENCE.index(badge)


def _corpus(record: UrlRecord) -> str:
    return whole_content(record.blocks)


def _all(suites: dict[str, dict[str, list[UrlRecord]]]) -> list[tuple[str, UrlRecord]]:
    return [
        (f"{mode}/{view}", rec)
        for mode, views in suites.items()
        for view, records in views.items()
        for rec in records
    ]


def test_every_requested_url_has_exactly_one_record(suites):
    for views in suites.values():
        for records in views.values():
            urls = [r.requested_url for r in records]
            assert sorted(urls) == sorted(set(SCOPE.input_urls)), urls
            for rec in records:
                assert rec.inventory.denominator_basis == "observed_inventory_only"


def test_every_scope_url_is_in_the_manifest():
    missing = [u for u in SCOPE.input_urls if urlsplit(u).path not in BY_PATH]
    assert missing == []


def test_zero_false_verified_within_scope(suites):
    """V-7: VERIFIED only where the manifest says the page is fully capturable."""
    false_verified = [
        (label, _path(rec))
        for label, rec in _all(suites)
        if rec.badge is UrlBadge.VERIFIED_WITHIN_SCOPE
        and BY_PATH[_path(rec)].get("expect_badge") != UrlBadge.VERIFIED_WITHIN_SCOPE.value
    ]
    assert false_verified == []


def test_nothing_is_verified_while_discovery_is_open(suites):
    for label, rec in _all(suites):
        if label.endswith("/recorded"):
            assert rec.badge is not UrlBadge.VERIFIED_WITHIN_SCOPE, (label, _path(rec))
            assert COUNTERFACTUAL_WARNING not in rec.warnings
        else:
            assert rec.discovery.closed_within_scope or rec.badge is UrlBadge.EXCLUDED


def test_no_badge_is_weaker_than_the_manifest(suites):
    """No under-reporting (K14): a page expected BLOCKED never shows PARTIAL or better."""
    weaker = [
        (label, _path(rec), rec.badge.value, BY_PATH[_path(rec)]["expect_badge"])
        for label, rec in _all(suites)
        if "expect_badge" in BY_PATH[_path(rec)]
        and _rank(rec.badge) > _rank(UrlBadge(BY_PATH[_path(rec)]["expect_badge"]))
    ]
    assert weaker == []


def test_counterfactual_render_badges_match_manifest_exactly(suites):
    mismatched = [
        (_path(rec), rec.badge.value, BY_PATH[_path(rec)]["expect_badge"])
        for rec in suites["render"]["closed"]
        if "expect_badge" in BY_PATH[_path(rec)]
        and not BY_PATH[_path(rec)].get("needs_render")
        and rec.badge.value != BY_PATH[_path(rec)]["expect_badge"]
    ]
    assert mismatched == []


def test_gated_pages_are_reported_without_a_render_pass(suites):
    """K14: gates found by the fetch worker set the badge in a --no-render run."""
    for rec in suites["no_render"]["closed"]:
        fixture = BY_PATH[_path(rec)]
        if fixture.get("expect_gate"):
            assert rec.badge.value == fixture["expect_badge"], (_path(rec), rec.badge, rec.gaps)
            assert "render_not_run:source_channel_only" in rec.warnings


def test_pdf_but_html_is_blocked_as_wrong_payload(suites):
    """K15: HTML served where a document was expected never passes."""
    for label, rec in _all(suites):
        if _path(rec) == "/pdf-but-html":
            assert rec.badge is UrlBadge.BLOCKED, (label, rec.badge, rec.gaps)
            assert any(g.reason.startswith("wrong_payload") for g in rec.gaps), label


def test_verified_records_hold_the_promised_content_and_no_traps(suites):
    for label, rec in _all(suites):
        if rec.badge is not UrlBadge.VERIFIED_WITHIN_SCOPE:
            continue
        fixture = BY_PATH[_path(rec)]
        corpus = _corpus(rec)
        main = whole_content(rec.blocks, include_boilerplate=False)
        for text in fixture.get("must_contain", []) + fixture.get("must_contain_normalized", []):
            assert text in corpus, (label, _path(rec), text)
        for text in fixture.get("must_not_contain", []):
            assert text not in corpus, (label, _path(rec), text)
        for text in fixture.get("must_not_contain_in_main", []):
            assert text not in main, (label, _path(rec), text)
        assert all(b.status.value == "VERIFIED_EXTRACTED" for b in rec.blocks), label
