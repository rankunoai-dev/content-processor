"""Turn gate findings and verification facts into typed gaps (feasibility doc section 2)."""

from __future__ import annotations

import hashlib

from src.modules.content_processor.contracts import Gap
from src.modules.content_processor.enums import EntityStatus, GapType
from src.modules.content_processor.governor.gates import GateFinding

__all__ = ["GATE_STATUS", "gap_for_gate", "gap_id_for"]

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


def gap_id_for(run_id: str, resource_id: str, reason: str) -> str:
    """Stable id so a re-run updates the same gap instead of duplicating it."""
    digest = hashlib.sha256(f"{run_id}|{resource_id}|{reason}".encode()).hexdigest()[:16]
    return f"gap-{digest}"


def gap_for_gate(
    run_id: str, resource_id: str, finding: GateFinding, evidence_ids: list[str]
) -> tuple[Gap, EntityStatus]:
    """Build the gap for a gate finding and return the resource status it implies."""
    gap_type, blocking_status, partial_status = GATE_STATUS[finding.kind]
    status = blocking_status if finding.severity == "blocks_main_content" else partial_status
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
