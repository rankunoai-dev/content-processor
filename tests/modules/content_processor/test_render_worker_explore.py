"""Render worker + exploration verdicts with a fake renderer and a fake explorer (no browser)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.integrations.browser_client import RenderResult
from src.integrations.evidence_store import LocalEvidenceStore
from src.modules.content_processor.contracts import (
    Authorization,
    InteractionPolicy,
    Resource,
    RunScope,
    Task,
)
from src.modules.content_processor.enums import (
    AuthorizationKind,
    EntityStatus,
    GapType,
    PriorityClass,
    TaskKind,
)
from src.modules.content_processor.explore import ControlRecord, ExplorationResult, ExploredState
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.workers.render_worker import RenderWorker

HOST = "testserver"
BASE_HTML = (
    b"<html><body><main><h1>Plans</h1><div role='tablist'>"
    b"<button role='tab' aria-selected='true'>Monthly</button>"
    b"<button role='tab' aria-selected='false'>Annual</button></div>"
    b"<div><p>Monthly plan costs $12.</p></div>"
    b"<div hidden data-cp-hidden='1'><p>Annual plan costs $120.</p></div>"
    b"<details><summary>Refunds?</summary><p>Refunds within 14 days.</p></details>"
    b"</main></body></html>"
)
BASE_TEXT = "Plans\nMonthly\nAnnual\nMonthly plan costs $12.\nRefunds?"
STATE_HTML = (
    b"<html><body><main><h1>Plans</h1><div role='tablist'>"
    b"<button role='tab' aria-selected='false'>Monthly</button>"
    b"<button role='tab' aria-selected='true'>Annual</button></div>"
    b"<div hidden data-cp-hidden='1'><p>Monthly plan costs $12.</p></div>"
    b"<div><p>Annual plan costs $120.</p></div>"
    b"<details open><summary>Refunds?</summary><p>Refunds within 14 days.</p></details>"
    b"</main></body></html>"
)
STATE_TEXT = "Plans\nMonthly\nAnnual\nAnnual plan costs $120.\nRefunds?\nRefunds within 14 days."


class FakeRenderer:
    tier_name = "T3_FIDELITY_BROWSER"

    async def render(self, url: str, on_page=None) -> RenderResult:
        if on_page is not None:
            await on_page(None)
        return RenderResult(
            requested_url=url,
            final_url=url,
            http_status=200,
            headers={"content-type": "text/html"},
            dom_html=BASE_HTML,
            inner_text=BASE_TEXT,
            screenshot_jpeg=b"\xff\xd8\xff\xd9",
            settle={"stable": True},
        )


class FakeExplorer:
    def __init__(self, result: ExplorationResult) -> None:
        self.result = result

    async def explore(self, page: Any) -> ExplorationResult:
        return self.result


def _scope(allowed: list[str]) -> RunScope:
    return RunScope(
        tenant_id="t1",
        input_urls=["http://testserver/plans"],
        hosts_in_scope=[HOST],
        authorization=Authorization(
            kind=AuthorizationKind.PUBLIC_THIRD_PARTY,
            reference="public",
            approved_by="lead",
            approved_at=datetime(2026, 10, 16, tzinfo=UTC),
            hosts=[HOST],
        ),
        interaction_policy=InteractionPolicy(allowed=allowed),  # type: ignore[arg-type]
        egress_allow_private=True,
    )


def _seed(ledger: MemoryLedger) -> Resource:
    res = Resource(
        resource_id="res-plans",
        run_id="run-t",
        requested_url="http://testserver/plans",
        final_url="http://testserver/plans",
        schedule_key="http://testserver/plans",
        status=EntityStatus.EXTRACTED_UNVERIFIED,
    )
    ledger.upsert_resource(res, HOST)
    ledger.enqueue(
        [
            Task(
                run_id="run-t",
                kind=TaskKind.RENDER,
                idempotency_key="render-plans",
                host=HOST,
                priority=PriorityClass.HTML_PAGE,
                payload={
                    "resource_id": res.resource_id,
                    "requested_url": res.final_url,
                    "schedule_key": res.schedule_key,
                },
            )
        ]
    )
    return res


def _state() -> ExploredState:
    return ExploredState(
        index=1,
        control_id="1",
        kind="tab",
        label="Annual",
        inner_text=STATE_TEXT,
        dom_html=STATE_HTML,
        text_delta_chars=len(STATE_TEXT) - len(BASE_TEXT),
    )


async def _run(tmp_path: Path, exploration: ExplorationResult, allowed: list[str]):
    ledger = MemoryLedger(base_delay_s=0)
    res = _seed(ledger)
    worker = RenderWorker(
        ledger=ledger,
        renderer=FakeRenderer(),
        evidence=LocalEvidenceStore(tmp_path, "t1"),
        scope=_scope(allowed),
        run_id="run-t",
        worker_id="r1",
        explorer_factory=lambda _policy: FakeExplorer(exploration),
    )
    await worker.run_until_idle(idle_polls=1, poll_s=0, max_wait_s=0)
    updated = ledger.get_resource(res.resource_id)
    assert updated is not None
    return ledger, updated


async def test_closed_exploration_verifies_and_keeps_only_new_text(tmp_path: Path):
    exploration = ExplorationResult(
        controls=[
            ControlRecord(control_id="0", kind="tab", label="Monthly", status="initially_open"),
            ControlRecord(
                control_id="1", kind="tab", label="Annual", status="visited", state_index=1
            ),
            ControlRecord(control_id="2", kind="disclosure", label="Refunds?", status="visited"),
        ],
        states=[_state()],
        actions_used=2,
        action_budget=50,
    )
    ledger, res = await _run(tmp_path, exploration, ["expand_disclosure", "select_tab"])
    assert res.status is EntityStatus.VERIFIED_EXTRACTED, res.status_reason
    reason = res.status_reason or ""
    assert "discovery_closed_within_scope" in reason
    assert "collapsed_revealed=1" in reason
    state_blocks = [b for b in ledger.get_blocks(res.resource_id) if "|state:" in b.state_id]
    texts = {b.normalized_text for b in state_blocks}
    assert texts == {"Annual plan costs $120.", "Refunds within 14 days."}
    assert all(b.status is EntityStatus.VERIFIED_EXTRACTED for b in state_blocks)
    assert all(b.block_id.startswith("res-plans:state1:") for b in state_blocks)
    facts = ledger.summaries[res.resource_id]["explore"]
    assert facts["states"] == 1 and facts["new_blocks_from_states"] == 2
    assert ledger.get_gaps(res.resource_id) == []


async def test_unvisited_tab_makes_resource_partial_with_gap(tmp_path: Path):
    exploration = ExplorationResult(
        controls=[
            ControlRecord(control_id="0", kind="tab", label="Monthly", status="initially_open"),
            ControlRecord(
                control_id="1",
                kind="tab",
                label="Annual",
                status="unvisited",
                reason="action_budget_exhausted",
            ),
        ],
        actions_used=0,
        action_budget=0,
        stop_reason="action_budget_exhausted",
    )
    ledger, res = await _run(tmp_path, exploration, ["expand_disclosure", "select_tab"])
    assert res.status is EntityStatus.PARTIAL
    assert "discovery_open:unvisited_controls=1" in (res.status_reason or "")
    gaps = ledger.get_gaps(res.resource_id)
    assert len(gaps) == 1
    assert gaps[0].gap_type is GapType.UNVISITED_STATE
    assert gaps[0].reason == "unvisited_tab:action_budget_exhausted:Annual"
    assert gaps[0].retry_eligible is True
    assert gaps[0].evidence_ids, "the screenshot and DOM are linked as proof"


async def test_unimplemented_scroll_keeps_discovery_open_without_downgrade(tmp_path: Path):
    exploration = ExplorationResult(
        controls=[],
        unexplored_action_kinds=["scroll"],
    )
    ledger, res = await _run(tmp_path, exploration, ["scroll", "expand_disclosure", "select_tab"])
    reason = res.status_reason or ""
    assert "discovery_closed_for_controls" in reason
    assert "discovery_open:not_implemented=scroll" in reason
    assert "collapsed_unrevealed=1" in reason
    assert res.status is EntityStatus.EXTRACTED_UNVERIFIED


async def test_still_collapsed_text_in_a_state_is_not_kept_as_new(tmp_path: Path):
    """Regression: a state where a region is still collapsed must not claim its text."""
    first = _state().model_copy(
        update={
            "dom_html": STATE_HTML.replace(b"<details open>", b"<details>"),
            "inner_text": STATE_TEXT.replace("\nRefunds within 14 days.", ""),
        }
    )
    second = _state().model_copy(update={"index": 2, "control_id": "2", "kind": "disclosure"})
    exploration = ExplorationResult(states=[first, second], actions_used=2, action_budget=50)
    ledger, res = await _run(tmp_path, exploration, ["expand_disclosure", "select_tab"])
    assert res.status is EntityStatus.VERIFIED_EXTRACTED, res.status_reason
    by_text = {
        b.normalized_text: b.state_id
        for b in ledger.get_blocks(res.resource_id)
        if "|state:" in b.state_id
    }
    assert by_text["Refunds within 14 days."] == "run-t|state:2"


async def test_explorer_error_is_reported_not_raised(tmp_path: Path):
    exploration = ExplorationResult(error="PlaywrightError:Target closed")
    _, res = await _run(tmp_path, exploration, ["expand_disclosure"])
    assert "discovery_open:explore_error" in (res.status_reason or "")
