"""Research: Semrush client and caps, CSV import, themes, demand, movers, CLI, API, tools."""

from __future__ import annotations

import base64
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from src.modules.analyst.library import card
from src.modules.analyst.tools import run_tool
from src.modules.content_processor import cli
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.webapi import __main__ as webapi_main
from src.modules.content_processor.webapi.app import create_app
from src.modules.research import commands as research_commands
from src.modules.research.build import estimated_visits, movers
from src.modules.research.importer import ImportError_, read_positions_csv
from src.modules.research.semrush import (
    SemrushClient,
    SemrushError,
    UnitBudget,
    UnitEntry,
    UnitLog,
    parse_rows,
)
from src.modules.research.service import (
    ResearchSettings,
    estimate_units,
    import_csv,
    pull_project,
    research_store,
    unit_log,
)
from src.modules.research.store import research_tables
from tests.modules.analyst.conftest import CLIENT, World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture

CLIENT_EXPORT = (
    "Keyword,Position,Previous position,Search Volume,Keyword Difficulty,CPC,URL,Traffic (%)\n"
    f"term life insurance,4,6,22000,70,12.5,{CLIENT}/life/term,3.1\n"
    f"what is term life insurance,9,9,5400,40,3.2,{CLIENT}/blog/what-is-term-life-insurance,0.8\n"
    f"fixed annuities,2,2,8100,55,9.0,{CLIENT}/retirement/annuities,4.0\n"
    "beneficiary change form,15,,1300,10,0.5,,0.1\n"
    ",3,,100,,,,\n"
    "term life insurance,4,,22000,,,,\n"
)


HEADER = "Keyword;Position;Previous Position;Search Volume;CPC;Url;Traffic (%);Competition\n"


