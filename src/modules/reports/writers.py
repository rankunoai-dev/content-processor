"""Write a report as Excel, CSV, Word, PDF or JSON, and turn Markdown into a Word or PDF document.

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
- **PDF** (`.pdf`): the same document as Word, for people who only need to read it:
  the same row and cell limits with the same notes, landscape for wide tables, the
  header row repeated on every page, page numbers. Tables are laid out row by row.
  Needs `reportlab` (the `pdf` extra); without it PDF is not offered. The built-in
  font covers Latin scripts only; a character it lacks is shown as "?" and a note
  says how many were replaced. `CP_PDF_FONT` (and `CP_PDF_FONT_BOLD`) name a
  TrueType file with wider coverage, for example a Noto or DejaVu font.
- **JSON** (`.json`): every table with every row and nothing shortened, plus the
  module's own raw record when the report has one (`data`), for other tools.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal
from xml.sax.saxutils import escape

from src.modules.content_processor.export.tabular import Cell, Table, write_csv, write_xlsx

__all__ = [
    "FORMATS",
    "PDF_FONT_SETTING",
    "WORD_CELL",
    "WORD_ROWS",
    "Format",
    "Report",
    "file_slug",
    "markdown_to_docx",
    "markdown_to_pdf",
    "pdf_available",
    "word_available",
    "write_report",
]

Format = Literal["xlsx", "csv", "docx", "pdf", "json"]
FORMATS: Final[tuple[Format, ...]] = ("xlsx", "csv", "docx", "pdf", "json")
WORD_ROWS: Final = 300
WORD_CELL: Final = 600
_WIDE: Final = 6
PDF_FONT_SETTING: Final = "CP_PDF_FONT"
"""Environment variable: a TrueType font file for PDFs (`CP_PDF_FONT_BOLD` its bold face)."""


@dataclass
class Report:
    """What a download holds."""

    key: str
    title: str
    subtitle: str
    summary: list[str] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    data: Any = None
    """The module's own record (JSON-ready), added to the JSON download as `data`."""


def word_available() -> bool:
    """True when python-docx is installed."""
    try:
        import docx  # noqa: F401
    except ImportError:
        return False
    return True


def pdf_available() -> bool:
    """True when reportlab is installed."""
    try:
        import reportlab.platypus  # noqa: F401
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


def _rows_note(table: Table) -> str:
    return (
        f"Showing the first {WORD_ROWS:,} of {len(table.rows):,} rows; the Excel "
        "download has every row."
    )


def _clipped_note(clipped: int) -> str:
    return f"{clipped:,} long cell(s) shortened here; the Excel download has the full text."


def _clip(text: str) -> tuple[str, bool]:
    if len(text) > WORD_CELL:
        return text[:WORD_CELL].rstrip() + " …", True
    return text, False


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
            text, cut = _clip(_text(row.get(name)))
            clipped += cut
            cell.text = ""
            cell.paragraphs[0].add_run(text).font.size = Pt(8)
    if len(table.rows) > WORD_ROWS:
        _note(doc, _rows_note(table))
    if clipped:
        _note(doc, _clipped_note(clipped))


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


def _write_json(report: Report, path: Path) -> Path:
    target = path.with_suffix(".json")
    body: dict[str, Any] = {
        "report": report.key,
        "title": report.title,
        "subtitle": report.subtitle,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "summary": list(report.summary),
        "tables": [
            {
                "name": t.name,
                "columns": list(t.columns),
                "rows": [{c: row.get(c) for c in t.columns} for row in t.rows],
            }
            for t in report.tables
            if t.columns
        ],
    }
    if report.data is not None:
        body["data"] = report.data
    target.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
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
    if fmt == "pdf":
        return _write_pdf(report, base)
    if fmt == "json":
        return _write_json(report, base)
    return _write_docx(report, base)


# -- PDF (reportlab) -------------------------------------------------------------------------


@dataclass(frozen=True)
class _PdfFont:
    """The registered font names a PDF uses, and whether a character has a glyph."""

    regular: str
    bold: str
    italic: str
    fits: Callable[[str], bool]


