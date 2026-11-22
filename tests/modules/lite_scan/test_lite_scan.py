"""Competitor lite scan: the inventory, the sample, the browser-free scrape, and more.

How themes, monitoring and gaps treat a sample is tested here too.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from src.core.config import get_settings
from src.modules.content_processor.projects import runner as runner_module
from src.modules.content_processor.projects.runner import (
    LITE_FILE,
    ScrapeRunner,
    build_scope,
    subprocess_command,
)
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.webapi.app import create_app
from src.modules.gaps.build import build_findings
from src.modules.lite_scan import service as lite_service
from src.modules.lite_scan.infer import add_inferred, address_words, place
from src.modules.lite_scan.models import LiteInventory, LitePick
from src.modules.lite_scan.sample import Ranking, choose_sample, is_page, section_of
from src.modules.lite_scan.service import (
    latest_lite_run,
    lite_store,
    rankings_for,
    start_lite_scan,
)
from src.modules.monitoring.recrawl import read_sitemaps
from src.modules.monitoring.service import run_project_check
from src.modules.theming.build import build_theme_map
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.monitoring.test_monitoring import watched as watched  # noqa: F401 - fixture
from tests.modules.monitoring.test_recrawl import sitemap_site
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture

R = "https://www.rival.com"
WRITE = {"X-Requested-With": "content-processor"}


def _big_site() -> list[str]:
    urls = [f"{R}/blog/post-{i}" for i in range(600)]
    urls += [f"{R}/help/article-{i}" for i in range(300)]
    urls += [f"{R}/products/item-{i}" for i in range(50)]
    urls += [f"{R}/news/{2020 + i % 5}/story-{i}" for i in range(40)]
    urls += [f"{R}/pricing", f"{R}/", f"{R}/about", f"{R}/brochure.pdf", f"{R}/logo.png"]
    return urls


def test_sitemap_keeps_every_address_on_the_website() -> None:
    index = read_sitemaps("rival.com", sitemap_site())
    assert index.urls == [f"{R}/term", f"{R}/pet/dogs", f"{R}/about"]  # undated too


def test_pages_and_sections() -> None:
    assert is_page(f"{R}/blog/post") and is_page(f"{R}/x.html") and is_page(f"{R}/")
    assert not is_page(f"{R}/a.pdf") and not is_page(f"{R}/b.PNG") and not is_page("ftp://x/y")
    assert section_of(f"{R}/pricing") == "/"
    assert section_of(f"{R}/blog/guides/how-to") == "/blog/guides"
    assert section_of(f"{R}/news/2024/a") == section_of(f"{R}/news/2025/b") == "/news/{n}"
    assert section_of(f"{R}/p/3fa85f64-5717-4562/a") == "/p/{id}"


def test_sample_takes_pinned_ranking_core_then_every_section() -> None:
    ranking = {
        f"{R}/blog/post-7": Ranking(12, 4.0),
        f"{R}/help/article-3": Ranking(30, 9.0),
        f"{R}/a.pdf": Ranking(99, 99.0),  # not a page
    }
    picks, sections = choose_sample(
        website="rival.com",
        inventory=_big_site(),
        ranking=ranking,
        pinned=[f"{R}/blog/post-500"],
        limit=30,
        per_section=3,
    )
    assert len(picks) == 30 and len({p.url for p in picks}) == 30
    assert picks[0].url == f"{R}/blog/post-500" and picks[0].reason == "pinned"
    assert [p.url for p in picks[1:3]] == [f"{R}/help/article-3", f"{R}/blog/post-7"]
    assert picks[1].detail == "ranks for 30 keyword(s)"
    core = [p for p in picks if p.reason == "core"]
    assert f"{R}/pricing" in {p.url for p in core} and len(core) <= int(30 * 0.25)
    by = {s.section: s for s in sections}
    assert by["/blog"].pages == 600 and by["/news/{n}"].pages == 40
    assert all(s.sampled >= 1 for s in sections)  # every section is represented
    assert all(p.url.split(".")[-1] not in ("pdf", "png") for p in picks)
    again, _ = choose_sample(
        website="rival.com",
        inventory=_big_site(),
        ranking=ranking,
        pinned=[f"{R}/blog/post-500"],
        limit=30,
        per_section=3,
    )
    assert [p.url for p in again] == [p.url for p in picks]  # the same inputs, the same sample


def test_lite_runs_use_plain_downloads_and_only_the_listed_pages(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = world.store.get(world.project_id)
    full = build_scope(project, [f"{R}/a"])
    lite = build_scope(project, [f"{R}/a"], lite=True)
    assert full["mode"] == "SUPPLIED_URLS_PLUS_EMBEDDED"
    assert full["access_tier_ceiling"] == "T3_FIDELITY_BROWSER"
    assert lite["mode"] == "SUPPLIED_URLS" and lite["access_tier_ceiling"] == "T2_IMPERSONATED_HTTP"
    assert lite["budgets"] == {"cost_ceiling_usd": 0.0, "max_runtime_s": 7200}

    runner = ScrapeRunner(world.store, command=lambda *_a: 0, knowledge=lambda *_a: (True, ""))
    site = world.site_ids["competitor"]
    state = runner.submit(
        world.project_id,
        site,
        started_by="test",
        urls=[f"{R}/a", f"{R}/b"],
        lite={"inventory": 900, "sample": 2},
    )
    folder = world.store.run_dir(world.project_id, site, state.run_id)
    assert state.scan == "lite" and state.url_count == 2
    assert json.loads((folder / "urls.json").read_text("utf-8")) == [f"{R}/a", f"{R}/b"]
    assert json.loads((folder / LITE_FILE).read_text("utf-8"))["inventory"] == 900
    with pytest.raises(ValueError, match="needs the pages"):
        runner.submit(world.project_id, site, started_by="t", urls=[], lite={})

    seen: list[list[str]] = []

    class Done:
        def __init__(self, argv: list[str], **_kw: Any) -> None:
            seen.append(argv)

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", Done)
    scope = folder / "b1_a1_scope.json"
    scope.write_text("{}", encoding="utf-8")
    subprocess_command(scope, folder / "b1.records.jsonl", threading.Event())
    assert "--no-render" in seen[0] and "--no-docs" in seen[0]
    (folder / LITE_FILE).unlink()
    subprocess_command(scope, folder / "b1.records.jsonl", threading.Event())
    assert "--no-render" not in seen[1]
    assert runner_module.LITE_FILE == LITE_FILE


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def submit(self, project_id: str, site_id: str, **kw: Any) -> Any:
        self.calls.append({"site": site_id, **kw})

        class State:
            run_id = f"{site_id}_lite"

        return State()


def big_sitemap() -> httpx.Client:
    body = "".join(f"<url><loc>{u}</loc></url>" for u in _big_site())
    xml = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=xml)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_start_lite_scan_reads_the_sitemap_samples_and_queues(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CP_LITE_SCAN_MAX_PAGES", "40")
    get_settings.cache_clear()
    try:
        runner = Recorder()
        rival = world.site_ids["competitor"]
        found, state = start_lite_scan(
            world.store, runner, world.project_id, rival, started_by="Tester", http=big_sitemap
        )  # type: ignore[arg-type]
        assert state.run_id == f"{rival}_lite" and found.run_id == state.run_id
        call = runner.calls[0]
        assert len(call["urls"]) == 40 == len(found.sample)
        assert call["lite"]["inventory"] == found.pages == 993  # pdf and png left out
        assert found.basis() == "based on 40 of about 993 pages (lite scan)"
        assert lite_store(world.store).latest(world.project_id, rival) == found
        assert set(lite_store(world.store).project(world.project_id)) == {rival}
        with pytest.raises(ValueError, match="client website"):
            start_lite_scan(
                world.store,
                runner,
                world.project_id,
                world.site_ids["client"],
                started_by="Tester",
                http=big_sitemap,
            )  # type: ignore[arg-type]

        def nothing() -> httpx.Client:
            return httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(404)))

        # the website's own address list still gives a sample when no sitemap is readable
        found2, _ = start_lite_scan(
            world.store, runner, world.project_id, rival, started_by="Tester", http=nothing
        )  # type: ignore[arg-type]
        assert found2.inventory == [] and found2.sample
        assert found2.basis().startswith("based on a sample of")
    finally:
        get_settings.cache_clear()


def test_rankings_come_from_the_search_data(world: World) -> None:
    from src.modules.research.service import pull_project
    from tests.modules.research.test_research import ON, semrush

    pull_project(world.store, world.access, world.project_id, ON, http=semrush)
    site = world.store.site(world.project_id, world.site_ids["competitor"])
    found = rankings_for(world.store, world.project_id, site)
    assert found and all(r.keywords >= 1 for r in found.values())
    assert all("rival.com" in u for u in found)


def test_address_words_place_pages_in_themes() -> None:
    assert address_words(f"{R}/insurance/term-life-quotes.html") == {
        "insurance",
        "term",
        "life",
        "quote",
    }
    themes = {"th-term": ["term life", "quote"], "th-pet": ["pet", "dogs"]}
    assert place(f"{R}/term-life/quotes", themes) == "th-term"
    assert place(f"{R}/pets/dogs-and-cats", themes) == "th-pet"
    assert place(f"{R}/careers", themes) is None
    assert place(f"{R}/pet-quote", {"a": ["pet"], "b": ["quote"]}) is None  # a tie


def test_inferred_pages_are_counted_but_never_change_ownership(themed: World) -> None:
    play = themed.runner().playground([themed.project_id])
    theme_map = build_theme_map(play, themed.project_id)
    rival = next(w for w in theme_map.websites if w.role == "competitor")
    target = theme_map.themes[0]
    word = target.terms[0].split()[0]
    inventory = [f"{R}/section/{word}-guide-{i}" for i in range(25)] + [f"{R}/careers"]
    lite = LiteInventory(
        project_id=themed.project_id,
        site_id=themed.site_ids["competitor"],
        website="rival.com",
        created_at=theme_map.built_at,
        inventory=inventory,
        sample=[LitePick(url=f"{R}/section/{word}-guide-0", reason="section")],
        run_id=rival.run_id,
        max_pages=150,
    )
    marked = add_inferred(theme_map, [lite])
    site = next(w for w in marked.websites if w.website == rival.website)
    assert site.scan == "lite" and site.inventory == 26 and "lite scan" in site.basis
    theme = next(t for t in marked.themes if t.theme_id == target.theme_id)
    cov = next(c for c in theme.coverage if c.website == rival.website)
    assert 0 < cov.inferred <= 24  # the sampled page is not counted twice
    assert [t.ownership for t in marked.themes] == [t.ownership for t in theme_map.themes]
    assert add_inferred(theme_map, []) == theme_map

    roles = {w.website: w.role for w in marked.websites}
    findings = build_findings(marked, roles)
    naming = [f for f in findings if rival.website in f.competitors]
    assert naming and all(
        f.basis.startswith(f"{rival.website}: based on 1 of about 26") for f in naming
    )
    plain = build_findings(theme_map, roles)
    assert all(f.basis == "" for f in plain)


def test_monitoring_never_compares_a_sample_with_a_full_scrape(watched: World) -> None:
    run_project_check(watched.store, watched.access, watched.project_id)
    from tests.modules.monitoring.test_monitoring import _next_scrapes

    _next_scrapes(watched)
    site = watched.site_ids["competitor"]
    latest = max(watched.store.runs(watched.project_id, site), key=lambda r: r.created_at)
    watched.store.save_run(latest.model_copy(update={"scan": "lite"}))
    assert latest_lite_run(watched.store.runs(watched.project_id, site)) is not None
    check, _ = run_project_check(watched.store, watched.access, watched.project_id)
    rival = next(d for d in check.websites if d.role == "competitor")
    assert rival.note.startswith("first lite scan") and not rival.new_pages
    assert not rival.removed_pages and not rival.changed_pages


def test_api_starts_and_shows_a_lite_scan(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lite_service, "_client", big_sitemap)
    signer = TokenSigner(b"k" * 32)
    runner = ScrapeRunner(world.store, command=lambda *_a: 0, knowledge=lambda *_a: (True, ""))
    exports = world.root.parent / "exports"
    exports.mkdir()
    client = TestClient(
        create_app(exports, projects=(world.store, runner, signer)), base_url="http://127.0.0.1"
    )
    pid, rival = world.project_id, world.site_ids["competitor"]
    base = f"/api/projects/{pid}/sites/{rival}/lite"
    assert client.get(base).status_code == 404
    assert client.post(base, json={}, headers=WRITE).status_code == 401
    token, _ = signer.issue(pid)
    started = client.post(base, json={}, headers={**WRITE, "X-Project-Token": token})
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["inventory"] == 993 and body["sampled"] == body["max_pages"] == 150
    assert body["run"]["scan"] == "lite" and body["sections"]
    shown = client.get(base).json()
    assert shown["run_id"] == body["run_id"] and "inventory" in shown
    again = client.post(base, json={}, headers={**WRITE, "X-Project-Token": token})
    assert again.status_code == 409 and "already queued" in again.json()["detail"]
    client_site = f"/api/projects/{pid}/sites/{world.site_ids['client']}/lite"
    refused = client.post(client_site, json={}, headers={**WRITE, "X-Project-Token": token})
    assert refused.status_code == 409 and "client website" in refused.json()["detail"]
    assert client.get(f"/api/projects/{pid}/sites/nope/lite").status_code == 404


def test_paths_are_checked(tmp_path: Path) -> None:
    store = lite_service.LiteStore(tmp_path)
    assert store.latest("../x", "y") is None and store.project("../x") == {}
