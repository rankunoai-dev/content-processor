"""Write a report as Excel, CSV or Word, and turn Markdown into a Word document.

A **report** is a title, a line saying what it is and when its data was made, a few
summary lines, and tables (the same tables the module's workbook has).

- **Excel** (`.xlsx`): one sheet per table, nothing cut (long text is split into parts,
  very long tables into several sheets, by the shared writer).
- **CSV**: one table gives one `.csv` file; several give a `.zip` with one `.csv` per
  table. UTF-8 with a BOM so Excel reads it; cells that would run as formulas are
  guarded.
- **Word** (`.docx`): the title, summary and every table, for reading and sharing.
  Word is not a data format, so a long table shows its first `WORD_ROWS` rows and a
  long cell its first `WORD_CELL` characters, each time with a line saying so and
  pointing to the Excel file, which has everything. Internal id columns are left
  out of Word (they stay in Excel and CSV). Wide tables turn the page landscape.
  Needs `python-docx` (the `office` extra); without it Word is not offered.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from src.modules.content_processor.export.tabular import Cell, Table, write_csv, write_xlsx

__all__ = [
    "FORMATS",
    "WORD_CELL",
    "WORD_ROWS",
    "Format",
    "Report",
    "file_slug",
    "markdown_to_docx",
    "word_available",
    "write_report",
]

Format = Literal["xlsx", "csv", "docx"]
FORMATS: Final[tuple[Format, ...]] = ("xlsx", "csv", "docx")
WORD_ROWS: Final = 300
WORD_CELL: Final = 600
_WIDE: Final = 6


@dataclass
class Report:
    """What a download holds."""

    key: str
    title: str
    subtitle: str
    summary: list[str] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)


def word_available() -> bool:
    """True when python-docx is installed."""
    try:
        import docx  # noqa: F401
    except ImportError:
        return False
    return True


def file_slug(text: str) -> str:
    """A safe part of a file name."""
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:60] or "report"


def _text(value: Cell) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".") if value % 1 else f"{int(value):,}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _write_csv(report: Report, path: Path) -> Path:
    tables = [t for t in report.tables if t.columns]
    if len(tables) == 1:
        target = path.with_suffix(".csv")
        write_csv(target, tables[0])
        return target
    target = path.with_suffix(".zip")
    folder = path.parent / f"{path.stem}_csv"
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for n, table in enumerate(tables, start=1):
            name = f"{n:02d}_{file_slug(table.name)}.csv"
            write_csv(folder / name, table)
            archive.write(folder / name, name)
    return target


def _document(title: str) -> Any:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10)
    doc.core_properties.title = title
    return doc


def _landscape(doc: Any) -> None:
    from docx.enum.section import WD_ORIENT

    section = doc.sections[-1]
    if section.orientation != WD_ORIENT.LANDSCAPE:
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width, section.page_height = section.page_height, section.page_width


def _note(doc: Any, text: str) -> None:
    para = doc.add_paragraph()
    run = para.add_run(text)
    run.italic = True


def _readable_columns(table: Table) -> list[str]:
    """Internal ids help in a spreadsheet, not in a document people read."""
    kept = [c for c in table.columns if not c.casefold().endswith(" id")]
    return kept or list(table.columns)


def _word_table(doc: Any, table: Table) -> None:
    from docx.shared import Pt

    columns = _readable_columns(table)
    rows = table.rows[:WORD_ROWS]
    grid = doc.add_table(rows=1, cols=len(columns))
    grid.style = "Table Grid"
    for cell, name in zip(grid.rows[0].cells, columns, strict=True):
        cell.text = ""
        run = cell.paragraphs[0].add_run(name)
        run.bold = True
        run.font.size = Pt(9)
    clipped = 0
    for row in rows:
        cells = grid.add_row().cells
        for cell, name in zip(cells, columns, strict=True):
            text = _text(row.get(name))
            if len(text) > WORD_CELL:
                text = text[:WORD_CELL].rstrip() + " …"
                clipped += 1
            cell.text = ""
            cell.paragraphs[0].add_run(text).font.size = Pt(8)
    if len(table.rows) > WORD_ROWS:
        _note(
            doc,
            f"Showing the first {WORD_ROWS:,} of {len(table.rows):,} rows; the Excel "
            "download has every row.",
        )
    if clipped:
        _note(
            doc, f"{clipped:,} long cell(s) shortened here; the Excel download has the full text."
        )


def _write_docx(report: Report, path: Path) -> Path:
    target = path.with_suffix(".docx")
    doc = _document(report.title)
    if any(len(_readable_columns(t)) > _WIDE for t in report.tables):
        _landscape(doc)
    doc.add_heading(report.title, level=0)
    _note(doc, report.subtitle)
    for line in report.summary:
        doc.add_paragraph(line, style="List Bullet")
    for table in report.tables:
        if not table.columns:
            continue
        doc.add_heading(table.name, level=1)
        if not table.rows:
            _note(doc, "Nothing to list.")
            continue
        _word_table(doc, table)
    doc.save(str(target))
    return target


def write_report(report: Report, fmt: Format, folder: Path, name: str) -> Path:
    """Write the report in one format into `folder`; returns the file (its suffix by format)."""
    folder.mkdir(parents=True, exist_ok=True)
    base = folder / name
    if fmt == "xlsx":
        target = base.with_suffix(".xlsx")
        write_xlsx(target, report.tables)
        return target
    if fmt == "csv":
        return _write_csv(report, base)
    return _write_docx(report, base)


# -- Markdown to Word (analyst card results) --------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_INLINE = re.compile(r"(\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\)|`[^`]+`)")
_LINK = re.compile(r"^\[([^\]]+)\]\(([^)]+)\)$")


def _inline(para: Any, text: str) -> None:
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            para.add_run(part[2:-2]).bold = True
        elif link := _LINK.match(part):
            para.add_run(f"{link.group(1)} ({link.group(2)})")
        elif part.startswith("`") and part.endswith("`"):
            para.add_run(part[1:-1]).font.name = "Consolas"
        else:
            para.add_run(part)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _md_table(doc: Any, lines: Sequence[str]) -> None:
    rows = [_cells(line) for line in lines if not re.match(r"^\s*\|?\s*:?-{2,}", line)]
    if not rows:
        return
    width = max(len(r) for r in rows)
    grid = doc.add_table(rows=0, cols=width)
    grid.style = "Table Grid"
    for i, row in enumerate(rows):
        cells = grid.add_row().cells
        for j in range(width):
            cells[j].text = ""
            _inline(cells[j].paragraphs[0], row[j] if j < len(row) else "")
            if i == 0:
                for run in cells[j].paragraphs[0].runs:
                    run.bold = True


def markdown_to_docx(markdown: str, path: Path, title: str) -> Path:
    """A Word document from Markdown: headings, paragraphs, lists, tables, quotes, bold, links."""
    doc = _document(title)
    lines = markdown.splitlines()
    i = 0
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            _inline(doc.add_paragraph(), " ".join(p.strip() for p in paragraph))
            paragraph.clear()

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("|"):
            flush()
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            _md_table(doc, block)
            continue
        if line.strip().startswith("```"):
            flush()
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            run = doc.add_paragraph().add_run("\n".join(code))
            run.font.name = "Consolas"
            i += 1
            continue
        if heading := _HEADING.match(line):
            flush()
            doc.add_heading(heading.group(2).strip(), level=min(len(heading.group(1)), 4))
        elif bullet := _BULLET.match(line):
            flush()
            _inline(doc.add_paragraph(style="List Bullet"), bullet.group(1))
        elif numbered := _NUMBERED.match(line):
            flush()
            _inline(doc.add_paragraph(style="List Number"), numbered.group(1))
        elif line.startswith(">"):
            flush()
            para = doc.add_paragraph(style="Intense Quote")
            _inline(para, line.lstrip("> ").strip())
        elif line.strip() in ("---", "***") or not line.strip():
            flush()
        else:
            paragraph.append(line)
        i += 1
    flush()
    target = path.with_suffix(".docx")
    target.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(target))
    return target
