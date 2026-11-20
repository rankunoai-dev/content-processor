"""The semantic similarity workbook: sheets, plain-language method, and a JSON sidecar.

The sidecar (`semantic_tables`, written as JSON next to the workbook) lets the Downloads
module offer the same tables as Excel, CSV or Word without running the analysis again.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from src.modules.content_processor.analysis.semantic import (
    SemanticPage,
    SemanticResult,
    overlap_reading,
)
from src.modules.content_processor.export.tabular import Cell, Table, expand_split_column

__all__ = ["METHOD_ROWS", "read_tables", "semantic_tables", "write_tables"]

METHOD_ROWS: tuple[tuple[str, str], ...] = (
    (
        "What this is",
        "Pages that say the same thing, scored by meaning rather than by shared wording. "
        "Two pages can score high with none of the same sentences (a rewrite, or two pages "
        "on one topic), and two pages that only share a template do not.",
    ),
    (
        "Step 1: furniture removed",
        "A paragraph that appears on many pages (menus, footers, disclaimers, sign-up "
        "blocks) is site furniture and is dropped before any score. Whole-page scores "
        "would otherwise put two unrelated press releases at about 0.90. Repeated text that "
        "stays inside one section of the website is listed on the Repeated blocks sheet.",
    ),
    (
        "Step 2: three numbers per pair",
        "Meaning score: how close the two pages' own content is in meaning (0 to 1; the "
        "cosine of their mean paragraph vectors). Word overlap: the share of words the two "
        "pages have in common (Jaccard). Same-text ratio: how much of the text is literally "
        "the same (only computed where it can matter). Coverage: how much of each page's own "
        "content has a matching paragraph on the other page.",
    ),
    (
        "Step 3: recurring series set aside",
        "Pages with the same title once dates, numbers and amounts are replaced (monthly "
        "declarations, the same notice for several funds) are a recurring series. Their pairs "
        "are on the Recurring series sheet with the series' typical scores and never count as "
        "duplicate content, unless the text is literally the same on the same date.",
    ),
    (
        "Step 4: evidence for Copy and High",
        "A Copy or High pair is kept only when the pages share most of their words (overlap "
        "0.60 or more) or at least half of each page's own content has a matching paragraph "
        "on the other page (coverage 0.50). Pairs between two different recurring templates "
        "(a monthly declaration and a fund notice) are set aside: they share a subject by "
        "design, not content.",
    ),
    (
        "Tiers (meaning score on own content)",
        "Identical: the same text (same-text ratio 0.97 or more). Copy: 0.95 to 1.00, the same "
        "content with small changes. High: 0.85 to 0.95, the same content said again, with "
        "other words or other figures. Moderate: 0.70 to 0.85, the same topic with different "
        "content: pages that should point to each other or share a hub, not be merged. Below "
        "0.70 is not reported. A typical pair of unrelated editorial pages scores about 0.5.",
    ),
    (
        "Word overlap, read alongside",
        "0.80 or more: copy-level. 0.40 to 0.80: a shared template or heavy reuse. Below 0.25: "
        "normal for unrelated pages. A Moderate or High meaning score with low word overlap is "
        "the case word-matching tools miss.",
    ),
    (
        "Caveats",
        "Scores are not percentages of shared text: 0.82 means the two pages' own content "
        "points in nearly the same direction, not that 82% is the same. The tiers hold only "
        "after furniture removal; whole-page scores inflate unrelated pages. Pages with fewer "
        "than 40 words of own content (tables, forms, short notices) are compared on their "
        "whole text only. The embedding model reads a limited window per paragraph, so very "
        "long paragraphs are judged by their start.",
    ),
)


def _split_long(table: Table, *columns: str) -> Table:
    """Split list columns that can exceed one Excel cell (long series, big hubs)."""
    order = list(table.columns)
    for column in columns:
        if any(isinstance(r.get(column), str) and len(str(r[column])) > 30_000 for r in table.rows):
            order = expand_split_column(table.rows, order, column)
    table.columns = order
    return table


def _page_rows(pages: Sequence[SemanticPage], series_names: Mapping[str, str]) -> Table:
    rows: list[dict[str, Cell]] = []
    for p in sorted(pages, key=lambda p: p.url):
        rows.append(
            {
                "URL": p.url,
                "Website": p.website,
                "Title": p.title,
                "Page type": p.page_type,
                "Section": p.section,
                "Recurring series": series_names.get(p.series_id or "", ""),
                "Published": p.published,
                "Captured words": p.captured_words,
                "Own words": p.own_words,
                "Furniture words": p.furniture_words,
                "Thin (whole text only)": "yes" if p.thin else "no",
                "Robots meta": p.robots_meta,
                "Canonical is itself": ""
                if p.canonical_self is None
                else ("yes" if p.canonical_self else "no"),
                "Placeholder text": "yes" if p.placeholder else "no",
            }
        )
    return Table(
        name="All pages",
        columns=list(rows[0]) if rows else ["URL"],
        rows=rows,
        widths={"URL": 60, "Title": 50, "Website": 18, "Section": 16, "Recurring series": 40},
    )


def semantic_tables(result: SemanticResult) -> list[Table]:
    """Every sheet of the workbook, Summary first."""
    by_url = {p.url: p for p in result.pages}
    series_names = {s.series_id: s.name for s in result.series}
    counts = result.tier_counts()
    summary_rows: list[dict[str, Cell]] = [
        {"Item": "Pages read", "Value": len(result.pages)},
        {"Item": "Pages compared by meaning", "Value": sum(1 for p in result.pages if not p.thin)},
        {
            "Item": "Thin pages (compared on whole text only)",
            "Value": sum(1 for p in result.pages if p.thin),
        },
        {"Item": "Furniture paragraphs removed", "Value": len(result.furniture)},
        {"Item": "Identical pairs", "Value": counts["Identical"]},
        {"Item": "Copy pairs (0.95+)", "Value": counts["Copy"]},
        {"Item": "High pairs (0.85 to 0.95)", "Value": counts["High"]},
        {"Item": "Moderate pairs (0.70 to 0.85, topic hubs)", "Value": counts["Moderate"]},
        {"Item": "Topic hubs", "Value": len(result.hubs)},
        {"Item": "Recurring series", "Value": len(result.series)},
        {
            "Item": "Pages in a recurring series",
            "Value": sum(len(s.members) for s in result.series),
        },
        {"Item": "Series pairs set aside (not duplicates)", "Value": result.series_pairs},
        {
            "Item": "Pairs across two series or templates set aside",
            "Value": result.cross_series_pairs,
        },
        {
            "Item": "High-scoring pairs without paragraph evidence set aside",
            "Value": result.unsupported_pairs,
        },
        {"Item": "Repeated blocks", "Value": len(result.repeated_blocks)},
        {"Item": "Cut-off or ambiguous titles", "Value": len(result.flags.cut_off_titles)},
        {"Item": "Pages hidden from search (noindex)", "Value": len(result.flags.noindex)},
        {"Item": "Placeholder pages", "Value": len(result.flags.placeholders)},
        {"Item": "Duplicate addresses", "Value": len(result.flags.duplicate_addresses)},
        {"Item": "Embedding model", "Value": result.model},
        {
            "Item": "Paragraphs embedded this build / from cache",
            "Value": f"{result.embedded} / {result.cached}",
        },
        {"Item": "Pair list capped", "Value": "yes" if result.capped else "no"},
        {"Item": "Seconds", "Value": round(result.seconds, 1)},
    ]
    summary = Table(
        name="Summary",
        columns=["Item", "Value"],
        rows=summary_rows,
        widths={"Item": 48, "Value": 40},
    )

    dup_rows: list[dict[str, Cell]] = []
    for pair in result.duplicates:
        a, b = by_url[pair.a], by_url[pair.b]
        dup_rows.append(
            {
                "Tier": pair.tier,
                "URL A": pair.a,
                "URL B": pair.b,
                "Title A": a.title,
                "Title B": b.title,
                "Meaning score": pair.meaning,
                "Word overlap": pair.overlap,
                "Overlap reading": overlap_reading(pair.overlap),
                "Same-text ratio": pair.same_text,
                "Coverage A": pair.coverage_a,
                "Coverage B": pair.coverage_b,
                "Published A": a.published,
                "Published B": b.published,
                "Website A": a.website,
                "Website B": b.website,
                "Why": pair.why
                + (f" (group of {pair.group_size} pages)" if pair.group_size else ""),
            }
        )
    duplicates = Table(
        name="Duplicates",
        columns=list(dup_rows[0]) if dup_rows else ["Tier", "URL A", "URL B"],
        rows=dup_rows,
        widths={"URL A": 55, "URL B": 55, "Title A": 40, "Title B": 40, "Why": 50},
    )

    hub_rows: list[dict[str, Cell]] = [
        {
            "Hub": h.label,
            "Pages": len(h.members),
            "Pairs": h.pairs,
            "Median meaning score": h.median_meaning,
            "Page URLs": "\n".join(h.members),
            "Page titles": "\n".join(by_url[u].title for u in h.members),
        }
        for h in result.hubs
    ]
    hubs = Table(
        name="Topic hubs",
        columns=["Hub", "Pages", "Pairs", "Median meaning score", "Page URLs", "Page titles"],
        rows=hub_rows,
        wrap_columns=frozenset({"Page URLs", "Page titles"}),
        widths={"Hub": 30, "Page URLs": 70, "Page titles": 60},
    )

    series_rows: list[dict[str, Cell]] = [
        {
            "Series": s.name,
            "Pages": len(s.members),
            "First published": s.first_published,
            "Last published": s.last_published,
            "Pairs": s.pairs,
            "Median meaning score": s.median_meaning,
            "Median word overlap": s.median_overlap,
            "Page URLs": "\n".join(s.members),
        }
        for s in result.series
    ]
    series = Table(
        name="Recurring series",
        columns=list(series_rows[0]) if series_rows else ["Series", "Pages"],
        rows=series_rows,
        wrap_columns=frozenset({"Page URLs"}),
        widths={"Series": 60, "Page URLs": 70},
    )

    block_rows: list[dict[str, Cell]] = [
        {
            "Text": r.text,
            "Words": r.words,
            "Pages": len(r.pages),
            "Confined to": r.confined_to,
            "Removed before scoring": "yes" if r.removed_from_scoring else "no",
            "Page URLs": "\n".join(r.pages),
        }
        for r in result.repeated_blocks
    ]
    blocks = Table(
        name="Repeated blocks",
        columns=["Text", "Words", "Pages", "Confined to", "Removed before scoring", "Page URLs"],
        rows=block_rows,
        wrap_columns=frozenset({"Text", "Page URLs"}),
        widths={"Text": 80, "Confined to": 24, "Page URLs": 70},
    )

    flag_rows: list[dict[str, Cell]] = []
    for url, reason in result.flags.cut_off_titles:
        flag_rows.append({"Flag": "Cut-off or ambiguous title", "URL": url, "Detail": reason})
    for url in result.flags.noindex:
        flag_rows.append(
            {"Flag": "Hidden from search (noindex)", "URL": url, "Detail": by_url[url].robots_meta}
        )
    for url in result.flags.placeholders:
        flag_rows.append({"Flag": "Placeholder text", "URL": url, "Detail": by_url[url].title})
    for a_url, b_url, ratio in result.flags.duplicate_addresses:
        flag_rows.append(
            {
                "Flag": "Duplicate address",
                "URL": a_url,
                "Detail": f"same text as {b_url} (same-text ratio {ratio})",
            }
        )
    flags = Table(
        name="Flags",
        columns=["Flag", "URL", "Detail"],
        rows=flag_rows,
        wrap_columns=frozenset({"Detail"}),
        widths={"Flag": 30, "URL": 60, "Detail": 90},
    )

    furniture = Table(
        name="Furniture removed",
        columns=["Paragraph", "On pages"],
        rows=[{"Paragraph": text, "On pages": count} for text, count in result.furniture],
        wrap_columns=frozenset({"Paragraph"}),
        widths={"Paragraph": 110},
    )
    method = Table(
        name="Method",
        columns=["Step", "Meaning"],
        rows=[{"Step": k, "Meaning": v} for k, v in METHOD_ROWS],
        wrap_columns=frozenset({"Meaning"}),
        widths={"Step": 36, "Meaning": 120},
    )
    return [
        summary,
        _page_rows(result.pages, series_names),
        duplicates,
        _split_long(hubs, "Page URLs", "Page titles"),
        _split_long(series, "Page URLs"),
        _split_long(blocks, "Page URLs", "Text"),
        flags,
        furniture,
        method,
    ]


def write_tables(path: Path, tables: Sequence[Table]) -> None:
    """Save the sheets as JSON so the Downloads module can offer them in other formats."""
    data = [
        {
            "name": t.name,
            "columns": t.columns,
            "rows": t.rows,
            "wrap_columns": sorted(t.wrap_columns),
            "widths": dict(t.widths),
        }
        for t in tables
    ]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def read_tables(path: Path) -> list[Table]:
    """The sheets saved by `write_tables`."""
    data: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return [
        Table(
            name=str(t["name"]),
            columns=[str(c) for c in t["columns"]],
            rows=list(t["rows"]),
            wrap_columns=frozenset(str(c) for c in t.get("wrap_columns", [])),
            widths={str(k): float(v) for k, v in t.get("widths", {}).items()},
        )
        for t in data
    ]
