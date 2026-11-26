"""Research insights: intent, Search Console, the page report, links, and their gap findings."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.modules.content_processor import cli
from src.modules.content_processor.knowledge.inventory_models import (
    HeadFacts,
    Issue,
    LinkItem,
    PageInventory,
)
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.webapi.app import create_app
from src.modules.gaps.build import build_findings, priority
from src.modules.research.build import finish_snapshot
from src.modules.research.gsc import (
    GscError,
    GscRow,
    GscSnapshot,
    ctr_opportunities,
    expected_ctr,
    read_gsc_export,
)
from src.modules.research.importer import read_positions_csv
from src.modules.research.intent import brand_terms, intent_of, weighted
from src.modules.research.links import build_graph, page_key, pagerank
from src.modules.research.models import KeywordRow, ResearchSnapshot, WebsiteResearch
from src.modules.research.pages import build_page_report
from src.modules.research.service import import_csv, research_store
from src.modules.research.store import page_tables
from src.modules.theming.models import ThemeMap
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.research.test_research import CLIENT_EXPORT, WRITE
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture

NOW = datetime(2026, 11, 26, tzinfo=UTC)
SITE = "https://www.example.com"

# -- intent ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keyword", "intent"),
    [
        ("term life insurance", "commercial"),
        ("best life insurance companies", "commercial"),
        ("procurement software", "commercial"),
        ("life insurance quotes", "transactional"),
        ("long term care insurance cost", "transactional"),
        ("what is an annuity", "informational"),
        ("how much life insurance do i need for a family of four", "informational"),
        ("example login", "navigational"),
        ("example life insurance", "navigational"),
        ("rival", "navigational"),
        ("", "informational"),
    ],
)
def test_intent_rules(keyword: str, intent: str) -> None:
    assert intent_of(keyword, brand_terms(["example.com", "Rival Corp", "www.rival.com"])) == intent


def test_demand_per_theme_is_split_and_weighted_by_intent() -> None:
    rows = [
        KeywordRow(keyword="best term life insurance", position=3, volume=1000),
        KeywordRow(keyword="what is term life insurance", position=5, volume=1000),
        KeywordRow(keyword="what is term life insurance", position=9, volume=1000),
    ]
    snap = ResearchSnapshot(
        snapshot_id="research-20261126-000000-000",
        project_id="p1",
        created_at=NOW,
        database="us",
        websites=[
            WebsiteResearch(
                website="example.com",
                name="Example",
                role="client",
                source="csv_import",
                pulled_at=NOW,
                keywords=rows,
            )
        ],
    )
    finish_snapshot(snap, None)
    assert [r.intent for r in rows] == ["commercial", "informational", "informational"]
    (theme,) = snap.demand
    assert theme.by_intent == {"commercial": 1000, "informational": 1000}  # distinct keywords
    assert theme.weighted_volume == weighted(theme.by_intent) == 1500


def test_an_export_with_two_pages_for_one_keyword_keeps_both() -> None:
    rows = read_positions_csv(
        (
            "Keyword,Position,Search Volume,URL\n"
            f"term life,9,1000,{SITE}/a\n"
            f"term life,4,1000,{SITE}/b\n"
            f"term life,4,1000,{SITE}/b\n"
            f"term life,30,1000,{SITE}/c\n"
        ).encode()
    )
    (row,) = rows
    assert (row.position, row.url, row.other_urls) == (4, f"{SITE}/b", [f"{SITE}/a", f"{SITE}/c"])


# -- Search Console ----------------------------------------------------------------------------


def _gsc_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "Queries.csv",
            "Top queries,Clicks,Impressions,CTR,Position\n"
            "term life,10,2000,0.5%,2.0\n"
            "annuity,200,1000,20%,1.2\n"
            "rare,1,50,2%,3\n",
        )
        archive.writestr(
            "Pages.csv",
            "Top pages,Clicks,Impressions,CTR,Position\n"
            f"{SITE}/life/term,12,3000,0.40%,3.1\n"
            f"{SITE}/retirement/annuities,300,1500,20.00%,1.1\n",
        )
        archive.writestr("Filters.csv", "Filter,Value\nSearch type,Web\nDate,Last 3 months\n")
        archive.writestr(
            "Queries by page.csv",
            "Query,Page,Clicks,Impressions,CTR,Position\n"
            f"term life,{SITE}/life/term,8,1200,0.7%,2.0\n"
            f"term life,{SITE}/blog/term,2,800,0.3%,6.0\n",
        )
    return buffer.getvalue()


def test_search_console_export_and_ctr_opportunities() -> None:
    snap = read_gsc_export(_gsc_zip(), project_id="p1", website="example.com", now=NOW)
    assert snap.period == "Last 3 months"
    assert (len(snap.queries), len(snap.pages), len(snap.pairs)) == (3, 2, 2)
    assert snap.queries[0].ctr == pytest.approx(0.005)
    found = ctr_opportunities(snap)
    assert [(o.kind, o.label) for o in found] == [
        ("query", "term life"),
        ("page", f"{SITE}/life/term"),
    ], "annuity's CTR is above usual; 'rare' has too few impressions"
    assert found[0].extra_clicks == round(2000 * (expected_ctr(2.0) - 0.005))
    assert expected_ctr(1) == 0.28 and 0.15 > expected_ctr(2.5) > 0.10 and expected_ctr(15) == 0.01
    one_csv = read_gsc_export(
        b"Top pages;Clicks;Impressions;CTR;Position\nhttps://x.com/;1;10;10%;1\n",
        project_id="p1",
        website="x.com",
        now=NOW,
    )
    assert one_csv.pages[0].ctr == pytest.approx(0.1)
    with pytest.raises(GscError):
        read_gsc_export(b"a,b\n1,2\n", project_id="p1", website="x.com", now=NOW)
    with pytest.raises(GscError):
        read_gsc_export(b"PK not a zip", project_id="p1", website="x.com", now=NOW)


# -- links and the page report -------------------------------------------------------------------


def _link(url: str, region: str, order: int) -> LinkItem:
    return LinkItem(url=url, kind="internal", ui_region=region, order=order, seen_in=["source"])


def _page(path: str, links: list[LinkItem], issues: tuple[str, ...] = ()) -> PageInventory:
    return PageInventory(
        run_id="run-1",
        url=f"{SITE}{path}",
        page_key=path,
        inventoried_at=NOW,
        status="inventoried",
        head=HeadFacts(title=f"Title {path}"),
        links=links,
        issues=[Issue(code=c, severity="warning", message=c) for c in issues],
    )


def _site() -> list[PageInventory]:
    hub = [_link(f"{SITE}/a", "main_content", i) for i in range(1)] + [
        _link(f"{SITE}/b", "card", 2),
        _link(f"{SITE}/c", "footer", 3),
    ]
    return [
        _page("/", [_link(f"{SITE}/hub", "main_content", 0), _link(f"{SITE}/c", "nav_menu", 1)]),
        _page("/hub", hub),
        _page("/a", [_link(f"{SITE}/b", "main_content", 0)], ("h1_multiple",)),
        _page("/b", [_link(f"{SITE}/hub/", "main_content", 0)]),
        _page("/c", [], ("description_missing", "title_too_long")),
        _page("/d", []),
    ]


def test_link_graph_counts_page_text_only() -> None:
    graph = build_graph(_site())
    assert page_key("http://EXAMPLE.com/hub/?x=1#top") == "example.com/hub"
    c = page_key(f"{SITE}/c")
    assert not graph.body_in.get(c) and len(graph.chrome_in[c]) == 2  # nav and footer only
    assert graph.orphans() == [f"{SITE}/c", f"{SITE}/d"], "home never; menus do not count"
    assert graph.near_orphans() == [f"{SITE}/a"]  # /hub has two: home and /b
    assert max(graph.rank.values()) == 100.0
    assert pagerank([], {}) == {}
    assert graph.rank[page_key(f"{SITE}/b")] > graph.rank[page_key(f"{SITE}/d")]


def _snapshot(
    rows: list[KeywordRow], snapshot_id: str = "research-20261126-000000-002"
) -> ResearchSnapshot:
    snap = ResearchSnapshot(
        snapshot_id=snapshot_id,
        project_id="p1",
        created_at=NOW,
        database="us",
        websites=[
            WebsiteResearch(
                website="example.com",
                name="Example",
                role="client",
                source="csv_import",
                pulled_at=NOW,
                keywords=rows,
            )
        ],
    )
    finish_snapshot(snap, None)
    return snap


def _theme_map() -> ThemeMap:
    return ThemeMap.model_validate(
        {
            "map_id": "themes-1",
            "project_id": "p1",
            "built_at": NOW,
            "method": "test",
            "websites": [],
            "themes": [
                {
                    "theme_id": "th-1",
                    "label": "Term life",
                    "terms": ["term"],
                    "pages": 3,
                    "coverage": [],
                    "ownership": "client_owned",
                    "ownership_reason": "",
                }
            ],
            "pages": [
                {"url": f"{SITE}/{p}", "website": "example.com", "theme_id": "th-1"}
                for p in ("hub", "a", "c")
            ],
        }
    )


def test_page_report_joins_rankings_links_health_history_and_search_console() -> None:
    before = _snapshot(
        [KeywordRow(keyword="term quotes", position=8, volume=500, url=f"{SITE}/hub")],
        "research-20261126-000000-001",
    )
    now = _snapshot(
        [
            KeywordRow(keyword="term life insurance", position=12, volume=4000, url=f"{SITE}/c"),
            KeywordRow(
                keyword="what is term life",
                position=5,
                volume=1000,
                url=f"{SITE}/a",
                other_urls=[f"{SITE}/b"],
            ),  # fmt: skip
            KeywordRow(keyword="term quotes", position=9, volume=500, url=f"{SITE}/a"),
        ]
    )
    gsc = GscSnapshot(
        project_id="p1",
        website="example.com",
        imported_at=NOW,
        period="Last 3 months",
        pages=[GscRow(page=f"{SITE}/c", clicks=5, impressions=4000, ctr=0.00125, position=6)],
        pairs=[
            GscRow(query="term", page=f"{SITE}/a", impressions=300, position=4),
            GscRow(query="term", page=f"{SITE}/c", impressions=200, position=7),
        ],
    )
    report = build_page_report(
        "p1",
        "example.com",
        snapshot=now,
        previous=before,
        inventory=_site(),
        theme_map=_theme_map(),
        gsc=gsc,
        now=NOW,
        run_id="run-1",
    )
    rows = {r.url: r for r in report.pages}
    a = rows[f"{SITE}/a"]
    assert (a.keywords, a.volume, a.best_position, a.top_keyword) == (
        2,
        1500,
        5,
        "what is term life",
    )
    assert a.average_position == 7.0 and a.flags == ["several H1s"] and a.theme == "Term life"
    assert rows[f"{SITE}/c"].commercial_volume == 4000 and rows[f"{SITE}/c"].gsc_impressions == 4000
    sources = {(c.keyword, c.source) for c in report.cannibalisation}
    assert sources == {
        ("what is term life", "semrush_rows"),
        ("term quotes", "url_swap"),
        ("term", "search_console"),
    }
    assert report.stuck == [f"{SITE}/c", f"{SITE}/a"]  # ranking 4-20 with problems
    assert report.zero_traffic == [f"{SITE}/b", f"{SITE}/d", f"{SITE}/hub"]
    assert report.orphans == [f"{SITE}/c", f"{SITE}/d"]
    (suggestion, *_) = report.link_suggestions
    assert suggestion.target_url == f"{SITE}/c" and suggestion.anchor == "term life insurance"
    assert suggestion.source_url in {f"{SITE}/hub", f"{SITE}/a"}, "same theme, stronger"
    assert [o.label for o in report.ctr_opportunities] == [f"{SITE}/c"]
    assert "6 scraped pages" in report.notes[0]
    sheets = [t.name for t in page_tables(report)]
    assert sheets[:3] == ["Pages", "Cannibalisation", "Stuck pages"] and "How to read" in sheets

    findings = build_findings(_theme_map(), {"example.com": "client"}, research=now, pages=report)
    kinds = {f.kind for f in findings}
    assert {"cannibalisation", "stuck_page", "link_opportunity", "ctr_opportunity"} <= kinds
    assert {"unlinked_pages", "zero_traffic"} <= kinds


def test_an_empty_report_says_what_is_missing() -> None:
    report = build_page_report("p1", "example.com", snapshot=None, now=NOW)
    assert report.pages == [] and len(report.notes) == 3
    assert "no scrape inventory" in report.notes[0] and "Search Console" in report.notes[2]


def test_buyer_intent_raises_and_research_intent_lowers_priority() -> None:
    base = priority("keyword_gap", demand=1000)
    assert priority("keyword_gap", demand=1000, intent_bonus=8) == base + 8
    assert priority("funnel_gap", intent_bonus=-50) == 0


# -- app and command line --------------------------------------------------------------------


def test_api_and_cli_page_report(
    themed: World, tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    signer = TokenSigner(b"k" * 32)
    exports = tmp_path / "exports"
    exports.mkdir()
    app = create_app(
        exports,
        projects=(themed.store, ScrapeRunner(themed.store), signer),
        knowledge=themed.access,
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    base = f"/api/projects/{themed.project_id}/research"
    assert client.get(f"{base}/pages").status_code == 404
    assert client.get(f"{base}/gsc").status_code == 404
    token, _ = signer.issue(themed.project_id)
    auth = {**WRITE, "X-Project-Token": token}
    body = {
        "website": "example.com",
        "file_name": "export.zip",
        "content_base64": base64.b64encode(_gsc_zip()).decode(),
    }
    assert client.post(f"{base}/gsc/import", json=body, headers=WRITE).status_code == 401
    summary = client.post(f"{base}/gsc/import", json=body, headers=auth).json()
    assert (summary["queries"], summary["pages"], summary["period"]) == (3, 2, "Last 3 months")
    bad = {**body, "content_base64": base64.b64encode(b"x,y\n").decode()}
    assert client.post(f"{base}/gsc/import", json=bad, headers=auth).status_code == 422
    assert client.get(f"{base}/gsc").json()["clicks"] == 312
    import_csv(
        themed.store, themed.access, themed.project_id, "example.com", CLIENT_EXPORT.encode()
    )
    report = client.get(f"{base}/pages").json()
    assert report["website"] == "example.com" and report["ctr_opportunities"]
    assert any(p["keywords"] for p in report["pages"])
    again = client.get(f"{base}/pages").json()
    assert again["built_at"] == report["built_at"], "unchanged inputs: the saved report"
    assert client.get(f"{base}/pages/workbook").content[:2] == b"PK"
    assert research_store(themed.store).latest_pages(themed.project_id) is not None

    path = tmp_path / "gsc.zip"
    path.write_bytes(_gsc_zip())
    app_data = ["--app-data", str(themed.store.root)]
    assert cli.main(["research", "gsc-import", "--project", themed.project_id, *app_data]) == 2
    assert (
        cli.main(
            [
                "research",
                "gsc-import",
                "--project",
                themed.project_id,
                "--website",
                "example.com",
                "--file",
                str(path),
                *app_data,
            ]
        )  # fmt: skip
        == 0
    )
    assert cli.main(["research", "pages", "--project", themed.project_id, *app_data]) == 0
    out = capsysbinary.readouterr().out.decode("utf-8")
    summary_out = json.loads(out[out.rindex('{\n  "website"') :])
    assert summary_out["website"] == "example.com" and "files" in summary_out
