"""Write an offering catalog as JSON and as an Excel workbook for analysts."""

from __future__ import annotations

from pathlib import Path

from src.modules.content_processor.export.tabular import Cell, Table, write_xlsx
from src.modules.content_processor.knowledge.catalog_models import (
    CardAttribute,
    Catalog,
    Evidence,
    Offering,
)

__all__ = ["catalog_tables", "write_catalog"]

_SIGNAL_NAMES = {
    "header_menu": "header menu",
    "footer_menu": "footer menu",
    "other_menu": "other menu",
    "url_family": "address folder",
    "schema": "structured data",
    "page": "page",
}
_INDENT = {"family": "", "offering": "    ", "sub_offering": "        "}
_HOW_TO_READ = [
    (
        "Offerings",
        "The website's offerings as a tree: families, offerings under them, and "
        "sub-offerings. Indentation in the Offering column shows the level.",
    ),
    (
        "Signals",
        "Where the offering was found: header menu, footer menu, address folder, "
        "structured data. More independent signals means more certainty; 'Signal count' 1 "
        "means only one place lists it.",
    ),
    (
        "Card columns",
        "Description, Audiences, Numbers, Questions, Calls to action and Tools "
        "come from code reading the pages. Features, Deliverables and Pricing marked (model) "
        "come from a local AI model and were kept only when the quoted words were found in "
        "the cited section.",
    ),
    (
        "Evidence",
        "Every value can be traced: menu path, page address, structured-data field "
        "or section id (chk-...). The JSON file holds the full evidence for every value.",
    ),
    (
        "Page mapping",
        "Every page of the run: the offering it belongs to (by address, "
        "breadcrumb or structured data), or the non-offering section that covers it (News, "
        "Insights), or 'unmapped'.",
    ),
    (
        "Other menu entries",
        "Menu entries that are not offerings: tasks (sign in, file a "
        "claim), tools (calculators), content (insights) and corporate pages (careers).",
    ),
    (
        "Suggestions",
        "What a model proposed that code could not confirm, for a reviewer to "
        "accept or reject. Nothing here is applied.",
    ),
]


def _topic(topic: str, meaning: str) -> dict[str, Cell]:
    return {"Topic": topic, "Meaning": meaning}


def _values(attrs: list[CardAttribute]) -> str:
    return "\n".join(a.value + (" (model)" if a.source != "code" else "") for a in attrs)


def _evidence(items: list[Evidence]) -> str:
    return "\n".join(f"{e.kind}: {e.detail or e.ref}" for e in items)


def _offering_row(o: Offering, names: dict[str, str]) -> dict[str, Cell]:
    signals = sorted({_SIGNAL_NAMES.get(s, s) for s in o.signals})
    return {
        "Offering": _INDENT[o.level] + o.name,
        "Level": o.level.replace("_", "-"),
        "Family": o.family or "",
        "Parent": names.get(o.parent_id or "", ""),
        "Type": o.offering_type,
        "Link": o.href or "",
        "Signals": ", ".join(signals),
        "Signal count": len(signals),
        "Also called": ", ".join(o.aliases),
        "Description": o.description.value if o.description else "",
        "Audiences": _values(o.audiences),
        "Features": _values(o.features),
        "Deliverables": _values(o.deliverables),
        "Pricing": _values(o.pricing),
        "Numbers": ", ".join(a.value for a in o.numbers),
        "Questions": _values(o.questions),
        "Calls to action": _values(o.ctas),
        "Tools and tasks": ", ".join(o.tools),
        "Pages": len(o.pages),
        "Sections": sum(len(p.chunk_ids) for p in o.pages),
        "Evidence": _evidence(o.evidence),
        "Offering id": o.offering_id,
    }


def catalog_tables(catalog: Catalog) -> list[Table]:
    """The workbook's sheets."""
    names = {o.offering_id: o.name for o in catalog.offerings}
    offerings = Table(
        name="Offerings",
        columns=[
            "Offering",
            "Level",
            "Family",
            "Parent",
            "Type",
            "Link",
            "Signals",
            "Signal count",
            "Also called",
            "Description",
            "Audiences",
            "Features",
            "Deliverables",
            "Pricing",
            "Numbers",
            "Questions",
            "Calls to action",
            "Tools and tasks",
            "Pages",
            "Sections",
            "Evidence",
            "Offering id",
        ],
        rows=[_offering_row(o, names) for o in catalog.offerings],
        wrap_columns=frozenset(
            {
                "Description",
                "Audiences",
                "Features",
                "Deliverables",
                "Pricing",
                "Questions",
                "Calls to action",
                "Evidence",
            }
        ),
        widths={"Offering": 40, "Description": 60, "Features": 50, "Evidence": 60, "Link": 40},
    )
    mapping = Table(
        name="Page mapping", columns=["Page", "Placed under", "Kind", "Basis", "Sections"]
    )
    for o in catalog.offerings:
        for page in o.pages:
            mapping.rows.append(
                {
                    "Page": page.url,
                    "Placed under": o.name,
                    "Kind": "offering",
                    "Basis": page.basis.replace("_", " "),
                    "Sections": len(page.chunk_ids),
                }
            )
    for entry in catalog.other_entries:
        for url in entry.pages:
            mapping.rows.append(
                {
                    "Page": url,
                    "Placed under": " › ".join(entry.menu_path),
                    "Kind": f"section ({entry.role})",
                    "Basis": "address or breadcrumb",
                    "Sections": None,
                }
            )
    for url in catalog.unmapped_pages:
        mapping.rows.append(
            {"Page": url, "Placed under": "", "Kind": "unmapped", "Basis": "", "Sections": None}
        )
    others = Table(
        name="Other menu entries",
        columns=["Entry", "Role", "Menu path", "Under offering", "Link", "Pages"],
        rows=[
            {
                "Entry": e.label,
                "Role": e.role,
                "Menu path": " › ".join(e.menu_path),
                "Under offering": names.get(e.offering_id or "", ""),
                "Link": e.href or "",
                "Pages": len(e.pages),
            }
            for e in catalog.other_entries
        ],
    )
    suggestions = Table(
        name="Suggestions",
        columns=["Kind", "Offering", "Proposal", "Model", "Score", "Reason"],
        rows=[
            {
                "Kind": s.kind,
                "Offering": names.get(s.offering_id, s.offering_id),
                "Proposal": s.proposal,
                "Model": s.model,
                "Score": s.score,
                "Reason": s.reason,
            }
            for s in catalog.suggestions
        ],
    )
    how_rows = [_topic(t, m) for t, m in _HOW_TO_READ]
    how_rows.extend(_topic(f"Count: {k}", str(v)) for k, v in catalog.stats.items())
    how_rows.extend(_topic(f"Engine: {k}", v) for k, v in catalog.engines.items())
    how = Table(
        name="How to read",
        columns=["Topic", "Meaning"],
        rows=how_rows,
        wrap_columns=frozenset({"Meaning"}),
        widths={"Topic": 28, "Meaning": 100},
    )
    return [offerings, mapping, others, suggestions, how]


def write_catalog(catalog: Catalog, out_dir: Path, run_name: str) -> dict[str, str]:
    """Write `<run>.catalog.json` and `<run>_offering_catalog.xlsx`; return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{run_name}.catalog.json"
    xlsx_path = out_dir / f"{run_name}_offering_catalog.xlsx"
    json_path.write_text(catalog.model_dump_json(indent=2) + "\n", encoding="utf-8")
    write_xlsx(xlsx_path, catalog_tables(catalog))
    return {"catalog": str(json_path), "workbook": str(xlsx_path)}
