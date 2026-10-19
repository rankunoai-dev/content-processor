"""Claim extraction for I4: spans, quantities, negation, context."""

from __future__ import annotations

from datetime import date

import pytest

from src.modules.content_processor.analysis.claims import (
    claims_from_records,
    split_sentences,
    terms_of,
)
from src.modules.content_processor.enums import ContentLayer
from tests.modules.content_processor.l5_factory import block, record

URL = "http://h/page"


def _claims(*texts: str, **kw):
    return claims_from_records([record(URL, list(texts))], **kw)


def test_sentence_spans_are_trimmed_and_keep_money_decimals():
    text = "  Price is $1.50 today.  Next one follows!\nThird line"
    spans = split_sentences(text)
    assert [text[s:e] for s, e in spans] == [
        "Price is $1.50 today.",
        "Next one follows!",
        "Third line",
    ]


def test_every_claim_is_a_verbatim_slice_of_its_block():
    rec = record(URL, ["The fee is $120 per year. It is not refundable. Hello there."])
    claims = claims_from_records([rec])
    assert [c.text for c in claims] == ["The fee is $120 per year.", "It is not refundable."]
    source = rec.blocks[0].normalized_text
    for claim in claims:
        assert source[claim.start : claim.end] == claim.text
        assert claim.block_id == rec.blocks[0].block_id
        assert claim.evidence_ids == tuple(rec.blocks[0].evidence_ids)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The plan costs $1,200 per year.", [(1200.0, "USD")]),
        ("The plan costs USD 99.50 per seat.", [(99.5, "USD")]),
        ("Delivery costs Rs. 49 for small orders.", [(49.0, "INR")]),
        ("Delivery costs ₹2,499 for this model.", [(2499.0, "INR")]),
        ("Returns are accepted within 30 days here.", [(30.0, "day")]),
        ("The discount is 15% for every member.", [(15.0, "percent")]),
        ("The battery gives a 30-hour runtime easily.", [(30.0, "hour")]),
    ],
)
def test_quantities_are_normalised(text, expected):
    (claim,) = _claims(text)
    assert [(q.value, q.unit) for q in claim.quantities] == expected


def test_a_year_is_not_a_duration_and_a_question_is_not_a_claim():
    (claim,) = _claims("This guide was updated in 2026 for readers.")
    assert claim.quantities == () and claim.years == frozenset({2026})
    assert _claims("Is the fee refundable after 30 days?") == []
    assert _claims("Hello there.") == []
    assert _claims("Our team loves building great things together.") == []


def test_negation_and_conditions():
    (claim,) = _claims("Refunds are not available after purchase.")
    assert claim.negated
    (cond,) = _claims("Free shipping applies only to orders above the threshold.")
    assert cond.context.conditions == ["only to orders above the threshold"]


def test_effective_dates_with_and_without_month_and_day():
    (a,) = _claims("Effective from 1 March 2026 the policy applies to every order.")
    assert a.context.effective_from == date(2026, 3, 1)
    (b,) = _claims("The offer is valid until February 2027 for all buyers.")
    assert b.context.effective_to == date(2027, 2, 28)
    (c,) = _claims("Prices hold through 2026 for everyone.")
    assert c.context.effective_to == date(2026, 12, 31)
    (d,) = _claims("Starting from March 31, 2026 we ship on weekends.")
    assert d.context.effective_from == date(2026, 3, 1)  # day 31 > 28 falls back to the month


def test_geography_audience_and_entity_from_headings():
    rec = record(
        URL,
        blocks=[
            block("res-page", 0, "Pricing", heading=["Pricing"]),
            block(
                "res-page",
                1,
                "New customers in the UK and the US pay $10 per month.",
                heading=["Pricing", "Starter"],
            ),
        ],
    )
    (claim,) = claims_from_records([rec])
    assert claim.context.geography == "GB, US"
    assert claim.context.audience == "new customers"
    assert claim.context.entity == "Pricing" and claim.context.product_or_variant == "Starter"


def test_only_corpus_blocks_are_used():
    rec = record(
        URL,
        blocks=[
            block("res-page", 0, "The fee is $10 per month in nav.", role="nav"),
            block("res-page", 1, "The fee is $10 per month hidden.", layer=ContentLayer.HIDDEN),
            block("res-page", 2, "The fee is $10 per month here."),
            block("res-page", 3, "The fee is $10 per month here.", dedupe_of="res-page:b2"),
        ],
    )
    assert [c.text for c in claims_from_records([rec])] == ["The fee is $10 per month here."]
    with_nav = claims_from_records([rec], include_boilerplate=True)
    assert [c.role for c in with_nav] == ["nav", "main"]


def test_terms_drop_stop_words_numbers_and_months():
    assert terms_of("The 30 Refunds were NOT available in March") == frozenset(
        {"refund", "available"}
    )


def test_claim_keys_are_stable_and_ids_unique():
    first = _claims("The fee is $10 per month. The fee is $10 per month.")
    again = _claims("The fee is $10 per month. The fee is $10 per month.")
    assert [c.claim_key for c in first] == [c.claim_key for c in again]
    assert len({c.claim_id for c in first}) == 2
