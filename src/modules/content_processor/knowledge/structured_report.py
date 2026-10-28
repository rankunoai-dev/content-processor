"""Write a run's structured pages: JSON lines, one Markdown file per page, and a workbook."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from src.modules.content_processor.export.tabular import Cell, Table, write_xlsx
from src.modules.content_processor.knowledge.structured_models import StructuredPage
from src.modules.content_processor.knowledge.structured_page import markdown_name, to_markdown

__all__ = ["structure_tables", "write_structured"]

_HOW_TO_READ = [
    (
        "What this is",
        "Each page as an ordered outline, the way a visitor sees it: sections by heading, "
        "paragraphs, lists, tables, images and videos, and the page's tabs, accordions, "
        "cards and carousels with their labels. Header, footer, menus, cookie and chat "
        "boxes are left out. One Markdown file per page is in the '_pages' folder.",
    ),
    (
        "Page type",
        "What kind of page it is (product, category, service, article, case study, FAQ, "
        "contact, about, careers, legal, pricing, location, event, form, home, document, "
        "other), decided by code from structured data, the address and what the page shows. "
        "'Why' lists the signals; 'Confidence' is how clearly it beat the runner-up.",
    ),
    (
        "Shown after a click",
        "Text inside a closed tab or accordion is kept, marked 'shown after a click'. Text "
        "that only appeared once the engine clicked something is marked 'appears after a "
        "click' and placed under the control that revealed it.",
    ),
    (
        "Cited",
        "Share of the outline's text that was matched to the engine's extracted text, so "
        "it can be traced to the stored evidence.",
    ),
]


def _row(page: StructuredPage, md: str | None) -> dict[str, Cell]:
    c, label = page.counts, page.page_type
    nodes = c.get("text_nodes", 0)
    return {
        "Page": page.url,
        "Status": page.status,
        "Page type": label.page_type if label else None,
        "Confidence": label.confidence if label else None,
        "Runner-up": label.runner_up if label else None,
        "Why": "\n".join(label.signals) if label else None,
        "Title": page.h1 or page.title,
        "Author": page.author,
        "Read from": page.base_view,
        "Words": c.get("words"),
        "Sections": c.get("section"),
        "Tabs": c.get("tab"),
        "Accordion items": c.get("accordion_item"),
        "Cards": c.get("card"),
        "Tables": c.get("table"),
        "Images": c.get("image"),
        "After-click items": c.get("revealed"),
        "Cited %": round(100 * c.get("text_nodes_cited", 0) / nodes, 1) if nodes else None,
        "Markdown file": md,
    }


def structure_tables(
    pages: Sequence[StructuredPage], summary: dict[str, Any], md_names: dict[str, str]
) -> list[Table]:
    """The workbook's sheets."""
    rows = [_row(p, md_names.get(p.url)) for p in pages]
    profile = summary.get("site_profile", {})
    site = [
        {"Fact": "Site profile", "Value": profile.get("profile")},
        *(
            {"Fact": f"Pages: {kind}", "Value": n}
            for kind, n in sorted(profile.get("page_types", {}).items(), key=lambda kv: -kv[1])
        ),
        {"Fact": "Why", "Value": "\n".join(profile.get("signals", []))},
    ]
    return [
        Table(
            "Pages",
            list(rows[0]) if rows else ["Page"],
            rows,
            wrap_columns=frozenset({"Why"}),
            widths={"Page": 60.0, "Why": 60.0, "Title": 40.0},
        ),
        Table("Site profile", ["Fact", "Value"], site, wrap_columns=frozenset({"Value"})),
        Table(
            "How to read",
            ["Topic", "Meaning"],
            [{"Topic": t, "Meaning": m} for t, m in _HOW_TO_READ],
            widths={"Topic": 24.0, "Meaning": 110.0},
            wrap_columns=frozenset({"Meaning"}),
        ),
    ]


def write_structured(
    pages: Sequence[StructuredPage], summary: dict[str, Any], out_dir: Path, run_name: str
) -> dict[str, str]:
    """Write `<run>.structured.jsonl`, `<run>_pages/*.md` and `<run>_page_types.xlsx`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = out_dir / f"{run_name}.structured.jsonl"
    md_dir = out_dir / f"{run_name}_pages"
    xlsx = out_dir / f"{run_name}_page_types.xlsx"
    md_dir.mkdir(exist_ok=True)
    names: dict[str, str] = {}
    with jsonl.open("w", encoding="utf-8") as handle:
        for page in pages:
            handle.write(page.model_dump_json() + "\n")
            if page.status == "structured":
                name = markdown_name(page.url)
                (md_dir / name).write_text(to_markdown(page), encoding="utf-8")
                names[page.url] = name
    write_xlsx(xlsx, structure_tables(pages, summary, names))
    return {"structured": str(jsonl), "markdown": str(md_dir), "workbook": str(xlsx)}
