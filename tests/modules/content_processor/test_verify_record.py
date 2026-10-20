"""UrlRecord assembly (V-1): discovery, inventory, badge from gaps, run-level records."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.integrations.evidence_store import LocalEvidenceStore
from src.modules.content_processor.contracts import (
    ContentBlock,
    Gap,
    Resource,
    RunEvent,
)
from src.modules.content_processor.enums import (
    AccessTier,
    ContentLayer,
    EntityStatus,
    GapType,
    PriorityClass,
    UrlBadge,
)
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.scope.frontier import (
    ExcludedInput,
    FrontierEntry,
    FrontierPlan,
)
from src.modules.content_processor.verify.record import (
    COUNTERFACTUAL_WARNING,
    VERIFY_VERSION,
    assemble_url_record,
    corpus_blocks,
    derive_discovery,
    excluded_url_record,
    frames_from_evidence,
    url_records_for_run,
)
from src.modules.content_processor.verify.rules import FrameRef

S = EntityStatus
RUN = "run-v"


def _res(**kw: Any) -> Resource:
    base: dict[str, Any] = {
        "resource_id": "res-1",
        "run_id": RUN,
        "requested_url": "http://h/page",
        "final_url": "http://h/page",
        "schedule_key": "http://h/page",
        "sniffed_mime": "text/html",
        "payload_sha256": "abc",
        "acquired_at_tier": AccessTier.T3_FIDELITY_BROWSER,
        "status": S.VERIFIED_EXTRACTED,
        "status_reason": "rendered;all_rendered_blocks_verified;discovery_closed_within_scope",
    }
    base.update(kw)
    return Resource(**base)


def _block(
    n: int,
    status: EntityStatus = S.VERIFIED_EXTRACTED,
    *,
    state: str = "run|rendered",
    layer: ContentLayer = ContentLayer.RENDERED_VISIBLE,
) -> ContentBlock:
    return ContentBlock(
        block_id=f"b{n}",
        block_key=f"k{n}",
        resource_id="res-1",
        state_id=state,
        region_id=f"r{n}",
        reading_order=n,
        layer=layer,
        role="main",
        raw_text=f"text {n}",
        normalized_text=f"text {n}",
        method="m",
        tool_version="1",
        confidence=1.0,
        status=status,
        evidence_ids=["e"],
    )


def _gap(gap_type: GapType, status: EntityStatus, resource_id: str = "res-1") -> Gap:
    return Gap(
        gap_id=f"g-{gap_type.value}-{resource_id}",
        run_id=RUN,
        resource_id=resource_id,
        gap_type=gap_type,
        status=status,
        reason=gap_type.value.lower(),
    )


def _record(**kw: Any):
    base: dict[str, Any] = {
        "run_id": RUN,
        "target": _res(),
        "recorded_gaps": [],
        "blocks": [_block(1), _block(2)],
        "summaries": {"render": {}},
        "frames": [],
    }
    base.update(kw)
    return assemble_url_record(**base)


# -- discovery -------------------------------------------------------------------


def test_discovery_closed_only_on_the_explicit_token():
    state = derive_discovery(_res(), {"explore": {"x": {"stop_reason": "all_visited"}}}, [])
    assert state.closed_within_scope and state.stop_reason == "all_visited"
    assert not state.unknown_state_space


def test_discovery_reopened_by_unvisited_state_gap_or_open_token():
    gap = _gap(GapType.UNVISITED_STATE, S.PARTIAL)
    reopened = derive_discovery(_res(), {"render": {}}, [gap])
    assert not reopened.closed_within_scope and reopened.remaining_controls == 1
    contradicted = _res(
        status_reason="discovery_closed_within_scope;discovery_open:not_implemented"
    )
    assert derive_discovery(contradicted, {"render": {}}, []).stop_reason == "not_implemented"


def test_discovery_stop_reasons():
    assert derive_discovery(_res(status=S.BLOCKED), {}, []).stop_reason == "not_acquired"
    assert derive_discovery(_res(status_reason="source_extracted"), {}, []).stop_reason == (
        "not_rendered"
    )
    bare = _res(status_reason="rendered;discovery_open")
    assert derive_discovery(bare, {"render": {}}, []).stop_reason == "not_explored"
    counted = _res(status_reason="rendered;discovery_open:unvisited_controls=2")
    assert derive_discovery(counted, {"render": {}}, []).stop_reason == "unvisited_controls=2"


# -- corpus ----------------------------------------------------------------------


def test_corpus_prefers_rendered_channel_and_drops_non_corpus_layers():
    source = _block(1, S.EXTRACTED_UNVERIFIED, state="run|source")
    rendered = _block(2)
    hidden = _block(3, layer=ContentLayer.HIDDEN)
    assert corpus_blocks([source, rendered, hidden]) == [rendered]
    assert corpus_blocks([source, hidden]) == [source]


# -- assembly ---------------------------------------------------------------------


def test_clean_closed_page_is_verified_with_explicit_denominators():
    rec = _record(tool_versions={"engine": "0.1.0"})
    assert rec.badge is UrlBadge.VERIFIED_WITHIN_SCOPE
    assert rec.gaps == [] and rec.warnings == [
        "frames_from_source_dom_only",
        "r6:detector_unavailable",
    ]
    inv = rec.inventory
    assert inv.denominator_basis == "observed_inventory_only"
    assert (inv.resources_discovered, inv.resources_acquired) == (1, 1)
    assert (inv.regions_discovered, inv.regions_verified, inv.regions_needs_review) == (2, 2, 0)
    assert (
        rec.status_vector.discovery_closed and rec.status_vector.acquisition is S.VERIFIED_EXTRACTED
    )
    assert rec.tool_versions == {"engine": "0.1.0", "verify": VERIFY_VERSION}
    assert rec.tier_reached is AccessTier.T3_FIDELITY_BROWSER


def test_open_discovery_is_partial_with_r1_gap():
    rec = _record(target=_res(status_reason="rendered;discovery_open"))
    assert rec.badge is UrlBadge.PARTIAL
    assert [g.gap_type for g in rec.gaps] == [GapType.DISCOVERY_OPEN]
    assert "discovery_stop:not_explored" in rec.status_vector.reasons
    assert rec.status_vector.state_exploration is S.PARTIAL


def test_counterfactual_closes_discovery_and_says_so():
    rec = _record(
        target=_res(status_reason="rendered;discovery_open"), assume_discovery_closed=True
    )
    assert rec.badge is UrlBadge.VERIFIED_WITHIN_SCOPE
    assert COUNTERFACTUAL_WARNING in rec.warnings
    assert rec.discovery.stop_reason == COUNTERFACTUAL_WARNING


def test_k14_fetch_time_gate_lowers_badge_without_render():
    target = _res(status=S.EXTRACTED_UNVERIFIED, status_reason="source_extracted;gate:age")
    gate = _gap(GapType.ACCESS_BLOCKED, S.BLOCKED)
    source = [_block(1, S.EXTRACTED_UNVERIFIED, state="run|source")]
    rec = _record(target=target, recorded_gaps=[gate], blocks=source, summaries={})
    assert rec.badge is UrlBadge.BLOCKED
    assert "render_not_run:source_channel_only" in rec.warnings
    assert "status_lowered_by_gaps:EXTRACTED_UNVERIFIED->BLOCKED" in rec.warnings
    assert rec.status_vector.acquisition is S.BLOCKED


def test_unverified_region_is_needs_review():
    rec = _record(blocks=[_block(1), _block(2, S.EXTRACTED_UNVERIFIED)])
    assert rec.badge is UrlBadge.NEEDS_REVIEW
    assert rec.inventory.regions_needs_review == 1
    assert rec.status_vector.fidelity is S.EXTRACTED_UNVERIFIED


def test_unvisited_gap_counts_toward_region_denominator():
    rec = _record(
        target=_res(status_reason="rendered;discovery_open:unvisited_controls=1"),
        recorded_gaps=[_gap(GapType.UNVISITED_STATE, S.PARTIAL)],
    )
    assert rec.badge is UrlBadge.PARTIAL
    assert (rec.inventory.regions_discovered, rec.inventory.regions_unvisited) == (3, 1)


def test_children_enter_denominator_and_badge():
    blocked = _res(resource_id="c1", requested_url="http://h/a.png", status=S.BLOCKED)
    rec = _record(children=[blocked])
    assert rec.badge is UrlBadge.PARTIAL
    assert (rec.inventory.resources_discovered, rec.inventory.resources_blocked) == (2, 1)
    assert rec.status_vector.acquisition is S.BLOCKED
    in_flight = _res(resource_id="c2", requested_url="http://h/b.png", status=S.QUEUED)
    assert _record(children=[in_flight]).badge is UrlBadge.RUNNING


def test_outstanding_work_and_non_terminal_target_are_running():
    assert _record(work_outstanding=True).badge is UrlBadge.RUNNING
    rec = _record(target=_res(status=S.RETRY_SCHEDULED, payload_sha256=None), blocks=[])
    assert rec.badge is UrlBadge.RUNNING


def test_blocked_page_has_no_discovery_noise():
    rec = _record(target=_res(status=S.BLOCKED, status_reason="js_challenge:cf"), blocks=[])
    assert rec.badge is UrlBadge.BLOCKED
    assert [g.gap_type for g in rec.gaps] == [GapType.ACCESS_BLOCKED]
    assert rec.status_vector.fidelity is S.BLOCKED


def test_missing_frame_inventory_blocks_verified():
    rec = _record(frames=None)
    assert rec.badge is UrlBadge.NEEDS_REVIEW
    assert [g.reason for g in rec.gaps] == ["frame_inventory_unavailable"]


def test_hidden_frames_are_noted():
    rec = _record(frames=[FrameRef(url="http://h/px", hidden=True)])
    assert rec.badge is UrlBadge.VERIFIED_WITHIN_SCOPE
    assert "hidden_frames_skipped=1" in rec.warnings


def test_sticky_wrong_payload_overrides_a_later_verified_status():
    event = RunEvent(
        run_id=RUN,
        entity_type="resource",
        entity_id="res-1",
        old_state="DISCOVERED",
        new_state="BLOCKED",
        reason="wrong_payload:generic:expected=application/pdf:from=link_type",
    )
    rec = _record(history=[event])
    assert rec.badge is UrlBadge.BLOCKED
    assert rec.inventory.inventory_revision == 1


def test_excluded_input_record_keeps_the_denominator():
    rec = excluded_url_record(RUN, ExcludedInput(requested_url="ftp://x", reason="bad_scheme"))
    assert rec.badge is UrlBadge.EXCLUDED
    assert rec.inventory.resources_discovered == 1
    assert [(g.gap_type, g.reason) for g in rec.gaps] == [(GapType.POLICY_EXCLUDED, "bad_scheme")]


# -- frames from evidence -----------------------------------------------------------


def test_frames_from_evidence(tmp_path: Path):
    store = LocalEvidenceStore(tmp_path, "t1")
    html = (
        b"<html><body><main><p>x</p><iframe src='/embed'></iframe>"
        b"<div style='display:none'><iframe src='/px'></iframe></div></main></body></html>"
    )
    ref = store.put(html, "text/html")
    read = frames_from_evidence(store)
    frames = read(_res(payload_sha256=ref.sha256))
    assert frames is not None
    assert [(f.url, f.hidden) for f in frames] == [("http://h/embed", False), ("http://h/px", True)]
    assert read(_res(payload_sha256="0" * 64)) is None
    assert read(_res(payload_sha256=ref.sha256, sniffed_mime="application/pdf")) is None
    assert read(_res(payload_sha256=None)) is None


# -- run level ---------------------------------------------------------------------


def test_url_records_for_run_covers_every_requested_url():
    ledger = MemoryLedger()
    done = _res(status=S.EXTRACTED_UNVERIFIED, status_reason="source_extracted")
    ledger.upsert_resource(done, "h")
    ledger.upsert_blocks(done.resource_id, [_block(1, S.EXTRACTED_UNVERIFIED, state="r|source")])
    plan = FrontierPlan(
        entries=[
            FrontierEntry(
                schedule_key="http://h/page",
                requested_url="http://h/page",
                host="h",
                mandatory=False,
                priority=PriorityClass.HTML_PAGE,
            ),
            FrontierEntry(
                schedule_key="http://h/later",
                requested_url="http://h/later",
                aliases=["http://h/later?utm=1"],
                host="h",
                mandatory=False,
                priority=PriorityClass.HTML_PAGE,
            ),
        ],
        excluded=[ExcludedInput(requested_url="http://other/x", reason="out_of_scope_host")],
        hosts={"h": 2},
        egress={},
    )
    records = url_records_for_run(
        ledger,
        run_id=RUN,
        plan=plan,
        frames_for=lambda _r: [],
        expectations={"http://h/page": "application/pdf"},
    )
    by_url = {r.requested_url: r for r in records}
    assert set(by_url) == {"http://h/page", "http://h/later", "http://other/x"}
    assert by_url["http://h/page"].badge is UrlBadge.BLOCKED  # expected a PDF, got HTML
    assert by_url["http://h/later"].badge is UrlBadge.RUNNING
    assert by_url["http://h/later"].aliases == ["http://h/later?utm=1"]
    assert by_url["http://other/x"].badge is UrlBadge.EXCLUDED


def test_url_records_without_plan_reports_ledger_resources_only():
    ledger = MemoryLedger()
    ledger.upsert_resource(_res(), "h")
    records = url_records_for_run(ledger, run_id=RUN, outstanding={"res-1"})
    assert [r.badge for r in records] == [UrlBadge.RUNNING]


def test_static_documents_have_nothing_to_explore():
    image = _res(
        sniffed_mime="image/png", status=S.EXTRACTED_UNVERIFIED, status_reason="image_extracted"
    )
    state = derive_discovery(image, {}, [])
    assert state.closed_within_scope and state.stop_reason == "static_document"
    pdf = _res(sniffed_mime="application/pdf", status_reason="pdf_extracted")
    rec = _record(target=pdf, summaries={}, blocks=[_block(1)])
    assert rec.badge is UrlBadge.VERIFIED_WITHIN_SCOPE
    assert not any(g.gap_type is GapType.DISCOVERY_OPEN for g in rec.gaps)
    html = derive_discovery(_res(status_reason="source_extracted"), {}, [])
    assert not html.closed_within_scope and html.stop_reason == "not_rendered"
