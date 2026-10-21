"""Ledger integration and chaos tests. Need CP_DATABASE_URL (disposable Postgres).

Each test creates its own run id, so a shared database is safe; the schema is
idempotent. Chaos tests force the failure modes in ARCHITECTURE.md §15.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from src.modules.content_processor.contracts import RunEvent, RunScope, Task
from src.modules.content_processor.enums import PriorityClass, TaskKind, TaskState
from src.modules.content_processor.ledger.keys import task_key
from src.modules.content_processor.ledger.tasks import TaskLedger

pytestmark = pytest.mark.integration


@pytest.fixture
def ledger_db(database_url: str):
    led = TaskLedger(database_url, lease_ttl_s=2.0)
    led.create_schema()
    yield led
    led.close()


@pytest.fixture
def run(ledger_db: TaskLedger, scope: RunScope) -> str:
    run_id = f"test-{uuid.uuid4().hex[:8]}"
    ledger_db.create_run(run_id, scope, {"engine": "0.1.0"})
    return run_id


@pytest.fixture(autouse=True)
def _clean_db(ledger_db: TaskLedger):
    def _reset():
        with contextlib.suppress(Exception):
            ledger_db._conn.rollback()
        with ledger_db._conn.cursor() as cur:
            cur.execute(
                "TRUNCATE runs, host_policy, tasks, resources, resource_edges, states, regions, "
                "extractions, evidence, gaps, events, cost_ledger CASCADE"
            )
        ledger_db._conn.commit()

    _reset()
    yield
    _reset()


def _task(run_id: str, host: str, n: int, kind: TaskKind = TaskKind.FETCH) -> Task:
    return Task(
        run_id=run_id,
        kind=kind,
        idempotency_key=task_key(run_id, kind, f"{host}/{n}"),
        host=host,
        priority=PriorityClass.HTML_PAGE,
        max_attempts=2,
    )


def test_enqueue_is_idempotent(ledger_db: TaskLedger, run: str):
    tasks = [_task(run, "example.com", i) for i in range(3)]
    assert ledger_db.enqueue("t1", tasks) == 3
    assert ledger_db.enqueue("t1", tasks) == 0
    assert ledger_db.task_counts(run) == {"PENDING": 3}


def test_host_slot_limits_concurrency_to_one(ledger_db: TaskLedger, run: str):
    ledger_db.enqueue("t1", [_task(run, "example.com", i) for i in range(2)])
    first = ledger_db.lease("w1", [TaskKind.FETCH])
    assert first is not None and first.state is TaskState.LEASED
    second = ledger_db.lease("w2", [TaskKind.FETCH])
    assert second is None, "host slot must block a second concurrent fetch"
    assert ledger_db.complete(first.id or 0, "w1")
    # base_delay_ms gate: next_allowed_at is ~1s ahead; wait it out.
    import time

    time.sleep(1.1)
    third = ledger_db.lease("w2", [TaskKind.FETCH])
    assert third is not None
    assert ledger_db.complete(third.id or 0, "w2")


def test_plain_kinds_do_not_need_a_slot(ledger_db: TaskLedger, run: str):
    ledger_db.enqueue("t1", [_task(run, "example.com", i, TaskKind.VERIFY) for i in range(2)])
    a = ledger_db.lease("w1", [TaskKind.VERIFY])
    b = ledger_db.lease("w2", [TaskKind.VERIFY])
    assert a is not None and b is not None and a.id != b.id


@pytest.mark.chaos
def test_crashed_worker_lease_is_reclaimed_then_quarantined(ledger_db: TaskLedger, run: str):
    import time

    ledger_db.enqueue("t1", [_task(run, "example.com", 1)])
    t = ledger_db.lease("w-crash", [TaskKind.FETCH])
    assert t is not None and t.attempts == 1
    time.sleep(2.2)  # lease ttl is 2s; the worker never heartbeats
    reaped = ledger_db.reap()
    assert [r["state"] for r in reaped] == ["PENDING"]
    time.sleep(1.1)
    t2 = ledger_db.lease("w-crash-2", [TaskKind.FETCH])
    assert t2 is not None and t2.attempts == 2
    time.sleep(2.2)
    reaped = ledger_db.reap()
    assert [r["state"] for r in reaped] == ["QUARANTINED"]
    assert ledger_db.task_counts(run) == {"QUARANTINED": 1}


@pytest.mark.chaos
def test_lost_lease_cannot_complete(ledger_db: TaskLedger, run: str):
    import time

    ledger_db.enqueue("t1", [_task(run, "example.com", 1)])
    t = ledger_db.lease("w1", [TaskKind.FETCH])
    assert t is not None
    time.sleep(2.2)
    ledger_db.reap()
    assert ledger_db.heartbeat(t.id or 0, "w1") is False
    assert ledger_db.complete(t.id or 0, "w1") is False


def test_fail_with_retry_requeues_until_max(ledger_db: TaskLedger, run: str):
    ledger_db.enqueue("t1", [_task(run, "example.com", 1)])
    t = ledger_db.lease("w1", [TaskKind.FETCH])
    assert t is not None
    state = ledger_db.fail(
        t.id or 0, "w1", "503", retry=True, not_before=datetime.now(UTC) - timedelta(seconds=1)
    )
    assert state is TaskState.PENDING
    import time

    time.sleep(1.1)
    t = ledger_db.lease("w1", [TaskKind.FETCH])
    assert t is not None and t.attempts == 2
    assert ledger_db.fail(t.id or 0, "w1", "503 again", retry=True) is TaskState.FAILED


def test_checkpoint_survives_reclaim(ledger_db: TaskLedger, run: str):
    import time

    ledger_db.enqueue("t1", [_task(run, "example.com", 1)])
    t = ledger_db.lease("w1", [TaskKind.FETCH])
    assert t is not None
    assert ledger_db.checkpoint(t.id or 0, "w1", {"state_index": 3})
    time.sleep(2.2)
    ledger_db.reap()
    time.sleep(1.1)
    t2 = ledger_db.lease("w2", [TaskKind.FETCH])
    assert t2 is not None and t2.checkpoint == {"state_index": 3}


def test_cancel_stops_new_leases(ledger_db: TaskLedger, run: str):
    ledger_db.enqueue("t1", [_task(run, "example.com", 1)])
    ledger_db.cancel_run(run)
    assert ledger_db.lease("w1", [TaskKind.FETCH]) is None


def test_events_are_sequenced(ledger_db: TaskLedger, run: str):
    seq1 = ledger_db.append_event(
        RunEvent(run_id=run, entity_type="run", entity_id=run, old_state=None, new_state="ADMITTED")
    )
    seq2 = ledger_db.append_event(
        RunEvent(
            run_id=run,
            entity_type="run",
            entity_id=run,
            old_state="ADMITTED",
            new_state="ACQUIRING",
        )
    )
    assert seq2 > seq1 > 0


# -- CR-007 and K16 against real SQL --------------------------------------------


def test_leases_and_pending_counts_are_run_scoped(ledger_db: TaskLedger, run: str, scope: RunScope):
    other = f"test-{uuid.uuid4().hex[:8]}"
    ledger_db.create_run(other, scope, {"engine": "0.1.0"})
    ledger_db.enqueue("t1", [_task(run, "a.example", 1, TaskKind.VERIFY)])
    ledger_db.enqueue("t1", [_task(other, "b.example", i, TaskKind.VERIFY) for i in range(2)])
    assert ledger_db.pending_count([TaskKind.VERIFY], run_id=run) == 1
    assert ledger_db.pending_count([TaskKind.VERIFY], run_id=other) == 2
    mine = ledger_db.lease("w1", [TaskKind.VERIFY], run_id=run)
    assert mine is not None and mine.run_id == run
    assert ledger_db.lease("w1", [TaskKind.VERIFY], run_id=run) is None
    theirs = ledger_db.lease("w2", [TaskKind.VERIFY], run_id=other)
    assert theirs is not None and theirs.run_id == other


def test_blocks_of_every_channel_and_document_survive(ledger_db: TaskLedger, run: str):
    from src.modules.content_processor.adapters.html_source import extract_html_source
    from src.modules.content_processor.contracts import Resource

    html = b"<html><body><main><p>Same words.</p></main></body></html>"
    stored: dict[str, int] = {}
    for rid in ("res-k16-a", "res-k16-b"):
        ledger_db.upsert_resource(
            Resource(resource_id=rid, run_id=run, requested_url=rid, schedule_key=rid), "h"
        )
        source = extract_html_source(
            html, base_url="https://h/", evidence_id="e", resource_id=rid, state_id=f"{run}|source"
        ).blocks
        # The pre-K16 collision: a rendered copy with the very same block and region id.
        rendered = [b.model_copy(update={"state_id": f"{run}|rendered"}) for b in source]
        pdf = [
            b.model_copy(
                update={"state_id": f"{run}|pdf", "region_id": "reg-pdf-metadata", "method": "pdf"}
            )
            for b in source
        ]
        ledger_db.upsert_blocks(rid, [*source, *rendered, *pdf])
        stored[rid] = len(source) + len(rendered) + len(pdf)
    for rid, count in stored.items():
        blocks = ledger_db.get_blocks(rid)
        assert len(blocks) == count, rid
        assert {b.state_id.rsplit("|", 1)[1] for b in blocks} == {"source", "rendered", "pdf"}
