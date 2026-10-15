"""PDF extraction: native text, tables, visual image regions, and second-opinion triggers.

ARCHITECTURE.md §6:
- Primary: PyMuPDF spans with coordinates, font sizes, bboxes, page labels.
- Structured tables: PyMuPDF find_tables / pdfplumber.
- Secondary triggers (ADR-0003): garbled character ratio > 2% triggers second opinion
  via pypdf.
- Visual region inventory: Detects embedded raster images and scanned pages lacking
  native text, and outputs them as image regions requiring OCR.
"""

from __future__ import annotations

import hashlib
import io
import re
from typing import Any, Literal

import pymupdf
from pydantic import Field

from src.core.logger import get_logger
from src.core.schemas import StrictModel
from src.modules.content_processor.adapters.html_source import normalize_text
from src.modules.content_processor.adapters.ocr import detect_script_from_text
from src.modules.content_processor.adapters.pdf_extras import (
    PdfAttachment,
    annotations_of,
    attachments_of,
    form_fields_of,
    hidden_layer_names,
    invisible_text_of,
)
from src.modules.content_processor.contracts import ContentBlock, Provenance
from src.modules.content_processor.enums import ContentLayer, EntityStatus

__all__ = ["PdfExtraction", "PdfImageRegion", "extract_pdf"]

_logger = get_logger("content_processor.pdf")

TOOL_VERSION = "pdf/1.0"
Role = Literal["main", "nav", "footer", "aside", "legal", "chrome", "unknown"]

_DISCLAIMER_KEYWORDS = re.compile(
    r"copyright|all rights reserved|confidential|terms\s*(?:and|&)\s*conditions"
    r"|privacy policy|disclaimer",
    re.IGNORECASE,
)


class PdfImageRegion(StrictModel):
    """An image region or scanned page discovered in the PDF that may require OCR."""

    page_index: int = Field(ge=0)
    page_label: str
    bbox: tuple[float, float, float, float]
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    image_format: str
    image_bytes: bytes
    is_full_page: bool = False
    script_hint: str | None = None


