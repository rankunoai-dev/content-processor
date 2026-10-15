"""Status derivation (ARCHITECTURE.md §10, ADR-0005).

A URL badge is derived from the entity statuses and gaps by strict precedence.
It is a summary for humans; the status vector and gap list travel with it and
a badge can never absorb a gap.
"""

from __future__ import annotations

from collections.abc import Iterable

from src.modules.content_processor.contracts import Gap, StatusVector
from src.modules.content_processor.enums import (
    TERMINAL_ENTITY_STATUSES,
    EntityStatus,
    GapType,
    UrlBadge,
)

__all__ = ["derive_status_vector", "derive_url_badge", "is_terminal", "summarize_run_badge"]

_REVIEW_GAPS = frozenset(
    {
        GapType.FIDELITY_DISAGREEMENT,
        GapType.LOW_CONFIDENCE,
        GapType.SHADOW_BLOCK_SUSPECTED,
        GapType.DEGRADED_VARIANT,
        GapType.SOFT_REDIRECT_SUSPECTED,
        GapType.VOLATILE,
    }
)
_PARTIAL_GAPS = frozenset(
    {
        GapType.ACCESS_BLOCKED,
        GapType.ACQUISITION_FAILED,
        GapType.UNSUPPORTED_FORMAT,
        GapType.BUDGET_STOPPED,
        GapType.UNVISITED_STATE,
        GapType.LIMIT_HIT,
        GapType.DISCOVERY_OPEN,
    }
)


def is_terminal(status: EntityStatus) -> bool:
    """True when no further attempt is scheduled for the entity."""
    return status in TERMINAL_ENTITY_STATUSES


def derive_url_badge(
    *,
    target_status: EntityStatus,
    child_statuses: Iterable[EntityStatus],
    gaps: Iterable[Gap],
    discovery_closed: bool,
    work_outstanding: bool,
) -> UrlBadge:
    """Derive the badge for one URL.

    Args:
        target_status: Status of the target resource itself (the page/document).
        child_statuses: Statuses of every required child resource, state and region.
        gaps: All gaps recorded for the URL.
        discovery_closed: Whether bounded discovery finished within scope.
        work_outstanding: Whether any task for this URL is still pending or leased.

    Returns:
        The badge, by the precedence RUNNING > FAILED > BLOCKED > PARTIAL >
        NEEDS_REVIEW > VERIFIED_WITHIN_SCOPE > EXCLUDED.
    """
    children = list(child_statuses)
    gap_list = list(gaps)

    if work_outstanding or not is_terminal(target_status):
        return UrlBadge.RUNNING
    if target_status is EntityStatus.EXCLUDED_BY_POLICY:
        return UrlBadge.EXCLUDED
    if target_status is EntityStatus.FAILED:
        return UrlBadge.FAILED
    if target_status is EntityStatus.BLOCKED:
        return UrlBadge.BLOCKED

    gap_types = {g.gap_type for g in gap_list}
    partial_children = any(
        s
        in (
            EntityStatus.PARTIAL,
            EntityStatus.BLOCKED,
            EntityStatus.FAILED,
            EntityStatus.UNSUPPORTED,
            EntityStatus.BUDGET_EXHAUSTED,
        )
        for s in children
    )
    if (
        partial_children
        or target_status in (EntityStatus.PARTIAL, EntityStatus.BUDGET_EXHAUSTED)
        or gap_types & _PARTIAL_GAPS
        or not discovery_closed
    ):
        return UrlBadge.PARTIAL
    if (
        target_status is EntityStatus.EXTRACTED_UNVERIFIED
        or any(s is EntityStatus.EXTRACTED_UNVERIFIED for s in children)
        or gap_types & _REVIEW_GAPS
    ):
        return UrlBadge.NEEDS_REVIEW
    return UrlBadge.VERIFIED_WITHIN_SCOPE


def derive_status_vector(
    *,
    acquisition_statuses: Iterable[EntityStatus],
    region_statuses: Iterable[EntityStatus],
    state_statuses: Iterable[EntityStatus],
    discovery_closed: bool,
) -> StatusVector:
    """Collapse per-dimension statuses to one status each, worst-first."""

    def worst(statuses: Iterable[EntityStatus]) -> EntityStatus:
        order = (
            EntityStatus.FAILED,
            EntityStatus.BLOCKED,
            EntityStatus.BUDGET_EXHAUSTED,
            EntityStatus.UNSUPPORTED,
            EntityStatus.PARTIAL,
            EntityStatus.EXTRACTED_UNVERIFIED,
            EntityStatus.IN_PROGRESS,
            EntityStatus.QUEUED,
            EntityStatus.RETRY_SCHEDULED,
            EntityStatus.DISCOVERED,
            EntityStatus.EXCLUDED_BY_POLICY,
            EntityStatus.VERIFIED_NONCONTENT,
            EntityStatus.VERIFIED_EXTRACTED,
        )
        present = set(statuses)
        for candidate in order:
            if candidate in present:
                return candidate
        return EntityStatus.VERIFIED_NONCONTENT

    reasons: list[str] = []
    if not discovery_closed:
        reasons.append("discovery_open")
    return StatusVector(
        discovery_closed=discovery_closed,
        acquisition=worst(acquisition_statuses),
        fidelity=worst(region_statuses),
        state_exploration=worst(state_statuses),
        reasons=reasons,
    )


def summarize_run_badge(url_badges: Iterable[UrlBadge]) -> UrlBadge:
    """Run badge by the same precedence over its URLs; empty run is EXCLUDED."""
    badges = set(url_badges)
    for candidate in (
        UrlBadge.RUNNING,
        UrlBadge.FAILED,
        UrlBadge.BLOCKED,
        UrlBadge.PARTIAL,
        UrlBadge.NEEDS_REVIEW,
        UrlBadge.VERIFIED_WITHIN_SCOPE,
    ):
        if candidate in badges:
            return candidate
    return UrlBadge.EXCLUDED
