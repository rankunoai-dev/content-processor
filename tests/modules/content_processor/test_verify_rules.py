"""Unit tests for verification rules R1, R2, R3, R10 and the gap helpers."""

from __future__ import annotations

from typing import Any

import pytest

from src.modules.content_processor.contracts import (
    BlockVerdict,
    ContentBlock,
    DiscoveryState,
    Gap,
    Provenance,
    Resource,
    RunEvent,
)
from src.modules.content_processor.enums import (
    AccessTier,
    BlockKind,
    ContentLayer,
    EntityStatus,
    GapType,
)
from src.modules.content_processor.governor.gates import GateFinding
from src.modules.content_processor.verify.gaps import (
    TEASER_MIN_WORDS,
    gap_for_gate,
    gate_status,
    strictest_status,
)
from src.modules.content_processor.verify.rules import (
    FrameRef,
    expected_mime_for,
    rule_r1_inventory,
    rule_r2_acquisition,
    rule_r3_frames,
    rule_r10_payload,
)

S = EntityStatus
RUN = "run-r"
OPEN = DiscoveryState(
    closed_within_scope=False, remaining_controls=2, unknown_state_space=True, stop_reason="x"
)
CLOSED = DiscoveryState(closed_within_scope=True, remaining_controls=0, unknown_state_space=False)


def _res(**kw: Any) -> Resource:
    base: dict[str, Any] = {
        "resource_id": "res-1",
        "run_id": RUN,
        "requested_url": "http://h/page",
        "final_url": "http://h/page",
        "schedule_key": "http://h/page",
        "sniffed_mime": "text/html",
        "declared_mime": "text/html; charset=utf-8",
        "payload_sha256": "abc",
        "acquired_at_tier": AccessTier.T1_POLITE_HTTP,
        "status": S.VERIFIED_EXTRACTED,
        "status_reason": "rendered",
    }
    base.update(kw)
    return Resource(**base)


def _block(status: EntityStatus = S.VERIFIED_EXTRACTED, frame: str = "main") -> ContentBlock:
    return ContentBlock(
        block_id="b1",
        block_key="k1",
        resource_id="res-1",
        state_id="run|rendered",
        region_id="b1",
        reading_order=0,
        layer=ContentLayer.RENDERED_VISIBLE,
        raw_text="t",
        normalized_text="t",
        method="m",
        tool_version="1",
        confidence=1.0,
        status=status,
        evidence_ids=["e"],
        provenance=Provenance(frame_path=frame),
    )


def _types(gaps: list[Gap]) -> list[tuple[GapType, EntityStatus]]:
    return [(g.gap_type, g.status) for g in gaps]


# -- helpers -------------------------------------------------------------------


def test_strictest_status_only_lowers_terminal_outcomes():
    assert strictest_status(S.VERIFIED_EXTRACTED, [S.PARTIAL, S.EXTRACTED_UNVERIFIED]) is S.PARTIAL
    assert strictest_status(S.BLOCKED, [S.PARTIAL]) is S.BLOCKED
    assert strictest_status(S.EXTRACTED_UNVERIFIED, [S.BLOCKED]) is S.BLOCKED
    assert strictest_status(S.RETRY_SCHEDULED, [S.BLOCKED]) is S.RETRY_SCHEDULED
    assert strictest_status(S.EXCLUDED_BY_POLICY, [S.BLOCKED]) is S.EXCLUDED_BY_POLICY
    assert strictest_status(S.PARTIAL, [S.EXCLUDED_BY_POLICY]) is S.PARTIAL


def _lead_gen(severity: str, teaser: int | None) -> GateFinding:
    evidence = ["form:email_input"] + ([f"teaser_words={teaser}"] if teaser is not None else [])
    return GateFinding(
        kind="lead_gen_form_wall", severity=severity, evidence=evidence, main_word_count=20
    )


@pytest.mark.parametrize(
    ("severity", "teaser", "expected"),
    [
        ("blocks_main_content", None, S.BLOCKED),
        ("blocks_main_content", TEASER_MIN_WORDS - 1, S.BLOCKED),
        ("blocks_main_content", TEASER_MIN_WORDS, S.PARTIAL),
        ("partial", None, S.PARTIAL),
    ],
)
def test_lead_gen_wall_is_partial_only_when_a_teaser_is_kept(severity, teaser, expected):
    finding = _lead_gen(severity, teaser)
    assert gate_status(finding) is expected
    gap, implied = gap_for_gate(RUN, "res-1", finding, ["e"])
    assert implied is expected and gap.status is expected
    assert gap.gap_type is GapType.ACCESS_BLOCKED


def test_teaser_does_not_soften_other_gates():
    finding = GateFinding(
        kind="age_or_audience_gate",
        severity="blocks_main_content",
        evidence=["teaser_words=50"],
        main_word_count=5,
    )
    assert gate_status(finding) is S.BLOCKED