def semrush(rival_found: bool = True, calls: list[dict[str, str]] | None = None) -> httpx.Client:
    """A fake Semrush Analytics API."""

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        if calls is not None:
            calls.append(q)
        report, target = q["type"], q.get("domain") or q.get("phrase", "")
        if report == "domain_ranks":
            body = "Database;Domain;Rank;Organic Keywords;Organic Traffic;Organic Cost\n"
            body += f"us;{target};1500;3;900;2100.5\n"
        elif report == "domain_organic":
            if target == "rival.com" and not rival_found:
                body = "ERROR 50 :: NOTHING FOUND"
            elif target == "rival.com":
                body = (
                    HEADER
                    + "term life insurance;1;1;22000;12.5;https://www.rival.com/term;25.0;0.9\n"
                    "pet insurance for dogs;3;5;6600;4.1;https://www.rival.com/pet/dogs;5.0;0.7\n"
                )
            else:
                body = (
                    HEADER + f"term life insurance;7;4;22000;12.5;{CLIENT}/life/term;3.0;0.9\n"
                    f"fixed annuities;2;2;8100;9.0;{CLIENT}/retirement/annuities;4.0;0.6\n"
                )
        elif report == "phrase_this":
            body = "Keyword;Search Volume;CPC;Competition;Number of Results\n"
            body += f"{target};22000;12.5;0.9;150000000\n"
        elif report == "domain_organic_organic":
            body = (
                "Domain;Competitor Relevance;Common Keywords;Organic Keywords;Organic Traffic\n"
                "rival.com;0.61;140;5200;81000\n"
                "www.bigsure.com;0.44;95;12000;150000\n"
                "shop.example.com;0.40;80;300;900\n"
                "smallblog.net;0.05;3;40;20\n"
            )
        elif report == "phrase_organic":
            body = "Domain;Url\nrival.com;https://www.rival.com/term\nexample.com;https://www.example.com/life/term\n"
        else:
            body = "ERROR 132 :: API UNITS BALANCE IS ZERO"
        return httpx.Response(200, text=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


ON = ResearchSettings(
    enabled=True, api_key="k", monthly_units=100_000, pull_units=10_000, keyword_limit=50
)


# -- client, caps, parsing -----------------------------------------------------------------------


def test_parse_rows_and_errors() -> None:
    assert parse_rows("Keyword;Position\nterm;3\n") == [{"Keyword": "term", "Position": "3"}]
    assert parse_rows("ERROR 50 :: NOTHING FOUND") == []
    assert parse_rows("") == []
    with pytest.raises(SemrushError, match="API UNITS"):
        parse_rows("ERROR 132 :: API UNITS BALANCE IS ZERO")


def test_budget_refuses_before_spending_and_logs_actual_units(tmp_path: Path) -> None:
    log = UnitLog(tmp_path)
    assert UnitBudget(log, enabled=False, monthly_units=10, pull_units=10).refusal()
    assert "no monthly" in (
        UnitBudget(log, enabled=True, monthly_units=0, pull_units=10).refusal() or ""
    )
    log.add(
        UnitEntry(at=datetime.now(UTC), project_id="p", report="x", target="t", lines=1, units=95)
    )
    log.add(
        UnitEntry(
            at=datetime.now(UTC) - timedelta(days=40),
            project_id="p",
            report="x",
            target="t",
            lines=1,
            units=5000,
        )
    )
    budget = UnitBudget(log, enabled=True, monthly_units=100, pull_units=50)
    assert budget.left == 5 and budget.refusal() is None
    assert "used up" in (
        UnitBudget(log, enabled=True, monthly_units=95, pull_units=50).refusal() or ""
    )
    with pytest.raises(SemrushError, match="could use 20 units"):
        budget.reserve("domain_organic", 2)
    calls: list[dict[str, str]] = []
    big = UnitBudget(log, enabled=True, monthly_units=10_000, pull_units=1000)
    client = SemrushClient("k", big, log, "p", client=semrush(calls=calls))
    rows = client.domain_organic("rival.com", "us", 50)
    assert len(rows) == 2 and big.spent == 20  # charged for 2 lines returned, not 50 reserved
    assert calls[0]["display_limit"] == "50" and calls[0]["key"] == "k"
    assert log.used(datetime.now(UTC) - timedelta(minutes=1)) == 95 + 20
    with pytest.raises(SemrushError, match="API UNITS"):
        client._call("unknown_report", {}, 1, "x")
    broken = SemrushClient(
        "k",
        big,
        log,
        "p",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    with pytest.raises(SemrushError, match="could not reach"):
        broken.phrase_this("x", "us")
    client.close()


def test_csv_import_reads_exports_in_both_separators() -> None:
    rows = read_positions_csv(("﻿" + CLIENT_EXPORT).encode())
    assert [r.keyword for r in rows] == [
        "term life insurance",
        "what is term life insurance",
        "fixed annuities",
        "beneficiary change form",
    ]
    first = rows[0]
    assert (first.position, first.previous_position, first.volume, first.difficulty) == (
        4,
        6,
        22000,
        70,
    )
    assert rows[3].url is None and rows[3].previous_position is None
    semi = read_positions_csv(b"Keyword;Position;Search Volume\nx;1;1,000\n")
    assert semi[0].volume == 1000
    with pytest.raises(ImportError_, match="Keyword and Position"):
        read_positions_csv(b"Domain,Traffic\na,1\n")
    with pytest.raises(ImportError_, match="Keyword and Position"):
        read_positions_csv(b"Domain,Traffic\n")
    with pytest.raises(ImportError_, match="empty"):
        read_positions_csv(b"")
    assert read_positions_csv(b"Keyword,Position\n") == []


# -- snapshots -------------------------------------------------------------------------------------


def test_pull_places_keywords_in_themes_and_sums_demand(themed: World) -> None:
    calls: list[dict[str, str]] = []
    snapshot, files = pull_project(
        themed.store,
        themed.access,
        themed.project_id,
        ON,
        serp_keywords=["term life insurance", " "],
        http=lambda: semrush(calls=calls),
    )
    assert {c["type"] for c in calls} == {
        "domain_ranks",
        "domain_organic",
        "phrase_this",
        "phrase_organic",
        "domain_organic_organic",
    }
    sites = {w.website: w for w in snapshot.websites}
    assert sites["example.com"].role == "client" and sites["rival.com"].role == "competitor"
    assert sites["example.com"].overview.organic_traffic == 900
    term = next(r for r in sites["rival.com"].keywords if r.keyword == "term life insurance")
    assert term.theme_id is not None
    client_term = next(
        r for r in sites["example.com"].keywords if r.keyword == "term life insurance"
    )
    assert client_term.theme_id == term.theme_id  # by page for the client, by words for the rival
    demand = next(d for d in snapshot.demand if d.theme_id == term.theme_id)
    assert demand.volume == 22000 and demand.top10 == {"example.com": 1, "rival.com": 1}
    assert demand.traffic["rival.com"] == estimated_visits(22000, 1)
    assert (
        snapshot.keywords[0].serp[0].domain == "rival.com" and snapshot.keywords[0].volume == 22000
    )
    assert snapshot.units_spent == sum(10 * n for n in (1, 2, 1, 2, 1, 2)) + 4 * 40
    # 2 overviews, 2+2 keywords, 1 metric, 2 results, 4 organic competitors
    assert any("4 organic competitors of example.com" in n for n in snapshot.notes)
    assert Path(files["workbook"]).exists() and snapshot.theme_map_id
    with zipfile.ZipFile(files["workbook"]) as book:
        assert "Theme demand" in book.read("xl/workbook.xml").decode()


def test_pull_is_refused_without_switch_key_cap_or_room(themed: World) -> None:
    for settings, message in [
        (ResearchSettings(), "switched off"),
        (ResearchSettings(enabled=True, monthly_units=1000), "switched off"),
        (ResearchSettings(enabled=True, api_key="k"), "no monthly"),
        (
            ResearchSettings(
                enabled=True, api_key="k", monthly_units=10**6, pull_units=100, keyword_limit=200
            ),
            "could use up to",
        ),
    ]:
        with pytest.raises(SemrushError, match=message):
            pull_project(themed.store, themed.access, themed.project_id, settings, http=semrush)
    assert estimate_units(2, ResearchSettings(keyword_limit=200), 1) == 2 * (10 + 2000) + 110 + 800


def test_import_keeps_other_websites_and_movers_compare_snapshots(themed: World) -> None:
    store = research_store(themed.store)
    first, _ = pull_project(
        themed.store, themed.access, themed.project_id, ON, http=lambda: semrush(rival_found=False)
    )
    rival = next(w for w in first.websites if w.website == "rival.com")
    assert rival.keywords == [] and "no ranking keywords" in rival.note
    second, _ = import_csv(
        themed.store, themed.access, themed.project_id, "example.com", CLIENT_EXPORT.encode()
    )
    sites = {w.website: w for w in second.websites}
    assert sites["example.com"].source == "csv_import" and len(sites["example.com"].keywords) == 4
    assert sites["rival.com"].source == "semrush_api"  # kept from the previous snapshot
    assert store.previous(themed.project_id).snapshot_id == first.snapshot_id
    moved = {(m.keyword, m.before, m.after) for m in movers(first, second)}
    assert ("term life insurance", 7, 4) in moved  # up 3
    assert ("what is term life insurance", None, 9) in moved  # appeared
    assert ("fixed annuities", 2, 2) not in moved
    gone = movers(second, first)
    assert ("beneficiary change form", 15, None) in {(m.keyword, m.before, m.after) for m in gone}
    with pytest.raises(ImportError_):
        import_csv(themed.store, themed.access, themed.project_id, "example.com", b"x,y\n1,2\n")
    names = [t.name for t in research_tables(second, movers(first, second))]
    assert names == [
        "Websites",
        "Keywords",
        "Theme demand",
        "Movers",
        "Search results",
        "How to read",
    ]
    for bad in ("../x", "research-20260101-000000-000"):
        with pytest.raises(KeyError):
            store.get(themed.project_id, bad)
    with pytest.raises(KeyError):
        store.folder("../escape")


# -- CLI, API, cards ----------------------------------------------------------------


def test_cli_research(
    themed: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(webapi_main, "knowledge_access", lambda: themed.access)
    monkeypatch.setattr(ResearchSettings, "from_env", classmethod(lambda cls: ResearchSettings()))
    base = ["--project", themed.project_id, "--app-data", str(themed.root)]
    assert cli.main(["research", "show", *base]) == 1
    assert "no research yet" in capsys.readouterr().err
    assert cli.main(["research", "units", "--app-data", str(themed.root)]) == 0
    assert "unset" in json.loads(capsys.readouterr().out)["monthly_cap"]
    assert cli.main(["research", "estimate", *base, "--keyword", "a"]) == 0
    assert json.loads(capsys.readouterr().out)["worst_case_units"] == 2 * (10 + 2000) + 110 + 800
    assert cli.main(["research", "pull", *base]) == 2
    assert "switched off" in capsys.readouterr().err
    assert cli.main(["research", "import", *base]) == 2
    export = tmp_path / "positions.csv"
    export.write_text(CLIENT_EXPORT, encoding="utf-8")
    assert (
        cli.main(["research", "import", *base, "--website", "example.com", "--file", str(export)])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["websites"][0]["keywords"] == 4
    assert cli.main(["research", "show", *base]) == 0
    capsys.readouterr()
    assert cli.main(["research", "movers", *base]) == 1
    assert (
        cli.main(["research", "import", *base, "--website", "example.com", "--file", str(export)])
        == 0
    )
    capsys.readouterr()
    assert cli.main(["research", "movers", *base]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert cli.main(["research", "show"]) == 2
    assert cli.main(["research", "show", "--project", "nope", "--app-data", str(themed.root)]) == 2
    assert research_commands.register  # registered


WRITE = {"X-Requested-With": "content-processor"}


def test_api_research(themed: World, tmp_path: Path) -> None:
    from src.modules.research.api import (
        register_research_routes,  # noqa: F401 - wired by create_app
    )

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
    assert client.get("/api/research/status").json()["enabled"] is False
    assert client.get(base).json()["detail"] == "No research has been added yet."
    assert client.get(f"{base}/estimate?keywords=2").json()["websites"] == 2
    token, _ = signer.issue(themed.project_id)
    auth = {**WRITE, "X-Project-Token": token}
    assert client.post(f"{base}/pull", json={}, headers=WRITE).status_code == 401
    refused = client.post(f"{base}/pull", json={}, headers=auth)
    assert refused.status_code == 409 and "switched off" in refused.json()["detail"]
    body = {
        "website": "example.com",
        "file_name": "p.csv",
        "content_base64": base64.b64encode(CLIENT_EXPORT.encode()).decode(),
    }
    imported = client.post(f"{base}/import", json=body, headers=auth)
    assert imported.status_code == 200 and imported.json()["websites"][0]["source"] == "csv_import"
    assert (
        client.post(
            f"{base}/import", json={**body, "content_base64": "!!"}, headers=auth
        ).status_code
        == 422
    )
    bad = {**body, "content_base64": base64.b64encode(b"a,b\n1,2\n").decode()}
    assert client.post(f"{base}/import", json=bad, headers=auth).status_code == 422
    assert client.get(base).json()["snapshot_id"] == imported.json()["snapshot_id"]
    assert client.get(f"{base}/workbook").content[:2] == b"PK"
    assert client.get(f"{base}/movers").status_code == 404
    client.post(f"{base}/import", json=body, headers=auth)
    assert client.get(f"{base}/movers").json() == []
    assert client.get("/api/projects/nope/research").status_code == 404


def test_card_tools_and_preflight(themed: World) -> None:
    runner = themed.runner()
    serp = card("serp-intent-opportunity")
    check, _ = runner.preflight(
        serp, {"project": themed.project_id, "keyword": "term life insurance"}
    )
    assert any("needs search data" in p for p in check.problems)
    pull_project(
        themed.store,
        themed.access,
        themed.project_id,
        ON,
        serp_keywords=["term life insurance"],
        http=semrush,
    )
    check, _ = runner.preflight(
        serp, {"project": themed.project_id, "keyword": "term life insurance"}
    )
    assert check.runnable, check.problems
    play = runner.playground([themed.project_id])

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        text, is_error = run_tool(play, name, args)
        assert not is_error, text
        return dict(json.loads(text)["data"])

    ranks = call("get_keyword_rankings", {"website": "rival.com", "max_position": 3})
    assert [k["keyword"] for k in ranks["keywords"]] == [
        "term life insurance",
        "pet insurance for dogs",
    ]
    assert ranks["keywords"][0]["ref"].startswith("fact:ranking-")
    assert (
        call("get_keyword_rankings", {"website": "rival.com", "max_position": 1})["total_keywords"]
        == 2
    )
    serp_data = call("get_serp_overview", {"keyword": "Term life insurance"})
    assert serp_data["pulled_results"][0]["domain"] == "rival.com"
    assert {w["website"] for w in serp_data["project_websites"]} == {"example.com", "rival.com"}
    other = call("get_serp_overview", {"keyword": "fixed annuities"})
    assert other["pulled_results"] == [] and other["project_websites"][0]["position"] == 2
    metrics = call("get_keyword_metrics", {"keyword": "insurance"})
    assert {r["keyword"] for r in metrics["related"]} >= {"term life insurance"}
    assert call("get_keyword_metrics", {"keyword": "term life insurance"})["volume"] == 22000
    demand = call("get_search_demand", {})
    assert demand["themes"] and demand["themes"][0]["ref"].startswith("fact:demand-")
    text, is_error = run_tool(play, "get_keyword_metrics", {"keyword": "zebra"})
    assert is_error and "not in the latest research snapshot" in text
    text, is_error = run_tool(play, "get_keyword_rankings", {"website": "nope.org"})
    assert is_error
    # with Claude off, a keyword card's free context pack already carries the search data
    pack_runner = themed.runner(enabled=False)
    treemap = card("keyword-value-matrix")
    record = pack_runner.start(treemap, {"project": themed.project_id}, "pack")
    done = pack_runner.execute(treemap, record)
    assert done.status == "finished" and done.tool_calls >= 3
    pack = (pack_runner.runs.folder(done.run_id) / done.files["context_pack"]).read_text(
        encoding="utf-8"
    )
    assert '<tool_result tool="get_search_demand"' in pack and "pet insurance for dogs" in pack


def test_unit_log_location(themed: World) -> None:
    assert unit_log(themed.store).path.name == "units.jsonl"
