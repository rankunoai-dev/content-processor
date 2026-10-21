"""Report R1 (URL content) and the review queue, built from `UrlRecord`s.

Both reports have one row per requested URL (the review queue keeps only the
URLs that need a decision), and every row carries `run_id`, the evidence ids
and the block ids, so any number can be traced back to stored bytes.

The badge and gaps come from the record (L4, `verify/record.py`), never from a
worker's resource status, so these reports cannot claim more than the
verification brain does.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from src.modules.content_processor.contracts import Gap, UrlRecord
from src.modules.content_processor.enums import BADGE_PRECEDENCE, GapType, UrlBadge
from src.modules.content_processor.export.content import whole_content
from src.modules.content_processor.export.tabular import (
    Cell,
    Table,
    expand_split_column,
    xml_safe,
)

__all__ = [
    "R1_COLUMNS",
    "REVIEW_BADGES",
    "REVIEW_COLUMNS",
    "r1_rows",
    "r1_table",
    "review_rows",
    "review_table",
    "suggested_action",
]

R1_COLUMNS: tuple[str, ...] = (
    "run_id",
    "url",
    "aliases",
    "final_url",
    "badge",
    "word_count",
    "gap_count",
    "gap_types",
    "captured_at",
    "tier_reached",
    "warnings",
    "evidence_ids",
    "block_count",
    "block_ids",
    "control_chars",
    "whole_content",
)
"""`control_chars`: characters XLSX cannot hold (replaced there, verbatim in CSV/JSONL)."""

REVIEW_BADGES: frozenset[UrlBadge] = frozenset(
    {UrlBadge.FAILED, UrlBadge.BLOCKED, UrlBadge.PARTIAL, UrlBadge.NEEDS_REVIEW}
)
"""Badges that need a human decision. RUNNING is unfinished work, not a decision."""

REVIEW_COLUMNS: tuple[str, ...] = (
    "run_id",
    "url",
    "badge",
    "gap_types",
    "gaps",
    "suggested_action",
    "tier_reached",
    "final_url",
    "evidence_ids",
    "operator_action",
    "operator_note",
)

_ACTIONS: tuple[tuple[GapType, str], ...] = (
    (GapType.ACCESS_BLOCKED, "obtain the asset manually or approve a higher access tier"),
    (GapType.POLICY_EXCLUDED, "confirm the exclusion or record an authorization exception"),
    (GapType.ACQUISITION_FAILED, "retry later or attach the asset manually"),
    (GapType.UNSUPPORTED_FORMAT, "attach a readable copy or accept as unsupported"),
    (GapType.BUDGET_STOPPED, "raise the budget or accept partial coverage"),
    (GapType.LIMIT_HIT, "raise the limit or accept partial coverage"),
    (GapType.SHADOW_BLOCK_SUSPECTED, "compare against a browser visit"),
    (GapType.DEGRADED_VARIANT, "compare against a browser visit"),
    (GapType.SOFT_404_SUSPECTED, "confirm whether the page exists"),
    (GapType.SOFT_REDIRECT_SUSPECTED, "confirm the page is not a redirect to a generic page"),
    (GapType.UNVISITED_STATE, "review the unexplored states"),
    (GapType.DISCOVERY_OPEN, "review the unexplored states"),
    (GapType.FIDELITY_DISAGREEMENT, "compare source and rendered text"),
    (GapType.LOW_CONFIDENCE, "spot-check the extracted text"),
    (GapType.VOLATILE, "spot-check the extracted text"),
)


def _join(values: Iterable[str], sep: str = " ") -> str:
    return sep.join(dict.fromkeys(v for v in values if v))


def _evidence_ids(record: UrlRecord) -> list[str]:
    ids = [e for g in record.gaps for e in g.evidence_ids]
    ids.extend(e for b in record.blocks for e in b.evidence_ids)
    return list(dict.fromkeys(ids))


def _gap_types(gaps: Sequence[Gap]) -> str:
    return _join(sorted({g.gap_type.value for g in gaps}), ";")


def suggested_action(gaps: Sequence[Gap]) -> str:
    """First matching operator action for a record's gaps, most decisive first."""
    present = {g.gap_type for g in gaps}
    for gap_type, action in _ACTIONS:
        if gap_type in present:
            return action
    return "review the record"


def r1_rows(
    records: Iterable[UrlRecord], *, include_boilerplate: bool = True
) -> list[dict[str, Cell]]:
    """One row per requested URL, `whole_content` unsplit (verbatim, for JSONL)."""
    rows: list[dict[str, Cell]] = []
    for record in records:
        content = whole_content(record.blocks, include_boilerplate=include_boilerplate)
        rows.append(
            {
                "run_id": record.run_id,
                "url": record.requested_url,
                "aliases": _join(record.aliases, " | "),
                "final_url": record.final_url,
                "badge": record.badge.value,
                "word_count": len(content.split()),
                "gap_count": len(record.gaps),
                "gap_types": _gap_types(record.gaps),
                "captured_at": record.captured_at.isoformat(),
                "tier_reached": record.tier_reached.value if record.tier_reached else None,
                "warnings": _join(record.warnings, ";"),
                "evidence_ids": _join(_evidence_ids(record)),
                "block_count": len(record.blocks),
                "block_ids": _join(b.block_id for b in record.blocks),
                "control_chars": xml_safe(content)[1],
                "whole_content": content,
            }
        )
    return rows


def r1_table(rows: Sequence[dict[str, Cell]]) -> Table:
    """R1 ready for CSV/XLSX: long `whole_content` and `block_ids` split, never truncated."""
    copied = [dict(r) for r in rows]
    columns = expand_split_column(copied, R1_COLUMNS, "whole_content")
    columns = expand_split_column(copied, columns, "block_ids")
    return Table(name="R1 URL content", columns=columns, rows=copied)


def _severity(badge: UrlBadge) -> int:
    return BADGE_PRECEDENCE.index(badge)


def review_rows(
    records: Iterable[UrlRecord],
    decisions: Mapping[str, tuple[str, str]] | None = None,
) -> list[dict[str, Cell]]:
    """URLs needing a decision, most severe badge first.

    `decisions` (url -> (action, note), saved in the web app) pre-fill the
    operator columns; otherwise they are blank for the reviewer.
    """
    saved = decisions or {}
    chosen = sorted(
        (r for r in records if r.badge in REVIEW_BADGES),
        key=lambda r: (_severity(r.badge), r.requested_url),
    )
    rows: list[dict[str, Cell]] = []
    for record in chosen:
        rows.append(
            {
                "run_id": record.run_id,
                "url": record.requested_url,
                "badge": record.badge.value,
                "gap_types": _gap_types(record.gaps),
                "gaps": " || ".join(f"{g.gap_type.value}: {g.reason}" for g in record.gaps),
                "suggested_action": suggested_action(record.gaps),
                "tier_reached": record.tier_reached.value if record.tier_reached else None,
                "final_url": record.final_url,
                "evidence_ids": _join(_evidence_ids(record)),
                "operator_action": saved.get(record.requested_url, ("", ""))[0],
                "operator_note": saved.get(record.requested_url, ("", ""))[1],
            }
        )
    return rows


def review_table(rows: Sequence[dict[str, Cell]]) -> Table:
    """Review queue ready for CSV/XLSX (the gap list is split if it is very long)."""
    copied = [dict(r) for r in rows]
    columns = expand_split_column(copied, REVIEW_COLUMNS, "gaps")
    return Table(name="Review queue", columns=columns, rows=copied)
