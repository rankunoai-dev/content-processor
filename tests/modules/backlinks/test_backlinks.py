"""Backlinks: Ahrefs prices and caps, address rule, exports, stand-in, insights, API, CLI."""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from src.modules.analyst.tools import run_tool
from src.modules.backlinks.ahrefs import (
    MIN_UNITS,
    REFERRING_DOMAINS,
    TOP_PAGES,
    AhrefsBudget,
    AhrefsClient,
    AhrefsError,
    field_cost,
    price,
    row_cost,
)
from src.modules.backlinks.importer import (
    BacklinksImportError,
    decode_export,
    read_pages_csv,
    read_refdomains_csv,
)
from src.modules.backlinks.insights import (
    authority_gaps,
    build_insights,
    link_intersect,
    lost_domains,
)
from src.modules.backlinks.models import (
    BacklinkSnapshot,
    BacklinkStats,
    RefDomain,
    TopPage,
    WebsiteBacklinks,
)
from src.modules.backlinks.service import (
    BacklinksSettings,
    address_refusal,
    backlinks_store,
    estimate_units,
    fair_limits,
    import_csv,
    pull_project,
    unit_log,
)
from src.modules.backlinks.standin import DUMMY_NOTE, Dataset, make_dataset, serve
from src.modules.backlinks.store import backlink_tables
from src.modules.content_processor import cli
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import ProjectStore
from src.modules.content_processor.webapi import __main__ as webapi_main
from src.modules.content_processor.webapi.app import create_app
from src.modules.gaps.build import _backlink_findings
from src.modules.gaps.service import build_project_gaps
from src.modules.research.semrush import Pacer, UnitEntry, UnitLog
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture

SITES = [("example.com", "client"), ("rival.com", "competitor")]
NOW = datetime(2026, 11, 26, tzinfo=UTC)
WRITE = {"X-Requested-With": "content-processor"}


def _no_wait() -> Pacer:
    return Pacer(0.0, sleep=lambda _s: None)


def _settings(base_url: str, **extra: Any) -> BacklinksSettings:
    values: dict[str, Any] = {
        "enabled": True,
        "api_key": "dummy-key",
        "monthly_units": 100_000,
        "pull_units": 20_000,
        "refdomains_limit": 500,
        "pages_limit": 100,
        "min_interval_ms": 0,
        "base_url": base_url,
    }
    values.update(extra)
    return BacklinksSettings(**values)


@pytest.fixture
def standin() -> Iterator[tuple[str, Any]]:
    """The stand-in for the themed project's two websites, on a free port."""
    server = serve(make_dataset("p1", SITES), 0, slow_down_every=3)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v3/", server
    finally:
        server.shutdown()
        server.server_close()


def _site(
    website: str,
    role: str,
    refs: list[tuple[str, float]],
    *,
    dr: float | None = None,
    asked: int = 0,
    pages: list[TopPage] | None = None,
) -> WebsiteBacklinks:
    return WebsiteBacklinks(
        website=website,
        name=website,
        role=role,  # type: ignore[arg-type]
        source="csv_import",
        pulled_at=NOW,
        domain_rating=dr,
        refdomains=[RefDomain(domain=d, domain_rating=r, links_to_target=2) for d, r in refs],
        refdomains_asked=asked,
        top_pages=pages or [],
    )


# -- units, caps and the address rule ------------------------------------------------------


def test_every_call_costs_at_least_50_units_and_fields_are_priced() -> None:
    assert field_cost("domain") == 1 and field_cost("refdomains_target") == 5
    assert field_cost("traffic_domain") == 10
    assert row_cost(REFERRING_DOMAINS) == 7 and row_cost(TOP_PAGES) == 10
    assert price(REFERRING_DOMAINS, 1) == MIN_UNITS == 50
    assert price(REFERRING_DOMAINS, 500) == 3500 and price(TOP_PAGES, 100) == 1000
    assert price("site-explorer/unknown", 2) == 100
    settings = BacklinksSettings()
    assert estimate_units(3, settings) == 3 * (50 + 50 + 3500 + 1000)
    assert fair_limits(10_000, settings) == (500, 100)
    assert fair_limits(2_350, settings) == (250, 50), "both lists shrink by the same share"
    assert fair_limits(0, settings) == (10, 5)


