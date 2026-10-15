"""Programmatic PDF builders shared by the document tests."""

from __future__ import annotations

from typing import Any

import pymupdf

PAGE_H = 842


def grid_table(page: Any, top: float, rows: list[list[str]], *, col_w: float = 120) -> float:
    """Draw a ruled table starting at `top`; return its bottom y."""
    row_h = 24
    left = 60
    width = col_w * len(rows[0])
    for r in range(len(rows) + 1):
        y = top + r * row_h
        page.draw_line((left, y), (left + width, y))
    for c in range(len(rows[0]) + 1):
        x = left + c * col_w
        page.draw_line((x, top), (x, top + len(rows) * row_h))
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page.insert_text((left + c * col_w + 4, top + r * row_h + 16), cell, fontsize=10)
    return top + len(rows) * row_h


def two_page_table(*, repeat_header: bool = True, gap_page: bool = False) -> bytes:
    """A table that starts at the bottom of page 1 and continues at the top of page 2."""
    header = ["Item", "Qty", "Price"]
    doc: Any = pymupdf.open()
    p1 = doc.new_page()
    grid_table(p1, 640, [header, ["apple", "1", "10"], ["pear", "2", "20"], ["plum", "3", "30"]])
    if gap_page:
        doc.new_page().insert_text((72, 100), "unrelated page", fontsize=12)
    p2 = doc.new_page()
    rows2 = [["fig", "4", "40"], ["kiwi", "5", "50"]]
    grid_table(p2, 50, ([header] if repeat_header else []) + rows2)
    out: bytes = doc.tobytes()
    doc.close()
    return out


def form_annot_pdf() -> bytes:
    """Page with a filled form field, a sticky-note comment, an attachment and a hidden layer."""
    doc: Any = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Application form", fontsize=14)
    widget = pymupdf.Widget()
    widget.field_name = "email"
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.rect = pymupdf.Rect(72, 300, 300, 320)
    widget.field_value = "a@b.com"
    page.add_widget(widget)
    page.add_text_annot((400, 100), "reviewer says check clause 9")
    doc.embfile_add("data.csv", b"a,b\n1,2", desc="supporting data")
    doc.add_ocg("Draft layer", on=False)
    out: bytes = doc.tobytes()
    doc.close()
    return out


def ghost_text_pdf() -> bytes:
    """Page with painted text plus an invisible (render mode 3) layer, like a stale OCR layer."""
    doc: Any = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "visible heading text here", fontsize=12)
    page.insert_text((72, 300), "ghost words nobody can see", fontsize=12, render_mode=3)
    out: bytes = doc.tobytes()
    doc.close()
    return out
