"""Whole-content text for one URL (STAGE4_ANALYSIS_AND_INTERFACES.md, interface I1).

Default definition: `rendered_visible` and `collapsed_published` blocks, main
role first, then the other roles, in reading order, headings prefixed. Other
layers are available through `include_layers` but never mixed in silently.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from src.modules.content_processor.contracts import ContentBlock
from src.modules.content_processor.enums import ContentLayer

__all__ = ["DEFAULT_LAYERS", "ROLE_ORDER", "whole_content"]

DEFAULT_LAYERS: frozenset[ContentLayer] = frozenset(
    {ContentLayer.RENDERED_VISIBLE, ContentLayer.COLLAPSED_PUBLISHED}
)
ROLE_ORDER: tuple[str, ...] = ("main", "unknown", "aside", "legal", "nav", "chrome", "footer")


def whole_content(
    blocks: Sequence[ContentBlock],
    *,
    include_layers: Iterable[ContentLayer] = DEFAULT_LAYERS,
    include_boilerplate: bool = True,
    dedupe: bool = True,
) -> str:
    """Render the blocks of one URL as plain text.

    Args:
        blocks: All blocks of the URL (any layer, any role).
        include_layers: Layers to include; defaults to the visible corpus.
        include_boilerplate: When False, nav/chrome/footer roles are dropped.
        dedupe: Skip blocks that are exact duplicates of an earlier block.
    """
    layers = set(include_layers)
    roles = ROLE_ORDER if include_boilerplate else ("main", "unknown", "aside", "legal")
    chosen = [b for b in blocks if b.layer in layers and b.role in roles]
    chosen.sort(key=lambda b: (roles.index(b.role), b.reading_order))
    lines: list[str] = []
    last_heading: list[str] = []
    for b in chosen:
        if dedupe and b.dedupe_of is not None:
            continue
        if b.heading_path != last_heading and b.heading_path:
            depth = len(b.heading_path)
            title = b.heading_path[-1]
            if title and (not lines or lines[-1] != f"{'#' * depth} {title}"):
                lines.append(f"{'#' * depth} {title}")
            last_heading = list(b.heading_path)
            if b.normalized_text == title:
                continue  # the heading element's own text; already emitted as the heading
        if not b.normalized_text:
            continue
        if dedupe and lines and lines[-1] == b.normalized_text:
            continue
        lines.append(b.normalized_text)
    return "\n".join(lines)
