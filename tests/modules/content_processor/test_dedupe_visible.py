"""Duplicate links point at the most visible copy, and never make visible text disappear."""

from __future__ import annotations

from src.modules.content_processor.adapters.html_source import extract_html_source
from src.modules.content_processor.enums import ContentLayer
from src.modules.content_processor.export.content import drop_duplicates, whole_content
from src.modules.content_processor.verify.record import corpus_blocks

# Live John Hancock shape: the hidden mobile menu comes before the visible desktop menu.
PAGE = b"""<html><body>
<div class="mobile-menu" data-cp-hidden="1"><ul>
<li><a href="/i">Individuals</a></li><li><a href="/f">Financial professionals</a></li></ul></div>
<nav><ul><li><a href="/i">Individuals</a></li>
<li><a href="/f">Financial professionals</a></li></ul></nav>
<main><p>The fund declared its monthly distribution.</p></main>
</body></html>"""


def _blocks():
    return extract_html_source(
        PAGE, base_url="https://x.example/", evidence_id="e", resource_id="r"
    ).blocks


def test_the_visible_copy_is_kept_and_the_hidden_copy_points_to_it() -> None:
    blocks = _blocks()
    by_layer = {(b.normalized_text, b.layer): b for b in blocks}
    visible = by_layer[("Individuals", ContentLayer.RENDERED_VISIBLE)]
    hidden = by_layer[("Individuals", ContentLayer.HIDDEN)]
    assert visible.dedupe_of is None
    assert hidden.dedupe_of == visible.block_id


def test_the_menu_is_in_the_content_after_hidden_layers_are_filtered_out() -> None:
    corpus = corpus_blocks(_blocks())  # what a URL record keeps: visible layers only
    text = whole_content(corpus)
    assert "Individuals" in text and "Financial professionals" in text


def test_a_dangling_duplicate_link_keeps_its_text() -> None:
    """Records written before this fix point visible blocks at hidden copies."""
    blocks = _blocks()
    hidden = next(b for b in blocks if b.layer is ContentLayer.HIDDEN)
    visible = next(
        b
        for b in blocks
        if b.layer is ContentLayer.RENDERED_VISIBLE and b.normalized_text == "Individuals"
    )
    old_style = visible.model_copy(update={"dedupe_of": hidden.block_id})
    assert drop_duplicates([old_style]) == [old_style]
    assert drop_duplicates([hidden.model_copy(update={"dedupe_of": None}), old_style]) == [
        hidden.model_copy(update={"dedupe_of": None})
    ]


def test_equal_visibility_keeps_the_first_copy() -> None:
    page = b"<html><body><main><p>Same line.</p><p>Same line.</p></main></body></html>"
    blocks = extract_html_source(
        page, base_url="https://x.example/", evidence_id="e", resource_id="r"
    ).blocks
    first, second = [b for b in blocks if b.normalized_text == "Same line."]
    assert first.dedupe_of is None and second.dedupe_of == first.block_id
    assert drop_duplicates(blocks).count(first) == 1 and second not in drop_duplicates(blocks)
