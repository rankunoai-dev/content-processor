"""Gap Analysis: findings from themes, research, monitoring and catalogs; status; CLI; API; tool."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.modules.analyst.tools import run_tool
from src.modules.content_processor import cli
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.webapi import __main__ as webapi_main
from src.modules.content_processor.webapi.app import create_app
from src.modules.gaps.build import build_findings, finding_id, priority
from src.modules.gaps.service import build_project_gaps, gap_store
from src.modules.gaps.store import gap_tables
from src.modules.monitoring.service import run_project_check
from src.modules.research.service import import_csv, pull_project
from src.modules.theming.service import theme_store
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.monitoring.test_monitoring import _next_scrapes
from tests.modules.research.test_research import CLIENT_EXPORT, ON, semrush
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture


def _kinds(report: Any) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for f in report.findings:
        out.setdefault(f.kind, []).append(f)
    return out


def test_theme_findings_alone_say_what_is_missing(themed: World) -> None:
    report, files = build_project_gaps(themed.store, themed.access, themed.project_id)
    kinds = _kinds(report)
    gap = kinds["theme_gap"][0]
    assert "pet" in gap.title.casefold() and gap.competitors == ["rival.com"]
    assert any(e.kind == "pillar" and e.url for e in gap.evidence)
    funnel = kinds["funnel_gap"][0]
    assert "compare" in funnel.title and "term" in (funnel.theme or "").casefold()
    assert "keyword_gap" not in kinds and len(report.missing_inputs) == 2
    assert report.findings == sorted(report.findings, key=lambda f: (-f.priority, f.kind, f.title))
    assert Path(files["workbook"]).exists() and not files["report"].startswith("\\\\?\\")
    with zipfile.ZipFile(files["workbook"]) as book:
        assert "Evidence" in book.read("xl/workbook.xml").decode()
    assert [t.name for t in gap_tables(report)] == [
        "Findings",
        "Evidence",
        "Resolved",
        "How to read",
    ]


def test_all_modules_together_add_keyword_page_two_and_momentum(themed: World) -> None:
    pull_project(themed.store, themed.access, themed.project_id, ON, http=semrush)
    import_csv(
        themed.store, themed.access, themed.project_id, "example.com", CLIENT_EXPORT.encode()
    )
    run_project_check(themed.store, themed.access, themed.project_id)
    _next_scrapes(themed)
    run_project_check(themed.store, themed.access, themed.project_id)
    report, _ = build_project_gaps(themed.store, themed.access, themed.project_id)
    kinds = _kinds(report)
    assert report.missing_inputs == [] and report.research_snapshot_id and report.monitor_check_id
    keyword = kinds["keyword_gap"][0]
    assert any("pet insurance for dogs" in e.label for e in keyword.evidence)
    assert keyword.demand >= 6600
    near = kinds["striking_distance"][0]
    assert any("beneficiary change form" in e.label for e in near.evidence)
    momentum = kinds["competitor_momentum"][0]
    assert momentum.competitors == ["rival.com"] and len(momentum.evidence) == 3
    pet = next(f for f in kinds["theme_gap"] if "pet" in f.title.casefold())
    assert pet.demand >= 6600  # research demand now weighs the theme gap
    assert priority("theme_gap", competitor_pages=3, demand=6600) == pet.priority


def test_statuses_carry_over_and_gone_findings_are_resolved(themed: World) -> None:
    store = gap_store(themed.store)
    report, _ = build_project_gaps(themed.store, themed.access, themed.project_id)
    target = report.findings[0]
    entry = store.set_status(themed.project_id, target.finding_id, "accepted", "brief written")
    assert entry.status == "accepted"
    assert store.latest(themed.project_id).findings[0].status == "accepted"
    again, _ = build_project_gaps(themed.store, themed.access, themed.project_id)
    carried = next(f for f in again.findings if f.finding_id == target.finding_id)
    assert (carried.status, carried.note) == ("accepted", "brief written")
    with pytest.raises(KeyError):
        store.set_status(themed.project_id, "gap-000000000000", "done")
    with pytest.raises(KeyError):
        store.set_status(themed.project_id, "../x", "done")
    with pytest.raises(KeyError):
        store.folder("../escape")
    # the pet theme disappears from the map: its gap is reported as resolved
    latest_map = theme_store(themed.store).latest(themed.project_id)
    pruned = latest_map.model_copy(
        update={"themes": [t for t in latest_map.themes if not any("pet" in x for x in t.terms)]}
    )
    theme_store(themed.store).save(pruned)
    later, _ = build_project_gaps(themed.store, themed.access, themed.project_id)
    assert any("pet" in t.casefold() for t in later.resolved)


def test_offering_gaps_ignore_navigation_and_similar_names(themed: World) -> None:
    latest_map = build_project_gaps(themed.store, themed.access, themed.project_id)[0]
    theme_map = theme_store(themed.store).latest(themed.project_id)
    roles = {"example.com": "client", "rival.com": "competitor"}
    catalogs = {
        "example.com": [("Term Life Insurance", None), ("Annuities", None)],
        "rival.com": [
            ("Term life insurance plans", None),
            ("Pet Insurance", "https://www.rival.com/pet"),
            ("Industries We Serve", None),
            ("Next post How to choose a policy for your family today", None),
            ("Read more", None),
        ],
    }
    findings = build_findings(theme_map, roles, catalogs=catalogs)
    offers = [f for f in findings if f.kind == "offering_gap"]
    assert [f.title for f in offers] == [
        "Competitors offer Pet Insurance; the client's catalog does not"
    ]
    assert offers[0].evidence[0].url == "https://www.rival.com/pet"
    assert offers[0].finding_id == finding_id("offering_gap", "insurance pet")
    assert latest_map.report_id


# -- CLI, API, tool ---------------------------------------------------------------------------


def test_cli_gaps(
    themed: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(webapi_main, "knowledge_access", lambda: themed.access)
    base = ["--project", themed.project_id, "--app-data", str(themed.root)]
    assert cli.main(["gaps", "show", *base]) == 1
    assert "no gap analysis yet" in capsys.readouterr().err
    assert cli.main(["gaps", "build", *base]) == 0
    built = json.loads(capsys.readouterr().out)
    first = built["findings"][0]["id"]
    assert cli.main(["gaps", "show", *base]) == 0
    assert json.loads(capsys.readouterr().out)["report_id"] == built["report_id"]
    assert cli.main(["gaps", "status", *base]) == 2
    assert (
        cli.main(["gaps", "status", *base, "--finding", first, "--set", "done", "--note", "ok"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "done"
    assert (
        cli.main(["gaps", "status", *base, "--finding", "gap-000000000000", "--set", "done"]) == 2
    )
    assert cli.main(["gaps", "show", "--project", "nope", "--app-data", str(themed.root)]) == 2


WRITE = {"X-Requested-With": "content-processor"}


def test_api_gaps(themed: World, tmp_path: Path) -> None:
    signer = TokenSigner(b"k" * 32)
    exports = tmp_path / "exports"
    exports.mkdir()
    app = create_app(
        exports,
        projects=(themed.store, ScrapeRunner(themed.store), signer),
        knowledge=themed.access,
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    base = f"/api/projects/{themed.project_id}/gaps"
    assert client.get(base).json()["detail"] == "No gap analysis has been built yet."
    assert client.get("/api/projects/nope/gaps").status_code == 404
    assert client.post(base, json={}, headers=WRITE).status_code == 401
    token, _ = signer.issue(themed.project_id)
    auth = {**WRITE, "X-Project-Token": token}
    built = client.post(base, json={}, headers=auth).json()
    fid = built["findings"][0]["finding_id"]
    assert client.get(base).json()["report_id"] == built["report_id"]
    status = f"{base}/findings/{fid}/status"
    assert client.post(status, json={"status": "dismissed"}, headers=WRITE).status_code == 401
    assert client.post(status, json={"status": "maybe"}, headers=auth).status_code == 422
    saved = client.post(status, json={"status": "dismissed", "note": "out of scope"}, headers=auth)
    assert saved.json()["status"] == "dismissed"
    missing = f"{base}/findings/gap-000000000000/status"
    assert client.post(missing, json={"status": "done"}, headers=auth).status_code == 404
    assert client.get(f"{base}/workbook").content[:2] == b"PK"


def test_card_tool(themed: World) -> None:
    play = themed.runner().playground([themed.project_id])
    data = json.loads(run_tool(play, "get_gap_findings", {})[0])["data"]
    assert data["findings"] and data["findings"][0]["ref"].startswith("fact:gap-")
    first = data["findings"][0]
    gap_store(themed.store).set_status(themed.project_id, first["finding_id"], "dismissed")
    again = json.loads(run_tool(play, "get_gap_findings", {})[0])["data"]
    assert again["report_id"] == data["report_id"]  # reused
    assert first["finding_id"] not in {f["finding_id"] for f in again["findings"]}
    only = json.loads(run_tool(play, "get_gap_findings", {"kind": "funnel_gap"})[0])["data"]
    assert {f["kind"] for f in only["findings"]} == {"funnel_gap"}
    fresh = json.loads(run_tool(play, "get_gap_findings", {"rebuild": True})[0])["data"]
    assert fresh["report_id"] != data["report_id"]


def _site(website: str, role: str, rows: list[tuple[str, int, int]], **extra: Any) -> Any:
    from datetime import UTC, datetime

    from src.modules.research.models import KeywordRow, WebsiteResearch

    return WebsiteResearch(
        website=website,
        name=website,
        role=role,  # type: ignore[arg-type]
        source="semrush_api",
        pulled_at=datetime(2026, 11, 4, tzinfo=UTC),
        keywords=[
            KeywordRow(
                keyword=k, position=p, volume=v, url=f"https://{website}/{k.replace(' ', '-')}"
            )
            for k, p, v in rows
        ],
        **extra,
    )


def _snapshot(client: Any, *competitors: Any) -> Any:
    from datetime import UTC, datetime

    from src.modules.research.models import ResearchSnapshot

    return ResearchSnapshot(
        snapshot_id="research-20261104-120000-000",
        project_id="p",
        created_at=datetime(2026, 11, 4, tzinfo=UTC),
        database="us",
        websites=[client, *competitors],
    )


def test_keyword_gaps_by_the_clients_position_and_the_completeness_of_its_data() -> None:
    from types import SimpleNamespace

    from src.modules.gaps.build import _keyword_findings
    from src.modules.gaps.models import GapSettings
    from src.modules.research.models import DomainOverview

    coupa = _site(
        "coupa.com",
        "competitor",
        [
            ("supply chain esg", 2, 9000),
            ("esg compliance software", 5, 3000),
            ("scope 3 tool", 8, 400),
            ("supplier risk", 3, 5000),
            ("spend analysis", 4, 2000),
            ("procure to pay", 12, 7000),
        ],
    )
    rival = _site(
        "rival.com", "competitor", [("supply chain esg", 6, 9000), ("supplier risk", 1, 5000)]
    )
    client_rows = [
        ("supplier risk", 45, 5000),
        ("spend analysis", 15, 2000),
        ("vendor portal", 14, 800),
    ]
    roles = {"client.com": "client", "coupa.com": "competitor", "rival.com": "competitor"}
    themes = SimpleNamespace(themes=[])

    full = _snapshot(_site("client.com", "client", client_rows), coupa, rival)
    kinds = {f.kind: f for f in _keyword_findings(full, themes, roles)}  # type: ignore[arg-type]
    deficit = kinds["keyword_gap"]
    assert (
        deficit.title
        == "Complete deficit in keywords outside the themes: 3 keyword(s), 12,400 searches a month"
    )
    assert deficit.detail.startswith(
        "coupa.com ranks for 3 of them with 3 page(s); rival.com for 1 with 1 page(s)."
    )
    assert (
        "does not rank in the top 100" in deficit.detail
        and "all 3 ranking keywords" in deficit.detail
    )
    assert "2 other keyword(s) of this theme are on page two" in deficit.detail
    assert [e.label.split(" (")[0] for e in deficit.evidence] == [
        "supply chain esg",
        "esg compliance software",
        "scope 3 tool",
    ]
    assert "#2 coupa.com" in deficit.evidence[0].label  # the best-ranking competitor
    behind = kinds["keyword_behind"]
    assert (
        behind.evidence[0].label
        == "supplier risk (#1 rival.com, 5,000/mo; client #45 https://client.com/supplier-risk)"
    )
    assert "keyword_unverified" not in kinds
    assert {e.label.split(" (")[0] for e in kinds["striking_distance"].evidence} == {
        "spend analysis",
        "vendor portal",
    }

    cut = _site(
        "client.com",
        "client",
        client_rows,
        overview=DomainOverview(organic_keywords=900),
        keyword_limit=3,
    )
    cut_kinds = {f.kind: f for f in _keyword_findings(_snapshot(cut, coupa, rival), themes, roles)}  # type: ignore[arg-type]
    assert "keyword_gap" not in cut_kinds
    probable = cut_kinds["keyword_unverified"]
    assert probable.title.startswith("Probable gap in keywords outside the themes: 3 keyword(s)")
    assert "the top 3 of about 900 ranking keywords" in probable.detail
    assert "client not in the data pulled" in probable.evidence[0].label

    strict = GapSettings(min_volume=1000, competitor_top=3)
    s_kinds = {f.kind: f for f in _keyword_findings(full, themes, roles, strict)}  # type: ignore[arg-type]
    assert [e.label.split(" (")[0] for e in s_kinds["keyword_gap"].evidence] == ["supply chain esg"]
    assert {e.label.split(" (")[0] for e in s_kinds["striking_distance"].evidence} == {
        "spend analysis"
    }

    alone = _snapshot(coupa)  # the client is not in the search data at all
    no_client = {f.kind: f for f in _keyword_findings(alone, themes, roles)}  # type: ignore[arg-type]
    assert "there is no search data for the client yet" in no_client["keyword_unverified"].detail
    empty = _snapshot(
        _site("client.com", "client", []), coupa
    )  # complete and empty: ranks for none
    assert "keyword_gap" in {f.kind for f in _keyword_findings(empty, themes, roles)}  # type: ignore[arg-type]


def test_api_gap_settings(themed: World, tmp_path: Path) -> None:
    signer = TokenSigner(b"k" * 32)
    exports = tmp_path / "exports"
    exports.mkdir()
    app = create_app(
        exports,
        projects=(themed.store, ScrapeRunner(themed.store), signer),
        knowledge=themed.access,
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    base = f"/api/projects/{themed.project_id}/gaps"
    assert client.get(f"{base}/settings").json() == {"min_volume": 0, "competitor_top": 10}
    write = {"X-Requested-With": "content-processor"}
    assert (
        client.post(
            f"{base}/settings", json={"min_volume": 500, "competitor_top": 10}, headers=write
        ).status_code
        == 401
    )
    token, _ = signer.issue(themed.project_id)
    auth = {**write, "X-Project-Token": token}
    saved = client.post(
        f"{base}/settings", json={"min_volume": 500, "competitor_top": 5}, headers=auth
    )
    assert saved.json() == {"min_volume": 500, "competitor_top": 5}
    assert (
        client.post(
            f"{base}/settings", json={"min_volume": 500, "competitor_top": 50}, headers=auth
        ).status_code
        == 422
    )
    report = client.post(base, headers=auth, json={}).json()
    assert report["settings"] == {"min_volume": 500, "competitor_top": 5}
