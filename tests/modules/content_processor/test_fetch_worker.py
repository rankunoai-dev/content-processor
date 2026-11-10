"""Fetch-worker loop tests against an in-memory ledger and the in-process fixture app."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from fixtures.server import app
from src.integrations.evidence_store import LocalEvidenceStore
from src.integrations.http_client import FetchLimits, HttpFetcher
from src.modules.content_processor.contracts import (
    Authorization,
    Resource,
    RunEvent,
    RunScope,
    Task,
)
from src.modules.content_processor.enums import (
    AccessTier,
    AuthorizationKind,
    EntityStatus,
    PriorityClass,
    TaskKind,
    TaskState,
)
from src.modules.content_processor.governor.escalation import HostAccessState
from src.modules.content_processor.workers.fetch_worker import FetchWorker, user_agent_for
from src.modules.content_processor.workers.ports import RobotsCache

HOST = "testserver"


class InMemoryLedger:
    """Minimal LedgerPort for loop tests."""

    def __init__(self) -> None:
        self.tasks: dict[int, Task] = {}
        self.resources: dict[str, Resource] = {}
        self.blocks: dict[str, list] = {}
        self.events: list[RunEvent] = []
        self.hosts: dict[str, HostAccessState] = {}
        self.robots: dict[str, RobotsCache] = {}
        self.retry_after: dict[str, datetime | None] = {}
        self._next = 1

    def add(self, url: str, ceiling: AccessTier = AccessTier.T3_FIDELITY_BROWSER) -> Task:
        task = Task(
            id=self._next,
            run_id="run-t",
            kind=TaskKind.FETCH,
            idempotency_key=f"k{self._next}",
            host=HOST,
            priority=PriorityClass.HTML_PAGE,
            max_attempts=2,
            payload={"requested_url": url, "schedule_key": url, "aliases": [], "mandatory": False},
        )
        self.tasks[task.id or 0] = task
        self.hosts.setdefault(HOST, HostAccessState(host=HOST, ceiling=ceiling))
        self._next += 1
        return task

    def lease(self, worker: str, kinds: Sequence[TaskKind], run_id=None) -> Task | None:
        for t in self.tasks.values():
            if t.state is TaskState.PENDING:
                t.state = TaskState.LEASED
                t.attempts += 1
                t.lease_owner = worker
                return t
        return None

    def heartbeat(self, task_id: int, worker: str) -> bool:
        return True

    def pending_count(self, kinds: Sequence[TaskKind], run_id=None) -> int:
        return sum(
            1
            for t in self.tasks.values()
            if t.kind in kinds and t.state in (TaskState.PENDING, TaskState.LEASED)
        )

    def complete(self, task_id: int, worker: str) -> bool:
        self.tasks[task_id].state = TaskState.DONE
        return True

    def fail(self, task_id: int, worker: str, error: str, *, retry: bool, not_before=None):
        t = self.tasks[task_id]
        t.last_error = error
        t.not_before = not_before
        t.state = TaskState.PENDING if retry and t.attempts < t.max_attempts else TaskState.FAILED
        return t.state

    def upsert_resource(self, resource: Resource, host: str) -> None:
        self.resources[resource.schedule_key] = resource.model_copy(deep=True)

    def upsert_blocks(self, resource_id: str, blocks) -> None:
        self.blocks[resource_id] = list(blocks)

    def get_resource(self, resource_id: str):
        return next((r for r in self.resources.values() if r.resource_id == resource_id), None)

    def get_blocks(self, resource_id: str):
        return list(self.blocks.get(resource_id, []))

    def enqueue_followups(self, tasks) -> int:
        self.followups = getattr(self, "followups", []) + list(tasks)
        return len(tasks)

    def record_gap(self, gap) -> None:
        self.gaps = getattr(self, "gaps", {})
        self.gaps[gap.gap_id] = gap

    def get_gaps(self, resource_id: str):
        return [g for g in getattr(self, "gaps", {}).values() if g.resource_id == resource_id]

    def record_summary(self, resource_id: str, key: str, value) -> None:
        self.summaries = getattr(self, "summaries", {})
        self.summaries.setdefault(resource_id, {})[key] = value

    def append_event(self, event: RunEvent) -> int:
        self.events.append(event)
        return len(self.events)

    def load_host_state(self, tenant_id: str, host: str) -> HostAccessState:
        return self.hosts[host]

    def save_host_state(self, tenant_id: str, state: HostAccessState, *, retry_after_until=None):
        self.hosts[state.host] = state
        self.retry_after[state.host] = retry_after_until

    def get_robots(self, tenant_id: str, host: str) -> RobotsCache | None:
        return self.robots.get(host)

    def set_robots(self, tenant_id: str, host: str, cache: RobotsCache) -> None:
        self.robots[host] = cache


def _scope(kind: AuthorizationKind = AuthorizationKind.PUBLIC_THIRD_PARTY) -> RunScope:
    return RunScope(
        tenant_id="t1",
        input_urls=["http://testserver/pages/static_ok"],
        hosts_in_scope=[HOST],
        authorization=Authorization(
            kind=kind,
            reference="public",
            approved_by="lead",
            approved_at=datetime(2026, 10, 15, tzinfo=UTC),
            hosts=[HOST],
        ),
        egress_allow_private=True,
    )


@pytest.fixture
async def worker_env(tmp_path: Path):
    ledger = InMemoryLedger()
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
        run_id="run-t",
        worker_id="w1",
    )
    yield ledger, worker
    await fetcher.aclose()


async def _run(ledger: InMemoryLedger, worker: FetchWorker, url: str) -> Resource:
    ledger.add(url)
    await worker.run_until_idle(idle_polls=1, poll_s=0, max_wait_s=0)
    return ledger.resources[url]


async def test_clean_page_is_acquired_with_evidence(worker_env, tmp_path: Path):
    ledger, worker = worker_env
    res = await _run(ledger, worker, "http://testserver/pages/static_ok")
    assert res.status is EntityStatus.EXTRACTED_UNVERIFIED
    assert res.status_reason == "source_extracted"
    texts = [b.normalized_text for b in ledger.blocks[res.resource_id]]
    assert "Eligibility applies only to annual plans." in texts
    assert [t.kind for t in ledger.followups] == [TaskKind.RENDER]
    assert ledger.followups[0].payload["resource_id"] == res.resource_id
    assert res.payload_sha256 and LocalEvidenceStore(tmp_path, "t1").exists(res.payload_sha256)
    assert res.sniffed_mime == "text/html" and res.acquired_at_tier is AccessTier.T1_POLITE_HTTP
    assert res.block_verdict is not None and not res.block_verdict.is_block
    assert ledger.robots[HOST].status == 200
    assert ledger.hosts[HOST].clean_streak == 1
    assert [e.new_state for e in ledger.events if e.entity_type == "resource"] == [
        "EXTRACTED_UNVERIFIED"
    ]


async def test_robots_disallow_is_excluded_without_fetching(worker_env):
    ledger, worker = worker_env
    res = await _run(ledger, worker, "http://testserver/private/secret")
    assert res.status is EntityStatus.EXCLUDED_BY_POLICY
    assert res.status_reason == "robots_disallow:parsed"
    assert res.http_status is None and res.payload_sha256 is None


async def test_challenge_page_is_blocked_and_only_that_url_escalates(worker_env):
    ledger, worker = worker_env
    res = await _run(ledger, worker, "http://testserver/challenge-200")
    assert res.status is EntityStatus.BLOCKED
    assert res.block_verdict is not None and res.block_verdict.vendor == "cloudflare"
    assert res.status_reason.startswith("js_challenge:cloudflare:fetched_at=T1_POLITE_HTTP")
    assert res.acquired_at_tier is AccessTier.T1_POLITE_HTTP
    # One blocked page does not move the whole site to the browser tier.
    assert ledger.hosts[HOST].tier is AccessTier.T1_POLITE_HTTP
    assert not any(e.entity_type == "host" for e in ledger.events)


async def test_host_moves_to_the_browser_tier_after_three_distinct_urls_block(worker_env):
    ledger, worker = worker_env
    for n in (1, 2):
        await _run(ledger, worker, f"http://testserver/challenge-200?page={n}")
        assert ledger.hosts[HOST].tier is AccessTier.T1_POLITE_HTTP
    # The same URL blocking again is not a new URL.
    await _run(ledger, worker, "http://testserver/challenge-200?page=2")
    assert ledger.hosts[HOST].tier is AccessTier.T1_POLITE_HTTP
    await _run(ledger, worker, "http://testserver/challenge-200?page=3")
    assert ledger.hosts[HOST].tier is AccessTier.T3_FIDELITY_BROWSER
    moves = [e for e in ledger.events if e.entity_type == "host"]
    assert [(e.old_state, e.new_state, e.reason) for e in moves] == [
        ("T1_POLITE_HTTP", "T3_FIDELITY_BROWSER", "escalate_on:JS_CHALLENGE")
    ]


async def test_paid_tier_needed_emits_approval_event_not_spend(worker_env):
    ledger, worker = worker_env
    ledger.hosts[HOST] = HostAccessState(
        host=HOST, ceiling=AccessTier.T3_FIDELITY_BROWSER, tier=AccessTier.T3_FIDELITY_BROWSER
    )
    ledger.add("http://testserver/challenge-200")
    await worker.run_until_idle(idle_polls=1, poll_s=0, max_wait_s=0)
    # The host already stands at T3 and this run has no browser: the next step for a
    # JS challenge is T4+, which is paid: ask, do not escalate.
    assert ledger.hosts[HOST].tier is AccessTier.T3_FIDELITY_BROWSER
    approvals = [
        e for e in ledger.events if e.reason and e.reason.startswith("paid_tier_available")
    ]
    assert approvals and approvals[0].reason == "paid_tier_available:T4_PROXIED"


async def test_challenge_is_handed_to_the_browser_when_the_run_renders(worker_env, tmp_path: Path):
    ledger, worker = worker_env
    worker = FetchWorker(
        ledger=ledger,
        fetcher=worker._fetcher,
        evidence=LocalEvidenceStore(tmp_path, "t1"),
        scope=_scope(),
        run_id="run-t",
        worker_id="w1",
        browser_handoff=True,
    )
    res = await _run(ledger, worker, "http://testserver/challenge-200")
    assert res.status is EntityStatus.BLOCKED
    assert res.status_reason.endswith(":handed_to_browser")
    assert res.acquired_at_tier is None
    assert [t.kind for t in ledger.followups] == [TaskKind.RENDER]
    assert ledger.hosts[HOST].tier is AccessTier.T1_POLITE_HTTP
    # A host that already stands at T3 still hands a T1-blocked page to the browser
    # instead of asking for a paid tier the browser has not earned yet.
    ledger.hosts[HOST].tier = AccessTier.T3_FIDELITY_BROWSER
    res = await _run(ledger, worker, "http://testserver/challenge-200?page=2")
    assert res.status_reason.endswith(":handed_to_browser")
    assert not any((e.reason or "").startswith("paid_tier_available") for e in ledger.events)


async def test_rate_limit_requeues_with_retry_after(worker_env):
    ledger, worker = worker_env
    task = ledger.add("http://testserver/rate-limited")
    await worker.run_until_idle(idle_polls=1, poll_s=0, max_tasks=1, max_wait_s=0)
    t = ledger.tasks[task.id or 0]
    assert t.state is TaskState.PENDING and t.last_error == "rate_limited"
    assert t.not_before is not None and (t.not_before - datetime.now(UTC)).total_seconds() > 5
    assert ledger.retry_after[HOST] is not None
    assert ledger.resources[task.payload["schedule_key"]].status is EntityStatus.RETRY_SCHEDULED


async def test_pay_per_crawl_is_excluded(worker_env):
    ledger, worker = worker_env
    res = await _run(ledger, worker, "http://testserver/pay-per-crawl")
    assert res.status is EntityStatus.EXCLUDED_BY_POLICY
    assert res.status_reason.startswith("pay_per_crawl")


async def test_soft_404_is_flagged_for_review_but_its_text_is_kept(worker_env):
    ledger, worker = worker_env
    res = await _run(ledger, worker, "http://testserver/soft-404")
    assert res.status is EntityStatus.EXTRACTED_UNVERIFIED
    assert res.status_reason == "soft_404_suspected;source_extracted"
    texts = [b.normalized_text for b in ledger.blocks[res.resource_id]]
    assert any("could not be found" in t for t in texts)
    gaps = ledger.get_gaps(res.resource_id)
    assert [g.gap_type.value for g in gaps] == ["SOFT_404_SUSPECTED"]
    assert getattr(ledger, "followups", []) == []  # no render, no follow-ups
    assert ledger.hosts[HOST].consecutive_failures == 0


async def test_hard_404_ends_with_a_gap_and_no_content(worker_env):
    ledger, worker = worker_env
    res = await _run(ledger, worker, "http://testserver/pages/does_not_exist")
    assert res.http_status == 404
    assert res.status is EntityStatus.EXTRACTED_UNVERIFIED
    assert res.status_reason == "not_found:http_404;soft_404_suspected"
    assert res.block_verdict is not None and res.block_verdict.kind.value == "SOFT_404"
    assert res.resource_id not in ledger.blocks
    assert [g.gap_type.value for g in ledger.get_gaps(res.resource_id)] == ["SOFT_404_SUSPECTED"]
    assert getattr(ledger, "followups", []) == []
    assert ledger.hosts[HOST].consecutive_failures == 0
    assert ledger.hosts[HOST].circuit.value == "CLOSED"


async def test_login_redirect_is_blocked_with_typed_chain(worker_env):
    ledger, worker = worker_env
    res = await _run(ledger, worker, "http://testserver/gated")
    assert res.status is EntityStatus.BLOCKED
    assert res.status_reason.startswith("auth_wall")
    assert [h.kind.value for h in res.redirect_chain] == ["http"]
    assert res.final_url == "http://testserver/login?next=/gated"


async def test_transport_failure_retries_then_fails(tmp_path: Path):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    ledger = InMemoryLedger()
    fetcher = HttpFetcher(
        host_checker=lambda _h: (True, "t"), user_agent="t", transport=httpx.MockTransport(handler)
    )
    worker = FetchWorker(
        ledger=ledger,
        fetcher=fetcher,
        evidence=LocalEvidenceStore(tmp_path, "t1"),
        scope=_scope(),
        run_id="run-t",
        worker_id="w1",
    )
    task = ledger.add("http://testserver/x")
    try:
        await worker.run_until_idle(idle_polls=1, poll_s=0.01, max_wait_s=30)
    finally:
        await fetcher.aclose()
    t = ledger.tasks[task.id or 0]
    assert t.state is TaskState.FAILED and t.attempts == 2
    res = ledger.resources["http://testserver/x"]
    assert res.status is EntityStatus.FAILED and res.status_reason == "robots_unreachable"
    # Unreachable robots is transient (RFC 9309): retried, never cached, never EXCLUDED.
    assert HOST not in ledger.robots
    assert ledger.hosts[HOST].consecutive_failures == 2


def test_user_agent_identifies_on_authorized_sites():
    assert user_agent_for(_scope()).startswith("Mozilla/5.0")
    assert user_agent_for(_scope(AuthorizationKind.OWNER_AUTHORIZED)).startswith(
        "RankunoContentBot"
    )