def test_budget_refuses_before_anything_is_sent_and_charges_actual_rows(tmp_path: Path) -> None:
    log = UnitLog(tmp_path)
    assert "switched off" in (
        AhrefsBudget(log, enabled=False, monthly_units=10, pull_units=10).refusal() or ""
    )
    assert "no monthly" in (
        AhrefsBudget(log, enabled=True, monthly_units=0, pull_units=10).refusal() or ""
    )
    log.add(
        UnitEntry(at=datetime.now(UTC), project_id="p", report="x", target="t", lines=1, units=90)
    )
    assert "used up" in (
        AhrefsBudget(log, enabled=True, monthly_units=90, pull_units=50).refusal() or ""
    )
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        rows = [{"domain": "a.com", "domain_rating": 50}, {"domain": "www.b.com"}]
        return httpx.Response(200, json={"refdomains": rows})

    small = AhrefsBudget(log, enabled=True, monthly_units=10_000, pull_units=60)
    client = AhrefsClient(
        "secret",
        small,
        log,
        "p",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        pacer=_no_wait(),
    )
    with pytest.raises(AhrefsError, match="could use 70 units; only 60"):
        client.referring_domains("rival.com", 10)  # 10 rows x 7 fields > 60
    assert sent == [], "refused before anything was sent"
    rows = client.referring_domains("rival.com", 5)  # 5 x 7 = 35, priced at the minimum 50
    assert [r.domain for r in rows] == ["a.com", "b.com"] and small.spent == 50
    request = sent[0]
    assert request.headers["authorization"] == "Bearer secret"
    assert "secret" not in str(request.url), "the key travels in the header only"
    assert request.url.params["select"].startswith("domain,domain_rating")
    assert request.url.params["output"] == "json" and request.url.params["limit"] == "5"
    assert log.used(datetime.now(UTC) - timedelta(minutes=1)) == 90 + 50
    refused = AhrefsClient(
        "k",
        AhrefsBudget(log, enabled=True, monthly_units=10_000, pull_units=1000),
        log,
        "p",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(403, json={"error": "Insufficient plan"})
            )
        ),
        pacer=_no_wait(),
    )
    with pytest.raises(AhrefsError, match="HTTP 403.*Insufficient plan"):
        refused.domain_rating("rival.com", "2026-11-26")
    assert refused.budget.spent == 0, "a refused call is not charged"
    odd = AhrefsClient(
        "k",
        AhrefsBudget(log, enabled=True, monthly_units=10_000, pull_units=1000),
        log,
        "p",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="?"))),
        pacer=_no_wait(),
    )
    with pytest.raises(AhrefsError, match="not JSON"):
        odd.top_pages("rival.com", 5)
    client.close()


def test_only_ahrefs_or_this_computer_may_receive_the_key(tmp_path: Path) -> None:
    assert address_refusal("https://api.ahrefs.com/v3/") is None
    assert address_refusal("http://127.0.0.1:8791/v3/") is None
    assert address_refusal("http://localhost:8791/") is None
    assert address_refusal("http://[::1]:8791/") is None
    for bad in (
        "http://api.ahrefs.com/v3/",  # not encrypted
        "https://api.ahrefs.com.example.org/v3/",
        "https://example.org/",
        "ftp://127.0.0.1/",
    ):
        assert "refused" in (address_refusal(bad) or ""), bad
    with pytest.raises(AhrefsError, match="refused"):
        pull_project(ProjectStore(tmp_path), None, "p1", _settings("https://example.org/"))  # type: ignore[arg-type]
    with pytest.raises(AhrefsError, match="switched off"):
        pull_project(ProjectStore(tmp_path), None, "p1", BacklinksSettings())  # type: ignore[arg-type]


# -- exports ---------------------------------------------------------------------------------


