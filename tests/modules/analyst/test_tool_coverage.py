"""Analyst tools that answer whole-site questions: paging and filters, technical checks,
structured data, the page report without search data, and size-cut paging."""

from __future__ import annotations

import json
from typing import Any

from src.modules.analyst.technical import technical_checks
from src.modules.analyst.tools import _fit, run_tool
from src.modules.content_processor.knowledge.inventory_models import (
    HeadFacts,
    Issue,
    LinkItem,
    PageInventory,
    StructuredItem,
)
from tests.modules.analyst.conftest import CLIENT, WHEN, World, _jsonl, stamp
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.content_processor.l5_factory import record


def call(play: Any, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    text, is_error = run_tool(play, name, args or {})
    data = json.loads(text)
    assert not is_error, data
    return dict(data["data"])


def _link(url: str, *, kind: str = "internal", insecure: bool = False, order: int = 0) -> LinkItem:
    return LinkItem(
        url=url,
        anchor_text="go",
        kind=kind,  # type: ignore[arg-type]
        insecure=insecure,
        ui_region="main_content",
        order=order,
        seen_in=["source"],
    )


def _inv(url: str, **kw: Any) -> PageInventory:
    return PageInventory(
        run_id="run-c1",
        url=url,
        page_key="pg-" + url.rsplit("/", 1)[-1],
        inventoried_at=WHEN,
        status="inventoried",
        **kw,
    )


def test_technical_checks_from_stored_records_stamps_and_inventory() -> None:
    a, b, c, d, e = (f"{CLIENT}/{x}" for x in ("a", "b", "gone", "missing", "list?utm=1&page=2"))
    rec_b = record(b).model_copy(
        update={
            "final_url": f"{CLIENT}/b2",
            "summaries": {
                "http": {
                    "status": 200,
                    "redirects": [
                        {"kind": "http", "from": b, "to": f"{CLIENT}/b1", "status": 301},
                        {
                            "kind": "http",
                            "from": f"{CLIENT}/b1",
                            "to": f"{CLIENT}/b2",
                            "status": 301,
                        },
                    ],
                }
            },
        }
    )
    rec_c = record(c).model_copy(update={"summaries": {"http": {"status": 404}}})
    html = {"sniffed_mime": "text/html"}
    records = [
        record(a).model_copy(update=html),
        rec_b,
        rec_c,
        record(d),
        record(e).model_copy(update=html),
    ]
    stamps = [
        stamp(CLIENT, "/a", "Term life | Example Life", "run-c1").model_copy(
            update={"h1": "Term life"}
        ),
        stamp(CLIENT, "/b", "Term life | Example Life", "run-c1").model_copy(
            update={"h1": "Other heading"}
        ),
        stamp(CLIENT, "/missing", "Not here", "run-c1").model_copy(
            update={"knowledge_eligible": False, "exclusion_reason": "soft_404"}
        ),
    ]
    inventory = [
        _inv(
            a,
            links=[
                _link(c),
                _link("http://www.example.com/old", insecure=True, order=1),
                _link(f"{CLIENT}/never-scraped", order=2),
                _link(f"{CLIENT}/search?q=term", order=3),
            ],
            head=HeadFacts(hreflang={"es": b}),
        ),
        _inv(b, head=HeadFacts(robots={"robots": ["noindex", "follow"]}, indexable=False)),
    ]
    report = technical_checks(records, stamps, inventory)
    f = report.findings
    assert [x.detail for x in f["http_errors"]] == ["HTTP 404"]
    assert [x.url for x in f["soft_404"]] == [d]
    assert f["redirects"][0].url == b and f["redirects"][0].extra["hops"][0]["status"] == 301
    assert f["redirect_chains"][0].detail.startswith("2 hops")
    assert {x.url for x in f["duplicate_titles"]} == {a, b}
    assert [x.url for x in f["title_equals_h1"]] == [a]
    assert {x.url for x in f["parameter_urls"]} == {e, f"{CLIENT}/search?q=term"}
    assert [x.detail for x in f["insecure_links"]] == ["link to http://www.example.com/old"]
    assert [x.detail for x in f["broken_internal_links"]] == [f"links to {c} (HTTP 404)"]
    assert [x.url for x in f["robots_meta"]] == [b] and f["robots_meta"][0].detail == "noindex"
    assert "no return link from es" in f["hreflang"][0].detail
    assert any("did not fetch" in n for n in report.notes)
    assert not any("predates" in n for n in report.notes)  # statuses were kept
    old = technical_checks([record(a)], stamps[:1], [])
    assert any("predates kept answer statuses" in n for n in old.notes)


def _with_records_and_inventory(world: World) -> None:
    site = world.site_ids["client"]
    folder = world.store.run_dir(world.project_id, site, "run-c1")
    urls = [CLIENT + p for p in ("/life/term", "/about", "/life/term-copy")]
    _jsonl(folder / "run-c1.records.jsonl", [record(u) for u in urls])
    org = StructuredItem(
        syntax="json-ld",
        types=["Organization"],
        name="Example Life",
        properties=["name", "logo", "sameAs", "url"],
        missing_recommended=["address"],
        data={
            "@type": "Organization",
            "name": "Example Life",
            "logo": {"@type": "ImageObject", "url": CLIENT + "/logo.png"},
            "sameAs": ["https://x.com/examplelife", "https://www.linkedin.com/company/x"],
        },
    )
    crumbs = StructuredItem(
        syntax="json-ld",
        types=["BreadcrumbList"],
        properties=["itemListElement"],
        data={
            "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "item": {"@id": CLIENT, "name": "Home"}}],
        },
    )
    _jsonl(
        folder / "run-c1.inventory.jsonl",
        [
            _inv(urls[0], structured_data=[org, crumbs], issues=[_issue("title_too_long")]),
            _inv(urls[1], issues=[_issue("title_too_long"), _issue("missing_canonical")]),
            _inv(urls[2], head=HeadFacts(robots={"robots": ["noindex"]}, indexable=False)),
        ],
    )


def _issue(code: str) -> Issue:
    return Issue(code=code, severity="warning", message=code.replace("_", " "))


def test_whole_site_tools_filter_page_and_check(world: World) -> None:
    _with_records_and_inventory(world)
    play = world.runner().playground([world.project_id])

    pages = call(play, "list_pages", {"website": "example.com", "title_contains": "term"})
    assert pages["matching"] == 2 and pages["total_pages"] == 3
    first = call(play, "list_pages", {"website": "example.com", "limit": 2})
    rest = call(play, "list_pages", {"website": "example.com", "offset": first["next_offset"]})
    assert len(first["pages"]) == 2 and first["next_offset"] == 2 and rest["next_offset"] is None
    assert {p["url"] for p in first["pages"] + rest["pages"]} == {
        CLIENT + p for p in ("/life/term", "/about", "/life/term-copy")
    }
    noindex = call(play, "list_pages", {"website": "example.com", "noindex": True})
    assert [p["url"] for p in noindex["pages"]] == [CLIENT + "/life/term-copy"]
    assert call(play, "list_pages", {"website": "example.com", "with_query": True})["matching"] == 0

    summary = call(play, "get_seo_issues", {"website": "example.com"})
    assert summary["issues"][0]["pages"] == 2
    every = call(play, "get_seo_issues", {"website": "example.com", "code": "title_too_long"})
    assert every["pages_with_issue"] == 2 and every["next_offset"] is None
    paged = call(
        play, "get_seo_issues", {"website": "example.com", "code": "title_too_long", "limit": 1}
    )
    assert len(paged["pages"]) == 1 and paged["next_offset"] == 1
    text, err = run_tool(play, "get_seo_issues", {"website": "example.com", "code": "nope"})
    assert err and "codes: missing_canonical, title_too_long" in json.loads(text)["error"]

    checks = call(play, "get_technical_checks", {"website": "example.com"})
    by = {c["check"]: c for c in checks["checks"]}
    assert by["duplicate_titles"]["findings"] == 2  # /life/term and its copy
    assert by["robots_meta"]["findings"] == 1
    dup = call(
        play, "get_technical_checks", {"website": "example.com", "check": "duplicate_titles"}
    )
    assert dup["findings_total"] == 2 and dup["findings"][0]["also_on"]
    text, err = run_tool(play, "get_technical_checks", {"website": "example.com", "check": "x"})
    assert err and "unknown check" in json.loads(text)["error"]

    schema = call(play, "get_structured_data", {"website": "example.com"})
    assert {t["type"] for t in schema["types"]} == {"Organization", "BreadcrumbList"}
    assert schema["pages_without_structured_data"] == 2
    org = call(play, "get_structured_data", {"website": "example.com", "type": "organization"})
    item = org["items"][0]
    assert item["same_as"] == ["https://x.com/examplelife", "https://www.linkedin.com/company/x"]
    assert item["logo"] == {"@type": "ImageObject", "name": CLIENT + "/logo.png"}
    assert item["missing_recommended"] == ["address"]
    facts = call(play, "get_page_facts", {"url": CLIENT + "/life/term"})
    data = {tuple(s["types"]): s for s in facts["facts"]["structured_data"]}
    assert data[("Organization",)]["values"]["sameAs"][0] == "https://x.com/examplelife"
    assert data[("BreadcrumbList",)]["values"]["itemListElement"] == [{"name": "Home"}]


def test_page_report_runs_from_the_scrape_alone(world: World) -> None:
    play = world.runner().playground([world.project_id])
    report = call(play, "get_page_report", {"section": "summary"})
    assert report["research_snapshot"] is None and report["counts"]["pages"] >= 1
    unlinked = call(play, "get_page_report", {"section": "unlinked"})
    assert "no_links_from_page_text" in unlinked and "one_link_from_page_text" in unlinked


def test_a_paged_answer_cut_to_fit_says_where_to_continue() -> None:
    rows = [{"url": f"https://e.com/{i}", "pad": "x" * 2_000} for i in range(60)]
    payload = {"tool": "t", "data": {"offset": 100, "next_offset": None, "pages": rows}}
    out = json.loads(_fit(payload))
    shown = len(out["data"]["pages"])
    assert shown < 60 and out["data"]["next_offset"] == 100 + shown
