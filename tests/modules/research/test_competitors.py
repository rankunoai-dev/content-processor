"""Suggested competitors: found by Semrush and the search results, kept, added or dismissed."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from src.modules.analyst.playground import Playground
from src.modules.analyst.tools import run_tool
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.webapi.app import create_app
from src.modules.gaps.build import build_findings
from src.modules.research.competitors import (
    CompetitorBook,
    domain_of,
    from_competitor_rows,
    merge_suggestions,
    read_competitors_csv,
)
from src.modules.research.service import competitor_book, competitors_store, pull_project
from src.modules.theming.service import theme_store
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.research.test_research import ON, WRITE, semrush
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture

NOW = datetime(2026, 11, 4, tzinfo=UTC)
EXPORT = (
    "Domain,Competitor Relevance,Common Keywords,Organic Keywords,Organic Traffic,Organic Cost\n"
    "bigsure.com,52%,210,12000,150000,90000\n"
    "https://www.newco.io/,0.33,40,900,2000,100\n"
    "example.com,0.9,999,1,1,1\n"
    "not a domain,0.5,10,1,1,1\n"
)


def test_domains_and_rows() -> None:
    assert domain_of("https://www.Coupa.com/sourcing") == "coupa.com"
    assert domain_of("not a domain") is None and domain_of("") is None
    rows = [
        {"Domain": "www.a.com", "Competitor Relevance": "0.5", "Common Keywords": "1,200"},
        {"Domain": "a.com", "Competitor Relevance": "0.9"},
        {"Domain": "b.com", "Competitor Relevance": "75%", "Common Keywords": "x"},
    ]
    found = from_competitor_rows(rows, "semrush_competitors", NOW)
    assert [(s.domain, s.relevance, s.common_keywords) for s in found] == [
        ("a.com", 0.5, 1200),
        ("b.com", 0.75, 0),
    ]
    assert read_competitors_csv(b"Keyword,Position\nterm,3\n", NOW) is None
    parsed = read_competitors_csv(EXPORT.encode(), NOW) or []
    assert [s.domain for s in parsed] == ["bigsure.com", "newco.io", "example.com"]
    assert parsed[0].relevance == 0.52 and parsed[0].sources == ["competitors_export"]


def test_merge_keeps_decisions_and_drops_the_client(world: World) -> None:
    project = world.store.get(world.project_id)
    first = merge_suggestions(None, read_competitors_csv(EXPORT.encode(), NOW) or [], project, NOW)
    assert [s.domain for s in first.suggestions] == ["bigsure.com", "newco.io"]  # client dropped
    dismissed = first.model_copy(
        update={
            "suggestions": [
                s.model_copy(update={"status": "dismissed"}) if s.domain == "newco.io" else s
                for s in first.suggestions
            ]
        }
    )
    again = from_competitor_rows(
        [{"Domain": "newco.io", "Common Keywords": "60"}, {"Domain": "rival.com"}],
        "semrush_competitors",
        NOW,
    )
    merged = merge_suggestions(dismissed, again, project, NOW)
    by = {s.domain: s for s in merged.suggestions}
    assert by["newco.io"].status == "dismissed" and by["newco.io"].common_keywords == 60
    assert by["newco.io"].times_seen == 2
    assert by["newco.io"].sources == ["competitors_export", "semrush_competitors"]
    assert by["rival.com"].status == "added" and by["rival.com"].site_id  # already in the project
    assert merged.client_domain == "example.com"
    assert [s.status for s in merged.suggestions] == ["suggested", "added", "dismissed"]


def test_pull_remembers_competitors_and_gaps_flag_them(themed: World) -> None:
    snapshot, _ = pull_project(
        themed.store,
        themed.access,
        themed.project_id,
        ON,
        serp_keywords=["term life insurance"],
        http=semrush,
    )
    book = competitor_book(themed.store, themed.project_id)
    by = {s.domain: s for s in book.suggestions}
    assert set(by) == {"rival.com", "bigsure.com", "smallblog.net"}  # shop.example.com: client
    assert by["bigsure.com"].status == "suggested" and by["bigsure.com"].organic_traffic == 150000
    assert by["rival.com"].status == "added"
    assert by["rival.com"].sources == ["search_results", "semrush_competitors"]
    assert by["rival.com"].example_keywords == ["term life insurance"]
    assert any("2 not in the project" in n for n in snapshot.notes)

    theme_map = theme_store(themed.store).latest(themed.project_id)
    assert theme_map is not None
    findings = build_findings(theme_map, {}, suggested=book.suggestions)
    unwatched = next(f for f in findings if f.kind == "unwatched_competitor")
    assert unwatched.competitors == ["bigsure.com"]  # smallblog.net is not close enough
    assert "bigsure.com" in unwatched.detail and unwatched.evidence[0].kind == "competitor"

    play = Playground(themed.store, themed.access, [themed.project_id])
    text, is_error = run_tool(play, "get_competitor_suggestions", {})
    data = json.loads(text)["data"]
    assert not is_error and data["client_domain"] == "example.com"
    first = data["competitors"][0]
    assert first["domain"] == "bigsure.com" and first["in_project"] is False and first["ref"]


def test_pull_keeps_keywords_when_the_competitor_report_fails(themed: World) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["type"] == "domain_organic_organic":
            return httpx.Response(200, text="ERROR 132 :: API UNITS BALANCE IS ZERO")
        return semrush().send(request)

    snapshot, _ = pull_project(
        themed.store,
        themed.access,
        themed.project_id,
        ON,
        http=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert snapshot.websites and any("could not be pulled" in n for n in snapshot.notes)
    assert competitors_store(themed.store).get(themed.project_id) is None


def test_api_competitors(themed: World, tmp_path: Path) -> None:
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
    empty = client.get(f"{base}/competitors").json()
    assert empty["suggestions"] == [] and empty["client_domain"] == "example.com"
    token, _ = signer.issue(themed.project_id)
    auth = {**WRITE, "X-Project-Token": token}
    upload = {"file_name": "c.csv", "content_base64": base64.b64encode(EXPORT.encode()).decode()}
    assert client.post(f"{base}/competitors/import", json=upload, headers=WRITE).status_code == 401
    book = CompetitorBook.model_validate(
        client.post(f"{base}/competitors/import", json=upload, headers=auth).json()
    )
    assert [s.domain for s in book.suggestions] == ["bigsure.com", "newco.io"]
    wrong = {**upload, "content_base64": base64.b64encode(b"a,b\n1,2\n").decode()}
    assert client.post(f"{base}/competitors/import", json=wrong, headers=auth).status_code == 422
    misplaced = client.post(
        f"{base}/import", json={**upload, "website": "example.com"}, headers=auth
    )
    assert misplaced.status_code == 422 and "Suggested competitors" in misplaced.json()["detail"]

    response = client.post(f"{base}/competitors/bigsure.com/add", json={}, headers=auth)
    assert response.status_code == 200, response.text
    added = response.json()
    assert [(s["domain"], s["status"]) for s in added["suggestions"]] == [
        ("newco.io", "suggested"),
        ("bigsure.com", "added"),
    ]
    site = next(s for s in themed.store.sites(themed.project_id) if "bigsure.com" in s.domains)
    assert site.role == "competitor" and "Semrush competitors export" in site.note
    assert (
        client.post(f"{base}/competitors/bigsure.com/add", json={}, headers=auth).status_code == 409
    )

    dismissed = client.post(
        f"{base}/competitors/www.newco.io/dismiss", json={}, headers=auth
    ).json()
    assert {s["domain"]: s["status"] for s in dismissed["suggestions"]}["newco.io"] == "dismissed"
    restored = client.post(f"{base}/competitors/newco.io/restore", json={}, headers=auth).json()
    assert {s["domain"]: s["status"] for s in restored["suggestions"]}["newco.io"] == "suggested"
    assert (
        client.post(f"{base}/competitors/nope.com/dismiss", json={}, headers=auth).status_code
        == 404
    )

    themed.store.remove_site(themed.project_id, site.site_id)
    back = client.get(f"{base}/competitors").json()
    assert {s["domain"]: s["status"] for s in back["suggestions"]}["bigsure.com"] == "suggested"
