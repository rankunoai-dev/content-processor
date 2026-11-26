"""The keyword cannibalisation plan: cleaning, alignment, the eight patterns and link rules."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.modules.content_processor.knowledge.inventory_models import (
    HeadFacts,
    LinkItem,
    PageInventory,
)
from src.modules.gaps.build import build_findings
from src.modules.research.cannibal import build_plan, funnel_of, page_class
from src.modules.research.gsc import GscRow, GscSnapshot
from src.modules.research.models import KeywordRow, WebsiteResearch
from src.modules.research.page_models import PageReport
from src.modules.research.store import page_tables, plan_tables
from src.modules.theming.models import ThemeMap

NOW = datetime(2026, 11, 26, tzinfo=UTC)
S = "https://www.flynns.com"
BRANDS = ["flynns"]


def _theme(theme_id: str, terms: list[str], pillar: str | None) -> dict[str, object]:
    coverage = []
    if pillar:
        coverage = [
            {
                "website": "flynns.com",
                "pages": 1,
                "words": 100,
                "share": 1.0,
                "pillar": {"url": f"{S}{pillar}", "score": 1.0},
            }
        ]
    return {
        "theme_id": theme_id,
        "label": terms[0].title(),
        "terms": terms,
        "pages": 1,
        "coverage": coverage,
        "ownership": "client_owned",
        "ownership_reason": "",
    }


def _map() -> ThemeMap:
    pages = [
        ("/printers/", "th-print", "category"),
        ("/printers/office/", "th-office", "category"),
        ("/leasing/", "th-lease", "service"),
        ("/blog/printer-leasing-guide/", "th-guide", "article"),
        ("/blog/printer-leasing-tips/", "th-lease", "article"),
        ("/blog/printer-tips/", "th-tips", "article"),
        ("/solutions/print-security/", "th-sec", "service"),
        ("/product/c8100-series/", "th-print", "product"),
    ]
    return ThemeMap.model_validate(
        {
            "map_id": "themes-1",
            "project_id": "p1",
            "built_at": NOW,
            "method": "test",
            "websites": [],
            "themes": [
                _theme("th-print", ["printers", "printer"], "/printers/"),
                _theme("th-office", ["office printers", "office"], "/printers/office/"),
                _theme("th-lease", ["printer leasing", "leasing"], "/leasing/"),
                _theme("th-guide", ["leasing guide", "guide"], "/blog/printer-leasing-guide/"),
                _theme("th-tips", ["printer tips", "tips"], "/blog/printer-tips/"),
                _theme(
                    "th-sec", ["print security", "secure printing"], "/solutions/print-security/"
                ),
                _theme("th-mps", ["managed print"], None),
            ],
            "pages": [
                {"url": f"{S}{p}", "website": "flynns.com", "theme_id": t, "page_type": k}
                for p, t, k in pages
            ],
        }
    )


def _pair(query: str, page: str, impressions: int, position: float = 5, clicks: int = 3) -> GscRow:
    return GscRow(
        query=query, page=f"{S}{page}", clicks=clicks, impressions=impressions, position=position
    )


PAIRS = [
    _pair("printers", "/printers/", 100),
    _pair("office printers", "/printers/", 300),
    _pair("c8100 toner", "/printers/", 150),
    _pair("managed print services", "/printers/", 80),
    _pair("printers", "/printers/office/", 200),
    _pair("office printers", "/printers/office/", 100),
    _pair("printer leasing price", "/blog/printer-leasing-guide/", 500),
    _pair("printer tips for small offices", "/solutions/print-security/", 400),
    _pair("print security", "/solutions/print-security/", 50),
    _pair("leasing guide", "/blog/printer-leasing-tips/", 300),
    _pair("print security", "/author/jane/", 200),
    _pair("brooklyn printers", "/printers/locations/brooklyn/", 120),
    _pair("weather today", "/printers/", 90),
    _pair("flynns printers", "/printers/", 999),
    _pair("deep result", "/printers/", 70, position=35),
    *[
        _pair("printer near me", p, 100, clicks=0)
        for p in ("/printers/", "/leasing/", "/a/", "/b/")
    ],
]
SHARED = [
    f"Leasing paragraph number {i} explains terms, payments and the service plan." for i in range(5)
]
TEMPLATE = "Call us today for a free quote on printers and copiers in New York City."
PARAGRAPHS = {
    f"{S}/blog/printer-leasing-guide/": [
        *SHARED,
        "Ask about the printer leasing price for your office before you sign anything.",
        TEMPLATE,
    ],
    f"{S}/blog/printer-leasing-tips/": [
        *SHARED,
        "One more tip about leasing here today, short.",
        TEMPLATE,
    ],
    f"{S}/author/jane/": [
        "Jane writes about many things at the company every single week.",
        TEMPLATE,
    ],
    **{f"{S}/x{i}/": [TEMPLATE] for i in range(4)},
}


def _link(url: str, anchor: str, region: str = "main_content", alt: str | None = None) -> LinkItem:
    return LinkItem(
        url=url, anchor_text=anchor, image_alt=alt, kind="internal", ui_region=region,
        order=0, seen_in=["source"],
    )  # fmt: skip


def _page(path: str, links: list[LinkItem]) -> PageInventory:
    return PageInventory(
        run_id="r1",
        url=f"{S}{path}",
        page_key=path,
        inventoried_at=NOW,
        status="inventoried",
        head=HeadFacts(title=path),
        links=links,
    )


INVENTORY = [
    _page("/printers/", []),
    _page("/printers/office/", []),
    _page(
        "/leasing/",
        [
            _link(f"{S}/printers/office/", "Read more"),
            _link(f"{S}/printers/office/", "More", "footer"),
        ],
    ),
    _page(
        "/about/",
        [
            _link(f"{S}/printers/office/", "Read more"),
            _link(f"{S}/printers/office/", "office printer range"),
        ],
    ),
    _page("/blog/printer-leasing-guide/", [_link(f"{S}/printers/", "office printers")]),
    _page("/blog/printer-leasing-tips/", [_link(f"{S}/blog/printer-leasing-guide/", "", alt="")]),
    _page("/blog/printer-tips/", []),
    _page("/solutions/print-security/", []),
    _page("/product/c8100-series/", []),
    _page("/author/jane/", []),
    _page("/printers/locations/brooklyn/", []),
]


def _plan(gsc: GscSnapshot | None = None, site: WebsiteResearch | None = None):  # type: ignore[no-untyped-def]
    return build_plan(
        "flynns.com",
        site=site,
        gsc=gsc
        if gsc is not None or site is not None
        else GscSnapshot(project_id="p1", website="flynns.com", imported_at=NOW, pairs=PAIRS),
        theme_map=_map(),
        inventory=INVENTORY,
        paragraphs=PARAGRAPHS,
        brands=BRANDS,
        top_keyword={"flynns.com/printers/office": "office printers"},
    )


def test_funnel_and_page_class() -> None:
    assert funnel_of("printer leasing price") == "bofu"
    assert funnel_of("c8130 toner") == "bofu", "a model number is ready to buy"
    assert funnel_of("best office printers") == "mofu"
    assert funnel_of("how does leasing work for small companies today") == "tofu"
    assert page_class(f"{S}/author/jane/", "article") == "utility"
    assert page_class(f"{S}/blog/x/", None) == "article"
    assert page_class(f"{S}/printers/", "category") == "business"
    assert page_class(f"{S}/careers/", "careers") == "utility"


def test_cleaning_sets_aside_brand_automated_and_deep_searches() -> None:
    plan = _plan()
    assert plan.source == "search_console"
    assert plan.set_aside == {
        "branded": 999,
        "likely automated (0 clicks, 200+ impressions, 4+ pages)": 400,
        "beyond position 20": 70,
    }
    statuses = {(s.query, s.page.removeprefix(S)): s.status for s in plan.searches}
    assert statuses[("weather today", "/printers/")] == "unplaced"
    assert statuses[("managed print services", "/printers/")] == "no_page"
    assert statuses[("printers", "/printers/")] == "aligned"
    model = next(s for s in plan.searches if s.query == "c8100 toner")
    assert model.owner == f"{S}/product/c8100-series/" and model.funnel == "bofu"
    assert plan.template_paragraphs == 1


def test_alignment_scores_pages_against_the_90_percent_target() -> None:
    plan = _plan()
    by_url = {a.url.removeprefix(S): a for a in plan.alignment}
    printers = by_url["/printers/"]
    assert printers.status == "needs_work" and printers.share == pytest.approx(100 / 550, abs=1e-3)
    assert printers.misaligned_to == {
        f"{S}/printers/office/": 300,
        f"{S}/product/c8100-series/": 150,
    }
    assert plan.scored_pages == len(plan.alignment)


def test_each_competing_pair_gets_its_consolidation_pattern() -> None:
    plan = _plan()
    patterns = {(p.page.removeprefix(S), p.owner.removeprefix(S)): p.pattern for p in plan.pairs}
    assert patterns == {
        ("/printers/", "/printers/office/"): "hand_off_to_child",
        ("/printers/", "/product/c8100-series/"): "hand_off_to_child",
        ("/printers/office/", "/printers/"): "point_up_to_parent",
        ("/blog/printer-leasing-guide/", "/leasing/"): "separate_by_intent",
        ("/solutions/print-security/", "/blog/printer-tips/"): "answer_then_link",
        ("/blog/printer-leasing-tips/", "/blog/printer-leasing-guide/"): "merge_and_redirect",
        ("/author/jane/", "/solutions/print-security/"): "remove_from_index",
        ("/printers/locations/brooklyn/", "/printers/"): "localise",
    }
    assert [p.group for p in plan.pairs] == list(range(1, len(plan.pairs) + 1))
    assert plan.pairs[0].weight == max(p.weight for p in plan.pairs), "heaviest first"
    merge = next(p for p in plan.pairs if p.pattern == "merge_and_redirect")
    assert merge.shared_paragraphs == 5, "the template paragraph is not shared content"
    kinds = {(t.kind, t.url.removeprefix(S)) for t in plan.technical}
    assert kinds == {
        ("redirect_301", "/blog/printer-leasing-tips/"),
        ("remove_from_sitemap", "/blog/printer-leasing-tips/"),
        ("noindex_follow", "/author/jane/"),
        ("remove_from_sitemap", "/author/jane/"),
    }


def test_link_rules_repoint_fix_anchors_and_add_only_relevant_links() -> None:
    plan = _plan()
    changes = {
        (x.kind, x.source.removeprefix(S), x.destination.removeprefix(S)) for x in plan.links
    }
    assert ("repoint", "/blog/printer-leasing-guide/", "/printers/") in changes
    repoint = next(x for x in plan.links if x.kind == "repoint")
    assert repoint.new_destination == f"{S}/printers/office/"
    assert ("fix_anchor", "/leasing/", "/printers/office/") in changes, "generic anchor"
    assert not any(k == "fix_anchor" and s == "/about/" for k, s, _d in changes), (
        "a page with a descriptive link to the same page keeps its generic one"
    )
    assert ("fix_anchor", "/blog/printer-leasing-tips/", "/blog/printer-leasing-guide/") in changes
    assert ("add_link", "/blog/printer-leasing-guide/", "/leasing/") in changes
    assert not any(k == "add_link" and s == "/author/jane/" for k, s, _d in changes), (
        "no forced link from a page that never mentions the topic"
    )
    per_source: dict[str, int] = {}
    for x in plan.links:
        if x.kind == "add_link":
            per_source[x.source] = per_source.get(x.source, 0) + 1
    assert max(per_source.values()) <= 3


def test_sparse_search_console_pairs_fall_back_to_the_search_data() -> None:
    sparse = GscSnapshot(
        project_id="p1",
        website="flynns.com",
        imported_at=NOW,
        queries=[GscRow(query=f"q{i}", impressions=10) for i in range(10)],
        pairs=[_pair("printers", "/printers/office/", 200)],
    )
    site = WebsiteResearch(
        website="flynns.com",
        name="Flynns",
        role="client",
        source="csv_import",
        pulled_at=NOW,
        keywords=[
            KeywordRow(keyword="office printers", position=3, volume=2000, url=f"{S}/printers/"),
            KeywordRow(
                keyword="printers",
                position=2,
                volume=900,
                url=f"{S}/printers/",
                other_urls=[f"{S}/printers/office/"],
            ),  # fmt: skip
        ],
    )
    plan = _plan(gsc=sparse, site=site)
    assert plan.source == "search_data"
    assert {(s.query, s.page.removeprefix(S)) for s in plan.searches} == {
        ("office printers", "/printers/"),
        ("printers", "/printers/"),
        ("printers", "/printers/office/"),
    }
    assert (
        build_plan(
            "flynns.com",
            site=None,
            gsc=None,
            theme_map=None,
            inventory=[],
            paragraphs={},
            brands=[],
        ).source
        == "none"
    )


def test_plan_becomes_findings_and_workbook_sheets() -> None:
    plan = _plan()
    report = PageReport(project_id="p1", website="flynns.com", built_at=NOW, plan=plan)
    findings = [
        f
        for f in build_findings(_map(), {"flynns.com": "client"}, pages=report)
        if f.kind == "cannibalisation"
    ]
    assert len(findings) == len(plan.pairs)
    merge = next(f for f in findings if f.title.startswith("Merge and redirect"))
    assert "301 redirect" in merge.action and "redirect 301" in merge.action
    sheets = [t.name for t in plan_tables(report)]
    assert sheets == [
        "Plan summary", "Consolidation", "Link changes", "Technical", "Page alignment",
        "Search detail",
    ]  # fmt: skip
    assert [t.name for t in page_tables(report)][0] == "Pages"
    assert plan_tables(PageReport(project_id="p1", website="x", built_at=NOW)) == []