def test_referring_domains_export_in_utf16_with_tabs() -> None:
    text = (
        "#\tDomain\tDomain rating\tDomain traffic\tLinks to target\tDofollow links\t"
        "First seen\tLast seen\n"
        "1\twww.bignews.com\t88\t1,200,000\t14\t10\t2021-03-04 10:00:00\t2026-11-20 03:00:00\n"
        "2\tsmallblog.net\t12.5\t300\t2\t0\t2024-01-01\t\n"
        "3\t\t50\t\t\t\t\t\n"
        "4\tbignews.com\t88\t\t1\t1\t\t\n"
    )
    for data in (text.encode("utf-16"), text.encode("utf-16-le"), ("﻿" + text).encode()):
        rows = read_refdomains_csv(data)
        assert [r.domain for r in rows] == ["bignews.com", "smallblog.net"], "deduplicated"
        assert rows[0].domain_rating == 88 and rows[0].links_to_target == 14
        assert rows[0].dofollow_links == 10 and rows[0].first_seen == "2021-03-04"
        assert rows[0].last_seen == "2026-11-20" and rows[1].last_seen is None
    semicolons = b"Domain;DR;Backlinks\nexample.org;40;3\n"
    assert read_refdomains_csv(semicolons)[0].dofollow_links == 3, "no dofollow column: all"
    assert decode_export("x".encode("utf-16-be")) == "x"
    pages = b"Page URL,Referring domains,URL Rating\nhttps://a.com/x,4,20\n"
    with pytest.raises(BacklinksImportError, match="import it as top pages"):
        read_refdomains_csv(pages)
    with pytest.raises(BacklinksImportError, match="needs a Domain column"):
        read_refdomains_csv(b"a,b\n1,2\n")
    with pytest.raises(BacklinksImportError, match="empty"):
        read_refdomains_csv(b"")


def test_best_by_links_export() -> None:
    data = (
        "﻿#,Target URL,Title,Referring domains,Backlinks,UR,HTTP code\n"
        "1,https://www.example.com/,Home,40,300,35,200\n"
        "2,https://www.example.com/tools/calculator,Calculator,55,90,30,200\n"
        "3,not a url,,9,9,9,200\n"
        "4,https://www.example.com/old,Old,3,3,5,404\n"
    ).encode()
    pages = read_pages_csv(data)
    assert [p.url.rsplit("/", 1)[-1] for p in pages] == ["calculator", "", "old"]
    assert pages[0].referring_domains == 55 and pages[0].links == 90 and pages[0].url_rating == 30
    assert pages[2].http_code == 404
    with pytest.raises(BacklinksImportError, match="import it as referring domains"):
        read_pages_csv(b"Domain,DR\na.com,3\n")
    with pytest.raises(BacklinksImportError, match="Best by links"):
        read_pages_csv(b"a,b\n1,2\n")


def test_the_standin_exports_read_back_with_the_importers() -> None:
    data = make_dataset("p1", SITES)
    assert make_dataset("p1", SITES) == data, "deterministic"
    refs = data.refdomains_csv("rival.com")
    assert refs.startswith(b"\xff\xfe"), "UTF-16 with a byte order mark, as Ahrefs exports"
    rows = read_refdomains_csv(refs)
    rival = data.website("rival.com")
    assert rival is not None and len(rows) == len(rival.refdomains)
    assert {r.domain for r in rows} == {r.domain for r in rival.refdomains}
    pages = read_pages_csv(data.pages_csv("rival.com"))
    assert (
        len(pages) == len(rival.pages) and pages[0].referring_domains >= pages[-1].referring_domains
    )
    client, competitor = data.websites
    assert competitor.domain_rating > client.domain_rating, "competitors are made stronger"
    later = make_dataset("p1", SITES, period=1)
    assert {r.domain for r in later.websites[1].refdomains} != {r.domain for r in rival.refdomains}
    status, body = data.answer("site-explorer/nothing", {})
    assert status == 404 and "unknown" in body["error"]
    _status, empty = data.answer("site-explorer/domain-rating", {"target": "nobody.org"})
    assert empty["domain_rating"]["ahrefs_rank"] is None