def _latin(ch: str) -> bool:
    try:
        ch.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def _pdf_font() -> _PdfFont:
    """The font: the TrueType file `CP_PDF_FONT` names, else the built-in Helvetica (Latin)."""
    path = os.environ.get(PDF_FONT_SETTING, "").strip()
    if not path or not Path(path).is_file():
        return _PdfFont("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", _latin)
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    name = "CPReport-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:10]
    bold = name
    bold_path = os.environ.get(PDF_FONT_SETTING + "_BOLD", "").strip()
    if bold_path and Path(bold_path).is_file():
        bold = name + "-Bold"
    if name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(name, path))
        if bold != name:
            pdfmetrics.registerFont(TTFont(bold, bold_path))
        pdfmetrics.registerFontFamily(name, normal=name, bold=bold, italic=name, boldItalic=bold)
    glyphs = pdfmetrics.getFont(name).face.charToGlyph
    return _PdfFont(name, bold, name, lambda ch: ord(ch) in glyphs)


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _pdf_text(text: str, font: _PdfFont) -> tuple[str, int]:
    """Text safe for a reportlab paragraph: escaped, line breaks kept, missing glyphs as "?"."""
    text = _CONTROL.sub("", text.replace("\t", "    "))
    replaced = 0
    out: list[str] = []
    for ch in text:
        if ch == "\n" or font.fits(ch):
            out.append(ch)
        else:
            out.append("?")
            replaced += 1
    return escape("".join(out)).replace("\n", "<br/>"), replaced


def _glyph_note(replaced: int, full: str) -> str:
    return (
        f"{replaced:,} character(s) in a script this PDF's font does not include are shown "
        f'as "?"; the {full} download has the full text.'
    )


def _pdf_styles(font: _PdfFont) -> dict[str, Any]:
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle

    body = ParagraphStyle("body", fontName=font.regular, fontSize=9.5, leading=12.5, spaceAfter=4)
    grey = colors.HexColor("#555555")

    def heading(name: str, size: float, before: float) -> Any:
        return ParagraphStyle(
            name,
            parent=body,
            fontName=font.bold,
            fontSize=size,
            leading=size * 1.25,
            spaceBefore=before,
            spaceAfter=size / 3,
        )

    return {
        "title": heading("title", 18, 0),
        "h1": heading("h1", 14, 12),
        "h2": heading("h2", 12, 10),
        "h3": heading("h3", 10.5, 8),
        "h4": heading("h4", 9.5, 6),
        "body": body,
        "note": ParagraphStyle("note", parent=body, fontName=font.italic, textColor=grey),
        "bullet": ParagraphStyle("bullet", parent=body, leftIndent=14, bulletIndent=2),
        "quote": ParagraphStyle(
            "quote", parent=body, fontName=font.italic, leftIndent=16, textColor=grey
        ),
        "code": ParagraphStyle(
            "code",
            parent=body,
            fontName="Courier",
            fontSize=8.5,
            leading=10.5,
            backColor=colors.HexColor("#f4f4f4"),
            borderPadding=4,
            spaceBefore=4,
            spaceAfter=8,
        ),
        "cell": ParagraphStyle("cell", parent=body, fontSize=8, leading=9.8, spaceAfter=0),
        "head": ParagraphStyle(
            "head", parent=body, fontName=font.bold, fontSize=8, leading=9.8, spaceAfter=0
        ),
    }


def _pdf_document(path: Path, title: str, font: _PdfFont, *, wide: bool) -> Any:
    """A page template with margins and a footer: the title left, the page number right."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate

    size = landscape(A4) if wide else A4
    footer_title = "".join(ch if font.fits(ch) else "?" for ch in title[:120])

    def footer(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont(font.regular, 8)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(doc.leftMargin, 10 * mm, footer_title)
        canvas.drawRightString(size[0] - doc.rightMargin, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(path),
        pagesize=size,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=18 * mm,
        title=title,
        author="Content Processor",
    )
    doc.cp_footer = footer
    return doc


def _column_widths(
    columns: Sequence[str], rows: Sequence[Sequence[str]], width: float
) -> list[float]:
    """Share the page width by how long each column's text runs (sampled), within limits."""
    longest = [len(c) for c in columns]
    for row in rows[:100]:
        for i, text in enumerate(row):
            longest[i] = max(longest[i], min(len(text), 80))
    weights = [max(6.0, min(float(n), 48.0)) for n in longest]
    total = sum(weights)
    return [width * w / total for w in weights]


def _grid_style() -> Any:
    from reportlab.lib import colors
    from reportlab.platypus import TableStyle

    return TableStyle(
        [
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
        ]
    )


