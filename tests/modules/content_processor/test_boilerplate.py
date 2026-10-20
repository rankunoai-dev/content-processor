"""Template-aware boilerplate reuse (Bit 3)."""

from __future__ import annotations

import pytest

from src.modules.content_processor.grouping.boilerplate import (
    ReuseCache,
    find_shared_blocks,
    text_key,
)

NAV = "Home Products Services About us Contact support and help center"
FOOTER = "Services are offered through licensed professionals. Terms apply."


def _pages(n: int, extra_shared: list[str] | None = None) -> dict[str, list[str]]:
    out = {}
    for i in range(n):
        blocks = [NAV, FOOTER, f"Unique article body number {i} with its own wording here."]
        blocks.extend(extra_shared or [])
        out[f"https://h/services/p{i}"] = blocks
    return out


def test_navigation_and_footer_are_shared_and_unique_text_is_not() -> None:
    report = find_shared_blocks("grp-1", _pages(4))
    sampled = {b.sample for b in report.shared_blocks}
    assert NAV in sampled and FOOTER in sampled
    assert all("Unique article body" not in b.sample for b in report.shared_blocks)
    assert report.pages == 4
    assert all(b.share == 1.0 for b in report.shared_blocks)


def test_reuse_accounting_matches_the_blocks_each_page_reuses() -> None:
    report = find_shared_blocks("grp-1", _pages(4))
    # 4 pages x 3 blocks = 12 occurrences; 8 are shared nav/footer, 4 unique
    assert report.total_units == 12
    assert report.reused_units == 8
    assert report.unit_reuse_ratio == pytest.approx(8 / 12)
    assert all(len(keys) == 2 for keys in report.reused_by_page.values())


def test_share_threshold_controls_which_blocks_count_as_shared() -> None:
    pages = _pages(5)
    # The footer appears on only 3 of 5 pages.
    for url in list(pages)[:2]:
        pages[url] = [b for b in pages[url] if b != FOOTER]
    strict = find_shared_blocks("g", pages, min_share=0.8)
    assert {b.sample for b in strict.shared_blocks} == {NAV}
    loose = find_shared_blocks("g", pages, min_share=0.6)
    assert FOOTER in {b.sample for b in loose.shared_blocks}


def test_near_identical_disclaimer_is_not_shared() -> None:
    pages = _pages(3)
    for i, url in enumerate(list(pages)):
        pages[url] = pages[url] + [f"Rates are effective as of March {i + 1}, 2026 and may change."]
    report = find_shared_blocks("g", pages)
    assert all("effective as of" not in b.sample for b in report.shared_blocks)


def test_short_strings_are_ignored_even_when_repeated() -> None:
    pages = {f"https://h/{i}": ["Home", "Next", "More"] for i in range(4)}
    report = find_shared_blocks("g", pages)
    assert report.shared_blocks == []
    assert report.total_units == 0


def test_single_page_group_shares_nothing() -> None:
    report = find_shared_blocks("g", _pages(1))
    assert report.shared_blocks == []
    assert report.reused_units == 0


def test_text_keys_are_stable_and_distinct() -> None:
    assert text_key(NAV) == text_key(NAV)
    assert text_key(NAV) != text_key(FOOTER)


def test_reuse_cache_computes_each_distinct_text_once() -> None:
    cache = ReuseCache()
    calls: list[str] = []

    def verify(text: str) -> int:
        calls.append(text)
        return len(text)

    results = [cache.get_or_compute(t, verify) for t in [NAV, FOOTER, NAV, NAV, FOOTER]]
    assert results == [len(NAV), len(FOOTER), len(NAV), len(NAV), len(FOOTER)]
    assert calls == [NAV, FOOTER]
    assert (cache.computed, cache.reused) == (2, 3)
