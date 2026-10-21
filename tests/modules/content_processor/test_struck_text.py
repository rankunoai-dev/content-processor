"""Struck-out text (<del>, <s>, <strike>) is kept and marked: "Price ~~$200~~ $150"."""

from __future__ import annotations

import pytest

from src.modules.content_processor.adapters.html_source import (
    STRUCK_MARKER,
    extract_html_source,
    match_key,
    without_struck_markers,
    without_struck_text,
)
from src.modules.content_processor.analysis.contradictions import find_contradictions
from src.modules.content_processor.enums import ContentLayer, EntityStatus
from src.modules.content_processor.verify.reconcile import reconcile_source_vs_rendered
from src.modules.content_processor.verify.rules_extended import _norm
from tests.modules.content_processor.l5_factory import record


def _texts(html: bytes, state: str = "r|source") -> list[str]:
    blocks = extract_html_source(
        html, base_url="https://x/", evidence_id="e", resource_id="r", state_id=state
    ).blocks
    return [b.normalized_text for b in blocks if b.layer is ContentLayer.RENDERED_VISIBLE]


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            b"<main><p>Price <del>$200</del> $150 per year.</p></main>",
            "Price ~~$200~~ $150 per year.",
        ),
        (b"<main><p>Was <s> $99 </s>now $79</p></main>", "Was ~~$99~~ now $79"),
        (b"<main><p>Old <STRIKE>terms</STRIKE> and new.</p></main>", "Old ~~terms~~ and new."),
        (b"<main><p>Plan <del>$1<sup>1</sup></del> now</p></main>", "Plan ~~$1¹~~ now"),
        (b"<main><p>Fee <del><b>$5</b> monthly</del> free</p></main>", "Fee ~~$5 monthly~~ free"),
        (b"<main><p>Empty <del> </del>ok</p></main>", "Empty ok"),
    ],
    ids=["del", "s-with-spaces", "strike", "superscript-inside", "nested-inline", "empty"],
)
def test_struck_text_is_kept_and_marked(html: bytes, expected: str):
    assert _texts(html) == [expected]


def test_helpers():
    text = "Price ~~$200~~ $150, was ~~$180~~."
    assert without_struck_markers(text) == "Price $200 $150, was $180."
    assert without_struck_text(text) == "Price $150, was ."
    assert STRUCK_MARKER == "~~"
    assert match_key("Price ~~$200~~ $150") == match_key("PRICE $200 $150")
    assert _norm("Price ~~$200~~ $150") == match_key("Price $200 $150"), "R7/R13 compare alike"


def test_marked_block_still_verifies_against_the_visible_text():
    rendered = extract_html_source(
        b"<main><p>Price <del>$200</del> $150 per year.</p></main>",
        base_url="https://x/",
        evidence_id="e",
        resource_id="r",
        state_id="r|rendered",
    ).blocks
    # The browser's innerText keeps the struck words and has no markers.
    blocks, result = reconcile_source_vs_rendered([], rendered, "Price $200 $150 per year.")
    assert result.fully_verified
    assert blocks[0].status is EntityStatus.VERIFIED_EXTRACTED
    assert blocks[0].normalized_text == "Price ~~$200~~ $150 per year."


def test_a_struck_old_price_is_not_a_contradiction():
    current = record("http://h/pricing", ["The annual plan costs $150 per year."])
    struck = record("http://h/plans", ["The annual plan costs ~~$200~~ $150 per year."])
    assert find_contradictions([current, struck]).pairs == []
    # Without the strike-through the same two amounts are a real price disagreement.
    unstruck = record("http://h/plans", ["The annual plan costs $200 per year."])
    pairs = find_contradictions([current, unstruck]).pairs
    assert [" ".join(p.flags) for p in pairs] == ["numeric:USD:$200!=$150"]


def test_struck_negation_does_not_negate_the_claim():
    plain = record("http://h/a", ["Refunds are available within 30 days."])
    struck = record("http://h/b", ["Refunds are ~~not~~ available within 30 days."])
    assert find_contradictions([plain, struck]).pairs == []