def _pdf_grid(
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    font: _PdfFont,
    styles: dict[str, Any],
    width: float,
    *,
    cells_are_markup: bool = False,
) -> tuple[Any, int]:
    """A table that splits over pages with its header row repeated; and the glyphs replaced.

    Rows are laid out one by one: each cell becomes its own small paragraph, so the
    document is never built as one string.
    """
    from reportlab.platypus import LongTable, Paragraph

    replaced = 0

    def cell(text: str, style: Any) -> Any:
        nonlocal replaced
        if cells_are_markup:
            return Paragraph(text, style)
        safe, missing = _pdf_text(text, font)
        replaced += missing
        return Paragraph(safe, style)

    data: list[list[Any]] = [[cell(name, styles["head"]) for name in header]]
    for row in rows:
        data.append([cell(text, styles["cell"]) for text in row])
    grid = LongTable(data, colWidths=_column_widths(header, rows, width), repeatRows=1)
    grid.setStyle(_grid_style())
    return grid, replaced


def _pdf_table(table: Table, font: _PdfFont, styles: dict[str, Any], width: float) -> Iterator[Any]:
    from reportlab.platypus import Paragraph

    columns = _readable_columns(table)
    clipped = 0
    rows: list[list[str]] = []
    for row in table.rows[:WORD_ROWS]:
        cells = []
        for name in columns:
            text, cut = _clip(_text(row.get(name)))
            clipped += cut
            cells.append(text)
        rows.append(cells)
    grid, replaced = _pdf_grid(columns, rows, font, styles, width)
    yield grid
    if len(table.rows) > WORD_ROWS:
        yield Paragraph(_pdf_text(_rows_note(table), font)[0], styles["note"])
    if clipped:
        yield Paragraph(_pdf_text(_clipped_note(clipped), font)[0], styles["note"])
    if replaced:
        yield Paragraph(_glyph_note(replaced, "Excel"), styles["note"])


def _write_pdf(report: Report, path: Path) -> Path:
    from reportlab.platypus import Paragraph, Spacer

    target = path.with_suffix(".pdf")
    font = _pdf_font()
    styles = _pdf_styles(font)
    wide = any(len(_readable_columns(t)) > _WIDE for t in report.tables)
    doc = _pdf_document(target, report.title, font, wide=wide)
    replaced = 0

    def para(text: str, style: str, bullet: str | None = None) -> Any:
        nonlocal replaced
        safe, missing = _pdf_text(text, font)
        replaced += missing
        return Paragraph(safe, styles[style], bulletText=bullet)

    story: list[Any] = [para(report.title, "title"), para(report.subtitle, "note")]
    story += [para(line, "bullet", "•") for line in report.summary]
    for table in report.tables:
        if not table.columns:
            continue
        story.append(para(table.name, "h1"))
        if not table.rows:
            story.append(para("Nothing to list.", "note"))
            continue
        story.extend(_pdf_table(table, font, styles, doc.width))
        story.append(Spacer(1, 6))
    if replaced:
        story.append(Paragraph(_glyph_note(replaced, "Excel"), styles["note"]))
    doc.build(story, onFirstPage=doc.cp_footer, onLaterPages=doc.cp_footer)
    return target


# -- Markdown to Word and PDF (analyst card results) ---------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_INLINE = re.compile(r"(\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\)|`[^`]+`)")
_LINK = re.compile(r"^\[([^\]]+)\]\(([^)]+)\)$")
_RULE = re.compile(r"^\s*\|?\s*:?-{2,}")

BlockKind = Literal["heading", "bullet", "numbered", "quote", "paragraph", "code", "table"]


