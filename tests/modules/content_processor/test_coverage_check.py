"""Coverage: every line the browser shows must be in the extraction."""

from __future__ import annotations

from src.modules.content_processor.contracts import ContentBlock
from src.modules.content_processor.enums import ContentLayer, EntityStatus
from src.modules.content_processor.workers.render_worker import missing_visible_lines


def _b(n: int, text: str, layer: ContentLayer = ContentLayer.RENDERED_VISIBLE) -> ContentBlock:
    return ContentBlock(
        block_id=f"r:rendered:{n}",
        block_key=f"k{n}",
        resource_id="r",
        state_id="run|rendered",
        region_id=f"r{n}",
        reading_order=n,
        layer=layer,
        raw_text=text,
        normalized_text=text,
        method="t",
        tool_version="t",
        confidence=1.0,
        status=EntityStatus.VERIFIED_EXTRACTED,
        evidence_ids=["e"],
    )


def test_all_lines_covered_including_one_split_across_blocks() -> None:
    blocks = [
        _b(0, "Fund news"),
        _b(1, "Posted :"),
        _b(2, "March 1, 2023"),
        _b(3, "The fund pays $120¹ a year."),
    ]
    text = "Fund news\nPosted : March 1, 2023\nThe fund pays $1201 a year.\nOK"
    assert missing_visible_lines([(text, blocks)]) == []


def test_a_line_found_only_in_a_hidden_block_is_missing() -> None:
    blocks = [
        _b(0, "Fund news"),
        _b(1, "Closed-end funds release earnings data", ContentLayer.HIDDEN),
    ]
    text = "Fund news\nClosed-end funds release earnings data"
    assert missing_visible_lines([(text, blocks)]) == ["Closed-end funds release earnings data"]


def test_short_lines_and_repeats_are_not_judged_and_chrome_layers_count() -> None:
    blocks = [_b(0, "Chat with our virtual assistant", ContentLayer.THIRD_PARTY_CHROME)]
    text = "Menu\nSign in\nChat with our virtual assistant\nChat with our virtual assistant"
    assert missing_visible_lines([(text, blocks)]) == []


def test_struck_text_marks_are_ignored() -> None:
    blocks = [_b(0, "Price ~~$200~~ $150 per year")]
    assert missing_visible_lines([("Price $200 $150 per year", blocks)]) == []


def test_attribute_text_between_a_paragraph_and_its_link_does_not_break_the_line() -> None:
    """Live OneTrust banner: the link's aria-label is emitted between the two blocks."""
    blocks = [
        _b(0, "Cookies help us understand how aspects of our website function."),
        _b(1, "Privacy policy, opens in a new tab", ContentLayer.ACCESSIBILITY_ONLY),
        _b(2, "Privacy policy (opens in a new tab)"),
    ]
    line = (
        "Cookies help us understand how aspects of our website function. "
        "Privacy policy (opens in a new tab)"
    )
    assert missing_visible_lines([(line, blocks)]) == []


def test_each_snapshot_is_matched_with_its_own_full_extraction() -> None:
    """A card line spanning text first seen in the base page and text revealed by a state."""
    base = [_b(0, "October 8, 2026")]
    state = [
        _b(0, "John Hancock Investment Management"),
        _b(1, "October 8, 2026"),
        _b(2, "Funds release earnings data"),
    ]
    line = "John Hancock Investment ManagementOctober 8, 2026Funds release earnings data"
    assert missing_visible_lines([("October 8, 2026", base), (line, state)]) == []
