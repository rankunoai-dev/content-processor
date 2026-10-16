"""Source tagging: page regions, source formats and the stamp applied to every block."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.modules.content_processor.adapters.html_source import extract_html_source
from src.modules.content_processor.adapters.regions import classify_region
from src.modules.content_processor.contracts import ContentBlock, Provenance, Resource
from src.modules.content_processor.enums import (
    ContentLayer,
    EntityStatus,
    SourceFormat,
    UiRegion,
)
from src.modules.content_processor.provenance import source_format_for, stamp_blocks

PAGES = Path(__file__).resolve().parents[3] / "fixtures" / "pages"


def _regions() -> dict[str, UiRegion]:
    ex = extract_html_source(
        (PAGES / "regions.html").read_bytes(),
        base_url="https://acme.example/plans",
        evidence_id="e",
        resource_id="r",
    )
    out: dict[str, UiRegion] = {}
    for b in ex.blocks:
        assert b.ui_region is not None, b
        assert b.source_format is SourceFormat.HTML
        out.setdefault(b.normalized_text, b.ui_region)
    return out


@pytest.mark.parametrize(
    ("text", "region"),
    [
        ("We use cookies to improve the site.", UiRegion.COOKIE_BANNER),
        ("Acme", UiRegion.HEADER),
        ("Plans in the drawer", UiRegion.HAMBURGER_MENU),
        ("Mega menu link", UiRegion.MEGA_MENU),
        ("Home", UiRegion.BREADCRUMB),
        ("Simple pricing", UiRegion.HERO),
        ("Main body paragraph about pricing.", UiRegion.MAIN_CONTENT),
        ("Search snippet excerpt text.", UiRegion.SNIPPET),
        ("Card body text.", UiRegion.CARD),
        ("Midnight blue swatch", UiRegion.PALETTE),
        ("Carousel slide text.", UiRegion.CAROUSEL),
        ("Modal dialog text.", UiRegion.MODAL),
        ("Sidebar note.", UiRegion.SIDEBAR),
        ("Footer legal text.", UiRegion.FOOTER),
        ("Regions", UiRegion.PAGE_METADATA),
    ],
)
def test_every_block_is_tagged_with_its_place_on_the_page(text: str, region: UiRegion):
    assert _regions()[text] is region


def test_specific_region_survives_generic_containers():
    assert classify_region("div", {}, UiRegion.HAMBURGER_MENU) is UiRegion.HAMBURGER_MENU
    assert classify_region("nav", {}, UiRegion.HAMBURGER_MENU) is UiRegion.HAMBURGER_MENU
    assert classify_region("div", {"class": "card"}, UiRegion.MAIN_CONTENT) is UiRegion.CARD
    assert classify_region("span", {"role": "tooltip"}, UiRegion.CARD) is UiRegion.TOOLTIP
    assert classify_region("div", {"popover": ""}, UiRegion.UNKNOWN) is UiRegion.POPOVER


@pytest.mark.parametrize(
    ("mime", "method", "fmt"),
    [
        ("text/html", "html_source", SourceFormat.HTML),
        ("application/pdf", "pymupdf/native", SourceFormat.PDF),
        ("image/png", "tesseract/ocr", SourceFormat.IMAGE),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docling",
            SourceFormat.OFFICE_DOC,
        ),
        ("text/csv", "x", SourceFormat.SPREADSHEET),
        (None, "pymupdf/tables", SourceFormat.PDF),
        (None, "pillow/metadata", SourceFormat.IMAGE),
        ("application/octet-stream", "x", SourceFormat.OTHER),
    ],
)
def test_source_format(mime: str | None, method: str, fmt: SourceFormat):
    assert source_format_for(mime, method) is fmt


def _block(method: str, **extra: object) -> ContentBlock:
    return ContentBlock(
        block_id="r:1",
        block_key="k",
        resource_id="r",
        state_id="run|source",
        region_id="r:1",
        reading_order=0,
        layer=ContentLayer.RENDERED_VISIBLE,
        raw_text="x",
        normalized_text="x",
        method=method,
        tool_version="t",
        confidence=1.0,
        status=EntityStatus.EXTRACTED_UNVERIFIED,
        evidence_ids=["e"],
        provenance=Provenance(),
        **extra,  # type: ignore[arg-type]
    )


def test_stamp_adds_source_url_dates_and_document_regions():
    modified = datetime(2026, 9, 1, tzinfo=UTC)
    res = Resource(
        resource_id="r",
        run_id="run",
        requested_url="https://x.example/a.pdf?utm_source=y",
        final_url="https://x.example/a.pdf",
        schedule_key="https://x.example/a.pdf",
        sniffed_mime="application/pdf",
        page_modified_at=modified,
        page_modified_source="pdf:ModDate",
    )
    when = datetime(2026, 10, 16, 9, tzinfo=UTC)
    stamped = stamp_blocks(
        [_block("pymupdf/native"), _block("pymupdf/tables"), _block("pymupdf/metadata")],
        res,
        extracted_at=when,
    )
    assert [b.ui_region for b in stamped] == [
        UiRegion.PDF_PAGE,
        UiRegion.PDF_TABLE,
        UiRegion.DOCUMENT_METADATA,
    ]
    for b in stamped:
        assert b.source_format is SourceFormat.PDF
        assert b.source_url == "https://x.example/a.pdf"
        assert b.extracted_at == when
        assert b.page_modified_at == modified and b.page_modified_source == "pdf:ModDate"


def test_stamp_keeps_values_already_set():
    earlier = datetime(2026, 1, 1, tzinfo=UTC)
    res = Resource(
        resource_id="r", run_id="run", requested_url="https://x.example/", schedule_key="k"
    )
    block = _block("html_source", ui_region=UiRegion.SNIPPET, extracted_at=earlier)
    (stamped,) = stamp_blocks([block], res)
    assert stamped.ui_region is UiRegion.SNIPPET and stamped.extracted_at == earlier
