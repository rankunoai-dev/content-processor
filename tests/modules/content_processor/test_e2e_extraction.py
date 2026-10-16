"""Web to ledger end to end: K17 (doc extraction queued), K16 (block keys), CR-007 (run scope).

The end-to-end tests drive the real fetch worker and document worker through
the scheduler on the in-memory ledger against the in-process fixture app.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from fixtures.server import app
from src.integrations.evidence_store import LocalEvidenceStore
from src.integrations.http_client import HttpFetcher
from src.modules.content_processor.adapters.html_source import block_channel, extract_html_source
from src.modules.content_processor.contracts import Authorization, RunScope, Task
from src.modules.content_processor.enums import (
    AuthorizationKind,
    EntityStatus,
    PriorityClass,
    TaskKind,
    TaskState,
)
from src.modules.content_processor.ledger import sql
from src.modules.content_processor.ledger.keys import task_key
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.ledger.tasks import TaskLedger, storage_keys
from src.modules.content_processor.planner import fetch_tasks_for
from src.modules.content_processor.scheduler.runner import RunOptions, execute_run
from src.modules.content_processor.scope.frontier import build_frontier
from src.modules.content_processor.workers.doc_worker import DocWorker
from src.modules.content_processor.workers.fetch_worker import FetchWorker, extraction_kind_for
from src.modules.content_processor.workers.ports import RobotsCache

HOST = "testserver"
BASE = f"http://{HOST}"


def _scope(urls: list[str]) -> RunScope:
    return RunScope(
        tenant_id="t1",
        input_urls=urls,
        hosts_in_scope=[HOST],
        authorization=Authorization(
            kind=AuthorizationKind.PUBLIC_THIRD_PARTY,
            reference="public",
            approved_by="lead",
            approved_at=datetime(2026, 10, 16, tzinfo=UTC),
            hosts=[HOST],
        ),
        egress_allow_private=True,
    )


def _ledger() -> MemoryLedger:
    ledger = MemoryLedger(base_delay_s=0)
    ledger.robots[HOST] = RobotsCache(
        body="User-agent: *\nAllow: /\n", status=200, fetched_at=datetime.now(UTC)
    )
    return ledger


def _plan(ledger: MemoryLedger, run_id: str, urls: list[str]) -> RunScope:
    scope = _scope(urls)
    plan = build_frontier(scope, resolver=lambda _h: ["93.184.216.34"])
    ledger.enqueue(fetch_tasks_for(run_id, plan))
    return scope


def _workers(ledger: MemoryLedger, scope: RunScope, run_id: str, tmp: Path):
    fetcher = HttpFetcher(
        host_checker=lambda _h: (True, "test"),
        user_agent="test/1.0",
        transport=httpx.ASGITransport(app=app),
    )
    evidence = LocalEvidenceStore(tmp, "t1")
    fetch = FetchWorker(
        ledger=ledger,
        fetcher=fetcher,
        evidence=evidence,
        scope=scope,
        run_id=run_id,
        worker_id=f"{run_id}-fetch",
    )
    docs = DocWorker(
        ledger=ledger, evidence=evidence, run_id=run_id, worker_id=f"{run_id}-docs", isolate=False
    )
    return fetcher, fetch, docs


_FAST = RunOptions(idle_polls=1, poll_s=0.01, max_wait_s=0.0)


# -- K17 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("media_type", "kind"),
    [
        ("application/pdf", TaskKind.EXTRACT_DOC),
        ("image/png", TaskKind.EXTRACT_IMAGE),
        ("image/svg+xml", TaskKind.EXTRACT_IMAGE),
        ("application/zip", None),
        ("text/plain", None),
    ],
)
def test_extraction_kind_for(media_type, kind):
    assert extraction_kind_for(media_type) is kind


async def test_linked_pdf_and_image_are_extracted_by_the_doc_worker(tmp_path: Path):
    ledger, run = _ledger(), "run-k17"
    scope = _plan(ledger, run, [f"{BASE}/docs-hub"])
    fetcher, fetch, docs = _workers(ledger, scope, run, tmp_path)
    try:
        execution = await execute_run(
            ledger, run_id=run, stages={"fetch": fetch, "docs": docs}, options=_FAST
        )
    finally:
        await fetcher.aclose()
    assert execution.stop_reason == "drained" and execution.processed["docs"] == 2
    kinds = {t.kind: t.state for t in ledger._tasks.values() if t.kind is not TaskKind.FETCH}
    assert kinds[TaskKind.EXTRACT_DOC] is TaskState.DONE
    assert kinds[TaskKind.EXTRACT_IMAGE] is TaskState.DONE

    pdf = ledger.resources[f"{BASE}/files/annual-report.pdf"]
    texts = [b.normalized_text for b in ledger.get_blocks(pdf.resource_id)]
    assert "Premiums grew in every region." in " ".join(texts)
    assert pdf.status is not EntityStatus.IN_PROGRESS
    image = ledger.resources[f"{BASE}/files/logo.png"]
    assert image.status is not EntityStatus.IN_PROGRESS, "the image was handed off and finished"
    task = next(t for t in ledger._tasks.values() if t.kind is TaskKind.EXTRACT_DOC)
    assert task.payload["resource_id"] == pdf.resource_id
    assert task.payload["sha256"] == pdf.payload_sha256
    assert task.idempotency_key == task_key(run, TaskKind.EXTRACT_DOC, pdf.schedule_key)


async def test_payload_without_an_adapter_is_unsupported_not_left_in_progress(tmp_path: Path):
    ledger, run = _ledger(), "run-zip"
    scope = _plan(ledger, run, [f"{BASE}/files/archive.zip"])
    fetcher, fetch, _ = _workers(ledger, scope, run, tmp_path)

    def zip_body(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"PK\x03\x04rest", headers={"content-type": "x/zip"})

    fetch._fetcher = fetch._fetchers[next(iter(fetch._fetchers))] = HttpFetcher(
        host_checker=lambda _h: (True, "t"), user_agent="t", transport=httpx.MockTransport(zip_body)
    )
    try:
        await fetch.run_until_idle(idle_polls=1, poll_s=0, max_wait_s=0)
    finally:
        await fetcher.aclose()
        await fetch._fetcher.aclose()
    res = ledger.resources[f"{BASE}/files/archive.zip"]
    assert res.status is EntityStatus.UNSUPPORTED
    assert res.status_reason == "no_adapter:application/zip"
    assert [t.kind for t in ledger._tasks.values()] == [TaskKind.FETCH]


# -- K16 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state_id", "channel"),
    [
        ("run-1|source", "src"),
        ("run-1|rendered", "rendered"),
        ("run-1|state:2", "state2"),
        ("source", "src"),
        ("run-1|", "src"),
    ],
)
def test_block_channel(state_id, channel):
    assert block_channel(state_id) == channel


def test_source_rendered_and_state_blocks_of_one_page_have_distinct_ids():
    html = b"<html><body><main><p>One.</p><p>Two.</p></main></body></html>"
    ids: list[str] = []
    for state in ("r|source", "r|rendered", "r|state:1"):
        blocks = extract_html_source(
            html, base_url=BASE, evidence_id="evidence:x", resource_id="res-1", state_id=state
        ).blocks
        assert all(b.region_id == b.block_id for b in blocks)
        ids.extend(b.block_id for b in blocks)
    assert len(ids) == len(set(ids)) == 6
    assert ids[0] == "res-1:src:0", "source ids are unchanged"


def test_storage_keys_are_unique_per_resource_channel_and_method():
    html = b"<html><body><main><p>Same text.</p></main></body></html>"
    source = extract_html_source(
        html, base_url=BASE, evidence_id="e", resource_id="res-1", state_id="r|source"
    ).blocks[0]
    rendered = source.model_copy(update={"state_id": "r|rendered"})  # the old K16 collision
    pdf_a = source.model_copy(
        update={"state_id": "r|pdf", "region_id": "reg-pdf-metadata", "method": "pdf"}
    )
    keys = {
        storage_keys("res-1", source),
        storage_keys("res-1", rendered),
        storage_keys("res-1", pdf_a),
        storage_keys("res-2", pdf_a),
    }
    assert len(keys) == 4
    assert all(region.startswith(("res-1:", "res-2:")) for region, _ in keys)
    region, extraction = storage_keys("res-2", pdf_a)
    assert (region, extraction) == ("res-2:pdf:reg-pdf-metadata", "res-2:pdf:reg-pdf-metadata|pdf")


def test_task_ledger_writes_namespaced_block_keys():
    with patch("psycopg.connect") as connect:
        conn, cur = MagicMock(), MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        connect.return_value = conn
        ledger = TaskLedger("postgresql://test@localhost/test")
    block = extract_html_source(
        b"<p>x</p>", base_url=BASE, evidence_id="e", resource_id="res-9", state_id="run-9|rendered"
    ).blocks[0]
    ledger.upsert_blocks("res-9", [block])
    query, params = cur.execute.call_args[0]
    assert query == sql.SQL_UPSERT_BLOCK
    assert params["region_id"] == "res-9:rendered:res-9:rendered:0"
    assert params["extraction_id"] == "res-9:rendered:res-9:rendered:0|html_source"
    assert params["run_id"] == "run-9"
    assert '"block_id":"res-9:rendered:0"' in params["record"], "the record is stored as produced"


# -- CR-007 --------------------------------------------------------------------


def _task(run_id: str, n: int, kind: TaskKind = TaskKind.FETCH) -> Task:
    return Task(
        run_id=run_id,
        kind=kind,
        idempotency_key=task_key(run_id, kind, str(n)),
        host=f"h{n}.example",
        priority=PriorityClass.HTML_PAGE,
    )


def test_memory_ledger_leases_and_counts_within_one_run():
    ledger = MemoryLedger(base_delay_s=0)
    ledger.enqueue([_task("run-a", 1), _task("run-b", 2), _task("run-b", 3)])
    assert ledger.pending_count([TaskKind.FETCH]) == 3
    assert ledger.pending_count([TaskKind.FETCH], run_id="run-a") == 1
    leased = ledger.lease("w", [TaskKind.FETCH], run_id="run-b")
    assert leased is not None and leased.run_id == "run-b"
    second = ledger.lease("w", [TaskKind.FETCH], run_id="run-b")
    assert second is not None and second.run_id == "run-b"
    assert ledger.lease("w", [TaskKind.FETCH], run_id="run-b") is None
    assert ledger.lease("w", [TaskKind.FETCH], run_id="run-c") is None
    unscoped = ledger.lease("w", [TaskKind.FETCH])
    assert unscoped is not None and unscoped.run_id == "run-a"


def test_lease_sql_filters_by_run_and_task_ledger_passes_it():
    for statement in (sql.SQL_LEASE_PLAIN, sql.SQL_LEASE_WITH_SLOT):
        assert "(%(run_id)s::text IS NULL OR t.run_id = %(run_id)s::text)" in statement
    assert "(%(run_id)s::text IS NULL OR run_id = %(run_id)s::text)" in sql.SQL_PENDING_COUNT
    with patch("psycopg.connect") as connect:
        conn, cur = MagicMock(), MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.return_value = None
        connect.return_value = conn
        ledger = TaskLedger("postgresql://test@localhost/test")
    ledger.lease("w", [TaskKind.FETCH, TaskKind.EXTRACT_DOC], run_id="run-a")
    assert [c[0][1]["run_id"] for c in cur.execute.call_args_list] == ["run-a", "run-a"]
    cur.fetchone.return_value = {"n": 2}
    assert ledger.pending_count([TaskKind.FETCH], run_id="run-a") == 2
    assert cur.execute.call_args[0][1] == {"kinds": ["FETCH"], "run_id": "run-a"}


async def test_two_runs_share_a_ledger_without_touching_each_other(tmp_path: Path):
    ledger = _ledger()
    scope_a = _plan(ledger, "run-a", [f"{BASE}/pages/static_ok"])
    _plan(ledger, "run-b", [f"{BASE}/docs-hub"])
    fetcher, fetch_a, docs_a = _workers(ledger, scope_a, "run-a", tmp_path)
    try:
        execution = await execute_run(
            ledger, run_id="run-a", stages={"fetch": fetch_a, "docs": docs_a}, options=_FAST
        )
    finally:
        await fetcher.aclose()
    assert execution.stop_reason == "drained" and execution.processed["fetch"] == 1
    by_run: dict[str, set[TaskState]] = {}
    for t in ledger._tasks.values():
        if t.kind is not TaskKind.RENDER:  # run-a's render stage is not part of this drive
            by_run.setdefault(t.run_id, set()).add(t.state)
    assert by_run == {"run-a": {TaskState.DONE}, "run-b": {TaskState.PENDING}}
    assert {r.run_id for r in ledger.resources.values()} == {"run-a"}
