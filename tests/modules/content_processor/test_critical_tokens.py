"""V-4 / R9 critical tokens, the extraction fixes behind them, and responsive duplicates."""

from __future__ import annotations

import pytest

from src.modules.content_processor.adapters.html_source import extract_html_source, match_key
from src.modules.content_processor.enums import ContentLayer, EntityStatus, GapType, UrlBadge
from src.modules.content_processor.explore.model import ControlRecord, mark_responsive_duplicates
from src.modules.content_processor.verify.reconcile import reconcile_source_vs_rendered
from src.modules.content_processor.verify.rules import (
    R9_MAX_GAPS,
    critical_tokens,
    rule_r9_critical_tokens,
)
from tests.modules.content_processor.test_verify_record import _block, _record, _res

RUN = "run-v"


def _texts(html: bytes, state: str = "r|source") -> list[str]:
    blocks = extract_html_source(
        html, base_url="https://x/", evidence_id="e", resource_id="r", state_id=state
    ).blocks
    return [b.normalized_text for b in blocks if b.layer is ContentLayer.RENDERED_VISIBLE]


def _b(n: int, raw: str, normalized: str | None = None, **kw):
    block = _block(n, **kw)
    return block.model_copy(update={"raw_text": raw, "normalized_text": normalized or raw})


# -- extraction fixes ------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            b"<main><p>Annual fee $120<sup>1</sup> per year.</p></main>",
            ["Annual fee $120¹ per year."],
        ),
        (b"<main><p>Area 10 m<sup>2</sup></p></main>", ["Area 10 m²"]),
        (b"<main><p>CO<sub>2</sub> output</p></main>", ["CO₂ output"]),
        (b"<main><p>Brand<sup>TM</sup> rocks</p></main>", ["BrandTM rocks"]),
        (b"<main><p>Pay $1<span>,</span>200 now</p></main>", ["Pay $1,200 now"]),
        (
            b"<main><p>See <a href='/t'>our terms</a> for <b>details</b>.</p><p>Next</p></main>",
            ["See our terms for details.", "Next"],
        ),
    ],
)
def test_inline_markup_keeps_sentences_and_amounts_whole(html, expected):
    assert _texts(html) == expected


def test_block_elements_and_icons_still_separate_blocks():
    html = (
        b"<main><div>One<div>Two</div></div>"
        b'<p>It is <i class="material-icons">close</i> ok</p></main>'
    )
    blocks = extract_html_source(
        html, base_url="https://x/", evidence_id="e", resource_id="r", state_id="r|source"
    ).blocks
    assert [(b.layer.value, b.normalized_text) for b in blocks] == [
        ("rendered_visible", "One"),
        ("rendered_visible", "Two"),
        ("rendered_visible", "It is"),
        ("icon_artifact", "close"),
        ("rendered_visible", "ok"),
    ]


def test_superscript_blocks_still_verify_against_inner_text():
    # The browser's innerText reads "$1201"; the block keeps the marker as "$120¹".
    assert match_key("Fee $120¹") == match_key("FEE $1201")
    rendered = extract_html_source(
        b"<main><p>Annual fee $120<sup>1</sup> per year.</p></main>",
        base_url="https://x/",
        evidence_id="e",
        resource_id="r",
        state_id="r|rendered",
    ).blocks
    blocks, result = reconcile_source_vs_rendered([], rendered, "Annual fee $1201 per year.")
    assert result.fully_verified
    assert blocks[0].status is EntityStatus.VERIFIED_EXTRACTED


# -- R9 ----------------------------------------------------------------------------


def test_critical_tokens_counts_numbers_currency_negations_and_markers():
    found = critical_tokens("Not refundable: $1,200.50 or €3, 20% off*, won't apply [2] †")
    assert found == {
        "not": 1,
        "$": 1,
        "1,200.50": 1,
        "€": 1,
        "3": 1,
        "20": 1,
        "%": 1,
        "*": 1,
        "n't": 1,
        "2": 1,
        "[2]": 1,
        "†": 1,
    }
    assert critical_tokens("No plan includes none")["no"] == 1


def test_clean_blocks_and_allowed_normalization_pass():
    blocks = [
        _b(1, "Pay 1 200 EUR", "Pay 1 200 EUR"),
        _b(2, "Code 12­34", "Code 1234"),
        _b(3, "  not   covered  ", "not covered"),
        _b(4, "Fee $120¹", "Fee $120¹"),
    ]
    assert rule_r9_critical_tokens(RUN, _res(), blocks) == []


