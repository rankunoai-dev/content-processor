"""Synthetic UrlRecords for Lane 5 tests (reports, overlap, explorer)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from src.modules.content_processor.contracts import (
    ContentBlock,
    DiscoveryState,
    Gap,
    InventoryCounts,
    StatusVector,
    UrlRecord,
)
from src.modules.content_processor.enums import (
    AccessTier,
    ContentLayer,
    EntityStatus,
    GapType,
    UrlBadge,
)

RUN = "run-l5"


def block(
    rid: str,
    order: int,
    text: str,
    *,
    role: Literal["main", "nav", "footer", "aside", "legal", "chrome", "unknown"] = "main",
    layer: ContentLayer = ContentLayer.RENDERED_VISIBLE,
    heading: list[str] | None = None,
    dedupe_of: str | None = None,
) -> ContentBlock:
    return ContentBlock(
        block_id=f"{rid}:b{order}",
        block_key=f"k{order}",
        resource_id=rid,
        state_id=f"{RUN}|render",
        region_id=f"{rid}:r{order}",
        heading_path=heading or [],
        reading_order=order,
        layer=layer,
        role=role,
        raw_text=text,
        normalized_text=text,
        method="test",
        tool_version="t",
        confidence=1.0,
        status=EntityStatus.VERIFIED_EXTRACTED,
        evidence_ids=[f"evidence:{rid}"],
        dedupe_of=dedupe_of,
    )


def gap(rid: str, gap_type: GapType, reason: str = "because") -> Gap:
    return Gap(
        gap_id=f"gap-{rid}-{gap_type.value}",
        run_id=RUN,
        resource_id=rid,
        gap_type=gap_type,
        status=EntityStatus.BLOCKED,
        reason=reason,
        evidence_ids=[f"evidence:gap-{rid}"],
    )


def record(
    url: str,
    texts: list[str] | None = None,
    *,
    badge: UrlBadge = UrlBadge.VERIFIED_WITHIN_SCOPE,
    gaps: list[Gap] | None = None,
    blocks: list[ContentBlock] | None = None,
    aliases: list[str] | None = None,
    warnings: list[str] | None = None,
) -> UrlRecord:
    rid = "res-" + url.rsplit("/", 1)[-1]
    chosen = blocks if blocks is not None else [block(rid, i, t) for i, t in enumerate(texts or [])]
    return UrlRecord(
        run_id=RUN,
        requested_url=url,
        final_url=url,
        aliases=aliases or [],
        captured_at=datetime(2026, 10, 16, 9, 0, tzinfo=UTC),
        tool_versions={"verify": "test"},
        badge=badge,
        status_vector=StatusVector(
            discovery_closed=True,
            acquisition=EntityStatus.VERIFIED_EXTRACTED,
            fidelity=EntityStatus.VERIFIED_EXTRACTED,
            state_exploration=EntityStatus.VERIFIED_EXTRACTED,
            reasons=["test"],
        ),
        inventory=InventoryCounts(
            inventory_revision=0,
            resources_discovered=1,
            resources_acquired=1,
            resources_blocked=0,
            resources_failed=0,
            regions_discovered=len(chosen),
            regions_verified=len(chosen),
            regions_needs_review=0,
            regions_blocked=0,
            regions_unvisited=0,
        ),
        discovery=DiscoveryState(
            closed_within_scope=True, remaining_controls=0, unknown_state_space=False
        ),
        gaps=gaps or [],
        blocks=chosen,
        tier_reached=AccessTier.T1_POLITE_HTTP,
        warnings=warnings or [],
    )


def words(seed: int, n: int) -> str:
    """Deterministic pseudo-text: `n` words drawn from a 500-word vocabulary."""
    out = []
    x = seed * 2654435761 % 2**32
    for _ in range(n):
        x = (1103515245 * x + 12345) % 2**31
        out.append(f"w{x % 500}")
    return " ".join(out)
