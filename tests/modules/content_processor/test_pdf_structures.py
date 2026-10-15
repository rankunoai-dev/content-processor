"""PDF structures: forms, annotations, attachments, layers, ghost text, table continuation."""

from __future__ import annotations

from src.modules.content_processor.adapters.pdf import extract_pdf
from src.modules.content_processor.enums import ContentLayer
from tests.modules.content_processor.pdf_builders import (
    form_annot_pdf,
    ghost_text_pdf,
    two_page_table,
)


def _extract(data: bytes):  # type: ignore[no-untyped-def]
    return extract_pdf(data, resource_id="r", run_id="run", evidence_id="e")


def _tables(extraction):  # type: ignore[no-untyped-def]
    return [b for b in extraction.blocks if b.method == "pymupdf/tables"]


def test_form_field_value_is_labelled_with_its_name() -> None:
    forms = [b for b in _extract(form_annot_pdf()).blocks if b.method == "pymupdf/forms"]
    assert [b.raw_text for b in forms] == ["email: a@b.com"]


def test_annotation_comment_is_kept_in_its_own_layer() -> None:
    notes = [b for b in _extract(form_annot_pdf()).blocks if b.method == "pymupdf/annotations"]
    assert len(notes) == 1
    assert notes[0].layer is ContentLayer.COLLAPSED_PUBLISHED
    assert "check clause 9" in notes[0].raw_text


def test_attachment_and_hidden_layer_are_reported() -> None:
    result = _extract(form_annot_pdf())
    assert [(a.name, a.data) for a in result.attachments] == [("data.csv", b"a,b\n1,2")]
    assert "embedded_files:1" in result.warnings
    assert "hidden_layers:Draft layer" in result.warnings


def test_invisible_text_goes_to_hidden_layer_not_body() -> None:
    result = _extract(ghost_text_pdf())
    by_text = {b.raw_text: b for b in result.blocks}
    assert by_text["visible heading text here"].layer is ContentLayer.RENDERED_VISIBLE
    assert by_text["ghost words nobody can see"].layer is ContentLayer.HIDDEN
    assert any(w.startswith("invisible_text_layer") for w in result.warnings)


def test_table_continues_across_pages_and_repeated_header_is_dropped() -> None:
    result = _extract(two_page_table(repeat_header=True))
    tables = _tables(result)
    assert len(tables) == 1
    rows = tables[0].raw_text.splitlines()
    assert rows[0] == "| Item | Qty | Price |"
    assert rows.count("| Item | Qty | Price |") == 1
    assert [r for r in rows if "fig" in r or "kiwi" in r] == [
        "| fig | 4 | 40 |",
        "| kiwi | 5 | 50 |",
    ]
    assert "table_continued:p0-p1" in result.warnings
    assert "page_1_table_0" in (tables[0].provenance.pdf_object_id or "")


def test_continuation_without_repeated_header_keeps_every_row() -> None:
    tables = _tables(_extract(two_page_table(repeat_header=False)))
    assert len(tables) == 1
    assert "| fig | 4 | 40 |" in tables[0].raw_text


def test_tables_separated_by_another_page_are_not_merged() -> None:
    result = _extract(two_page_table(gap_page=True))
    assert len(_tables(result)) == 2
    assert not any(w.startswith("table_continued") for w in result.warnings)
