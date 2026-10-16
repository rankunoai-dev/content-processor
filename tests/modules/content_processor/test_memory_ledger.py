from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.modules.content_processor.contracts import (
    ContentBlock,
    Gap,
    Provenance,
    Region,
    Resource,
    RunEvent,
    State,
    Task,
)
from src.modules.content_processor.enums import (
    AccessTier,
    BlockKind,
    ContentLayer,
    EntityStatus,
    GapType,
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


def test_edges_and_resource_lookups():
    led = MemoryLedger()
    res1 = Resource(
        resource_id="res-1",
        run_id="run-1",
        requested_url="https://h.example/1",
        schedule_key="https://h.example/1",
        payload_sha256="hash-1",
        expected_mime="text/html",
        expected_mime_source="link_type",
    )
    res2 = Resource(
        resource_id="res-2",
        run_id="run-1",
        requested_url="https://h.example/2",
        schedule_key="https://h.example/2",
        payload_sha256="hash-2",
    )
    led.upsert_resource(res1, "h.example")
    led.upsert_resource(res2, "h.example")

    assert led.get_resource("res-1") is not None
    assert led.get_resource("nonexistent") is None
    assert led.find_resource_by_schedule_key("run-1", "https://h.example/1") is not None
    assert led.find_resource_by_schedule_key("other-run", "https://h.example/1") is None
    assert led.find_resource_by_payload("run-1", "hash-2") is not None
    assert led.find_resource_by_payload("run-1", "hash-missing") is None

    resources = led.list_resources("run-1")
    assert len(resources) == 2
    assert [r.resource_id for r in resources] == ["res-1", "res-2"]
    assert led.list_resources("other-run") == []

    led.record_edge("run-1", "res-1", "res-2", "embeds", evidence_id="ev-1")
    assert ("run-1", "res-1", "res-2", "embeds") in led.edges
    assert led.edges[("run-1", "res-1", "res-2", "embeds")]["evidence_id"] == "ev-1"


def test_states_and_regions():
    led = MemoryLedger()
    st = State(
        state_id="st-1",
        run_id="run-1",
        resource_id="res-1",
        state_key="default",
        status=EntityStatus.DISCOVERED,
    )
    led.upsert_state(st)
    states = led.get_states("run-1", "res-1")
    assert len(states) == 1
    assert states[0].state_id == "st-1"
    assert led.get_states("run-1", "res-2") == []

    reg = Region(
        region_id="reg-1",
        run_id="run-1",
        resource_id="res-1",
        region_key="main",
        status=EntityStatus.DISCOVERED,
    )
    led.upsert_region(reg)
    regions = led.get_regions("run-1", "res-1")
    assert len(regions) == 1
    assert regions[0].region_id == "reg-1"
    assert led.get_regions("run-1", "res-2") == []


def test_summaries_gaps_events_outstanding():
    led = MemoryLedger()
    led.record_summary("res-1", "render", {"width": 1280})
    led.record_summary("res-1", "explore", {"tabs": 2})
    summaries = led.get_summaries("res-1")
    assert summaries == {"render": {"width": 1280}, "explore": {"tabs": 2}}
    assert led.get_summaries("res-missing") == {}

    gap = Gap(
        gap_id="gap-1",
        run_id="run-1",
        resource_id="res-1",
        gap_type=GapType.SOFT_404_SUSPECTED,
        status=EntityStatus.EXTRACTED_UNVERIFIED,
        reason="soft_404_suspected",
    )
    led.record_gap(gap)
    gaps = led.get_gaps("res-1")
    assert len(gaps) == 1
    assert gaps[0].gap_id == "gap-1"
    assert led.get_gaps("res-missing") == []

    ev1 = RunEvent(
        run_id="run-1",
        entity_type="resource",
        entity_id="res-1",
        old_state=None,
        new_state="DISCOVERED",
    )
    ev2 = RunEvent(
        run_id="run-1",
        entity_type="resource",
        entity_id="res-2",
        old_state=None,
        new_state="DISCOVERED",
    )
    led.append_event(ev1)
    led.append_event(ev2)
    assert len(led.get_events("run-1")) == 2
    assert len(led.get_events("run-1", "res-1")) == 1
    assert len(led.get_events("run-2")) == 0

    res = Resource(
        resource_id="res-1",
        run_id="run-1",
        requested_url="https://h.example/1",
        schedule_key="https://h.example/1",
    )
    led.upsert_resource(res, "h.example")

    t1 = Task(
        run_id="run-1",
        kind=TaskKind.FETCH,
        idempotency_key="k1",
        host="h.example",
        priority=PriorityClass.HTML_PAGE,
        payload={"schedule_key": "https://h.example/1"},
    )
    t2 = Task(
        run_id="run-1",
        kind=TaskKind.RENDER,
        idempotency_key="k2",
        host="h.example",
        priority=PriorityClass.HTML_PAGE,
        payload={"resource_id": "res-2"},
    )
    led.enqueue_followups([t1, t2])

    assert led.outstanding_resource_ids("run-1", [TaskKind.FETCH]) == {"res-1"}
    assert led.outstanding_resource_ids("run-1", [TaskKind.RENDER]) == {"res-2"}
    assert led.outstanding_resource_ids("run-1", [TaskKind.FETCH, TaskKind.RENDER]) == {
        "res-1",
        "res-2",
    }
    assert led.outstanding_resource_ids("run-1", [TaskKind.EXTRACT_DOC]) == set()
    assert led.outstanding_resource_ids("run-2", [TaskKind.FETCH]) == set()
