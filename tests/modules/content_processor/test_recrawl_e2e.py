"""Two runs over the same URL: the second reuses unchanged pages and re-extracts changed ones."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from fixtures import server
from fixtures.server import app
from src.integrations.evidence_store import LocalEvidenceStore
from src.integrations.http_client import FetchLimits, HttpFetcher
from src.modules.content_processor.contracts import Authorization, RunScope, UrlRecord
from src.modules.content_processor.enums import (
    AuthorizationKind,
    ChangeStatus,
    SourceFormat,
    TaskKind,
    UiRegion,
)
from src.modules.content_processor.export.project_report import scrape_action
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.planner import fetch_tasks_for
from src.modules.content_processor.recrawl import Baseline
from src.modules.content_processor.recrawl.history import UrlHistory
from src.modules.content_processor.scheduler import (
    RunOptions,
    assemble_run_records,
    execute_run,
)
from src.modules.content_processor.scope.frontier import build_frontier
from src.modules.content_processor.workers.fetch_worker import FetchWorker
from src.modules.content_processor.workers.ports import RobotsCache

HOST = "127.0.0.1"
URL = f"http://{HOST}:8765/fresh"
SCOPE = RunScope(
    tenant_id="t1",
    input_urls=[URL],
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
DEFAULT = dict(server.FRESH_PAGE)


@pytest.fixture(autouse=True)
def _reset_page() -> Iterator[None]:
    server.FRESH_PAGE.clear()
    server.FRESH_PAGE.update(DEFAULT)
    yield
    server.FRESH_PAGE.clear()
    server.FRESH_PAGE.update(DEFAULT)


async def _run(
    tmp: Path, run_id: str, baseline: Baseline | None
) -> tuple[MemoryLedger, UrlRecord, list[str]]:
    plan = build_frontier(SCOPE, resolver=lambda _h: [HOST])
    ledger = MemoryLedger(base_delay_s=0.0)
    ledger.set_robots(
        "t1", HOST, RobotsCache(body="User-agent: *\n", status=200, fetched_at=datetime.now(UTC))
    )
    ledger.enqueue(fetch_tasks_for(run_id, plan))
    evidence = LocalEvidenceStore(tmp, "t1")
    sent: list[str] = []

    async def capture(request: httpx.Request) -> None:
        if request.url.path == "/fresh":
            sent.append(request.headers.get("if-none-match", ""))

    fetcher = HttpFetcher(
        host_checker=lambda _h: (True, "fixture"),
        user_agent="recrawl-test",
        transport=httpx.ASGITransport(app=app),
        limits=FetchLimits(max_bytes=1_000_000, total_timeout_s=5),
    )
    fetcher._client.event_hooks["request"].append(capture)  # noqa: SLF001 - observe headers
    worker = FetchWorker(
        ledger=ledger,
        fetcher=fetcher,
        evidence=evidence,
        scope=SCOPE,
        run_id=run_id,
        worker_id=f"{run_id}-fetch",
        baseline=baseline,
    )
    try:
        await execute_run(
            ledger,
            run_id=run_id,
            stages={"fetch": worker},
            options=RunOptions(stages=["fetch"], idle_polls=1, poll_s=0.01, max_wait_s=0.5),
        )
    finally:
        await fetcher.aclose()
    records = assemble_run_records(
        ledger, run_id=run_id, plan=plan, evidence=evidence, stages_that_will_run=["fetch"]
    )
    return ledger, next(r for r in records if r.requested_url == URL), sent


def _render_tasks(ledger: MemoryLedger) -> int:
    return sum(1 for t in ledger._tasks.values() if t.kind is TaskKind.RENDER)  # noqa: SLF001


async def test_first_run_tags_content_and_records_dates(tmp_path: Path):
    ledger, rec, sent = await _run(tmp_path, "run-1", None)
    assert sent == [""], "no baseline: plain GET"
    assert rec.change_status is None
    assert rec.http_etag == '"v1"'
    assert rec.page_modified_at == datetime(2026, 9, 1, 10, tzinfo=UTC)
    assert rec.page_modified_source == "jsonld:dateModified"
    assert rec.page_published_at == datetime(2025, 1, 15, tzinfo=UTC)
    assert rec.content_fingerprint and rec.payload_sha256
    assert [f.url for f in rec.followups] == [f"http://{HOST}:8765/files/brochure.pdf"]
    fee = next(b for b in rec.blocks if "annual fee" in b.normalized_text)
    assert fee.source_format is SourceFormat.HTML and fee.ui_region is UiRegion.MAIN_CONTENT
    assert fee.source_url == URL and fee.extracted_at is not None
    assert fee.page_modified_at == rec.page_modified_at
    assert _render_tasks(ledger) == 1


async def _second(
    tmp_path: Path, **page: Any
) -> tuple[MemoryLedger, UrlRecord, UrlRecord, list[str]]:
    _, first, _ = await _run(tmp_path / "a", "run-1", None)
    server.FRESH_PAGE.update(page)
    ledger, second, sent = await _run(tmp_path / "b", "run-2", Baseline([first]))
    return ledger, first, second, sent


async def test_unchanged_by_etag_reuses_without_extracting(tmp_path: Path):
    ledger, first, second, sent = await _second(tmp_path)
    assert sent == ['"v1"'], "conditional GET sent the previous ETag"
    assert second.change_status is ChangeStatus.UNCHANGED
    assert second.change_basis == "http_304_not_modified"
    assert second.reused_from_run_id == "run-1" and second.previous_run_id == "run-1"
    assert _render_tasks(ledger) == 0, "no render, exploration or OCR for an unchanged page"
    reused = next(b for b in second.blocks if "annual fee" in b.normalized_text)
    original = next(b for b in first.blocks if "annual fee" in b.normalized_text)
    assert reused.reused_from_run_id == "run-1"
    assert reused.extracted_at == original.extracted_at, "keeps the original extraction date"
    assert second.page_modified_at == first.page_modified_at
    assert second.resource_status_reason.startswith("unchanged:http_304_not_modified")
    # the brochure the page links to is re-queued and checked on its own
    assert any(t.payload.get("depth") == 1 for t in ledger._tasks.values())  # noqa: SLF001


async def test_unchanged_bytes_without_etag(tmp_path: Path):
    ledger, _, second, sent = await _second(tmp_path, send_etag=False)
    assert second.change_basis == "payload_identical"
    assert second.change_status is ChangeStatus.UNCHANGED and _render_tasks(ledger) == 0


async def test_nonce_changes_bytes_but_not_text(tmp_path: Path):
    server.FRESH_PAGE.update(send_etag=False, nonce=True)
    ledger, _, second, _ = await _second(tmp_path, send_etag=False, nonce=True)
    assert second.change_status is ChangeStatus.UNCHANGED
    assert second.change_basis == "visible_text_identical;bytes_differ"
    assert _render_tasks(ledger) == 0


async def test_changed_text_with_newer_date_is_re_extracted(tmp_path: Path):
    ledger, _, second, _ = await _second(
        tmp_path,
        etag='"v2"',
        text="Our annual fee is $150 for every plan.",
        modified="2026-10-10T08:00:00Z",
    )
    assert second.change_status is ChangeStatus.CHANGED
    assert second.change_basis == "site_modified_date_newer"
    assert second.reused_from_run_id is None
    assert any("$150" in b.normalized_text for b in second.blocks)
    assert _render_tasks(ledger) == 1, "a changed page goes through the full pipeline"


async def test_changed_text_with_stale_date_is_still_re_extracted(tmp_path: Path):
    _, _, second, _ = await _second(
        tmp_path, etag='"v2"', text="Our annual fee is $150 for every plan."
    )
    assert second.change_status is ChangeStatus.CHANGED
    assert second.change_basis == "visible_text_changed;site_modified_date_not_updated"


async def test_url_history_is_the_baseline_without_naming_a_previous_run(tmp_path: Path):
    """Run 1 is remembered; run 2 finds it in the history and reuses the unchanged page."""
    history = UrlHistory(tmp_path / "history", "t1")
    _, first, _ = await _run(tmp_path / "a", "run-1", None)
    assert history.remember([first], run_name="first") == 1
    ledger, second, sent = await _run(tmp_path / "b", "run-2", history.baseline_for([URL]))
    assert sent == ['"v1"'], "the history supplied the previous ETag"
    assert second.change_status is ChangeStatus.UNCHANGED
    assert _render_tasks(ledger) == 0
    since = first.captured_at.date().isoformat()
    assert scrape_action(second) == f"Reused (unchanged since {since}: server said not modified)"
    # the history now points at the newer check, still with the first capture's text date
    history.remember([second], run_name="second")
    entry = UrlHistory(tmp_path / "history", "t1").lookup(URL)
    assert entry is not None and entry.run_name == "second"
    assert entry.content_captured_at is not None
    assert entry.content_captured_at.date() == first.captured_at.date()


async def test_url_history_sees_a_changed_page_and_re_extracts(tmp_path: Path):
    history = UrlHistory(tmp_path / "history", "t1")
    _, first, _ = await _run(tmp_path / "a", "run-1", None)
    history.remember([first])
    server.FRESH_PAGE.update(etag='"v2"', text="Our annual fee is $150 for every plan.")
    ledger, second, _ = await _run(tmp_path / "b", "run-2", history.baseline_for([URL]))
    assert second.change_status is ChangeStatus.CHANGED
    assert _render_tasks(ledger) == 1
    assert scrape_action(second).startswith("Re-scraped (updated: ")