def test_expected_mime_strongest_evidence_first():
    assert expected_mime_for(_res(), "application/pdf") == "application/pdf"
    assert expected_mime_for(_res(content_disposition_name="a.docx")).endswith("document")
    assert expected_mime_for(_res(requested_url="http://h/f.PDF")) == "application/pdf"
    assert expected_mime_for(_res(final_url="http://h/x.png")) == "image/*"
    assert expected_mime_for(_res(requested_url="http://h/v1.2/page")) is None
    assert expected_mime_for(_res(content_disposition_name="noext")) is None


# -- R1 ------------------------------------------------------------------------


def test_r1_open_discovery_is_a_gap():
    gaps = rule_r1_inventory(RUN, _res(), discovery=OPEN)
    assert _types(gaps) == [(GapType.DISCOVERY_OPEN, S.PARTIAL)]
    assert gaps[0].reason.startswith("discovery_open:x") and gaps[0].retry_eligible


def test_r1_closed_and_terminal_has_no_gap():
    assert rule_r1_inventory(RUN, _res(), discovery=CLOSED, regions=[_block()]) == []


def test_r1_in_flight_children_and_regions_keep_inventory_open():
    child = _res(resource_id="c1", status=S.QUEUED)
    gaps = rule_r1_inventory(
        RUN, _res(), discovery=CLOSED, children=[child], regions=[_block(S.IN_PROGRESS)]
    )
    assert _types(gaps) == [(GapType.DISCOVERY_OPEN, S.PARTIAL)]
    assert gaps[0].reason == "non_terminal_entities:resources=1:regions=1"


def test_r1_skips_targets_without_content():
    assert rule_r1_inventory(RUN, _res(status=S.BLOCKED), discovery=OPEN) == []


def test_r1_gap_ids_are_stable():
    first = rule_r1_inventory(RUN, _res(), discovery=OPEN)
    again = rule_r1_inventory(RUN, _res(), discovery=OPEN)
    assert [g.gap_id for g in first] == [g.gap_id for g in again]


# -- R2 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "gap_type"),
    [
        (S.FAILED, GapType.ACQUISITION_FAILED),
        (S.UNSUPPORTED, GapType.UNSUPPORTED_FORMAT),
        (S.BUDGET_EXHAUSTED, GapType.BUDGET_STOPPED),
        (S.EXCLUDED_BY_POLICY, GapType.POLICY_EXCLUDED),
    ],
)
def test_r2_unacquired_terminal_statuses(status, gap_type):
    gaps = rule_r2_acquisition(RUN, _res(status=status, status_reason="why"))
    assert _types(gaps) == [(gap_type, status)]
    assert gaps[0].reason == "why"


def test_r2_blocked_is_retry_eligible_only_for_escalatable_blocks():
    challenge = BlockVerdict(kind=BlockKind.JS_CHALLENGE, vendor="cf", confidence=0.9)
    legal = BlockVerdict(kind=BlockKind.LEGAL, confidence=0.9)
    assert rule_r2_acquisition(RUN, _res(status=S.BLOCKED, block_verdict=challenge))[
        0
    ].retry_eligible
    assert not rule_r2_acquisition(RUN, _res(status=S.BLOCKED, block_verdict=legal))[
        0
    ].retry_eligible


def test_r2_does_not_duplicate_an_existing_access_gap():
    existing = Gap(
        gap_id="g",
        run_id=RUN,
        resource_id="res-1",
        gap_type=GapType.ACCESS_BLOCKED,
        status=S.BLOCKED,
        reason="login_wall",
    )
    assert rule_r2_acquisition(RUN, _res(status=S.BLOCKED), recorded=[existing]) == []


def test_r2_content_status_without_payload_is_a_failure():
    gaps = rule_r2_acquisition(RUN, _res(payload_sha256=None))
    assert _types(gaps) == [(GapType.ACQUISITION_FAILED, S.FAILED)]


def test_r2_covers_children_and_ignores_in_flight():
    child = _res(resource_id="c1", status=S.FAILED)
    running = _res(resource_id="c2", status=S.RETRY_SCHEDULED)
    gaps = rule_r2_acquisition(RUN, _res(), children=[child, running])
    assert [g.resource_id for g in gaps] == ["c1"]


# -- R3 ------------------------------------------------------------------------


def test_r3_unreadable_inventory_is_a_review_gap():
    gaps = rule_r3_frames(RUN, _res(), frames=None)
    assert _types(gaps) == [(GapType.LOW_CONFIDENCE, S.EXTRACTED_UNVERIFIED)]


def test_r3_frame_without_outcome_is_partial():
    gaps = rule_r3_frames(RUN, _res(), frames=[FrameRef(url="http://h/f")])
    assert _types(gaps) == [(GapType.UNSUPPORTED_FORMAT, S.PARTIAL)]
    assert gaps[0].reason == "frame_not_acquired:http://h/f"


