from __future__ import annotations

from src.modules.content_processor.contracts import Gap
from src.modules.content_processor.enums import EntityStatus, GapType, UrlBadge
from src.modules.content_processor.verify.status import (
    derive_status_vector,
    derive_url_badge,
    summarize_run_badge,
)

V = EntityStatus.VERIFIED_EXTRACTED


def _gap(kind: GapType, status: EntityStatus = EntityStatus.PARTIAL) -> Gap:
    return Gap(gap_id="g", run_id="r", resource_id="res", gap_type=kind, status=status, reason="x")


def _badge(**kw):
    base = {
        "target_status": V,
        "child_statuses": [],
        "gaps": [],
        "discovery_closed": True,
        "work_outstanding": False,
    }
    base.update(kw)
    return derive_url_badge(**base)


def test_verified_only_when_everything_closed_and_clean():
    assert _badge() is UrlBadge.VERIFIED_WITHIN_SCOPE


def test_outstanding_work_is_running():
    assert _badge(work_outstanding=True) is UrlBadge.RUNNING
    assert _badge(target_status=EntityStatus.IN_PROGRESS) is UrlBadge.RUNNING


def test_one_blocked_child_makes_url_partial_not_verified():
    assert _badge(child_statuses=[V, V, EntityStatus.BLOCKED]) is UrlBadge.PARTIAL


def test_open_discovery_is_partial_even_with_all_children_verified():
    assert _badge(child_statuses=[V, V], discovery_closed=False) is UrlBadge.PARTIAL


def test_unvisited_tab_gap_is_partial():
    assert _badge(gaps=[_gap(GapType.UNVISITED_STATE)]) is UrlBadge.PARTIAL


def test_review_gap_is_needs_review():
    assert (
        _badge(gaps=[_gap(GapType.LOW_CONFIDENCE, EntityStatus.EXTRACTED_UNVERIFIED)])
        is UrlBadge.NEEDS_REVIEW
    )


def test_partial_outranks_review():
    gaps = [_gap(GapType.LOW_CONFIDENCE), _gap(GapType.ACCESS_BLOCKED, EntityStatus.BLOCKED)]
    assert _badge(gaps=gaps) is UrlBadge.PARTIAL


def test_target_level_terminal_statuses():
    assert _badge(target_status=EntityStatus.BLOCKED) is UrlBadge.BLOCKED
    assert _badge(target_status=EntityStatus.FAILED) is UrlBadge.FAILED
    assert _badge(target_status=EntityStatus.EXCLUDED_BY_POLICY) is UrlBadge.EXCLUDED


def test_status_vector_reports_worst_per_dimension():
    vec = derive_status_vector(
        acquisition_statuses=[V, EntityStatus.BLOCKED],
        region_statuses=[V, EntityStatus.EXTRACTED_UNVERIFIED],
        state_statuses=[V],
        discovery_closed=False,
    )
    assert vec.acquisition is EntityStatus.BLOCKED
    assert vec.fidelity is EntityStatus.EXTRACTED_UNVERIFIED
    assert vec.state_exploration is V
    assert "discovery_open" in vec.reasons


def test_run_badge_precedence():
    assert (
        summarize_run_badge([UrlBadge.VERIFIED_WITHIN_SCOPE, UrlBadge.PARTIAL]) is UrlBadge.PARTIAL
    )
    assert summarize_run_badge([UrlBadge.EXCLUDED]) is UrlBadge.EXCLUDED
    assert summarize_run_badge([]) is UrlBadge.EXCLUDED
    assert summarize_run_badge([UrlBadge.RUNNING, UrlBadge.FAILED]) is UrlBadge.RUNNING
