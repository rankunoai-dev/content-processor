"""Turn gate findings and verification facts into typed gaps (feasibility doc section 2)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from src.modules.content_processor.contracts import Gap
from src.modules.content_processor.enums import EntityStatus, GapType
from src.modules.content_processor.governor.gates import GateFinding

__all__ = [
    "GATE_STATUS",
    "STATUS_SEVERITY",
    "TEASER_MIN_WORDS",
    "gap_for_gate",
    "gap_id_for",
    "gate_status",
    "strictest_status",
]

# gate kind -> (gap type, resource status when the gate blocks the main content)
GATE_STATUS: dict[str, tuple[GapType, EntityStatus, EntityStatus]] = {
    "lead_gen_form_wall": (GapType.ACCESS_BLOCKED, EntityStatus.BLOCKED, EntityStatus.PARTIAL),
    "login_wall": (GapType.ACCESS_BLOCKED, EntityStatus.BLOCKED, EntityStatus.BLOCKED),
    "age_or_audience_gate": (GapType.ACCESS_BLOCKED, EntityStatus.BLOCKED, EntityStatus.PARTIAL),
    "geo_pincode_modal": (GapType.UNVISITED_STATE, EntityStatus.PARTIAL, EntityStatus.PARTIAL),
    "product_unavailable": (
        GapType.DEGRADED_VARIANT,
        EntityStatus.EXTRACTED_UNVERIFIED,
        EntityStatus.EXTRACTED_UNVERIFIED,
    ),
}

# A lead-gen wall over a page that still publishes this many words of teaser
# outside the form is PARTIAL (teaser kept); with less it is BLOCKED.
TEASER_MIN_WORDS = 8

# Terminal outcomes from best to worst. Statuses not listed (in flight,
# excluded) are never raised or lowered by `strictest_status`.
STATUS_SEVERITY: dict[EntityStatus, int] = {
    EntityStatus.VERIFIED_EXTRACTED: 0,
    EntityStatus.VERIFIED_NONCONTENT: 0,
    EntityStatus.EXTRACTED_UNVERIFIED: 1,
    EntityStatus.PARTIAL: 2,
    EntityStatus.UNSUPPORTED: 2,
    EntityStatus.BUDGET_EXHAUSTED: 2,
    EntityStatus.BLOCKED: 3,
    EntityStatus.FAILED: 4,
}


def gap_id_for(run_id: str, resource_id: str, reason: str) -> str:
    """Stable id so a re-run updates the same gap instead of duplicating it."""
    digest = hashlib.sha256(f"{run_id}|{resource_id}|{reason}".encode()).hexdigest()[:16]
    return f"gap-{digest}"


def strictest_status(current: EntityStatus, implied: Iterable[EntityStatus]) -> EntityStatus:
    """Apply the worst implied outcome to a terminal status; never upgrades it.

    A status outside `STATUS_SEVERITY` (in flight or excluded) is returned
    unchanged: a gap cannot finish a running resource or un-exclude one.
    """
    if current not in STATUS_SEVERITY:
        return current
    result = current
    for status in implied:
        if STATUS_SEVERITY.get(status, -1) > STATUS_SEVERITY[result]:
            result = status
    return result


def _teaser_words(finding: GateFinding) -> int | None:
    for item in finding.evidence:
        key, _, value = item.partition("=")
        if key == "teaser_words" and value.isdigit():
            return int(value)
    return None


def gate_status(finding: GateFinding) -> EntityStatus:
    """Resource status a gate finding implies.

    A lead-gen wall that blocks the asset but leaves a teaser on the page is
    PARTIAL (the teaser is real, verified content); with nothing else it is
    BLOCKED (feasibility doc section 2).
    """
    _, blocking_status, partial_status = GATE_STATUS[finding.kind]
    if finding.severity != "blocks_main_content":
        return partial_status
    teaser = _teaser_words(finding)
    if finding.kind == "lead_gen_form_wall" and teaser is not None and teaser >= TEASER_MIN_WORDS:
        return partial_status
    return blocking_status


def gap_for_gate(
    run_id: str, resource_id: str, finding: GateFinding, evidence_ids: list[str]
) -> tuple[Gap, EntityStatus]:
    """Build the gap for a gate finding and return the resource status it implies."""
    gap_type = GATE_STATUS[finding.kind][0]
    status = gate_status(finding)
    gap = Gap(
        gap_id=gap_id_for(run_id, resource_id, finding.kind),
        run_id=run_id,
        resource_id=resource_id,
        gap_type=gap_type,
        status=status,
        reason=finding.kind + ("" if finding.severity == "partial" else ":blocks_main_content"),
        evidence_ids=list(evidence_ids),
        retry_eligible=finding.kind in ("geo_pincode_modal", "product_unavailable"),
    )
    return gap, status
