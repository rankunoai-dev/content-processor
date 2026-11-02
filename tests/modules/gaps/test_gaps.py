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