@dataclass(frozen=True)
class _Block:
    """One piece of a Markdown document, as both writers see it."""

    kind: BlockKind
    text: str = ""
    level: int = 0
    rows: tuple[tuple[str, ...], ...] = ()


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table_rows(lines: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    rows = [_cells(line) for line in lines if not _RULE.match(line)]
    if not rows:
        return ()
    width = max(len(r) for r in rows)
    return tuple(tuple(r[j] if j < len(r) else "" for j in range(width)) for r in rows)


def _markdown_blocks(markdown: str) -> Iterator[_Block]:
    """Headings, paragraphs, lists, tables, quotes and code, in order."""
    lines = markdown.splitlines()
    i = 0
    paragraph: list[str] = []

    def flush() -> Iterator[_Block]:
        if paragraph:
            yield _Block("paragraph", " ".join(p.strip() for p in paragraph))
            paragraph.clear()

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("|"):
            yield from flush()
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            if rows := _table_rows(block):
                yield _Block("table", rows=rows)
            continue
        if line.strip().startswith("```"):
            yield from flush()
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            yield _Block("code", "\n".join(code))
            i += 1
            continue
        if heading := _HEADING.match(line):
            yield from flush()
            yield _Block("heading", heading.group(2).strip(), min(len(heading.group(1)), 4))
        elif bullet := _BULLET.match(line):
            yield from flush()
            yield _Block("bullet", bullet.group(1))
        elif numbered := _NUMBERED.match(line):
            yield from flush()
            yield _Block("numbered", numbered.group(1))
        elif line.startswith(">"):
            yield from flush()
            yield _Block("quote", line.lstrip("> ").strip())
        elif line.strip() in ("---", "***") or not line.strip():
            yield from flush()
        else:
            paragraph.append(line)
        i += 1
    yield from flush()


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


def _md_table(doc: Any, rows: Sequence[Sequence[str]]) -> None:
    grid = doc.add_table(rows=0, cols=len(rows[0]))
    grid.style = "Table Grid"
    for i, row in enumerate(rows):
        cells = grid.add_row().cells
        for j, text in enumerate(row):
            cells[j].text = ""
            _inline(cells[j].paragraphs[0], text)
            if i == 0:
                for run in cells[j].paragraphs[0].runs:
                    run.bold = True


def markdown_to_docx(markdown: str, path: Path, title: str) -> Path:
    """A Word document from Markdown: headings, paragraphs, lists, tables, quotes, bold, links."""
    doc = _document(title)
    for block in _markdown_blocks(markdown):
        if block.kind == "table":
            _md_table(doc, block.rows)
        elif block.kind == "code":
            doc.add_paragraph().add_run(block.text).font.name = "Consolas"
        elif block.kind == "heading":
            doc.add_heading(block.text, level=block.level)
        elif block.kind == "bullet":
            _inline(doc.add_paragraph(style="List Bullet"), block.text)
        elif block.kind == "numbered":
            _inline(doc.add_paragraph(style="List Number"), block.text)
        elif block.kind == "quote":
            _inline(doc.add_paragraph(style="Intense Quote"), block.text)
        else:
            _inline(doc.add_paragraph(), block.text)
    target = path.with_suffix(".docx")
    target.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(target))
    return target


def _pdf_inline(text: str, font: _PdfFont) -> tuple[str, int]:
    """Markdown inline marks as reportlab paragraph markup; and the glyphs replaced."""
    out: list[str] = []
    replaced = 0
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            safe, missing = _pdf_text(part[2:-2], font)
            out.append(f"<b>{safe}</b>")
        elif link := _LINK.match(part):
            safe, missing = _pdf_text(f"{link.group(1)} ({link.group(2)})", font)
            out.append(safe)
        elif part.startswith("`") and part.endswith("`"):
            safe, missing = _pdf_text(part[1:-1], font)
            out.append(f'<font face="Courier">{safe}</font>')
        else:
            safe, missing = _pdf_text(part, font)
            out.append(safe)
        replaced += missing
    return "".join(out), replaced


def markdown_to_pdf(markdown: str, path: Path, title: str) -> Path:
    """A PDF from Markdown, laid out like `markdown_to_docx` (needs reportlab)."""
    from reportlab.platypus import Paragraph, Spacer, XPreformatted

    font = _pdf_font()
    styles = _pdf_styles(font)
    target = path.with_suffix(".pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    doc = _pdf_document(target, title, font, wide=False)
    story: list[Any] = []
    replaced = 0
    number = 0
    for block in _markdown_blocks(markdown):
        if block.kind != "numbered":
            number = 0
        if block.kind == "table":
            cells: list[list[str]] = []
            for row in block.rows:
                marked = []
                for text in row:
                    safe, missing = _pdf_inline(text, font)
                    replaced += missing
                    marked.append(safe)
                cells.append(marked)
            grid, _ = _pdf_grid(cells[0], cells[1:], font, styles, doc.width, cells_are_markup=True)
            story += [grid, Spacer(1, 6)]
            continue
        if block.kind == "code":
            safe, missing = _pdf_text(block.text, font)
            replaced += missing
            story.append(XPreformatted(safe.replace("<br/>", "\n"), styles["code"]))
            continue
        safe, missing = _pdf_inline(block.text, font)
        replaced += missing
        if block.kind == "heading":
            story.append(Paragraph(safe, styles[f"h{block.level}"]))
        elif block.kind == "bullet":
            story.append(Paragraph(safe, styles["bullet"], bulletText="•"))
        elif block.kind == "numbered":
            number += 1
            story.append(Paragraph(safe, styles["bullet"], bulletText=f"{number}."))
        elif block.kind == "quote":
            story.append(Paragraph(safe, styles["quote"]))
        else:
            story.append(Paragraph(safe, styles["body"]))
    if replaced:
        story.append(Paragraph(_glyph_note(replaced, "Markdown"), styles["note"]))
    if not story:
        story.append(Paragraph("", styles["body"]))
    doc.build(story, onFirstPage=doc.cp_footer, onLaterPages=doc.cp_footer)
    return target