# -- insights and gap findings ---------------------------------------------------------------


def _three() -> list[WebsiteBacklinks]:
    return [
        _site("client.com", "client", [("shared.org", 70), ("mine.net", 30)], dr=35),
        _site(
            "rival.com",
            "competitor",
            [("big.org", 90), ("mid.org", 50), ("shared.org", 70), ("one.org", 60)],
            dr=62,
        ),
        _site(
            "other.com",
            "competitor",
            [("big.org", 90), ("mid.org", 50), ("shared.org", 70), ("blog.rival.com", 40)],
            dr=40,
        ),
    ]


def test_link_intersect_needs_two_competitors_and_leaves_out_the_client() -> None:
    sites = _three()
    sites[2].refdomains.append(RefDomain(domain="spam.biz", domain_rating=80, is_spam=True))
    sites[1].refdomains.append(RefDomain(domain="spam.biz", domain_rating=80, is_spam=True))
    rows, need = link_intersect(sites)
    assert need == 2
    assert [r.domain for r in rows] == ["big.org", "mid.org"], "spam, the client's, own left out"
    assert rows[0].competitors == ["rival.com", "other.com"] and rows[0].links == 4
    one, need = link_intersect(sites[:2])
    assert need == 1 and [r.domain for r in one] == ["big.org", "one.org", "mid.org"]
    assert link_intersect(sites[:1]) == ([], 2)
    gaps = authority_gaps(sites)
    assert [(g.competitor, g.gap, g.metric) for g in gaps] == [
        ("rival.com", 27.0, "domain_rating"),
        ("other.com", 5.0, "domain_rating"),
    ]
    no_dr = [_site("c.com", "client", [("x.org", 1)]), _site("r.com", "competitor", [])]
    no_dr[1].stats = BacklinkStats(live_refdomains=40)
    assert authority_gaps(no_dr)[0].metric == "referring_domains"
    assert authority_gaps(no_dr)[0].gap == 39
    insights = build_insights(sites)
    assert insights.client == "client.com" and len(insights.link_intersect) == 2


def test_lost_domains_only_count_what_the_new_list_would_have_held() -> None:
    before = _site("rival.com", "competitor", [("a.org", 80), ("b.org", 50), ("c.org", 10)])
    cut = _site("rival.com", "competitor", [("a.org", 80), ("d.org", 40)], asked=2)
    assert lost_domains(before, cut) == ["b.org"], "c.org is weaker than the cut-off"
    full = cut.model_copy(update={"refdomains_asked": 0})
    assert lost_domains(before, full) == ["b.org", "c.org"]
    assert lost_domains(None, full) == []


def test_gap_findings_from_the_link_intersect_and_the_authority_gap() -> None:
    sites = _three()
    snapshot = BacklinkSnapshot(
        snapshot_id="backlinks-20261126-101010-000",
        project_id="p1",
        created_at=NOW,
        websites=sites,
        insights=build_insights(sites),
    )
    findings = {f.kind: f for f in _backlink_findings(snapshot)}
    intersect = findings["link_intersect"]
    assert intersect.title == "2 website(s) link to 2 or more competitors but not to the client"
    assert "big.org (DR 90)" in intersect.detail and intersect.competitors == [
        "other.com",
        "rival.com",
    ]
    assert intersect.evidence[0].label == "big.org (DR 90): links to rival.com, other.com"
    assert intersect.evidence[0].kind == "link" and intersect.evidence[0].value == 90
    gap = findings["authority_gap"]
    assert gap.title == "rival.com has Domain Rating 62 against the client's 35"
    assert gap.competitors == ["rival.com"], "other.com is only 5 behind: no finding"
    assert len([f for f in _backlink_findings(snapshot) if f.kind == "authority_gap"]) == 1


# -- the module end to end -------------------------------------------------------------------