class PdfExtraction(StrictModel):
    """Full extraction result for one PDF document."""

    blocks: list[ContentBlock] = Field(default_factory=list)
    image_regions: list[PdfImageRegion] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    page_count: int = Field(ge=0)
    is_scanned: bool = False
    has_tables: bool = False
    garbled_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    attachments: list[PdfAttachment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _is_garbled(text: str) -> float:
    """Return the fraction of unprintable or replacement characters in the text."""
    if not text:
        return 0.0
    bad_chars = sum(1 for ch in text if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\r\n\t"))
    return bad_chars / len(text)


def _try_pypdf_fallback(pdf_bytes: bytes) -> list[str]:
    """Second-opinion text extractor via pypdf (ADR-0003)."""
    try:
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages_text: list[str] = []
        for p in reader.pages:
            pages_text.append(p.extract_text() or "")
        return pages_text
    except Exception as exc:
        _logger.warning("pypdf_fallback_failed", extra={"error": str(exc)})
        return []


_CONTINUATION_TOP = 0.20  # table starts in the top 20% of the page ...
_CONTINUATION_BOTTOM = 0.80  # ... and the previous one ended in the bottom 20%


def _clean_rows(raw_rows: list[list[Any]]) -> list[list[str]]:
    return [[" ".join(str(c or "").split()) for c in row] for row in raw_rows]


def _table_markdown(rows: list[list[str]]) -> str:
    """Render rows as a Markdown table; the first row is the header."""
    if not rows:
        return ""
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join("---" for _ in rows[0]) + " |"]
    lines.extend("| " + " | ".join(r) + " |" for r in rows[1:])
    return "\n".join(lines)


def _is_continuation(
    last: dict[str, Any] | None,
    *,
    page_index: int,
    table_index: int,
    cols: int,
    top_ratio: float,
) -> bool:
    """Whether a table continues the previous page's table.

    True when it is the page's first table, sits at the top, has the same column
    count, and the previous table ended near the bottom of its page.
    """
    return (
        last is not None
        and table_index == 0
        and page_index == last["page_index"] + 1
        and cols == last["cols"]
        and top_ratio <= _CONTINUATION_TOP
        and last["bottom_ratio"] >= _CONTINUATION_BOTTOM
    )


def _block(
    *,
    resource_id: str,
    run_id: str,
    evidence_id: str,
    key: str,
    region: str,
    text: str,
    order: int,
    page_index: int,
    page_label: str,
    bbox: tuple[float, float, float, float],
    method: str,
    layer: ContentLayer,
    heading_path: list[str],
    object_id: str,
) -> ContentBlock:
    norm, ops = normalize_text(text)
    digest = hashlib.sha256(f"{key}|{norm}".encode()).hexdigest()
    return ContentBlock(
        block_id=f"blk-{digest[:12]}",
        block_key=key,
        resource_id=resource_id,
        state_id=f"{run_id}|pdf",
        region_id=region,
        heading_path=list(heading_path),
        reading_order=order,
        page_index=page_index,
        page_label=page_label,
        bbox=bbox,
        layer=layer,
        role="main",
        raw_text=text,
        normalized_text=norm,
        normalization_ops=ops,
        method=method,
        tool_version=TOOL_VERSION,
        confidence=1.0,
        status=EntityStatus.VERIFIED_EXTRACTED,
        evidence_ids=[evidence_id],
        provenance=Provenance(pdf_object_id=object_id, frame_path="pdf"),
    )


def extract_pdf(
    pdf_bytes: bytes,
    *,
    resource_id: str,
    run_id: str,
    evidence_id: str,
    base_url: str | None = None,
) -> PdfExtraction:
    """Extract native text, tables, and visual image regions from a PDF document."""
    try:
        # PyMuPDF ships no usable type information; treat the document as Any.
        fitz: Any = pymupdf
        doc: Any = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        _logger.error("pdf_open_error", extra={"error": str(exc), "resource_id": resource_id})
        return PdfExtraction(
            blocks=[],
            image_regions=[],
            metadata={},
            page_count=0,
            warnings=[f"pdf_open_error:{exc}"],
        )

    metadata: dict[str, str] = {}
    if doc.metadata:
        for k, v in doc.metadata.items():
            if v and isinstance(v, str):
                metadata[k] = v.strip()

    blocks: list[ContentBlock] = []
    image_regions: list[PdfImageRegion] = []
    warnings: list[str] = []

    # Metadata content block if present
    if "title" in metadata or "author" in metadata:
        meta_parts: list[str] = []
        if "title" in metadata:
            meta_parts.append(f"Title: {metadata['title']}")
        if "author" in metadata:
            meta_parts.append(f"Author: {metadata['author']}")
        meta_text = " | ".join(meta_parts)
        norm_meta, ops_meta = normalize_text(meta_text)
        meta_hash = hashlib.sha256(f"{resource_id}|pdf|meta".encode()).hexdigest()
        blocks.append(
            ContentBlock(
                block_id=f"blk-{meta_hash[:12]}",
                block_key=f"{resource_id}|pdf|metadata",
                resource_id=resource_id,
                state_id=f"{run_id}|pdf",
                region_id="reg-pdf-metadata",
                reading_order=0,
                page_index=0,
                page_label="metadata",
                layer=ContentLayer.METADATA,
                role="unknown",
                raw_text=meta_text,
                normalized_text=norm_meta,
                normalization_ops=ops_meta,
                method="pymupdf/metadata",
                tool_version=TOOL_VERSION,
                confidence=1.0,
                status=EntityStatus.VERIFIED_EXTRACTED,
                evidence_ids=[evidence_id],
                provenance=Provenance(frame_path="pdf"),
            )
        )

    page_count = len(doc)
    reading_order_idx = 1
    total_chars = 0
    garbled_chars = 0
    pages_with_native_text = 0
    has_tables = False
    invisible_chars = 0
    last_table: dict[str, Any] | None = None

    for page_index in range(len(doc)):
        page = doc[page_index]
        page_label = page.get_label() or str(page_index + 1)
        page_rect = page.rect
        page_dict = page.get_text("dict")
        page_text_blocks = [b for b in page_dict.get("blocks", []) if b.get("type") == 0]
        invisible = invisible_text_of(page)
        page_plain = page.get_text()
        page_script = detect_script_from_text(page_plain) if len(page_plain.strip()) >= 20 else None

        # Font sizes for heading heuristics
        font_sizes: list[float] = []
        for b in page_text_blocks:
            for line in b.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("text", "").strip():
                        font_sizes.append(float(span.get("size", 10.0)))

        median_font_size = 11.0
        if font_sizes:
            font_sizes.sort()
            median_font_size = font_sizes[len(font_sizes) // 2]

        current_heading_path: list[str] = []

        # 1. Native text blocks
        for b_idx, b in enumerate(page_text_blocks):
            lines = b.get("lines", [])
            line_texts: list[str] = []
            max_size = 0.0
            is_bold = False

            for line in lines:
                span_texts: list[str] = []
                for span in line.get("spans", []):
                    t = span.get("text", "")
                    if t:
                        span_texts.append(t)
                        size = float(span.get("size", 10.0))
                        if size > max_size:
                            max_size = size
                        flags = int(span.get("flags", 0))
                        if flags & 16 != 0:  # PyMuPDF span flag bit 4 = bold (bit 1 = italic)
                            is_bold = True
                if span_texts:
                    line_texts.append("".join(span_texts))

            raw_text = "\n".join(line_texts).strip()
            if not raw_text:
                continue

            is_ghost = bool(invisible) and " ".join(raw_text.split()) in invisible
            if is_ghost:
                invisible_chars += len(raw_text)
            else:
                pages_with_native_text += 1
            total_chars += len(raw_text)
            garbled_chars += sum(
                1 for ch in raw_text if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\r\n\t")
            )

            # Heading vs Body vs Legal/Footer heuristic
            bbox_tuple = (
                float(b["bbox"][0]),
                float(b["bbox"][1]),
                float(b["bbox"][2]),
                float(b["bbox"][3]),
            )
            is_heading = max_size >= 1.25 * median_font_size or (is_bold and len(raw_text) < 120)
            is_near_bottom = bbox_tuple[3] >= 0.9 * page_rect.height

            role: Role = "main"
            if is_ghost:
                role = "unknown"
            elif is_near_bottom and _DISCLAIMER_KEYWORDS.search(raw_text):
                role = "legal"
            elif is_heading:
                role = "main"
                clean_heading = " ".join(raw_text.split())
                current_heading_path = [clean_heading]

            norm_text, ops = normalize_text(raw_text)
            blk_hash = hashlib.sha256(
                f"{resource_id}|pdf|p{page_index}|b{b_idx}|{norm_text}".encode()
            ).hexdigest()

            blocks.append(
                ContentBlock(
                    block_id=f"blk-{blk_hash[:12]}",
                    block_key=f"{resource_id}|pdf|p{page_index}|b{b_idx}",
                    resource_id=resource_id,
                    state_id=f"{run_id}|pdf",
                    region_id=f"reg-pdf-p{page_index}-b{b_idx}",
                    heading_path=list(current_heading_path),
                    reading_order=reading_order_idx,
                    page_index=page_index,
                    page_label=page_label,
                    bbox=bbox_tuple,
                    layer=ContentLayer.HIDDEN if is_ghost else ContentLayer.RENDERED_VISIBLE,
                    role=role,
                    raw_text=raw_text,
                    normalized_text=norm_text,
                    normalization_ops=ops,
                    method="pymupdf/native",
                    tool_version=TOOL_VERSION,
                    confidence=1.0,
                    status=EntityStatus.VERIFIED_EXTRACTED,
                    evidence_ids=[evidence_id],
                    provenance=Provenance(
                        pdf_object_id=f"page_{page_index}_block_{b_idx}",
                        frame_path="pdf",
                    ),
                )
            )
            reading_order_idx += 1

        # 2. Structured tables, merging a table that continues from the previous page
        try:
            tab_finder = page.find_tables()
            for t_idx, tab in enumerate(tab_finder.tables if tab_finder else []):
                rows = _clean_rows(tab.extract())
                if not rows:
                    continue
                has_tables = True
                tab_bbox = (
                    float(tab.bbox[0]),
                    float(tab.bbox[1]),
                    float(tab.bbox[2]),
                    float(tab.bbox[3]),
                )
                bottom_ratio = tab_bbox[3] / page_rect.height
                if _is_continuation(
                    last_table,
                    page_index=page_index,
                    table_index=t_idx,
                    cols=len(rows[0]),
                    top_ratio=tab_bbox[1] / page_rect.height,
                ):
                    assert last_table is not None  # noqa: S101 - guaranteed by _is_continuation
                    # A repeated header row on the new page is dropped, not duplicated.
                    body = rows[1:] if rows[0] == last_table["rows"][0] else rows
                    last_table["rows"].extend(body)
                    last_table["pages"].append(page_index)
                    last_table["page_index"] = page_index
                    last_table["bottom_ratio"] = bottom_ratio
                    prev = blocks[last_table["block_index"]]
                    merged_md = _table_markdown(last_table["rows"])
                    merged_norm, merged_ops = normalize_text(merged_md)
                    first_p = last_table["pages"][0]
                    blocks[last_table["block_index"]] = prev.model_copy(
                        update={
                            "raw_text": merged_md,
                            "normalized_text": merged_norm,
                            "normalization_ops": merged_ops,
                            "provenance": Provenance(
                                pdf_object_id=f"page_{first_p}_table_{last_table['t_idx']}"
                                + "".join(f"+page_{q}_table_0" for q in last_table["pages"][1:]),
                                frame_path="pdf",
                            ),
                        }
                    )
                    warnings.append(f"table_continued:p{first_p}-p{page_index}")
                    continue
                table_md = _table_markdown(rows)
                blocks.append(
                    _block(
                        resource_id=resource_id,
                        run_id=run_id,
                        evidence_id=evidence_id,
                        key=f"{resource_id}|pdf|p{page_index}|tab{t_idx}",
                        region=f"reg-pdf-p{page_index}-tab{t_idx}",
                        text=table_md,
                        order=reading_order_idx,
                        page_index=page_index,
                        page_label=page_label,
                        bbox=tab_bbox,
                        method="pymupdf/tables",
                        layer=ContentLayer.RENDERED_VISIBLE,
                        heading_path=current_heading_path,
                        object_id=f"page_{page_index}_table_{t_idx}",
                    )
                )
                last_table = {
                    "block_index": len(blocks) - 1,
                    "page_index": page_index,
                    "t_idx": t_idx,
                    "pages": [page_index],
                    "cols": len(rows[0]),
                    "rows": rows,
                    "bottom_ratio": bottom_ratio,
                }
                reading_order_idx += 1
        except Exception as exc:
            _logger.debug("find_tables_error", extra={"error": str(exc), "page": page_index})

        # 2b. Form fields and annotation comments (content a text dump does not label)
        for f_idx, field in enumerate(form_fields_of(page)):
            blocks.append(
                _block(
                    resource_id=resource_id,
                    run_id=run_id,
                    evidence_id=evidence_id,
                    key=f"{resource_id}|pdf|p{page_index}|form{f_idx}",
                    region=f"reg-pdf-p{page_index}-form{f_idx}",
                    text=f"{field.name}: {field.value}",
                    order=reading_order_idx,
                    page_index=page_index,
                    page_label=page_label,
                    bbox=field.bbox,
                    method="pymupdf/forms",
                    layer=ContentLayer.RENDERED_VISIBLE,
                    heading_path=current_heading_path,
                    object_id=f"page_{page_index}_field_{f_idx}",
                )
            )
            reading_order_idx += 1
        for a_idx, annot in enumerate(annotations_of(page)):
            # Free-text annotations are painted on the page and already appear as native text.
            if annot.kind == "FreeText" and annot.content in page_plain:
                continue
            blocks.append(
                _block(
                    resource_id=resource_id,
                    run_id=run_id,
                    evidence_id=evidence_id,
                    key=f"{resource_id}|pdf|p{page_index}|annot{a_idx}",
                    region=f"reg-pdf-p{page_index}-annot{a_idx}",
                    text=f"[{annot.kind}] {annot.content}",
                    order=reading_order_idx,
                    page_index=page_index,
                    page_label=page_label,
                    bbox=annot.bbox,
                    method="pymupdf/annotations",
                    layer=ContentLayer.COLLAPSED_PUBLISHED,
                    heading_path=current_heading_path,
                    object_id=f"page_{page_index}_annot_{a_idx}",
                )
            )
            reading_order_idx += 1

        # 3. Embedded images and visual regions
        try:
            page_images = page.get_images()
            for img_info in page_images:
                xref = img_info[0]
                rects = page.get_image_rects(xref)
                for rect in rects:
                    # Check if full page or significant visual region
                    is_full_page = (
                        rect.width >= 0.7 * page_rect.width
                        and rect.height >= 0.7 * page_rect.height
                    )
                    # Extract raw image
                    try:
                        extracted_img = doc.extract_image(xref)
                        img_bytes = extracted_img.get("image", b"")
                        img_ext = extracted_img.get("ext", "png")
                        img_w = int(extracted_img.get("width", int(rect.width)))
                        img_h = int(extracted_img.get("height", int(rect.height)))

                        image_regions.append(
                            PdfImageRegion(
                                page_index=page_index,
                                page_label=page_label,
                                bbox=(
                                    float(rect.x0),
                                    float(rect.y0),
                                    float(rect.x1),
                                    float(rect.y1),
                                ),
                                width=img_w,
                                height=img_h,
                                image_format=img_ext,
                                image_bytes=img_bytes,
                                is_full_page=is_full_page,
                                script_hint=page_script,
                            )
                        )
                    except Exception as exc:
                        _logger.debug(
                            "extract_image_xref_error",
                            extra={"error": str(exc), "xref": xref, "page": page_index},
                        )
        except Exception as exc:
            _logger.debug("get_images_error", extra={"error": str(exc), "page": page_index})

    attachments = attachments_of(doc)
    if attachments:
        warnings.append(f"embedded_files:{len(attachments)}")
    hidden_layers = hidden_layer_names(doc)
    if hidden_layers:
        warnings.append("hidden_layers:" + ",".join(hidden_layers))
    if invisible_chars:
        warnings.append(f"invisible_text_layer:chars={invisible_chars}")
    doc.close()

    # Second-opinion trigger: garbled text > 2% (ADR-0003)
    garbled_ratio = (garbled_chars / total_chars) if total_chars > 0 else 0.0
    if garbled_ratio > 0.02:
        warnings.append(f"garbled_text_detected:ratio={garbled_ratio:.3f}")
        pypdf_pages = _try_pypdf_fallback(pdf_bytes)
        if pypdf_pages:
            warnings.append("pypdf_second_opinion_acquired")

    # Scanned document detection: has pages and image regions, but 0 native text blocks
    is_scanned = pages_with_native_text == 0 and len(image_regions) > 0

    return PdfExtraction(
        blocks=blocks,
        image_regions=image_regions,
        metadata=metadata,
        page_count=page_count,
        is_scanned=is_scanned,
        has_tables=has_tables,
        garbled_ratio=garbled_ratio,
        attachments=attachments,
        warnings=warnings,
    )
