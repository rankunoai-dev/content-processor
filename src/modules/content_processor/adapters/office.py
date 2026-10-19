"""Office Open XML extraction (C-9): DOCX, XLSX and PPTX with pure-Python libraries.

Uses python-docx, openpyxl and python-pptx only: no LibreOffice, no torch, no
external binaries. Legacy binary formats (.doc, .xls, .ppt) are not parsed here;
they are reported as UNSUPPORTED_FORMAT by the caller.

Safety comes before parsing. An OOXML file is a zip, so its declared member
count and uncompressed size are checked first (zip bombs), then the parser runs
inside the doc worker's isolated child process with a wall-clock limit.

Layers follow ARCHITECTURE.md axiom 3: tracked deletions, hidden text, hidden
slides and speaker notes are kept in their own layers, never merged into body text.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Iterator
from typing import Any, Literal

from pydantic import Field

from src.core.logger import get_logger
from src.core.schemas import StrictModel
from src.modules.content_processor.adapters.html_source import normalize_text
from src.modules.content_processor.contracts import ContentBlock, Provenance
from src.modules.content_processor.enums import ContentLayer, EntityStatus

__all__ = [
    "MAX_MEMBERS",
    "MAX_SHEET_ROWS",
    "MAX_UNCOMPRESSED_BYTES",
    "OfficeExtraction",
    "OfficeLimitError",
    "OfficeParseError",
    "detect_office_kind",
    "extract_office",
]

_logger = get_logger("content_processor.office")

TOOL_VERSION = "office/1.0"
MAX_MEMBERS = 5_000
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_SHEET_ROWS = 5_000
MAX_SHEET_COLS = 100

OfficeKind = Literal["docx", "xlsx", "pptx"]
Role = Literal["main", "nav", "footer", "aside", "legal", "chrome", "unknown"]

# Part names that identify each OOXML flavour inside the zip.
_KIND_MARKERS: dict[str, OfficeKind] = {
    "word/document.xml": "docx",
    "xl/workbook.xml": "xlsx",
    "ppt/presentation.xml": "pptx",
}
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class OfficeParseError(RuntimeError):
    """The file claims to be an OOXML document but cannot be parsed."""


class OfficeLimitError(RuntimeError):
    """The zip container exceeds a safety limit; it is refused before parsing."""


class OfficeExtraction(StrictModel):
    """Extraction output for one OOXML document."""

    kind: OfficeKind
    blocks: list[ContentBlock] = Field(default_factory=list)
    unit_count: int = Field(ge=0, description="Sections, sheets or slides, by kind")
    warnings: list[str] = Field(default_factory=list)
    status: EntityStatus = EntityStatus.EXTRACTED_UNVERIFIED


def detect_office_kind(data: bytes) -> OfficeKind | None:
    """Identify an OOXML flavour from zip member names; None for anything else."""
    if not data.startswith(b"PK\x03\x04"):
        return None
    try:
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    except zipfile.BadZipFile:
        return None
    for marker, kind in _KIND_MARKERS.items():
        if marker in names:
            return kind
    return None


def is_legacy_ole(data: bytes) -> bool:
    """True for the OLE2 compound format used by .doc, .xls and .ppt."""
    return data.startswith(_OLE_MAGIC)


def check_zip_limits(data: bytes) -> None:
    """Refuse containers with too many members or an implausible expansion."""
    try:
        infos = zipfile.ZipFile(io.BytesIO(data)).infolist()
    except zipfile.BadZipFile as exc:
        raise OfficeParseError(f"bad_zip:{exc}") from exc
    if len(infos) > MAX_MEMBERS:
        raise OfficeLimitError(f"zip_members:{len(infos)}>{MAX_MEMBERS}")
    total = sum(i.file_size for i in infos)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise OfficeLimitError(f"zip_uncompressed:{total}>{MAX_UNCOMPRESSED_BYTES}")
    for info in infos:
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise OfficeLimitError(f"zip_ratio:{info.filename}")


def extract_office(
    data: bytes,
    *,
    resource_id: str,
    run_id: str,
    evidence_id: str,
) -> OfficeExtraction:
    """Extract text, tables, notes and comments from a DOCX, XLSX or PPTX file.

    Raises OfficeLimitError for oversized containers and OfficeParseError for
    files that fail to parse. Both are caught by the doc worker.
    """
    kind = detect_office_kind(data)
    if kind is None:
        raise OfficeParseError("not_ooxml")
    check_zip_limits(data)
    ctx = _Ctx(resource_id=resource_id, run_id=run_id, evidence_id=evidence_id, kind=kind)
    try:
        if kind == "docx":
            return _extract_docx(data, ctx)
        if kind == "xlsx":
            return _extract_xlsx(data, ctx)
        return _extract_pptx(data, ctx)
    except (OfficeParseError, OfficeLimitError):
        raise
    except Exception as exc:  # noqa: BLE001 - library errors vary; report uniformly
        raise OfficeParseError(f"{kind}_parse_error:{type(exc).__name__}:{exc}") from exc


class _Ctx:
    """Shared state while building the blocks of one document."""

    def __init__(
        self, *, resource_id: str, run_id: str, evidence_id: str, kind: OfficeKind
    ) -> None:
        self.resource_id = resource_id
        self.run_id = run_id
        self.evidence_id = evidence_id
        self.kind = kind
        self.blocks: list[ContentBlock] = []
        self.warnings: list[str] = []
        self.order = 0
        self.heading_path: list[str] = []

    def add(
        self,
        *,
        key: str,
        text: str,
        method: str,
        layer: ContentLayer,
        role: Role = "main",
        page_index: int | None = None,
        page_label: str | None = None,
        object_id: str | None = None,
    ) -> None:
        """Append one block; empty text is skipped."""
        clean = text.strip()
        if not clean:
            return
        norm, ops = normalize_text(clean)
        full_key = f"{self.resource_id}|{self.kind}|{key}"
        digest = hashlib.sha256(f"{full_key}|{norm}".encode()).hexdigest()
        self.order += 1
        self.blocks.append(
            ContentBlock(
                block_id=f"blk-{digest[:12]}",
                block_key=full_key,
                resource_id=self.resource_id,
                state_id=f"{self.run_id}|{self.kind}",
                region_id=f"reg-{self.kind}-{key}",
                heading_path=list(self.heading_path),
                reading_order=self.order,
                page_index=page_index,
                page_label=page_label,
                layer=layer,
                role=role,
                raw_text=clean,
                normalized_text=norm,
                normalization_ops=ops,
                method=method,
                tool_version=TOOL_VERSION,
                confidence=1.0,
                status=EntityStatus.VERIFIED_EXTRACTED,
                evidence_ids=[self.evidence_id],
                provenance=Provenance(pdf_object_id=object_id, frame_path=self.kind),
            )
        )

    def table_markdown(self, rows: list[list[str]]) -> str:
        """Render rows as a Markdown table; the first row is the header."""
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        padded = [r + [""] * (width - len(r)) for r in rows]
        head = "| " + " | ".join(padded[0]) + " |"
        rule = "| " + " | ".join("---" for _ in range(width)) + " |"
        body = ["| " + " | ".join(r) + " |" for r in padded[1:]]
        return "\n".join([head, rule, *body])


# -- DOCX --------------------------------------------------------------------


def _extract_docx(data: bytes, ctx: _Ctx) -> OfficeExtraction:
    import docx  # local import: keeps module import cheap for non-Office callers
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = docx.Document(io.BytesIO(data))
    body = doc.element.body
    table_no = 0
    para_no = 0
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, doc)
            style = (para.style.name if para.style is not None else "") or ""
            if style.startswith(("Heading", "Title")) and para.text.strip():
                ctx.heading_path = [" ".join(para.text.split())]
            _add_paragraph(ctx, child, f"p{para_no}", "docx/paragraph")
            para_no += 1
        elif tag == "tbl":
            table = Table(child, doc)
            rows = [[" ".join(c.text.split()) for c in r.cells] for r in table.rows]
            md = ctx.table_markdown(rows)
            ctx.add(
                key=f"tbl{table_no}",
                text=md,
                method="docx/table",
                layer=ContentLayer.RENDERED_VISIBLE,
                object_id=f"table_{table_no}",
            )
            table_no += 1

    for s_idx, section in enumerate(doc.sections):
        for kind_name, part in (("header", section.header), ("footer", section.footer)):
            if part.is_linked_to_previous:
                continue
            text = " ".join(p.text for p in part.paragraphs)
            ctx.add(
                key=f"s{s_idx}-{kind_name}",
                text=text,
                method=f"docx/{kind_name}",
                layer=ContentLayer.RENDERED_VISIBLE,
                role="chrome",
            )

    for comment in _docx_comments(doc):
        ctx.add(
            key=f"comment{comment[0]}",
            text=f"[comment] {comment[1]}",
            method="docx/comments",
            layer=ContentLayer.COLLAPSED_PUBLISHED,
        )

    if table_no:
        ctx.warnings.append(f"tables:{table_no}")
    return OfficeExtraction(
        kind="docx",
        blocks=ctx.blocks,
        unit_count=len(doc.sections),
        warnings=["docx_extracted", *ctx.warnings],
    )


def _add_paragraph(ctx: _Ctx, p_el: Any, key: str, method: str) -> None:
    """Add visible text, hidden text (w:vanish) and tracked deletions as separate blocks."""
    visible = p_el.xpath("./w:r/w:t | ./w:hyperlink/w:r/w:t | ./w:ins/w:r/w:t")
    hidden = p_el.xpath(".//w:r[w:rPr/w:vanish]/w:t")
    deleted = p_el.xpath(".//w:del//w:delText")
    hidden_set = set(map(id, hidden))
    visible_text = "".join(t.text or "" for t in visible if id(t) not in hidden_set)
    if visible_text.strip():
        ctx.add(key=key, text=visible_text, method=method, layer=ContentLayer.RENDERED_VISIBLE)
    hidden_text = "".join(t.text or "" for t in hidden)
    if hidden_text.strip():
        ctx.add(
            key=f"{key}-hidden",
            text=hidden_text,
            method="docx/hidden_text",
            layer=ContentLayer.HIDDEN,
        )
    deleted_text = "".join(t.text or "" for t in deleted)
    if deleted_text.strip():
        ctx.add(
            key=f"{key}-deleted",
            text=deleted_text,
            method="docx/tracked_deletion",
            layer=ContentLayer.HIDDEN,
        )


def _docx_comments(doc: Any) -> Iterator[tuple[int, str]]:
    """Comment texts from word/comments.xml, if the document has any."""
    from docx.oxml import parse_xml

    for part in doc.part.package.iter_parts():
        if str(part.partname).endswith("/word/comments.xml"):
            root = parse_xml(part.blob)
            for idx, comment in enumerate(root.iter(f"{{{_W}}}comment")):
                text = " ".join(t.text or "" for t in comment.iter(f"{{{_W}}}t"))
                if text.strip():
                    yield idx, " ".join(text.split())


# -- XLSX --------------------------------------------------------------------


def _extract_xlsx(data: bytes, ctx: _Ctx) -> OfficeExtraction:
    import openpyxl  # type: ignore[import-untyped]  # no openpyxl stubs in this project

    # Two streaming passes: formulas (to find cells with no cached value) and values.
    wb_formulas = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    wb_values = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    uncached = 0
    try:
        for idx, (ws_f, ws_v) in enumerate(
            zip(wb_formulas.worksheets, wb_values.worksheets, strict=True)
        ):
            hidden = getattr(ws_v, "sheet_state", "visible") != "visible"
            layer = ContentLayer.HIDDEN if hidden else ContentLayer.RENDERED_VISIBLE
            rows: list[list[str]] = []
            truncated = False
            for row_f, row_v in zip(
                ws_f.iter_rows(), ws_v.iter_rows(values_only=True), strict=False
            ):
                if len(rows) >= MAX_SHEET_ROWS:
                    truncated = True
                    break
                cells: list[str] = []
                for cell_f, value in zip(row_f, row_v, strict=False):
                    if (
                        isinstance(cell_f.value, str)
                        and cell_f.value.startswith("=")
                        and value is None
                    ):
                        uncached += 1
                    text = "" if value is None else str(value)
                    cells.append(" ".join(text.split())[:500])
                    if len(cells) >= MAX_SHEET_COLS:
                        break
                rows.append(cells)
            rows = _trim_empty(rows)
            if truncated:
                ctx.warnings.append(f"sheet_truncated:{ws_v.title}:>{MAX_SHEET_ROWS}_rows")
            ctx.heading_path = [str(ws_v.title)]
            ctx.add(
                key=f"sheet{idx}-title",
                text=f"Sheet: {ws_v.title}",
                method="xlsx/sheet",
                layer=layer,
                page_index=idx,
                page_label=str(ws_v.title),
            )
            ctx.add(
                key=f"sheet{idx}",
                text=ctx.table_markdown(rows),
                method="xlsx/table",
                layer=layer,
                page_index=idx,
                page_label=str(ws_v.title),
            )
        unit_count = len(wb_values.worksheets)
    finally:
        wb_formulas.close()
        wb_values.close()

    if uncached:
        ctx.warnings.append(f"formula_without_cached_value:{uncached}")
    return OfficeExtraction(
        kind="xlsx",
        blocks=ctx.blocks,
        unit_count=unit_count,
        warnings=["xlsx_extracted", *ctx.warnings],
        status=(
            EntityStatus.PARTIAL
            if any(w.startswith("sheet_truncated") for w in ctx.warnings)
            else EntityStatus.EXTRACTED_UNVERIFIED
        ),
    )


def _trim_empty(rows: list[list[str]]) -> list[list[str]]:
    """Drop trailing empty rows and columns that openpyxl reports for formatted ranges."""
    while rows and not any(rows[-1]):
        rows.pop()
    if not rows:
        return rows
    width = max(len(r) for r in rows)
    while width and all(len(r) < width or not r[width - 1] for r in rows):
        width -= 1
    return [r[:width] for r in rows]


# -- PPTX --------------------------------------------------------------------


def _extract_pptx(data: bytes, ctx: _Ctx) -> OfficeExtraction:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(io.BytesIO(data))
    slides = list(prs.slides)
    for s_idx, slide in enumerate(slides):
        label = str(s_idx + 1)
        hidden = slide._element.get("show") == "0"
        base_layer = ContentLayer.HIDDEN if hidden else ContentLayer.RENDERED_VISIBLE
        title = slide.shapes.title.text_frame.text if slide.shapes.title is not None else ""
        ctx.heading_path = [" ".join(title.split())] if title.strip() else []

        for sh_idx, shape in enumerate(_walk_shapes(slide.shapes, MSO_SHAPE_TYPE)):
            key = f"s{s_idx}-sh{sh_idx}"
            if shape.has_text_frame:
                text = "\n".join(p.text for p in shape.text_frame.paragraphs)
                ctx.add(
                    key=key,
                    text=text,
                    method="pptx/text",
                    layer=base_layer,
                    page_index=s_idx,
                    page_label=label,
                )
            if getattr(shape, "has_table", False) and shape.has_table:
                rows = [[" ".join(c.text.split()) for c in r.cells] for r in shape.table.rows]
                ctx.add(
                    key=f"{key}-tbl",
                    text=ctx.table_markdown(rows),
                    method="pptx/table",
                    layer=base_layer,
                    page_index=s_idx,
                    page_label=label,
                )
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                alt = _alt_text(shape)
                if alt:
                    ctx.add(
                        key=f"{key}-alt",
                        text=f"[image alt] {alt}",
                        method="pptx/alt_text",
                        layer=ContentLayer.ACCESSIBILITY_ONLY,
                        page_index=s_idx,
                        page_label=label,
                    )

        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame
            if notes is not None and notes.text.strip():
                ctx.add(
                    key=f"s{s_idx}-notes",
                    text=notes.text,
                    method="pptx/notes",
                    layer=ContentLayer.SOURCE_ONLY,
                    page_index=s_idx,
                    page_label=label,
                )

    if any(slide._element.get("show") == "0" for slide in slides):
        ctx.warnings.append("hidden_slides_present")
    return OfficeExtraction(
        kind="pptx",
        blocks=ctx.blocks,
        unit_count=len(slides),
        warnings=["pptx_extracted", *ctx.warnings],
    )


def _walk_shapes(shapes: Any, shape_type_enum: Any) -> Iterator[Any]:
    """Yield shapes, descending into groups."""
    for shape in shapes:
        if shape.shape_type == shape_type_enum.GROUP:
            yield from _walk_shapes(shape.shapes, shape_type_enum)
        else:
            yield shape


def _alt_text(shape: Any) -> str:
    """Accessibility description of a picture (cNvPr/@descr), if set."""
    found = shape._element.xpath(".//*[local-name()='cNvPr']/@descr")
    return " ".join(str(found[0]).split()) if found else ""
