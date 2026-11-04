"""Smart recrawl: sitemaps, core pages, the plan, the queued scrapes; richer change alerts."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from src.modules.monitoring import recrawl
from src.modules.monitoring.check import _competitor_moves
from src.modules.monitoring.diff import RunFiles, diff_runs, load_run_files
from src.modules.monitoring.models import SiteDiff, WatchSettings
from src.modules.monitoring.recrawl import SitemapIndex, core_pages, plan_site, read_sitemaps
from src.modules.monitoring.service import MonitorWatcher, monitor_store, recrawl_project
from src.modules.theming.service import build_project_themes
from tests.modules.analyst.conftest import World, chunk, stamp
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.monitoring.test_monitoring import watched as watched  # noqa: F401 - fixture

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
URLSET = (
    '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://www.rival.com/term</loc><lastmod>2026-11-01</lastmod></url>"
    "<url><loc>https://www.rival.com/pet/dogs</loc><lastmod>2026-09-01T10:00:00Z</lastmod></url>"
    "<url><loc>https://www.rival.com/about</loc></url>"
    "</urlset>"
)


def sitemap_site(requests: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(str(request.url))
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nSitemap: https://www.rival.com/index.xml\n"
                "Sitemap: https://evil.example/steal.xml\n",
            )
        if path == "/index.xml":
            return httpx.Response(
                200,
                text='<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<sitemap><loc>https://www.rival.com/pages.xml.gz</loc></sitemap>"
                "<sitemap><loc>https://other.example/x.xml</loc></sitemap>"
                "<sitemap><loc>https://www.rival.com/bomb.xml</loc></sitemap></sitemapindex>",
            )
        if path == "/pages.xml.gz":
            return httpx.Response(200, content=gzip.compress(URLSET.encode()))
        if path == "/bomb.xml":
            return httpx.Response(
                200,
                text='<!DOCTYPE x [<!ENTITY a "aaaa">]><urlset><url><loc>&a;</loc></url></urlset>',
            )
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sitemaps_follow_indexes_on_the_same_site_and_refuse_dtds() -> None:
    requests: list[str] = []
    index = read_sitemaps("rival.com", sitemap_site(requests))
    assert index.files == ["https://www.rival.com/index.xml", "https://www.rival.com/pages.xml.gz"]
    assert index.addresses == 3 and len(index.lastmod) == 2
    assert index.of("https://www.rival.com/term") == datetime(2026, 11, 1, tzinfo=UTC)
    assert index.of("https://www.rival.com/about") is None
    assert not any("evil.example" in r or "other.example" in r for r in requests)
    empty = read_sitemaps(
        "nothing.com", httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(404)))
    )
    assert empty.files == [] and empty.note == "no sitemap found"
    walled = read_sitemaps(
        "walled.com", httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(403)))
    )
    assert walled.note.startswith("the website refused the sitemap request (HTTP 403)")


def test_core_pages_are_ranked_by_why() -> None:
    urls = [
        "https://rival.com/",
        "https://rival.com/blog/2024/how-to-save",
        "https://rival.com/plans/compare-term-life",
        "https://rival.com/about",
        "https://rival.com/blog/2025/pinned-story",
        "https://rival.com/errors",
    ]
    core = core_pages(
        urls, website="rival.com", theme_map=None, site=None, pinned=[urls[4]], limit=3
    )
    assert list(core.values()) == [
        "pinned by a person",
        "address names compare, plans",
        "home page or one level from it",
    ]
    assert recrawl.url_key(urls[1]) not in core
    assert recrawl.url_key(urls[5]) not in core_pages(
        urls, website="rival.com", theme_map=None, site=None
    )


def _decisions(**settings: Any) -> dict[str, tuple[str, str]]:
    urls = [f"https://rival.com/{p}" for p in ("new", "core", "fresh", "old", "quiet", "idle")]
    key = recrawl.url_key
    captured = {key(u): NOW - timedelta(days=5) for u in urls[1:]}
    captured[key(urls[3])] = NOW - timedelta(days=40)
    sitemap = SitemapIndex(
        lastmod={
            key(urls[2]): NOW - timedelta(days=1),  # changed after our capture
            key(urls[4]): NOW - timedelta(days=60),  # unchanged since
        },
        files=["x"],
        addresses=6,
    )
    pages = plan_site(
        urls,
        captured=captured,
        core={key(urls[1]): "pillar page of a theme"},
        sitemap=sitemap,
        settings=WatchSettings(**settings),
        now=NOW,
    )
    return {p.url.rsplit("/", 1)[1]: (p.action, p.reason) for p in pages}


def test_plan_decides_page_by_page() -> None:
    got = _decisions()
    assert got["new"] == ("check", "never captured")
    assert got["core"] == ("check", "core page (pillar page of a theme)")
    assert got["fresh"] == ("check", "sitemap shows a change on 3 Nov 2026")
    assert got["old"] == ("check", "last checked 40 days ago")
    assert got["quiet"] == ("skip", "sitemap: unchanged since the last capture")
    assert got["idle"] == ("skip", "not a core page; next check by 29 Nov 2026")
    assert _decisions(other_every_days=3)["idle"] == ("check", "last checked 5 days ago")


def _fill_lists(w: World) -> None:
    """Each website's address list = the pages of its latest scrape (the fixture has none)."""
    for site in w.store.sites(w.project_id):
        run = recrawl._latest_finished(w.store.runs(w.project_id), site.site_id)
        if run is None:
            continue
        folder = w.store.run_dir(w.project_id, site.site_id, run.run_id)
        files = load_run_files(folder, run.run_id)
        w.store.add_urls(w.project_id, site.site_id, sorted(files.stamps))


class SubmittingRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def submit(self, project_id: str, site_id: str, **kw: Any) -> Any:
        self.calls.append({"site": site_id, **kw})
        if site_id.startswith("rival"):
            raise ValueError("A scrape of Rival is already queued or running.")

        class State:
            run_id = f"{site_id}_20261104-120000"

        return State()


def test_recrawl_queues_scrapes_with_their_skips_and_keeps_the_plan(
    watched: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_project_themes(watched.store, watched.access, watched.project_id)
    _fill_lists(watched)
    # every page captured 5 days ago: core pages are checked, the others carried forward
    monkeypatch.setattr(
        recrawl,
        "_captures",
        lambda _folder, _run: {
            recrawl.url_key(u.url): NOW - timedelta(days=5)
            for s in watched.store.sites(watched.project_id)
            for u in watched.store.urls(watched.project_id, s.site_id)
        },
    )
    store = monitor_store(watched.store)
    store.set_watch(watched.project_id, WatchSettings(rescrape_every_days=7, core_limit=2))
    preview = recrawl_project(watched.store, None, watched.project_id, http=sitemap_site, now=NOW)
    assert preview.trigger == "preview" and all(s.run_id is None for s in preview.sites)
    by_role = {s.role: s for s in preview.sites}
    rival = by_role["competitor"]
    assert rival.sitemap_files and rival.core == 2 and rival.check == 3
    changed = [p for p in rival.pages if p.reason.startswith("sitemap shows a change")]
    assert [p.url for p in changed] == ["https://www.rival.com/term"]  # lastmod 1 Nov > 30 Oct
    assert rival.skip == len(rival.pages) - 3
    runner = SubmittingRunner()
    plan = recrawl_project(
        watched.store, runner, watched.project_id, trigger="manual", http=sitemap_site, now=NOW
    )  # type: ignore[arg-type]
    client_call = next(c for c in runner.calls if not c["site"].startswith("rival"))
    client_plan = next(s for s in plan.sites if s.role == "client")
    assert sorted(client_call["skip_fetch"]) == sorted(
        p.url for p in client_plan.pages if p.action == "skip"
    )
    assert client_call["skip_reason"] == "not due in the recrawl plan"
    assert (
        client_plan.run_id
        and "already queued" in next(s for s in plan.sites if s.role == "competitor").note
    )
    assert store.latest_plan(watched.project_id).plan_id == plan.plan_id


def test_watcher_follows_the_plan(watched: World, monkeypatch: pytest.MonkeyPatch) -> None:
    _fill_lists(watched)
    runner = SubmittingRunner()
    watcher = MonitorWatcher(watched.store, watched.access, runner, http=sitemap_site)  # type: ignore[arg-type]
    watcher.tick()  # the first auto-check
    monitor_store(watched.store).set_watch(
        watched.project_id, WatchSettings(auto_check=False, rescrape_every_days=1)
    )
    assert watcher.tick(datetime.now(UTC) + timedelta(days=3))["rescraped"] == [watched.project_id]
    assert runner.calls and all(c["started_by"] == "scheduled recrawl" for c in runner.calls)
    assert monitor_store(watched.store).latest_plan(watched.project_id).trigger == "scheduled"


# -- what changed --------------------------------------------------------------------------------

BASE = "https://www.rival.com"


def _files(run: str, *, words: int, headings: list[str], h1: str, extra: list[Any]) -> RunFiles:
    st = stamp(BASE, "/sourcing", "Sourcing", run, main_word_count=words, headings=headings)
    st = st.model_copy(update={"h1": h1})
    body = [chunk(st, f"{run}-1", "Run sourcing events with suppliers.", 0, ["Sourcing"])]
    chunks = body + [c(st, run) for c in extra]
    return RunFiles(stamps={st.url: st}, chunks={st.url: chunks}, seen={st.url})


def test_a_rewritten_page_reports_words_headings_terms_and_new_blocks() -> None:
    old = _files(
        "r1",
        words=800,
        headings=["Streamline your RFP processes", "Supplier scoring and evaluation"],
        h1="Strategic Sourcing Software",
        extra=[],
    )
    new = _files(
        "r2",
        words=2000,
        headings=["AI-Powered RFP Evaluation", "Generative AI Supplier Matching"],
        h1="Autonomous Strategic Sourcing Platform",
        extra=[
            lambda st, run: chunk(
                st,
                f"{run}-t",
                "Plan | Price\nPro | $49 per user per month",
                1,
                ["Pricing"],
                kind="table",
            ),
            lambda st, run: chunk(
                st, f"{run}-f", "Can I cancel?\nYes, any time.", 2, ["FAQ"], kind="faq"
            ),
        ],
    )
    change = diff_runs(old, new).changed[0]
    assert (change.words_before, change.words_after, change.words_delta) == (800, 2000, 1200)
    assert change.headings_added == ["AI-Powered RFP Evaluation", "Generative AI Supplier Matching"]
    assert change.headings_removed == [
        "Streamline your RFP processes",
        "Supplier scoring and evaluation",
    ]
    assert {"Generative", "AI", "Autonomous", "Platform"} <= set(change.new_terms)
    assert {"Streamline", "Software"} <= set(change.dropped_terms)
    assert change.blocks_added == {"tables": 1, "pricing blocks": 1, "FAQ answers": 1}
    assert diff_runs(new, old).changed[0].blocks_removed == change.blocks_added

    site = SiteDiff(website="rival.com", name="Rival", role="competitor", changed_pages=[change])
    alerts = {a.kind: a for a in _competitor_moves(site)}
    assert alerts["competitor_content_growth"].detail == "Sourcing: 800 → 2,000 words (+1,200)"
    assert (
        "added" in alerts["competitor_messaging_shift"].detail
        and "Generative" in alerts["competitor_messaging_shift"].detail
    )
    assert alerts["competitor_new_tables"].severity == "high"
    assert "pricing or comparison blocks" in alerts["competitor_new_tables"].title
    small = change.model_copy(
        update={
            "words_after": 900,
            "headings_added": [],
            "headings_removed": [],
            "head_changes": {},
            "blocks_added": {"FAQ answers": 1},
        }
    )
    assert _competitor_moves(site.model_copy(update={"changed_pages": [small]})) == []


def test_api_recrawl_plan(watched: World, tmp_path: Any) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.modules.content_processor.projects.security import TokenSigner
    from src.modules.monitoring.api import register_monitoring_routes
    from src.modules.reports.catalog import build_report

    _fill_lists(watched)
    signer = TokenSigner(b"k" * 32)
    app = FastAPI()
    runner = SubmittingRunner()
    register_monitoring_routes(
        app,
        projects=watched.store,
        access=watched.access,
        signer=signer,
        runner=runner,  # type: ignore[arg-type]
        sitemap_http=sitemap_site,
    )
    client = TestClient(app)
    base = f"/api/projects/{watched.project_id}/monitoring"
    assert client.get(f"{base}/recrawl").status_code == 404
    assert client.post(f"{base}/recrawl", json={}).status_code == 401
    token, _ = signer.issue(watched.project_id)
    auth = {"X-Project-Token": token}
    preview = client.post(f"{base}/recrawl", json={}, headers=auth).json()
    assert preview["trigger"] == "preview" and runner.calls == []
    assert client.get(f"{base}/recrawl").json()["plan_id"] == preview["plan_id"]
    queued = client.post(f"{base}/recrawl", json={"queue": True}, headers=auth).json()
    assert queued["trigger"] == "manual" and runner.calls
    saved = client.post(
        f"{base}/watch",
        json={
            "auto_check": True,
            "rescrape_every_days": 7,
            "push_threshold": 3,
            "other_every_days": 14,
            "use_sitemaps": False,
        },
        headers=auth,
    ).json()
    assert saved["other_every_days"] == 14 and saved["use_sitemaps"] is False
    assert saved["smart_recrawl"] is True  # not sent: kept
    report = build_report("recrawl", watched.store, watched.project_id)
    assert report is not None and [t.name for t in report.tables] == ["Websites", "Pages"]
    assert "carried forward without a request" in report.summary[0]