@pytest.mark.parametrize(
    ("raw", "normalized", "detail"),
    [
        ("This is not covered.", "This is covered.", "lost=not"),
        ("Fee $120¹", "Fee $1201", "lost=120¹;added=1201"),
        ("Price €40", "Price 40", "lost=€"),
        ("You can't cancel", "You can cancel", "lost=n't"),
        ("Rates apply*", "Rates apply", "lost=*"),
    ],
)
def test_damaged_tokens_are_a_fidelity_gap(raw, normalized, detail):
    (gap,) = rule_r9_critical_tokens(RUN, _res(), [_b(1, raw, normalized)])
    assert gap.gap_type is GapType.FIDELITY_DISAGREEMENT
    assert gap.status is EntityStatus.EXTRACTED_UNVERIFIED
    assert gap.region_id == "r1" and gap.evidence_ids == ["e"]
    assert gap.reason.startswith("critical_tokens_changed:")
    for part in detail.split(";"):
        kind, tokens = part.split("=")
        for token in tokens.split(","):
            assert token in gap.reason.split(f"{kind}=")[1], (token, gap.reason)


def test_damaged_fee_reason_is_exact():
    (gap,) = rule_r9_critical_tokens(RUN, _res(), [_b(1, "Fee $120¹", "Fee $1201")])
    assert gap.reason == "critical_tokens_changed:lost=120¹;added=1201"


def test_number_split_across_adjacent_blocks():
    blocks = [_b(1, "Pay $1,"), _b(2, "200 now"), _b(3, "Total 12."), _b(4, "Plain words")]
    (gap,) = rule_r9_critical_tokens(RUN, _res(), blocks)
    assert gap.reason == "number_split_across_blocks:Pay $1,|200 now"
    assert gap.region_id == "r1"
    # Not adjacent in reading order, or in another state: no gap.
    apart = [_b(1, "Pay $1,"), _b(3, "200 now")]
    other_state = [_b(1, "Pay $1,"), _b(2, "200 now", state="run|state:1")]
    assert rule_r9_critical_tokens(RUN, _res(), apart) == []
    assert rule_r9_critical_tokens(RUN, _res(), other_state) == []


def test_gap_ids_are_stable_and_capped():
    damaged = [_b(n, f"not {n}", f"{n}") for n in range(R9_MAX_GAPS + 5)]
    first = rule_r9_critical_tokens(RUN, _res(), damaged)
    again = rule_r9_critical_tokens(RUN, _res(), damaged)
    assert len(first) == R9_MAX_GAPS
    assert [g.gap_id for g in first] == [g.gap_id for g in again]
    assert len({g.gap_id for g in first}) == R9_MAX_GAPS
    duplicate = _b(1, "not x", "x").model_copy(update={"dedupe_of": "b0"})
    assert rule_r9_critical_tokens(RUN, _res(), [duplicate]) == []


def test_record_with_a_changed_amount_needs_review():
    clean = _record()
    assert clean.badge is UrlBadge.VERIFIED_WITHIN_SCOPE
    damaged = _record(blocks=[_block(1), _b(2, "Fee $120¹", "Fee $1201")])
    assert damaged.badge is UrlBadge.NEEDS_REVIEW
    assert [g.gap_type for g in damaged.gaps] == [GapType.FIDELITY_DISAGREEMENT]


# -- responsive duplicates ----------------------------------------------------------


def _ctrl(cid: str, kind: str, label: str, status: str, reason: str | None = None):
    return ControlRecord(control_id=cid, kind=kind, label=label, status=status, reason=reason)


def test_never_visible_twin_of_a_visited_control_is_excluded():
    controls = [
        _ctrl("t1", "tab", "Coverage  Details", "visited"),
        _ctrl("t2", "tab", "coverage details", "unvisited", "never_visible"),
        _ctrl("a1", "accordion", "FAQ", "initially_open"),
        _ctrl("a2", "accordion", "faq", "unvisited", "never_visible"),
    ]
    assert mark_responsive_duplicates(controls) == 2
    assert (controls[1].status, controls[1].reason) == ("excluded", "responsive_duplicate_of:t1")
    assert (controls[3].status, controls[3].reason) == ("excluded", "responsive_duplicate_of:a1")


@pytest.mark.parametrize(
    "twin",
    [
        _ctrl("x", "accordion", "Coverage details", "visited"),  # different kind
        _ctrl("x", "tab", "Other label", "visited"),  # different label
        _ctrl("x", "tab", "Coverage details", "unvisited", "click_failed:x"),  # twin not visited
        _ctrl("x", "tab", "Coverage details", "excluded", "third_party_chrome"),
    ],
)
def test_without_a_visited_twin_the_control_stays_a_gap(twin):
    hidden = _ctrl("t2", "tab", "Coverage details", "unvisited", "never_visible")
    assert mark_responsive_duplicates([twin, hidden]) == 0
    assert (hidden.status, hidden.reason) == ("unvisited", "never_visible")


def test_only_never_visible_controls_and_labelled_ones_are_deduplicated():
    controls = [
        _ctrl("t1", "tab", "Plans", "visited"),
        _ctrl("t2", "tab", "Plans", "unvisited", "click_failed:timeout"),
        _ctrl("e1", "tab", "", "visited"),
        _ctrl("e2", "tab", "  ", "unvisited", "never_visible"),
    ]
    assert mark_responsive_duplicates(controls) == 0
    assert controls[1].status == "unvisited" and controls[3].status == "unvisited"