def test_a_pull_through_the_standin_and_the_real_client(
    themed: World, standin: tuple[str, Any]
) -> None:
    base_url, server = standin
    snapshot, files = pull_project(
        themed.store, themed.access, themed.project_id, _settings(base_url), pacer=_no_wait()
    )
    assert snapshot.notes[1] == DUMMY_NOTE and "slow down" in snapshot.notes[-1]
    data = server.RequestHandlerClass.dataset
    by_site = {w.website: w for w in snapshot.websites}
    assert list(by_site) == ["example.com", "rival.com"]
    for site in data.websites:
        pulled = by_site[site.domain]
        assert pulled.domain_rating == site.domain_rating and pulled.source == "ahrefs_api"
        assert len(pulled.refdomains) == len(site.refdomains) and pulled.refdomains_asked == 500
        assert pulled.stats is not None and pulled.stats.live_refdomains == len(site.refdomains)
        assert len(pulled.top_pages) == len(site.pages)
        assert pulled.new_domains is None, "the first snapshot has no history"
    assert snapshot.insights.min_competitors == 1 and snapshot.insights.link_intersect
    assert snapshot.insights.authority[0].competitor == "rival.com"
    calls = server.RequestHandlerClass.calls
    assert len(calls) == 11, "four calls per website; calls 3, 6 and 9 answered 429 and retried"
    log = unit_log(themed.store)
    assert snapshot.units_spent == log.used(datetime.now(UTC) - timedelta(minutes=5))
    assert Path(files["workbook"]).is_file()
    again, _ = pull_project(
        themed.store, themed.access, themed.project_id, _settings(base_url), pacer=_no_wait()
    )
    assert all(w.new_domains == 0 and w.lost_domains == [] for w in again.websites)
    assert again.insights.lost == {"example.com": 0, "rival.com": 0}
    assert backlinks_store(themed.store).history(themed.project_id)[0] == again.snapshot_id
    tight = _settings(base_url, pull_units=2_000)
    cut, _ = pull_project(themed.store, themed.access, themed.project_id, tight, pacer=_no_wait())
    assert cut.units_spent <= 2_000 and cut.websites[0].refdomains_asked < 500
    assert "share the pull cap" in cut.websites[0].note
    with pytest.raises(AhrefsError, match="at least 220 are needed"):
        pull_project(
            themed.store,
            themed.access,
            themed.project_id,
            _settings(base_url, pull_units=300),
            pacer=_no_wait(),
        )


def test_a_failed_website_keeps_its_last_data(themed: World) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["target"] == "rival.com":
            return httpx.Response(500, json={"error": "boom"})
        endpoint = request.url.path.split("/v3/", 1)[1]
        status, body = make_dataset("p1", SITES).answer(endpoint, dict(request.url.params))
        return httpx.Response(status, json=body)

    def http() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    settings = _settings("https://api.ahrefs.com/v3/")
    snapshot, _ = pull_project(
        themed.store, themed.access, themed.project_id, settings, http=http, pacer=_no_wait()
    )
    assert [w.website for w in snapshot.websites] == ["example.com"]
    assert DUMMY_NOTE not in snapshot.notes, "the real address: no dummy note"
    assert any("could not be pulled: Ahrefs refused" in n for n in snapshot.notes)
    import_csv(
        themed.store,
        themed.access,
        themed.project_id,
        "rival.com",
        "refdomains",
        make_dataset("p1", SITES).refdomains_csv("rival.com"),
    )
    kept, _ = pull_project(
        themed.store, themed.access, themed.project_id, settings, http=http, pacer=_no_wait()
    )
    assert kept.websites[1].note == "Kept from the last snapshot" and kept.websites[1].refdomains

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network")

    with pytest.raises(AhrefsError, match="could not reach Ahrefs"):
        pull_project(
            themed.store,
            themed.access,
            themed.project_id,
            settings,
            http=lambda: httpx.Client(transport=httpx.MockTransport(down)),
            pacer=_no_wait(),
        )


