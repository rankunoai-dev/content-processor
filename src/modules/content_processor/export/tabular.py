"""Tables to CSV and XLSX without silent loss (STAGE4 §2, report R1 and friends).

Two spreadsheet limits shape every report:

* An XLSX cell holds at most 32,767 characters, counted the way Excel counts
  them (UTF-16 code units, so an emoji is two). `split_cell` cuts long text
  into parts that concatenate back exactly, and `expand_split_column` turns
  one long column into `name`, `name_part_2`, … plus a part count and a flag.
  The XLSX writer refuses any cell over the limit instead of truncating it.
* XML 1.0 cannot carry most C0 control characters. The XLSX writer replaces
  them with U+FFFD and reports how many it replaced; CSV and JSONL keep the
  text verbatim.

CSV is written as UTF-8 with a BOM so Excel detects the encoding. Text cells
that a spreadsheet would run as a formula (`=`, `+`, `-`, `@`, tab, CR first)
get a leading apostrophe in CSV, because the content comes from third-party
pages; XLSX cells are typed as strings and are never formulas, so they stay
verbatim. JSONL is the verbatim, programmatic copy.

The XLSX writer uses only the standard library (inline strings, one sheet per
table, a bold header row, frozen header pane).
"""

from __future__ import annotations

import csv
import json
import math
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

__all__ = [
    "XLSX_CELL_LIMIT",
    "Cell",
    "Table",
    "expand_split_column",
    "split_cell",
    "utf16_len",
    "write_csv",
    "write_jsonl",
    "write_xlsx",
    "xml_safe",
]

XLSX_CELL_LIMIT = 32_767
"""Maximum characters (UTF-16 code units) in one spreadsheet cell."""

Cell = str | int | float | bool | None
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_SHEET_FORBIDDEN = set("[]:*?/\\")


@dataclass
class Table:
    """A named sheet: ordered columns and rows keyed by column name."""

    name: str
    columns: list[str]
    rows: list[dict[str, Cell]] = field(default_factory=list)
    wrap_columns: frozenset[str] = frozenset()
    """XLSX only: cells of these columns wrap text, so line breaks show in Excel."""
    widths: Mapping[str, float] = field(default_factory=dict)
    """XLSX only: column widths in characters."""


