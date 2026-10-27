"""Render-worker loop tests with a fake renderer (no browser)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.integrations.browser_client import RenderResult
from src.integrations.evidence_store import LocalEvidenceStore
from src.modules.content_processor.adapters.html_source import extract_html_source
from src.modules.content_processor.contracts import Authorization, Resource, RunScope, Task
from src.modules.content_processor.enums import (
    AccessTier,
    AuthorizationKind,
    EntityStatus,
    PriorityClass,
    TaskKind,
    TaskState,
)
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.workers.render_worker import (
    RENDER_MAX_ATTEMPTS,
    RenderWorker,
)

HOST = "testserver"
SOURCE_HTML = (
    b"<html><head><title>App</title></head><body><div id='root'>"
    b"<p>Loading...</p></div><script></script></body></html>"
)
RENDERED_HTML = (
    b"<html><head><title>App</title></head><body><div id='root'><main>"
    b"<p>Hydrated content arrived.</p>"
    b"<p data-cp-hidden='1'>hidden trap</p></main></div></body></html>"
)


class FakeRenderer:
    tier_name = "T3_FIDELITY_BROWSER"

    def __init__(self, result: RenderResult) -> None:
        self.result = result
        self.calls: list[str] = []
        self.run_hook = False

    async def render(self, url: str, on_page=None) -> RenderResult:
        self.calls.append(url)
        if on_page is not None and self.run_hook:
            await on_page(None)
        return self.result


def _scope() -> RunScope:
    return RunScope(
        tenant_id="t1",
        input_urls=["http://testserver/app"],
        hosts_in_scope=[HOST],
        authorization=Authorization(
            kind=AuthorizationKind.PUBLIC_THIRD_PARTY,
            reference="public",
            approved_by="lead",
            approved_at=datetime(2026, 10, 15, tzinfo=UTC),
            hosts=[HOST],
        ),
        egress_allow_private=True,
    )


def _seed(ledger: MemoryLedger) -> Resource:
    res = Resource(
        resource_id="res-app",
        run_id="run-t",
        requested_url="http://testserver/app",
        final_url="http://testserver/app",
        schedule_key="http://testserver/app",
        status=EntityStatus.EXTRACTED_UNVERIFIED,
        status_reason="source_extracted;needs_render",
        acquired_at_tier=AccessTier.T1_POLITE_HTTP,
    )
    ledger.upsert_resource(res, HOST)
    src = extract_html_source(
        SOURCE_HTML,
        base_url=res.final_url or "",
        evidence_id="e-src",
        resource_id=res.resource_id,
        state_id="run-t|source",
    )
    ledger.upsert_blocks(res.resource_id, src.blocks)
    ledger.enqueue(
        [
            Task(
                run_id="run-t",
                kind=TaskKind.RENDER,
                idempotency_key="render-1",
                host=HOST,
                priority=PriorityClass.HTML_PAGE,
                max_attempts=2,
                payload={
                    "resource_id": res.resource_id,
                    "requested_url": res.final_url,
                    "schedule_key": res.schedule_key,
                },
            )
        ]
    )
    return res


def _ok_result() -> RenderResult:
    return RenderResult(
        requested_url="http://testserver/app",
        final_url="http://testserver/app",
        http_status=200,
        headers={"content-type": "text/html"},
        dom_html=RENDERED_HTML,
        inner_text="Hydrated content arrived.",
        screenshot_jpeg=b"\xff\xd8\xff\xd9",
        settle={"stable": True, "polls": 4},
        annotation={"total": 5, "hidden": 1, "icons": 0},
    )


async def test_render_verifies_hydrated_content(tmp_path: Path):
    ledger = MemoryLedger(base_delay_s=0)
    res = _seed(ledger)
    renderer = FakeRenderer(_ok_result())
    worker = RenderWorker(
        ledger=ledger,
        renderer=renderer,
        evidence=LocalEvidenceStore(tmp_path, "t1"),
        scope=_scope(),
        run_id="run-t",
        worker_id="r1",
    )
    assert await worker.run_until_idle(idle_polls=1, poll_s=0, max_wait_s=0) == 1
    assert renderer.calls == ["http://testserver/app"]
    updated = ledger.get_resource(res.resource_id)
    assert updated is not None and updated.status is EntityStatus.VERIFIED_EXTRACTED
    assert "all_rendered_blocks_verified" in (updated.status_reason or "")
    assert "hydrated_blocks=1" in (updated.status_reason or "")
    assert "source_only_not_rendered=1" in (updated.status_reason or "")
    assert "discovery_open" in (updated.status_reason or "")
    blocks = ledger.get_blocks(res.resource_id)
    states = {b.state_id for b in blocks}
    assert states == {"run-t|source", "run-t|rendered"}
    hidden = [b for b in blocks if b.layer.value == "hidden"]
    assert hidden and hidden[0].normalized_text == "hidden trap"
    summary = ledger.summaries[res.resource_id]["render"]
    assert summary["verified_blocks"] >= 1 and summary["hidden_blocks"] == 1
    assert ledger.task_counts() == {"DONE": 1}
    # evidence: DOM, visible text and screenshot were stored
    store = LocalEvidenceStore(tmp_path, "t1")
    assert all(store.exists(e.split(":", 1)[1]) for e in ledger.events[-1].evidence_ids)


async def test_cookie_and_chat_widgets_on_the_screenshot_are_kept_as_third_party(
    tmp_path: Path,
):
    """Widgets that load after the DOM is saved: their text backs R6, never the content."""
    ledger = MemoryLedger(base_delay_s=0)
    res = _seed(ledger)
    result = _ok_result().model_copy(
        update={
            "overlay_html": (
                '<html><body><div id="onetrust-banner-sdk"><p>We use cookies to improve your '
                "experience and personalize content.</p></div>"
                '<div class="jh-webchat-tooltip"><p>Hi, I am the virtual assistant. Need help?'
                "</p></div></body></html>"
            )
        }
    )
    worker = RenderWorker(
        ledger=ledger,
        renderer=FakeRenderer(result),
        evidence=LocalEvidenceStore(tmp_path, "t1"),
        scope=_scope(),
        run_id="run-t",
        worker_id="r1",
    )
    await worker.run_until_idle(idle_polls=1, poll_s=0, max_wait_s=0)
    overlay = [b for b in ledger.get_blocks(res.resource_id) if b.state_id == "run-t|overlay"]
    assert {b.layer.value for b in overlay} == {"third_party_chrome"}
    texts = " ".join(b.normalized_text for b in overlay)
    assert "personalize content" in texts and "Need help?" in texts
    store = LocalEvidenceStore(tmp_path, "t1")
    assert all(store.exists(e.split(":", 1)[1]) for b in overlay for e in b.evidence_ids)
    updated = ledger.get_resource(res.resource_id)
    assert updated is not None and updated.status is EntityStatus.VERIFIED_EXTRACTED


async def test_render_block_at_t3_asks_for_paid_tier(tmp_path: Path):
    ledger = MemoryLedger(base_delay_s=0)
    res = _seed(ledger)
    state = ledger.load_host_state("t1", HOST)
    state.tier = AccessTier.T3_FIDELITY_BROWSER
    ledger.save_host_state("t1", state)
    challenge = _ok_result().model_copy(
        update={
            "headers": {"content-type": "text/html", "cf-mitigated": "challenge"},
            "dom_html": (
                b"<html><head><title>Just a moment...</title></head><body>"
                b"<div id='challenge-platform'>Checking your browser</div></body></html>"
            ),
            "inner_text": "Checking your browser",
        }
    )
    worker = RenderWorker(
        ledger=ledger,
        renderer=FakeRenderer(challenge),
        evidence=LocalEvidenceStore(tmp_path, "t1"),
        scope=_scope(),
        run_id="run-t",
        worker_id="r1",
    )
    await worker.run_until_idle(idle_polls=1, poll_s=0, max_wait_s=0)
    updated = ledger.get_resource(res.resource_id)
    assert updated is not None and updated.status is EntityStatus.BLOCKED
    assert updated.status_reason is not None and updated.status_reason.startswith(
        "js_challenge:cloudflare:fetched_at=T3"
    )
    assert any(e.reason == "paid_tier_available:T4_PROXIED" for e in ledger.events)
    assert ledger.hosts[HOST].tier is AccessTier.T3_FIDELITY_BROWSER


async def test_render_failure_retries_then_partial(tmp_path: Path):
    ledger = MemoryLedger(base_delay_s=0)
    res = _seed(ledger)
    failing = RenderResult(
        requested_url="http://testserver/app",
        final_url="http://testserver/app",
        transport_error="render_timeout",
    )
    worker = RenderWorker(
        ledger=ledger,
        renderer=FakeRenderer(failing),
        evidence=LocalEvidenceStore(tmp_path, "t1"),
        scope=_scope(),
        run_id="run-t",
        worker_id="r1",
        render_retry_delays_s=(),
    )
    await worker.run_until_idle(idle_polls=1, poll_s=0.01, max_wait_s=30)
    assert ledger.task_counts() == {"FAILED": 1}
    updated = ledger.get_resource(res.resource_id)
    assert updated is not None and updated.status is EntityStatus.PARTIAL
    assert updated.status_reason == "render_failed:render_timeout"


async def test_a_failed_render_is_retried_minutes_later_not_seconds(tmp_path: Path):
    """A slow spell lasts minutes: each retry waits longer (30 s, 90 s, 180 s)."""
    ledger = MemoryLedger(base_delay_s=0)
    _seed(ledger)
    stored = next(iter(ledger._tasks.values()))
    stored.max_attempts = RENDER_MAX_ATTEMPTS
    failing = RenderResult(
        requested_url="http://testserver/app",
        final_url="http://testserver/app",
        transport_error="PlaywrightError:Page.goto: Timeout 30000ms exceeded.",
    )
    worker = RenderWorker(
        ledger=ledger,
        renderer=FakeRenderer(failing),
        evidence=LocalEvidenceStore(tmp_path, "t1"),
        scope=_scope(),
        run_id="run-t",
        worker_id="r1",
    )
    waits = []
    for _ in range(RENDER_MAX_ATTEMPTS):
        task = ledger.lease("r1", [TaskKind.RENDER], run_id="run-t")
        assert task is not None
        before = datetime.now(UTC)
        await worker.process(task)
        stored = ledger._tasks[task.id or 0]
        if stored.state.value == "FAILED":
            break
        assert stored.not_before is not None
        waits.append(round((stored.not_before - before).total_seconds()))
        stored.not_before = None  # let the next try be leased now
    assert waits == [30, 90, 180]
    # the 4th try fails too: the page is left partial, never dropped
    assert ledger.task_counts() == {"FAILED": 1}


async def test_missing_resource_fails_terminally(tmp_path: Path):
    ledger = MemoryLedger(base_delay_s=0)
    ledger.enqueue(
        [
            Task(
                run_id="run-t",
                kind=TaskKind.RENDER,
                idempotency_key="x",
                host=HOST,
                priority=PriorityClass.HTML_PAGE,
                payload={
                    "resource_id": "nope",
                    "requested_url": "http://testserver/x",
                    "schedule_key": "k",
                },
            )
        ]
    )
    worker = RenderWorker(
        ledger=ledger,
        renderer=FakeRenderer(_ok_result()),
        evidence=LocalEvidenceStore(tmp_path, "t1"),
        scope=_scope(),
        run_id="run-t",
        worker_id="r1",
    )
    await worker.run_until_idle(idle_polls=1, poll_s=0, max_wait_s=0)
    assert list(ledger.task_counts()) == [TaskState.FAILED.value]


@pytest.mark.parametrize("seconds", [0.0])
def test_retry_at_helper(seconds: float):
    from src.modules.content_processor.workers.render_worker import retry_at

    assert retry_at(seconds) <= datetime.now(UTC)