def test_imports_build_the_snapshot_and_the_gap_report_uses_it(themed: World) -> None:
    data = make_dataset("p1", SITES)
    for website in ("example.com", "rival.com"):
        import_csv(
            themed.store,
            themed.access,
            themed.project_id,
            website,
            "refdomains",
            data.refdomains_csv(website),
        )
    snapshot, files = import_csv(
        themed.store,
        themed.access,
        themed.project_id,
        "example.com",
        "pages",
        data.pages_csv("example.com"),
    )
    assert [w.source for w in snapshot.websites] == ["csv_import", "csv_import"]
    assert snapshot.insights.client_top_pages[0].referring_domains >= 1
    assert snapshot.insights.authority[0].metric == "referring_domains", "exports carry no DR"
    assert snapshot.units_spent == 0 and Path(files["workbook"]).is_file()
    sheets = [t.name for t in backlink_tables(snapshot)]
    assert sheets[:4] == ["Websites", "Referring domains", "Top pages", "Link intersect"]
    assert sheets[-1] == "How to read"
    with pytest.raises(BacklinksImportError):
        import_csv(themed.store, themed.access, themed.project_id, "rival.com", "pages", b"x\n")
    with pytest.raises(ValueError, match="no website matches"):
        import_csv(themed.store, themed.access, themed.project_id, "nope.org", "pages", b"")
    report, _files = build_project_gaps(themed.store, themed.access, themed.project_id)
    kinds = {f.kind for f in report.findings}
    assert "link_intersect" in kinds, "a referring domain count gap under 2x makes no finding"
    found = next(f for f in report.findings if f.kind == "link_intersect")
    assert "but not to the client" in found.title and found.evidence


def test_the_claude_desktop_tool(themed: World) -> None:
    play = themed.runner().playground([themed.project_id])
    text, is_error = run_tool(play, "get_backlinks", {})
    assert is_error and "no backlink data" in text
    data = make_dataset("p1", SITES)
    for website in ("example.com", "rival.com"):
        import_csv(
            themed.store,
            themed.access,
            themed.project_id,
            website,
            "refdomains",
            data.refdomains_csv(website),
        )
    import_csv(
        themed.store,
        themed.access,
        themed.project_id,
        "example.com",
        "pages",
        data.pages_csv("example.com"),
    )
    text, is_error = run_tool(play, "get_backlinks", {"website": "rival.com", "limit": 5})
    assert not is_error, text
    out = json.loads(text)["data"]
    assert out["websites"][0]["ref"].startswith("fact:backlinks-")
    assert len(out["link_intersect"]) == 5 and out["link_intersect"][0]["ref"]
    assert out["client_top_pages"][0]["ref"] and out["authority_gap"]
    assert len(out["referring_domains"]["rows"]) == 5
    text, is_error = run_tool(play, "get_backlinks", {"website": "nope.org"})
    assert is_error


