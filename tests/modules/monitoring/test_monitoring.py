"""Monitoring: diffs, prices, template churn, drift, alerts, store, watcher, CLI, API, tools."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
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
from src.modules.monitoring.diff import RunFiles, diff_runs, price_changes
from src.modules.monitoring.models import WatchSettings
from src.modules.monitoring.service import MonitorWatcher, monitor_store, run_project_check
from src.modules.monitoring.store import check_tables
from tests.modules.analyst.conftest import CLIENT, RIVAL, World, chunk, stamp
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.theming.test_theming import (
    PET,
    TERM,
    _add,
    _catalog,
    _client_pages,
    _page,
)

PRICED = TERM + "\nPlans start at $25 per month for healthy adults."


def _rival(run_id: str, *, extra_term: int = 0, cheaper: bool = False, retitle: bool = False):
    text = PRICED.replace("$25", "$19") if cheaper else PRICED
    pages = [
        _page(
            RIVAL,
            "/term",
            "Term life insurance plans: now cheaper" if retitle else "Term life insurance plans",
            run_id,
            text,
            1,
        ),
        _page(RIVAL, "/term/compare", "Compare term life insurance", run_id, TERM + " Compare.", 2),
        _page(RIVAL, "/term/rates", "Term life insurance rates", run_id, TERM, 3),
        _page(RIVAL, "/pet", "Pet insurance", run_id, PET, 4),
        _page(RIVAL, "/pet/dogs", "Pet insurance for dogs", run_id, PET + " Dogs.", 5),
        _page(RIVAL, "/pet/cats", "Pet insurance for cats", run_id, PET + " Cats.", 6),
    ]
    for i in range(extra_term):
        pages.append(
            _page(
                RIVAL,
                f"/term/new-{i}",
                f"Term life insurance for age {30 + i}",
                run_id,
                TERM + f" Ages {30 + i}.",
                10 + i,
            )
        )
    return pages


def _client(run_id: str, *, drop_last: bool = False, related: bool = False):
    pages = _client_pages(run_id)
    if drop_last:
        pages = pages[:-1]
    if related:  # a "related links" line added on every page: template churn, not a change
        pages = [
            (s, c.model_copy(update={"text": c.text + "\nRead our latest news"})) for s, c in pages
        ]
    return pages


@pytest.fixture
def watched(world: World) -> World:
    _add(world, "client", "run-c2", _client("run-c2"), 10, _catalog("run-c2"))
    _add(world, "competitor", "run-r2", _rival("run-r2"), 10)
    return world


def _next_scrapes(world: World) -> None:
    _add(
        world,
        "client",
        "run-c3",
        _client("run-c3", drop_last=True, related=True),
        20,
        _catalog("run-c3"),
    )
    _add(
        world,
        "competitor",
        "run-r3",
        _rival("run-r3", extra_term=3, cheaper=True, retitle=True),
        20,
    )


def _check(world: World, **kw: Any) -> Any:
    return run_project_check(world.store, world.access, world.project_id, **kw)[0]


# -- diff --------------------------------------------------------------------------------


def test_price_changes_match_by_wording() -> None:
    changes = price_changes(
        ["Plans start at $25 per month today."], ["Plans start at $19 per month today."]
    )
    assert [(c.before, c.after) for c in changes] == [("$25 per month", "$19 per month")]
    assert price_changes(["APR is 5.1% now"], ["APR is 4.9% now"])[0].after == "4.9%"
    assert price_changes(["$25 here"], ["completely different $19 wording"]) == []


def _files(pages: dict[str, tuple[str, list[str]]]) -> RunFiles:
    files = RunFiles()
    for n, (url, (title, lines)) in enumerate(pages.items()):
        st = stamp(CLIENT, url, title, "r")
        files.stamps[st.url] = st
        files.chunks[st.url] = [chunk(st, f"c{n}", "\n".join(lines), 0, [title])]
    return files


def test_diff_separates_template_churn_and_ignores_formatting_and_missing_fields() -> None:
    old = _files({f"/p{i}": (f"Page {i}", [f"Body {i}.", "Old footer"]) for i in range(4)})
    new_pages = {f"/p{i}": (f"Page {i}", [f"body  {i}", "Read more"]) for i in range(4)}
    new_pages["/p0"] = ("Page 0", ["Body 0 now mentions annuities.", "Read more"])
    new = _files(new_pages)
    new.stamps[CLIENT + "/p1"] = new.stamps[CLIENT + "/p1"].model_copy(
        update={"title": "Page one renamed"}
    )
    old.stamps[CLIENT + "/p2"] = old.stamps[CLIENT + "/p2"].model_copy(
        update={"meta_description": None}
    )
    new.stamps[CLIENT + "/p2"] = new.stamps[CLIENT + "/p2"].model_copy(
        update={"meta_description": "Now set"}
    )
    result = diff_runs(old, new)
    lines = {t.line: (t.added_on, t.removed_on) for t in result.template}
    assert lines == {"Read more": (4, 0), "Old footer": (0, 4)}
    changed = {c.url: c for c in result.changed}
    assert set(changed) == {CLIENT + "/p0", CLIENT + "/p1"}
    assert changed[CLIENT + "/p0"].added_text == ["Body 0 now mentions annuities."]
    assert changed[CLIENT + "/p1"].head_changes["title"]["after"] == "Page one renamed"
    assert result.unchanged == 2  # p2 (description only newly captured) and p3 (formatting)
    assert 0 < changed[CLIENT + "/p0"].drift <= 1


# -- checks ---------------------------------------------------------------------------------


def test_first_check_is_a_baseline_when_websites_have_one_scrape(world: World) -> None:
    check = _check(world)
    notes = {d.website: d.note for d in check.websites}
    assert all("first scrape" in n for n in notes.values())
    assert check.alerts == [] and check.previous_check_id is None and check.theme_map_id


def test_second_check_finds_push_price_rewrite_removal_and_drift(watched: World) -> None:
    first = _check(watched)
    again = _check(watched)
    assert {d.note for d in again.websites} == {"nothing new scraped since the last check"}
    assert again.previous_check_id == first.check_id and again.alerts == []
    _next_scrapes(watched)
    check = _check(watched)
    by_site = {d.website: d for d in check.websites}
    rival, client = by_site["rival.com"], by_site["example.com"]
    assert (rival.baseline_run, rival.latest_run) == ("run-r2", "run-r3")
    assert len(rival.new_pages) == 3 and all(p.theme_id for p in rival.new_pages)
    term = next(c for c in rival.changed_pages if c.url == RIVAL + "/term")
    assert [(p.before, p.after) for p in term.prices] == [("$25 per month", "$19 per month")]
    assert term.head_changes["title"]["after"] == "Term life insurance plans: now cheaper"
    assert [p.url for p in client.removed_pages] == [CLIENT + "/help/forms/beneficiary"]
    assert client.changed_pages == [] and client.template_changes[0].line == "Read our latest news"
    kinds = {(a.kind, a.severity, a.website) for a in check.alerts}
    assert ("competitor_push_in_client_theme", "high", "rival.com") in kinds
    assert ("price_change", "high", "rival.com") in kinds
    assert ("client_pages_removed", "high", "example.com") in kinds
    assert ("competitor_rewrites", "medium", "rival.com") in kinds
    assert check.alerts[0].severity == "high"
    term_drift = next(t for t in check.theme_drift if t.new_pages.get("rival.com"))
    assert term_drift.new_pages == {"rival.com": 3} and term_drift.ownership_before == "contested"
    assert term_drift.ownership_after == "competitor_led"
    shift = [a for a in check.alerts if a.kind == "ownership_shift"]
    assert shift and shift[0].severity == "high"
    assert check.stats["price_changes"] == 1 and check.stats["new_pages"] == 3


def test_store_watch_and_workbook(watched: World) -> None:
    store = monitor_store(watched.store)
    assert store.latest(watched.project_id) is None and store.history(watched.project_id) == []
    assert store.watch(watched.project_id) == WatchSettings()
    check, files = run_project_check(watched.store, watched.access, watched.project_id)
    assert store.latest(watched.project_id) == check
    assert store.get(watched.project_id, check.check_id) == check
    assert store.history(watched.project_id) == [check.check_id]
    for bad in ("../x", "check-20260101-000000-000"):
        with pytest.raises(KeyError):
            store.get(watched.project_id, bad)
    with pytest.raises(KeyError):
        store.folder("../escape")
    assert [t.name for t in check_tables(check)] == [
        "Alerts",
        "Websites",
        "Changed pages",
        "New and removed pages",
        "Theme drift",
        "How to read",
    ]
    with zipfile.ZipFile(files["workbook"]) as book:
        assert "Theme drift" in book.read("xl/workbook.xml").decode()
    assert not files["check"].startswith("\\\\?\\")
    saved = store.set_watch(watched.project_id, WatchSettings(rescrape_every_days=7))
    assert store.watch(watched.project_id) == saved


class FakeRunner:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def submit_all(self, project_id: str, *, started_by: str) -> list[Any]:
        self.calls.append((project_id, started_by))
        if self.fail:
            raise ValueError("No website has URLs waiting to be scraped.")
        return []


def test_watcher_checks_after_new_scrapes_and_rescrapes_only_when_opted_in(watched: World) -> None:
    runner = FakeRunner()
    watcher = MonitorWatcher(watched.store, watched.access, runner, wake_s=0.01)  # type: ignore[arg-type]
    first = watcher.tick()
    assert first == {"checked": [watched.project_id], "rescraped": []}
    assert monitor_store(watched.store).latest(watched.project_id).trigger == "auto"
    assert watcher.tick() == {"checked": [], "rescraped": []}  # nothing new since
    store = monitor_store(watched.store)
    store.set_watch(watched.project_id, WatchSettings(auto_check=False, rescrape_every_days=1))
    later = datetime.now(UTC) + timedelta(days=3)
    assert watcher.tick(later) == {"checked": [], "rescraped": [watched.project_id]}
    assert runner.calls == [(watched.project_id, "scheduled monitoring")]
    assert watcher.tick(later + timedelta(hours=1))["rescraped"] == []  # waits a full interval
    failing = MonitorWatcher(watched.store, watched.access, FakeRunner(fail=True))  # type: ignore[arg-type]
    store.set_watch(watched.project_id, WatchSettings(auto_check=False, rescrape_every_days=1))
    assert failing.tick(later) == {"checked": [], "rescraped": []}
    off = MonitorWatcher(watched.store, watched.access, None)
    assert off.tick(later)["rescraped"] == []
    watcher.start()
    watcher.stop()


def test_watcher_skips_projects_with_scrapes_running(watched: World) -> None:
    runs = watched.store.runs(watched.project_id)
    watched.store.save_run(runs[0].model_copy(update={"status": "running"}))
    assert MonitorWatcher(watched.store, watched.access, None).tick()["checked"] == []


# -- CLI, API, tools ------------------------------------------------------------------------------


def test_cli_monitor(
    watched: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(webapi_main, "knowledge_access", lambda: watched.access)
    base = ["--project", watched.project_id, "--app-data", str(watched.root)]
    assert cli.main(["monitor", "show", *base]) == 1
    assert "no check yet" in capsys.readouterr().err
    assert cli.main(["monitor", "check", *base]) == 0
    out = json.loads(capsys.readouterr().out)
    assert Path(out["files"]["workbook"]).exists()
    assert cli.main(["monitor", "show", *base]) == 0
    assert json.loads(capsys.readouterr().out)["check_id"] == out["check_id"]
    assert (
        cli.main(
            [
                "monitor",
                "watch",
                *base,
                "--auto-check",
                "off",
                "--rescrape-every-days",
                "7",
                "--push-threshold",
                "5",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["rescrape_every_days"] == 7
    assert cli.main(["monitor", "watch", *base, "--rescrape-every-days", "400"]) == 2
    assert cli.main(["monitor", "show", "--project", "nope", "--app-data", str(watched.root)]) == 2


WRITE = {"X-Requested-With": "content-processor"}


def test_api_monitoring(watched: World, tmp_path: Path) -> None:
    signer = TokenSigner(b"k" * 32)
    exports = tmp_path / "exports"
    exports.mkdir()
    app = create_app(
        exports,
        projects=(watched.store, ScrapeRunner(watched.store), signer),
        knowledge=watched.access,
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    base = f"/api/projects/{watched.project_id}/monitoring"
    assert client.get(base).json()["detail"] == "No monitoring check has been run yet."
    assert client.get("/api/projects/nope/monitoring").status_code == 404
    assert client.post(base, json={}, headers=WRITE).status_code == 401
    token, _ = signer.issue(watched.project_id)
    auth = {**WRITE, "X-Project-Token": token}
    ran = client.post(base, json={}, headers=auth)
    assert ran.status_code == 200 and client.get(base).json()["check_id"] == ran.json()["check_id"]
    assert client.get(f"{base}/workbook").content[:2] == b"PK"
    assert client.get(f"{base}/watch").json()["auto_check"] is True
    body = {"auto_check": False, "rescrape_every_days": 7, "push_threshold": 4}
    assert client.post(f"{base}/watch", json=body, headers=WRITE).status_code == 401
    assert (
        client.post(
            f"{base}/watch", json={**body, "rescrape_every_days": 400}, headers=auth
        ).status_code
        == 422
    )
    saved = client.post(f"{base}/watch", json=body, headers=auth).json()
    assert saved["rescrape_every_days"] == 7 and saved["auto_check"] is False


def test_card_tools(watched: World) -> None:
    _next_scrapes(watched)
    play = watched.runner().playground([watched.project_id])
    data = json.loads(run_tool(play, "get_monitoring_alerts", {})[0])["data"]
    assert data["alerts"] and data["alerts"][0]["ref"].startswith("fact:alert-")
    assert data["alerts"][0]["severity"] == "high"
    again = json.loads(run_tool(play, "get_monitoring_alerts", {})[0])["data"]
    assert again["check_id"] == data["check_id"]
    fresh = json.loads(run_tool(play, "get_monitoring_alerts", {"run_check": True})[0])["data"]
    assert fresh["check_id"] != data["check_id"]
    changes = json.loads(run_tool(play, "get_changes", {"website": "example.com"})[0])["data"]
    assert changes["template_changes"][0]["line"] == "Read our latest news"
    rival = json.loads(run_tool(play, "get_changes", {"website": "rival.com"})[0])["data"]
    priced = next(c for c in rival["changed_pages"] if c["prices"])
    assert priced["prices"][0]["after"] == "$19 per month"


def test_tracking_and_excluded_pages_are_not_new_or_removed(tmp_path: Path) -> None:
    from src.modules.monitoring.diff import load_run_files

    def write(run: str, stamps: list[Any]) -> None:
        (tmp_path / f"{run}.stamps.jsonl").write_text(
            "".join(s.model_dump_json() + "\n" for s in stamps), encoding="utf-8"
        )

    page = stamp(CLIENT, "/form", "Form", "r")
    pixel = stamp(CLIENT, "/pixel", "", "r", is_tracking=True)
    write("old", [page, pixel])
    write(
        "new",
        [page.model_copy(update={"knowledge_eligible": False, "exclusion_reason": "duplicate"})],
    )
    old, new = load_run_files(tmp_path, "old"), load_run_files(tmp_path, "new")
    assert set(old.stamps) == {CLIENT + "/form"} and new.stamps == {}
    result = diff_runs(old, new)
    assert result.removed == [] and result.added == []  # still there, only excluded now
    assert load_run_files(tmp_path, "missing").stamps == {}
