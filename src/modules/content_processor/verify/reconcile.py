"""Source-vs-rendered reconciliation (rules R6/R7 in ARCHITECTURE.md section 10).

A rendered-visible block is VERIFIED_EXTRACTED only when its normalized text
is present in the browser's visible text (`innerText`). Collapsed-but-published
blocks are present in the DOM but not visible until a disclosure opens them;
they are reported as pending state exploration, never as verified-visible.
Blocks present in the source but absent from the render are kept and
labelled, never deleted; blocks that only appear after rendering are the
hydrated delta (case 03/04). A block whose text survived but whose heading
context changed is reported as moved, not as both lost and new.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.content_processor.adapters.html_source import match_key
from src.modules.content_processor.contracts import ContentBlock
from src.modules.content_processor.enums import ContentLayer, EntityStatus

__all__ = ["ReconcileResult", "reconcile_source_vs_rendered"]

_VISIBLE = (ContentLayer.RENDERED_VISIBLE, ContentLayer.COLLAPSED_PUBLISHED)


class ReconcileResult(StrictModel):
    """Counts and pointers from one reconciliation."""

    source_visible: int = Field(ge=0)
    rendered_visible: int = Field(ge=0)
    matched_keys: int = Field(ge=0)
    moved_keys: int = Field(ge=0, description="Same text, different heading context")
    rendered_only_keys: int = Field(ge=0)
    source_only_keys: int = Field(ge=0)
    verified_blocks: int = Field(ge=0)
    unverified_blocks: int = Field(ge=0)
    collapsed_pending: int = Field(ge=0, description="Published but collapsed; needs a disclosure")
    verified_block_ids: list[str] = Field(default_factory=list)
    unverified_block_ids: list[str] = Field(default_factory=list)
    source_only_block_ids: list[str] = Field(default_factory=list)
    rendered_only_block_ids: list[str] = Field(default_factory=list)

    @property
    def fully_verified(self) -> bool:
        """Every rendered-visible block was found in the visible text."""
        return self.rendered_visible > 0 and self.unverified_blocks == 0


def _norm(text: str) -> str:
    return match_key(text)


def reconcile_source_vs_rendered(
    source_blocks: Sequence[ContentBlock],
    rendered_blocks: Sequence[ContentBlock],
    inner_text: str,
) -> tuple[list[ContentBlock], ReconcileResult]:
    """Verify rendered blocks against visible text and diff them with the source channel.

    Returns:
        The rendered blocks with statuses set, and the reconciliation summary.
    """
    haystack = _norm(inner_text)
    squashed = "".join(haystack.split())
    source_visible = [b for b in source_blocks if b.layer in _VISIBLE]
    rendered_visible = [b for b in rendered_blocks if b.layer in _VISIBLE]
    source_keys = {b.block_key for b in source_visible}
    rendered_keys = {b.block_key for b in rendered_visible}
    source_texts = {_norm(b.normalized_text) for b in source_visible}
    rendered_texts = {_norm(b.normalized_text) for b in rendered_visible}

    updated: list[ContentBlock] = []
    verified_ids: list[str] = []
    unverified_ids: list[str] = []
    rendered_only_ids: list[str] = []
    collapsed_pending = 0
    moved = 0
    for block in rendered_blocks:
        copy = block.model_copy(deep=True)
        if block.layer is ContentLayer.COLLAPSED_PUBLISHED:
            copy.status = EntityStatus.EXTRACTED_UNVERIFIED
            collapsed_pending += 1
        elif block.layer is ContentLayer.RENDERED_VISIBLE:
            needle = _norm(block.normalized_text)
            if needle and (needle in haystack or "".join(needle.split()) in squashed):
                copy.status = EntityStatus.VERIFIED_EXTRACTED
                verified_ids.append(copy.block_id)
            else:
                copy.status = EntityStatus.EXTRACTED_UNVERIFIED
                unverified_ids.append(copy.block_id)
        if block.layer in _VISIBLE and block.block_key not in source_keys:
            if _norm(block.normalized_text) in source_texts:
                moved += 1
            else:
                rendered_only_ids.append(copy.block_id)
        updated.append(copy)

    source_only_ids = [
        b.block_id
        for b in source_visible
        if b.block_key not in rendered_keys and _norm(b.normalized_text) not in rendered_texts
    ]
    result = ReconcileResult(
        source_visible=len(source_keys),
        rendered_visible=len(rendered_keys),
        matched_keys=len(source_keys & rendered_keys),
        moved_keys=moved,
        rendered_only_keys=len(rendered_only_ids),
        source_only_keys=len(source_only_ids),
        verified_blocks=len(verified_ids),
        unverified_blocks=len(unverified_ids),
        collapsed_pending=collapsed_pending,
        verified_block_ids=verified_ids,
        unverified_block_ids=unverified_ids,
        source_only_block_ids=source_only_ids,
        rendered_only_block_ids=rendered_only_ids,
    )
    return updated, result
