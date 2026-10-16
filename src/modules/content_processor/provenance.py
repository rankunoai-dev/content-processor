"""Stamp every extracted block with where it came from and when.

Each worker calls `stamp_blocks` right before it saves blocks, so every block,
whichever adapter produced it (HTML source, rendered DOM, explored state, PDF,
image OCR), carries:

- `source_format`  html, pdf, image, office_doc, ... (from the payload type)
- `ui_region`      where it sits: set by the HTML extractor (hamburger_menu,
                   snippet, palette, footer, ...); for documents derived from
                   the extraction method (pdf_page, pdf_table, image_text, ...)
- `source_url`     the URL it was taken from (final URL after redirects)
- `extracted_at`   when we extracted it
- `page_modified_at` / `page_modified_source`  when the site says it was updated
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from src.modules.content_processor.contracts import ContentBlock, Resource
from src.modules.content_processor.enums import ContentLayer, SourceFormat, UiRegion

__all__ = ["source_format_for", "stamp_blocks", "ui_region_for_method"]

_MIME_FORMAT: tuple[tuple[str, SourceFormat], ...] = (
    ("text/html", SourceFormat.HTML),
    ("application/xhtml", SourceFormat.HTML),
    ("application/pdf", SourceFormat.PDF),
    ("image/", SourceFormat.IMAGE),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml", SourceFormat.OFFICE_DOC),
    ("application/msword", SourceFormat.OFFICE_DOC),
    ("application/rtf", SourceFormat.OFFICE_DOC),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml", SourceFormat.SPREADSHEET),
    ("application/vnd.ms-excel", SourceFormat.SPREADSHEET),
    ("text/csv", SourceFormat.SPREADSHEET),
    ("application/vnd.openxmlformats-officedocument.presentationml", SourceFormat.PRESENTATION),
    ("application/vnd.ms-powerpoint", SourceFormat.PRESENTATION),
    ("application/json", SourceFormat.JSON),
    ("application/xml", SourceFormat.XML),
    ("text/xml", SourceFormat.XML),
    ("audio/", SourceFormat.AUDIO),
    ("video/", SourceFormat.VIDEO),
    ("text/", SourceFormat.TEXT),
)
_METHOD_FORMAT: tuple[tuple[str, SourceFormat], ...] = (
    ("pymupdf", SourceFormat.PDF),
    ("pdf", SourceFormat.PDF),
    ("pillow", SourceFormat.IMAGE),
    ("html_source", SourceFormat.HTML),
)


def source_format_for(mime: str | None, method: str | None = None) -> SourceFormat:
    """Artefact type from the payload's media type, falling back to the extractor."""
    if mime:
        lowered = mime.lower()
        for prefix, fmt in _MIME_FORMAT:
            if lowered.startswith(prefix):
                return fmt
    if method:
        for prefix, fmt in _METHOD_FORMAT:
            if method.startswith(prefix):
                return fmt
    return SourceFormat.OTHER


def ui_region_for_method(method: str, fmt: SourceFormat) -> UiRegion:
    """Region for blocks whose extractor has no DOM context (documents, images)."""
    if method.endswith("/metadata"):
        return UiRegion.DOCUMENT_METADATA
    if method.endswith("/tables"):
        return UiRegion.PDF_TABLE
    if method.endswith("/forms"):
        return UiRegion.PDF_FORM
    if method.endswith("/annotations"):
        return UiRegion.PDF_ANNOTATION
    if fmt is SourceFormat.PDF:
        return UiRegion.PDF_PAGE
    if fmt is SourceFormat.IMAGE or method.startswith("tesseract"):
        return UiRegion.IMAGE_TEXT
    return UiRegion.UNKNOWN


def stamp_blocks(
    blocks: Sequence[ContentBlock],
    resource: Resource,
    *,
    extracted_at: datetime | None = None,
) -> list[ContentBlock]:
    """Return copies of `blocks` tagged with source, place and dates.

    Values a block already carries (e.g. `ui_region` from the HTML extractor,
    or an earlier `extracted_at` on a reused block) are kept.
    """
    when = extracted_at or datetime.now(UTC)
    url = resource.final_url or resource.requested_url
    out: list[ContentBlock] = []
    for block in blocks:
        fmt = block.source_format or source_format_for(resource.sniffed_mime, block.method)
        region = block.ui_region
        if region is None:
            region = (
                UiRegion.PAGE_METADATA
                if block.layer is ContentLayer.METADATA and fmt is SourceFormat.HTML
                else ui_region_for_method(block.method, fmt)
            )
        out.append(
            block.model_copy(
                update={
                    "source_format": fmt,
                    "ui_region": region,
                    "source_url": block.source_url or url,
                    "extracted_at": block.extracted_at or when,
                    "page_modified_at": block.page_modified_at or resource.page_modified_at,
                    "page_modified_source": (
                        block.page_modified_source or resource.page_modified_source
                    ),
                }
            )
        )
    return out