def utf16_len(text: str) -> int:
    """Length as Excel counts it (UTF-16 code units)."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def split_cell(text: str, limit: int = XLSX_CELL_LIMIT) -> list[str]:
    """Split `text` into parts of at most `limit` UTF-16 units; `"".join` restores it.

    A surrogate pair is never cut in half. Empty text yields one empty part.
    """
    if limit < 2:
        msg = "limit must be at least 2 (one astral character)"
        raise ValueError(msg)
    parts: list[str] = []
    start = 0
    units = 0
    for index, ch in enumerate(text):
        width = 2 if ord(ch) > 0xFFFF else 1
        if units + width > limit:
            parts.append(text[start:index])
            start = index
            units = 0
        units += width
    parts.append(text[start:])
    return parts


def expand_split_column(
    rows: Sequence[dict[str, Cell]],
    columns: Sequence[str],
    column: str,
    *,
    limit: int = XLSX_CELL_LIMIT,
) -> list[str]:
    """Split one text column in place; return the new column order.

    `column` keeps part 1, `column_part_2` … `column_part_N` hold the rest
    (N is the most parts any row needed), `column_parts` holds the count and
    `column_split` flags rows whose text did not fit in one cell.
    """
    most = 1
    for row in rows:
        value = row.get(column)
        parts = split_cell(value if isinstance(value, str) else "", limit)
        row[column] = parts[0]
        for n, part in enumerate(parts[1:], start=2):
            row[f"{column}_part_{n}"] = part
        row[f"{column}_parts"] = len(parts)
        row[f"{column}_split"] = len(parts) > 1
        most = max(most, len(parts))
    extra = [f"{column}_part_{n}" for n in range(2, most + 1)]
    position = list(columns).index(column) + 1
    ordered = list(columns[:position]) + extra + list(columns[position:])
    return [*ordered, f"{column}_parts", f"{column}_split"]


def _legal_xml_char(ch: str) -> bool:
    cp = ord(ch)
    return (
        cp in (0x9, 0xA, 0xD)
        or 0x20 <= cp <= 0xD7FF
        or 0xE000 <= cp <= 0xFFFD
        or 0x10000 <= cp <= 0x10FFFF
    )


def xml_safe(text: str) -> tuple[str, int]:
    """Replace characters XML 1.0 cannot carry with U+FFFD; return (text, replaced)."""
    if all(_legal_xml_char(ch) for ch in text):
        return text, 0
    out: list[str] = []
    replaced = 0
    for ch in text:
        if _legal_xml_char(ch):
            out.append(ch)
        else:
            out.append("�")
            replaced += 1
    return "".join(out), replaced


def _csv_value(value: Cell, formula_guard: bool) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        if formula_guard and value.startswith(_FORMULA_PREFIXES):
            return "'" + value
        return value
    return str(value)


def write_csv(path: Path, table: Table, *, formula_guard: bool = True) -> int:
    """Write one table as CSV (UTF-8 with BOM). Returns the row count."""
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(table.columns)
        for row in table.rows:
            writer.writerow([_csv_value(row.get(c), formula_guard) for c in table.columns])
    return len(table.rows)


def write_jsonl(path: Path, rows: Iterable[Mapping[str, object]]) -> int:
    """Write rows verbatim as JSON lines. Returns the row count."""
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            count += 1
    return count


def _column_letter(index: int) -> str:
    letters = ""
    n = index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _sheet_name(name: str, taken: set[str]) -> str:
    clean = "".join("_" if ch in _SHEET_FORBIDDEN else ch for ch in name).strip("'") or "Sheet"
    clean = clean[:31]
    candidate = clean
    n = 2
    while candidate.lower() in taken:
        suffix = f" ({n})"
        candidate = clean[: 31 - len(suffix)] + suffix
        n += 1
    taken.add(candidate.lower())
    return candidate


class _Sheet:
    def __init__(self) -> None:
        self.replaced = 0

    def cell(self, ref: str, value: Cell, style: int = 0) -> str:
        style_attr = f' s="{style}"' if style else ""
        if value is None:
            return ""
        if isinstance(value, bool):
            return f'<c r="{ref}" t="b"{style_attr}><v>{int(value)}</v></c>'
        if isinstance(value, int | float):
            if isinstance(value, float) and not math.isfinite(value):
                value = str(value)  # NaN/inf have no XLSX number form
            else:
                return f'<c r="{ref}"{style_attr}><v>{value!r}</v></c>'
        if utf16_len(value) > XLSX_CELL_LIMIT:
            msg = f"cell {ref} exceeds {XLSX_CELL_LIMIT} characters; split it first"
            raise ValueError(msg)
        safe, replaced = xml_safe(value)
        self.replaced += replaced
        return (
            f'<c r="{ref}" t="inlineStr"{style_attr}>'
            f'<is><t xml:space="preserve">{escape(safe)}</t></is></c>'
        )

    def xml(self, table: Table) -> str:
        rows: list[str] = []
        header = "".join(
            self.cell(f"{_column_letter(i)}1", name, style=1)
            for i, name in enumerate(table.columns)
        )
        rows.append(f'<row r="1">{header}</row>')
        for r, row in enumerate(table.rows, start=2):
            cells = "".join(
                self.cell(
                    f"{_column_letter(i)}{r}",
                    row.get(name),
                    style=_WRAP_STYLE if name in table.wrap_columns else 0,
                )
                for i, name in enumerate(table.columns)
            )
            rows.append(f'<row r="{r}">{cells}</row>')
        cols = "".join(
            f'<col min="{i + 1}" max="{i + 1}" width="{table.widths[name]}" customWidth="1"/>'
            for i, name in enumerate(table.columns)
            if name in table.widths
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
            'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            + (f"<cols>{cols}</cols>" if cols else "")
            + f"<sheetData>{''.join(rows)}</sheetData></worksheet>"
        )


_CONTENT_TYPES_HEAD = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/></Relationships>'
)
_WRAP_STYLE = 2

_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
    '<alignment wrapText="1" vertical="top"/></xf></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def write_xlsx(path: Path, tables: Sequence[Table]) -> int:
    """Write tables as sheets of one workbook. Returns characters replaced for XML.

    Raises:
        ValueError: A text cell is longer than `XLSX_CELL_LIMIT`; split it with
            `expand_split_column` first. Nothing is ever truncated.
    """
    if not tables:
        msg = "an XLSX workbook needs at least one table"
        raise ValueError(msg)
    taken: set[str] = set()
    sheets: list[tuple[str, str]] = []
    replaced = 0
    for table in tables:
        sheet = _Sheet()
        body = sheet.xml(table)
        replaced += sheet.replaced
        sheets.append((_sheet_name(table.name, taken), body))
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    workbook_sheets = "".join(
        f'<sheet name="{escape(name, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
        for i, (name, _) in enumerate(sheets, start=1)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{workbook_sheets}</sheets></workbook>"
    )
    sheet_rels = "".join(
        f'<Relationship Id="rId{i}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    styles_rel = (
        f'<Relationship Id="rId{len(sheets) + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{sheet_rels}{styles_rel}</Relationships>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES_HEAD + overrides + "</Types>")
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", _STYLES)
        for i, (_, body) in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{i}.xml", body)
    return replaced