def test_r3_frame_outcomes_from_children_and_blocks():
    frames = [
        FrameRef(url="http://h/blocked"),
        FrameRef(url="http://h/failed"),
        FrameRef(url="http://h/ok"),
        FrameRef(url="http://h/running"),
        FrameRef(url="http://h/inline"),
        FrameRef(url="http://h/pixel", hidden=True),
    ]
    children = [
        _res(resource_id="c1", requested_url="http://h/blocked", status=S.BLOCKED),
        _res(resource_id="c2", requested_url="http://h/failed", status=S.FAILED),
        _res(resource_id="c3", requested_url="http://h/ok"),
        _res(resource_id="c4", requested_url="http://h/running", status=S.QUEUED),
    ]
    gaps = rule_r3_frames(
        RUN, _res(), frames=frames, children=children, blocks=[_block(frame="http://h/inline")]
    )
    assert [(g.gap_type, g.reason) for g in gaps] == [
        (GapType.ACCESS_BLOCKED, "frame_blocked:http://h/blocked"),
        (GapType.ACQUISITION_FAILED, "frame_failed:http://h/failed"),
    ]


def test_r3_only_applies_to_acquired_html():
    assert rule_r3_frames(RUN, _res(sniffed_mime="application/pdf"), frames=None) == []
    assert rule_r3_frames(RUN, _res(status=S.BLOCKED), frames=None) == []


# -- R10 -----------------------------------------------------------------------


def test_r10_clean_page_has_no_gap():
    assert rule_r10_payload(RUN, _res()) == []


def test_r10_html_for_an_expected_document_is_blocked():
    gaps = rule_r10_payload(
        RUN, _res(requested_url="http://h/pdf-but-html"), expected_mime="application/pdf"
    )
    assert _types(gaps) == [(GapType.ACCESS_BLOCKED, S.BLOCKED)]
    assert gaps[0].reason == "wrong_payload:html_for:application/pdf"


def test_r10_extension_alone_is_enough():
    gaps = rule_r10_payload(RUN, _res(requested_url="http://h/doc.pdf", final_url="http://h/login"))
    assert gaps[0].reason == "wrong_payload:html_for:application/pdf"


def test_r10_wrong_payload_is_sticky_across_a_later_upgrade():
    earlier = RunEvent(
        run_id=RUN,
        entity_type="resource",
        entity_id="res-1",
        old_state="DISCOVERED",
        new_state="BLOCKED",
        reason="wrong_payload:generic:expected=application/pdf:from=link_type",
    )
    unrelated = earlier.model_copy(update={"entity_id": "other"})
    gaps = rule_r10_payload(RUN, _res(), history=[unrelated, earlier])
    assert _types(gaps) == [(GapType.ACCESS_BLOCKED, S.BLOCKED)]
    assert gaps[0].reason == earlier.reason


def test_r10_classifier_wrong_payload_verdict():
    verdict = BlockVerdict(kind=BlockKind.WRONG_PAYLOAD, confidence=0.7)
    gaps = rule_r10_payload(
        RUN, _res(status=S.BLOCKED, status_reason="wrong_payload:x", block_verdict=verdict)
    )
    assert gaps[0].reason == "wrong_payload:wrong_payload:x"


def test_r10_block_page_stored_as_content():
    verdict = BlockVerdict(kind=BlockKind.JS_CHALLENGE, vendor="cf", confidence=0.9)
    gaps = rule_r10_payload(RUN, _res(block_verdict=verdict))
    assert _types(gaps) == [(GapType.ACCESS_BLOCKED, S.BLOCKED)]
    assert gaps[0].retry_eligible


def test_r10_soft_404_soft_redirect_truncation_placeholder():
    soft = BlockVerdict(kind=BlockKind.SOFT_404, confidence=0.75)
    assert _types(rule_r10_payload(RUN, _res(block_verdict=soft))) == [
        (GapType.LOW_CONFIDENCE, S.EXTRACTED_UNVERIFIED)
    ]
    redirected = _res(requested_url="http://h/deep/page", final_url="http://h/")
    assert _types(rule_r10_payload(RUN, redirected)) == [
        (GapType.SOFT_REDIRECT_SUSPECTED, S.EXTRACTED_UNVERIFIED)
    ]
    truncated = _res(status=S.PARTIAL, status_reason="truncated_body")
    assert _types(rule_r10_payload(RUN, truncated)) == [(GapType.LIMIT_HIT, S.PARTIAL)]
    placeholder = _res(status_reason="rendered;placeholder_content_suspected")
    assert _types(rule_r10_payload(RUN, placeholder)) == [
        (GapType.DEGRADED_VARIANT, S.EXTRACTED_UNVERIFIED)
    ]


def test_r10_html_expected_html_is_fine():
    assert rule_r10_payload(RUN, _res(), expected_mime="text/html") == []
