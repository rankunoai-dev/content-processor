"""Autonomous URL grouping (Bit 1): fixture pages and edge cases."""

from __future__ import annotations

import random
import re
from pathlib import Path

from src.modules.content_processor.grouping import PageSignal, group_urls
from src.modules.content_processor.grouping.gate import assess
from src.modules.content_processor.grouping.prefix import path_family

SERVICES = Path(__file__).resolve().parents[3] / "fixtures" / "services"
BASE = "http://localhost:8765"


def _page(url: str, body: str, status: int = 200, media: str = "text/html") -> PageSignal:
    return PageSignal(url=url, http_status=status, media_type=media, body=body)


def _fixture(name: str) -> str:
    return (SERVICES / f"{name}.html").read_text(encoding="utf-8")


SERVICE_SLUGS = ["wealth-planning", "retirement-income", "estate-planning", "tax-strategies"]


def _service_pages() -> list[PageSignal]:
    return [_page(f"{BASE}/services/{s}", _fixture(s)) for s in SERVICE_SLUGS]


# -- path families -----------------------------------------------------------------


def test_family_respects_segment_boundaries() -> None:
    assert path_family(f"{BASE}/services/tax") == "/services"
    assert path_family(f"{BASE}/services-archive/legacy-plan") == "/services-archive"
    assert path_family(f"{BASE}/") == "/"


# -- fixture pages --------------------------------------------------------------------


def test_interlinked_service_pages_form_one_template_group() -> None:
    result = group_urls(_service_pages())
    assert result.excluded == []
    assert len(result.groups) == 1
    group = result.groups[0]
    assert group.reason == "template_match"
    assert group.path_prefix == "/services"
    assert sorted(group.members) == sorted(p.url for p in _service_pages())
    assert group.template_fingerprint is not None
    assert group.confidence >= 0.85


def test_prefix_collision_archive_page_is_not_merged_into_services() -> None:
    pages = [
        *_service_pages(),
        _page(f"{BASE}/services-archive/legacy-plan", _fixture("archive/legacy-plan")),
    ]
    result = group_urls(pages)
    archive = [g for g in result.groups if g.path_prefix == "/services-archive"]
    assert len(archive) == 1 and archive[0].reason == "singleton"
    services = [g for g in result.groups if g.path_prefix == "/services"]
    assert len(services) == 1 and f"{BASE}/services-archive/legacy-plan" not in services[0].members


def test_blocked_error_page_is_excluded_not_grouped() -> None:
    blocked = _fixture("blocked")
    pages = [*_service_pages(), _page(f"{BASE}/services-blocked", blocked, status=403)]
    result = group_urls(pages)
    assert [(e.url, e.reason) for e in result.excluded] == [
        (f"{BASE}/services-blocked", "http_403")
    ]
    assert all(f"{BASE}/services-blocked" not in g.members for g in result.groups)


def test_block_page_served_with_200_is_still_excluded() -> None:
    blocked = _fixture("blocked")
    verdict = assess(_page(f"{BASE}/x", blocked, status=200))
    assert (verdict.kind, verdict.reason) == ("excluded", "challenge_or_block_page")


# -- edge cases -------------------------------------------------------------------------


def test_different_template_in_same_family_stays_separate() -> None:
    table_layout = (
        "<html><body><table><tr><td>Plan</td><td>Fee</td></tr>"
        "<tr><td>Basic</td><td>0</td></tr></table></body></html>"
    )
    pages = [*_service_pages(), _page(f"{BASE}/services/fees", table_layout)]
    result = group_urls(pages)
    fees = [g for g in result.groups if f"{BASE}/services/fees" in g.members]
    assert fees and fees[0].reason == "singleton"
    assert len(result.groups) == 2


def test_client_rendered_shell_is_flagged_not_fingerprinted() -> None:
    shell = '<html><body><div id="app"></div><script src="/app.js"></script></body></html>'
    shells = [_page(f"{BASE}/services/a", shell), _page(f"{BASE}/services/b", shell)]
    result = group_urls(shells)
    assert len(result.groups) == 1
    assert result.groups[0].reason == "spa_shell"
    assert result.groups[0].template_fingerprint is None


def test_non_html_and_empty_responses_are_excluded_with_reason() -> None:
    pages = [
        _page(f"{BASE}/services/pdf", "%PDF-1.4", media="application/pdf"),
        _page(f"{BASE}/services/empty", "   "),
        _page(f"{BASE}/services/none", "", status=0),
    ]
    reasons = {e.url: e.reason for e in group_urls(pages).excluded}
    assert reasons[f"{BASE}/services/pdf"] == "not_html:application/pdf"
    assert reasons[f"{BASE}/services/empty"] == "empty_body"
    assert f"{BASE}/services/none" in reasons


def test_grouping_is_deterministic_under_input_order() -> None:
    pages = [
        *_service_pages(),
        _page(f"{BASE}/services-archive/legacy-plan", _fixture("archive/legacy-plan")),
    ]
    expected = group_urls(pages).model_dump()
    for seed in range(5):
        shuffled = pages[:]
        random.Random(seed).shuffle(shuffled)  # noqa: S311 - test shuffle, not security
        assert group_urls(shuffled).model_dump() == expected


def test_threshold_controls_merging() -> None:
    # One extra aside on an otherwise identical template: close enough to merge at the
    # default threshold, different enough to split at 1.0.
    base = _fixture("wealth-planning")
    variant = base.replace("<footer>", "<aside><p>Promo</p></aside><footer>")
    pages = [
        _page(f"{BASE}/services/wealth-planning", base),
        _page(f"{BASE}/services/variant", variant),
    ]
    loose = group_urls(pages)
    assert [g.reason for g in loose.groups] == ["template_match"]
    strict = group_urls(pages, similarity_threshold=1.0)
    assert [g.reason for g in strict.groups] == ["singleton", "singleton"]


def test_reordered_sections_of_one_template_still_group() -> None:
    # Swapping the main content and the footer keeps the same elements, so the ordered
    # similarity stays above the default threshold; one template, reordered, is still one template.
    base = _fixture("wealth-planning")
    main = re.search(r"<main>.*</main>", base, re.S).group(0)
    footer = re.search(r"<footer>.*</footer>", base, re.S).group(0)
    swapped = base.replace(main, "@@MAIN@@").replace(footer, main).replace("@@MAIN@@", footer)
    pages = [_page(f"{BASE}/services/a", base), _page(f"{BASE}/services/b", swapped)]
    assert [g.reason for g in group_urls(pages).groups] == ["template_match"]


def test_markup_heavy_article_is_a_template_page_not_a_shell() -> None:
    # Real article pages can carry much more markup than text. A ratio rule misread them as
    # shells; an absolute word count does not.
    words = " ".join(["client content"] * 80)
    heavy_markup = "<div>" * 300 + f"<p>{words}</p>" + "</div>" * 300
    article = f"<html><body>{heavy_markup}<script src='/x.js'></script></body></html>"
    verdict = assess(_page(f"{BASE}/services/article", article))
    assert verdict.kind == "template"
