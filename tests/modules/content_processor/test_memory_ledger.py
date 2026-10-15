from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.modules.content_processor.contracts import ContentBlock, Provenance, RunEvent, Task
from src.modules.content_processor.enums import (
    AccessTier,
    BlockKind,
    ContentLayer,
    EntityStatus,
    PriorityClass,
    TaskKind,
    TaskState,
)
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.workers.ports import RobotsCache


def _task(n: int, host: str = "h.example", kind: TaskKind = TaskKind.FETCH) -> Task:
    return Task(
        run_id="r",
        kind=kind,
        idempotency_key=f"k{n}",
        host=host,
        priority=PriorityClass.HTML_PAGE,
        max_attempts=2,
        payload={"requested_url": f"https://{host}/{n}", "schedule_key": f"https://{host}/{n}"},
    )


def test_enqueue_dedupes_and_counts():
    led = MemoryLedger()
    assert led.enqueue([_task(1), _task(2), _task(1)]) == 2
    assert led.task_counts() == {"PENDING": 2}


def test_host_slot_and_politeness_delay():
    led = MemoryLedger(base_delay_s=0.2)
    led.enqueue([_task(1), _task(2)])
    first = led.lease("w", [TaskKind.FETCH])
    assert first is not None and first.state is TaskState.LEASED and first.attempts == 1
    assert led.lease("w", [TaskKind.FETCH]) is None, "one in-flight per host"
    assert led.complete(first.id or 0, "w")
    assert led.lease("w", [TaskKind.FETCH]) is None, "delay still running"
    assert led.pending_count([TaskKind.FETCH]) == 1
    import time

    time.sleep(0.25)
    second = led.lease("w", [TaskKind.FETCH])
    assert second is not None and second.id != first.id


def test_plain_kinds_bypass_host_slot():
    led = MemoryLedger()
    led.enqueue([_task(1, kind=TaskKind.VERIFY), _task(2, kind=TaskKind.VERIFY)])
    assert led.lease("a", [TaskKind.VERIFY]) is not None
    assert led.lease("b", [TaskKind.VERIFY]) is not None


def test_fail_retries_until_max_then_fails():
    led = MemoryLedger(base_delay_s=0)
    led.enqueue([_task(1)])
    t = led.lease("w", [TaskKind.FETCH])
    assert t is not None
    assert led.fail(t.id or 0, "w", "boom", retry=True) is TaskState.PENDING
    assert led.heartbeat(t.id or 0, "w") is False
    t = led.lease("w", [TaskKind.FETCH])
    assert t is not None and t.attempts == 2
    assert led.fail(t.id or 0, "w", "boom", retry=True) is TaskState.FAILED
    assert led.task_counts() == {"FAILED": 1}
    assert led.pending_count([TaskKind.FETCH]) == 0


def test_not_before_and_retry_after_are_respected():
    led = MemoryLedger(base_delay_s=0)
    led.enqueue([_task(1)])
    t = led.lease("w", [TaskKind.FETCH])
    assert t is not None
    later = datetime.now(UTC) + timedelta(seconds=60)
    led.fail(t.id or 0, "w", "429", retry=True, not_before=later)
    assert led.lease("w", [TaskKind.FETCH]) is None
    state = led.load_host_state("t", "h.example")
    led.save_host_state("t", state, retry_after_until=later)
    led.enqueue([_task(2)])
    assert led.lease("w", [TaskKind.FETCH]) is None, "retry-after hold applies to the host"


def test_circuit_open_blocks_leasing():
    led = MemoryLedger(base_delay_s=0)
    led.enqueue([_task(1)])
    state = led.load_host_state("t", "h.example")
    for _ in range(3):
        state.record_block(BlockKind.RATE)
    led.save_host_state("t", state)
    assert led.lease("w", [TaskKind.FETCH]) is None


def test_robots_cache_sets_crawl_delay_and_report_shape():
    led = MemoryLedger(base_delay_s=0)
    led.set_robots(
        "t",
        "h.example",
        RobotsCache(
            body="User-agent: *\nCrawl-delay: 3\n", status=200, fetched_at=datetime.now(UTC)
        ),
    )
    led.load_host_state("t", "h.example")
    led.enqueue([_task(1)])
    t = led.lease("w", [TaskKind.FETCH])
    assert t is not None
    led.upsert_blocks(
        "res-1",
        [
            ContentBlock(
                block_id="b1",
                block_key="k",
                resource_id="res-1",
                state_id="r|source",
                region_id="b1",
                reading_order=0,
                layer=ContentLayer.RENDERED_VISIBLE,
                raw_text="x",
                normalized_text="x",
                method="html_source",
                tool_version="t",
                confidence=1.0,
                status=EntityStatus.EXTRACTED_UNVERIFIED,
                evidence_ids=["e"],
                provenance=Provenance(),
            )
        ],
    )
    led.append_event(
        RunEvent(
            run_id="r",
            entity_type="host",
            entity_id="h.example",
            old_state=None,
            new_state="T1_POLITE_HTTP",
            reason="x",
        )
    )
    report = led.report()
    assert report["hosts"]["h.example"]["crawl_delay_s"] == 3.0
    assert report["hosts"]["h.example"]["tier"] == AccessTier.T1_POLITE_HTTP.value
    assert report["host_events"][0]["reason"] == "x"
    assert report["tasks"] == {"LEASED": 1}
