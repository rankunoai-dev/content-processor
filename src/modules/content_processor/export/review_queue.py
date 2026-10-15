"""HITL review queue export (feasibility doc, Path B)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path

from src.modules.content_processor.contracts import Gap, Resource
from src.modules.content_processor.enums import GapType

__all__ = ["REVIEW_GAP_TYPES", "write_review_queue"]

REVIEW_GAP_TYPES: frozenset[GapType] = frozenset(
    {GapType.ACCESS_BLOCKED, GapType.UNVISITED_STATE, GapType.DEGRADED_VARIANT}
)


def write_review_queue(
    path: Path, resources: Iterable[Resource], gaps_for: Callable[[str], list[Gap]]
) -> int:
    """Write one JSONL line per resource that needs a human decision. Returns the count."""
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for res in resources:
            gaps = [g for g in gaps_for(res.resource_id) if g.gap_type in REVIEW_GAP_TYPES]
            if not gaps and res.block_verdict is not None and res.block_verdict.is_block:
                gaps = []
            if not gaps and not (res.block_verdict is not None and res.block_verdict.is_block):
                continue
            row = {
                "url": res.requested_url,
                "resource_id": res.resource_id,
                "status": res.status.value,
                "status_reason": res.status_reason,
                "block": (
                    {"kind": res.block_verdict.kind.value, "vendor": res.block_verdict.vendor}
                    if res.block_verdict is not None and res.block_verdict.is_block
                    else None
                ),
                "gaps": [
                    {"type": g.gap_type.value, "reason": g.reason, "evidence": g.evidence_ids}
                    for g in gaps
                ],
                "tier_reached": res.acquired_at_tier.value if res.acquired_at_tier else None,
                "operator_action": "",
            }
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count