def test_api_backlinks(themed: World, tmp_path: Path, standin: tuple[str, Any]) -> None:
    from src.modules.backlinks import api as backlinks_api

    signer = TokenSigner(b"k" * 32)
    exports = tmp_path / "exports"
    exports.mkdir()
    app = create_app(
        exports,
        projects=(themed.store, ScrapeRunner(themed.store), signer),
        knowledge=themed.access,
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    base = f"/api/projects/{themed.project_id}/backlinks"
    assert client.get("/api/backlinks/status").json()["enabled"] is False
    assert client.get(base).json()["detail"] == "No backlink data has been added yet."
    assert client.get(f"{base}/workbook").status_code == 404
    estimate = client.get(f"{base}/estimate").json()
    assert estimate["websites"] == 2 and estimate["worst_case_units"] == 2 * 4600
    token, _ = signer.issue(themed.project_id)
    auth = {**WRITE, "X-Project-Token": token}
    assert client.post(f"{base}/pull", json={}, headers=WRITE).status_code == 401
    refused = client.post(f"{base}/pull", json={}, headers=auth)
    assert refused.status_code == 409 and "switched off" in refused.json()["detail"]
    data = make_dataset("p1", SITES)
    body = {
        "website": "rival.com",
        "kind": "refdomains",
        "file_name": "r.csv",
        "content_base64": base64.b64encode(data.refdomains_csv("rival.com")).decode(),
    }
    assert client.post(f"{base}/import", json=body, headers=WRITE).status_code == 401
    imported = client.post(f"{base}/import", json=body, headers=auth)
    assert imported.status_code == 200 and imported.json()["websites"][0]["website"] == "rival.com"
    wrong = {**body, "kind": "pages"}
    assert (
        "import it as referring domains"
        in client.post(f"{base}/import", json=wrong, headers=auth).json()["detail"]
    )
    assert (
        client.post(
            f"{base}/import", json={**body, "content_base64": "!!"}, headers=auth
        ).status_code
        == 422
    )
    assert (
        client.post(f"{base}/import", json={**body, "kind": "x"}, headers=auth).status_code == 422
    )
    assert client.get(base).json()["snapshot_id"] == imported.json()["snapshot_id"]
    assert client.get(f"{base}/workbook").content[:2] == b"PK"
    assert client.get("/api/projects/nope/backlinks").status_code == 404
    # with Ahrefs switched on (the stand-in), the same route pulls
    base_url, _server = standin
    on = backlinks_api.FastAPI()
    backlinks_api.register_backlinks_routes(
        on,
        projects=themed.store,
        access=themed.access,
        signer=signer,
        settings=lambda: _settings(base_url),
    )
    pulled = TestClient(on).post(f"{base}/pull", json={}, headers=auth)
    assert pulled.status_code == 200 and pulled.json()["units_spent"] > 0
    status = TestClient(on).get("/api/backlinks/status").json()
    assert status["enabled"] is True and status["used_this_month"] == pulled.json()["units_spent"]


def test_cli_backlinks(
    themed: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(webapi_main, "knowledge_access", lambda: themed.access)
    monkeypatch.setattr(BacklinksSettings, "from_env", classmethod(lambda cls: BacklinksSettings()))
    base = ["--project", themed.project_id, "--app-data", str(themed.root)]
    assert cli.main(["backlinks", "show", *base]) == 1
    assert "no backlink data" in capsys.readouterr().err
    assert cli.main(["backlinks", "units", "--app-data", str(themed.root)]) == 0
    assert "unset" in json.loads(capsys.readouterr().out)["monthly_cap"]
    assert cli.main(["backlinks", "estimate", *base]) == 0
    assert json.loads(capsys.readouterr().out)["worst_case_units"] == 2 * 4600
    assert cli.main(["backlinks", "pull", *base]) == 2
    assert "switched off" in capsys.readouterr().err
    assert cli.main(["backlinks", "import", *base]) == 2
    capsys.readouterr()
    export = tmp_path / "refs.csv"
    export.write_bytes(make_dataset("p1", SITES).refdomains_csv("rival.com"))
    args = ["backlinks", "import", *base, "--website", "rival.com", "--file", str(export)]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["websites"][0]["refdomains"] > 0
    assert cli.main([*args[:-1], str(tmp_path / "missing.csv")]) == 2
    capsys.readouterr()
    assert cli.main(["backlinks", "show", *base]) == 0
    assert json.loads(capsys.readouterr().out)["link_intersect"] > 0
    assert cli.main(["backlinks", "show"]) == 2
    assert cli.main(["backlinks", "show", "--project", "nope", "--app-data", str(themed.root)]) == 2


def test_the_standin_make_command(
    themed: World, tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    out = tmp_path / "dummy"
    args = ["backlinks-standin", "make", "--out", str(out), "--app-data", str(themed.root)]
    assert cli.main(args) == 2
    assert cli.main([*args, "--project", themed.project_id]) == 0
    assert b"DUMMY DATA" in capsysbinary.readouterr().out
    data = Dataset.load(out / "backlinks-dataset.json")
    assert {w.domain: w.role for w in data.websites} == {
        "example.com": "client",
        "rival.com": "competitor",
    }
    assert read_refdomains_csv((out / "rival.com-ahrefs-refdomains.csv").read_bytes())
    assert read_pages_csv((out / "example.com-ahrefs-best-by-links.csv").read_bytes())


def test_the_standin_wants_a_key(standin: tuple[str, Any]) -> None:
    base_url, _server = standin
    answer = httpx.get(base_url + "site-explorer/domain-rating?target=rival.com")
    assert answer.status_code == 401
