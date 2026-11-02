"""Theming: themes across websites, topics, pillars, ownership, funnel, store, CLI, API, tool."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.modules.analyst.tools import run_tool
from src.modules.content_processor import cli
from src.modules.content_processor.knowledge.catalog_models import (
    Catalog,
    Evidence,
    Offering,
    PageLink,
)
from src.modules.content_processor.knowledge.models import Chunk, Cta, PageStamp
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import SiteSpec
from src.modules.content_processor.webapi import __main__ as webapi_main
from src.modules.content_processor.webapi.app import create_app
from src.modules.theming.build import (
    PageDoc,
    build_theme_map,
    cluster,
    funnel_stage,
    ownership,
)
from src.modules.theming.service import build_project_themes, theme_store
from src.modules.theming.store import ThemeStore, compare_maps, theme_tables
from tests.modules.analyst.conftest import CLIENT, RIVAL, WHEN, World, chunk, stamp
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture

TERM = "Term life insurance gives level premiums and a death benefit for a fixed term of years."
ANNUITY = "Fixed annuities pay guaranteed retirement income; deferred annuities grow tax deferred."
FORMS = "Download the claim form and the beneficiary change form; submit forms online."
PET = (
    "Pet insurance covers vet bills for dogs and cats, accidents and illness, with wellness plans."
)


def _page(
    base: str, path: str, title: str, run_id: str, text: str, n: int, **extra: Any
) -> tuple[PageStamp, Chunk]:
    st = stamp(base, path, title, run_id, main_word_count=len(text.split()) * 10, **extra)
    return st, chunk(st, f"{run_id}-{n}", text, 0, [title])


def _client_pages(run_id: str, extra: int = 0) -> list[tuple[PageStamp, Chunk]]:
    pages = [
        _page(
            CLIENT,
            "/life/term",
            "Term life insurance",
            run_id,
            TERM,
            1,
            ctas=[Cta(text="Get a quote", url=CLIENT + "/quote", strength="strong")],
        ),
        _page(
            CLIENT,
            "/life/term/premiums",
            "Term life insurance premiums",
            run_id,
            TERM + " Premiums.",
            2,
        ),
        _page(
            CLIENT,
            "/blog/what-is-term-life-insurance",
            "What is term life insurance",
            run_id,
            TERM,
            3,
        ),
        _page(CLIENT, "/retirement/annuities", "Fixed annuities", run_id, ANNUITY, 4),
        _page(
            CLIENT,
            "/retirement/annuities/deferred",
            "Deferred annuities",
            run_id,
            ANNUITY + " Deferred.",
            5,
        ),
        _page(
            CLIENT,
            "/retirement/annuities/income",
            "Annuities retirement income",
            run_id,
            ANNUITY,
            6,
        ),
        _page(CLIENT, "/help/forms", "Claim forms", run_id, FORMS, 7),
        _page(CLIENT, "/help/forms/beneficiary", "Beneficiary change forms", run_id, FORMS, 8),
    ]
    for i in range(extra):
        pages.append(
            _page(
                CLIENT,
                f"/blog/term-life-{i}",
                f"Term life insurance guide {i}",
                run_id,
                TERM,
                20 + i,
            )
        )
    return pages


def _rival_pages(run_id: str) -> list[tuple[PageStamp, Chunk]]:
    return [
        _page(RIVAL, "/term", "Term life insurance plans", run_id, TERM, 1),
        _page(RIVAL, "/term/compare", "Compare term life insurance", run_id, TERM + " Compare.", 2),
        _page(RIVAL, "/term/rates", "Term life insurance rates", run_id, TERM, 3),
        _page(RIVAL, "/pet", "Pet insurance", run_id, PET, 4),
        _page(RIVAL, "/pet/dogs", "Pet insurance for dogs", run_id, PET + " Dogs.", 5),
        _page(RIVAL, "/pet/cats", "Pet insurance for cats", run_id, PET + " Cats.", 6),
    ]


def _catalog(run_id: str) -> Catalog:
    return Catalog(
        run_id=run_id,
        site="example.com",
        site_name="Example Life",
        built_at=WHEN,
        offerings=[
            Offering(
                offering_id="off-annuities",
                name="Annuities",
                level="family",
                href=CLIENT + "/retirement/annuities",
                signals=["header_menu"],
                evidence=[Evidence(kind="menu", ref="m", detail="Annuities")],
                pages=[PageLink(url=CLIENT + "/retirement/annuities", basis="same_url")],
            )
        ],
    )


def _add(
    world: World,
    role: str,
    run_id: str,
    pages: list[tuple[PageStamp, Chunk]],
    hours: int,
    catalog: Catalog | None = None,
) -> None:
    world.add_run(
        role, run_id, [p[0] for p in pages], [p[1] for p in pages], catalog, offset_h=hours
    )


@pytest.fixture
def themed(world: World) -> World:
    _add(world, "client", "run-c2", _client_pages("run-c2"), 10, _catalog("run-c2"))
    _add(world, "competitor", "run-r2", _rival_pages("run-r2"), 10)
    return world


def _map(world: World) -> Any:
    play = world.runner().playground([world.project_id])
    return build_theme_map(play, world.project_id)


def _theme_with(theme_map: Any, word: str) -> Any:
    return next(t for t in theme_map.themes if any(word in term for term in t.terms))


# -- the map ----------------------------------------------------------------------------------


def test_themes_cover_both_websites_with_ownership_pillars_and_funnel(themed: World) -> None:
    m = _map(themed)
    assert [w.website for w in m.websites] == ["example.com", "rival.com"]
    assert {w.website: w.pages for w in m.websites} == {"example.com": 8, "rival.com": 6}
    term = _theme_with(m, "term")
    pet = _theme_with(m, "pet")
    annuity = _theme_with(m, "annuit")
    assert pet.ownership == "client_gap" and "client none" in pet.ownership_reason
    assert annuity.ownership == "client_only"
    assert term.ownership == "contested"
    pages = {c.website: c.pages for c in term.coverage}
    assert pages == {"example.com": 3, "rival.com": 3}
    pillar = next(c.pillar for c in annuity.coverage if c.website == "example.com")
    assert pillar is not None and pillar.url == CLIENT + "/retirement/annuities"
    assert "an offering page in the catalog" in pillar.reasons
    assert annuity.offerings == ["Annuities"]
    by_url = {p.url: p for p in m.pages}
    assert by_url[CLIENT + "/help/forms"].funnel_stage == "support"
    assert by_url[CLIENT + "/blog/what-is-term-life-insurance"].funnel_stage == "learn"
    assert by_url[RIVAL + "/term/compare"].funnel_stage == "compare"
    assert by_url[CLIENT + "/retirement/annuities"].role == "pillar"
    assert by_url[CLIENT + "/retirement/annuities/deferred"].role == "spoke"
    assert m.stats["themes"] == len(m.themes) and m.stats["pages"] == 14
    assert all(t.theme_id.startswith("th-") for t in m.themes)


def test_theme_ids_are_stable_and_comparisons_show_growth(themed: World) -> None:
    first = _map(themed)
    second = _map(themed)
    assert [t.theme_id for t in first.themes] == [t.theme_id for t in second.themes]
    assert compare_maps(first, second) == {
        "from": first.map_id,
        "to": second.map_id,
        "appeared": [],
        "disappeared": [],
        "changed": [],
    }
    _add(themed, "client", "run-c3", _client_pages("run-c3", extra=4), 20, _catalog("run-c3"))
    third = _map(themed)
    diff = compare_maps(first, third)
    assert diff["appeared"] == [] and diff["disappeared"] == []
    moved = [c for c in diff["changed"] if "example.com" in c["pages"]]
    assert (
        moved
        and moved[0]["pages"]["example.com"]["after"] > moved[0]["pages"]["example.com"]["before"]
    )


def test_websites_without_knowledge_are_skipped(world: World) -> None:
    world.store.add_site(world.project_id, SiteSpec(name="Third", website="https://third.net"))
    m = _map(world)
    assert any("Third" in s for s in m.skipped)


def test_a_map_with_too_few_pages_has_no_themes(tmp_path: Path, world: World) -> None:
    project = world.store.create(
        name="Empty",
        owner_name="T",
        client_name="E",
        password="password-123",
        client_site=SiteSpec(name="Nobody", website="https://nobody.org"),
    )
    play = world.runner().playground([project.project_id])
    m = build_theme_map(play, project.project_id)
    assert m.themes == [] and m.stats == {"pages": 0, "themes": 0}


# -- the parts -----------------------------------------------------------------------------------


def test_cluster_is_deterministic_and_leaves_singletons_out() -> None:
    from src.modules.content_processor.knowledge.numeric import np

    rows = np.asarray(
        [[1, 0, 0], [0.9, 0.1, 0], [0, 1, 0], [0, 0.95, 0.05], [0, 0, 1], [0, 0, 0]],
        dtype=np.float32,
    )
    rows = rows / np.maximum(np.linalg.norm(rows, axis=1, keepdims=True), 1e-9)
    labels = cluster(rows, 0.8, 2)
    assert labels[0] == labels[1] != -1 and labels[2] == labels[3] != -1
    assert labels[4] == -1 and labels[5] == -1
    assert cluster(rows, 0.8, 2) == labels
    assert cluster(rows[:0], 0.8, 2) == []
    assert cluster(np.zeros((2, 3), dtype=np.float32), 0.5, 1) == [-1, -1]


def test_ownership_rules() -> None:
    assert ownership(0, 3)[0] == "client_gap"
    assert ownership(4, 0)[0] == "client_only"
    assert ownership(6, 3)[0] == "client_owned"
    assert ownership(4, 3)[0] == "contested"
    assert ownership(2, 3)[0] == "competitor_led"
    assert ownership(2, 5)[0] == "competitor_led"
    assert ownership(0, 0)[0] == "contested"


def test_funnel_stage_signals() -> None:
    def doc(path: str, title: str = "x", page_type: str | None = None, **extra: Any) -> PageDoc:
        st = stamp(CLIENT, path, title, "r", **extra)
        return PageDoc(
            url=st.url, website="example.com", title=title, h1=None, page_type=page_type, stamp=st
        )

    assert funnel_stage(doc("/help/claims"))[0] == "support"
    assert funnel_stage(doc("/getting-started"))[0] == "onboard"
    assert funnel_stage(doc("/compare-plans"))[0] == "compare"
    assert funnel_stage(doc("/get-a-quote"))[0] == "decide"
    assert funnel_stage(doc("/insights/x"))[0] == "learn"
    assert funnel_stage(doc("/x", page_type="faq"))[0] == "support"
    assert funnel_stage(doc("/x", page_type="product"))[0] == "decide"
    assert funnel_stage(doc("/x", title="Compare our plans"))[0] == "compare"
    strong = [Cta(text="Buy", url=CLIENT + "/b", strength="strong")]
    assert funnel_stage(doc("/x", ctas=strong))[0] == "decide"
    assert funnel_stage(doc("/x", page_type="article"))[0] == "learn"
    assert funnel_stage(doc("/x")) == ("unknown", "no signal")


# -- store, workbook, service, CLI, API, tool ----------------------------------------


def test_store_and_workbook(themed: World, tmp_path: Path) -> None:
    store = ThemeStore(tmp_path / "theming")
    assert store.latest(themed.project_id) is None and store.history(themed.project_id) == []
    m = _map(themed)
    store.save(m)
    assert store.latest(themed.project_id) == m
    assert store.get(themed.project_id, m.map_id) == m
    with pytest.raises(KeyError):
        store.get(themed.project_id, "../x")
    with pytest.raises(KeyError):
        store.get(themed.project_id, "themes-20260101-000000")
    with pytest.raises(KeyError):
        store.folder("../escape")
    names = [t.name for t in theme_tables(m)]
    assert names == ["Themes", "Ownership matrix", "Topics", "Pages", "How to read"]
    _m, files = build_project_themes(themed.store, themed.access, themed.project_id)
    assert not files["map"].startswith("\\\\?\\")
    with zipfile.ZipFile(files["workbook"]) as book:
        assert "Ownership matrix" in book.read("xl/workbook.xml").decode()


def test_cli_themes(
    themed: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(webapi_main, "knowledge_access", lambda: themed.access)
    base = ["--project", themed.project_id, "--app-data", str(themed.root)]
    assert cli.main(["themes", "show", *base]) == 1
    assert "no theme map yet" in capsys.readouterr().err
    assert cli.main(["themes", "build", *base]) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["stats"]["themes"] >= 3 and Path(built["files"]["workbook"]).exists()
    assert cli.main(["themes", "show", *base]) == 0
    assert json.loads(capsys.readouterr().out)["map_id"] == built["map_id"]
    assert cli.main(["themes", "compare", *base]) == 1
    assert "build it again" in capsys.readouterr().err
    assert cli.main(["themes", "build", *base]) == 0
    capsys.readouterr()
    assert cli.main(["themes", "compare", *base]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == []
    assert cli.main(["themes", "show", "--project", "nope", "--app-data", str(themed.root)]) == 2


WRITE = {"X-Requested-With": "content-processor"}


def test_api_themes(themed: World, tmp_path: Path) -> None:
    signer = TokenSigner(b"k" * 32)
    exports = tmp_path / "exports"
    exports.mkdir()
    app = create_app(
        exports,
        projects=(themed.store, ScrapeRunner(themed.store), signer),
        knowledge=themed.access,
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    base = f"/api/projects/{themed.project_id}/themes"
    assert client.get(base).json()["detail"] == "No theme map has been built yet."
    assert client.get("/api/projects/nope/themes").status_code == 404
    assert client.post(base, json={}, headers=WRITE).status_code == 401
    token, _ = signer.issue(themed.project_id)
    built = client.post(base, json={}, headers={**WRITE, "X-Project-Token": token})
    assert built.status_code == 200 and built.json()["stats"]["themes"] >= 3
    assert client.get(base).json()["map_id"] == built.json()["map_id"]
    book = client.get(f"{base}/workbook")
    assert book.status_code == 200 and book.content[:2] == b"PK"
    assert client.get(f"{base}/changes").status_code == 404
    client.post(base, json={}, headers={**WRITE, "X-Project-Token": token})
    assert client.get(f"{base}/changes").json()["appeared"] == []


def test_card_tool_reuses_or_builds_the_map(themed: World) -> None:
    play = themed.runner().playground([themed.project_id])
    text, is_error = run_tool(play, "get_theme_map", {})
    data = json.loads(text)["data"]
    assert not is_error and data["themes"] and data["themes"][0]["ref"].startswith("fact:theme-")
    first = data["map_id"]
    again = json.loads(run_tool(play, "get_theme_map", {})[0])["data"]
    assert again["map_id"] == first  # reused, not rebuilt
    fresh = json.loads(run_tool(play, "get_theme_map", {"rebuild": True})[0])["data"]
    assert fresh["map_id"] != first
    assert theme_store(themed.store).latest(themed.project_id) is not None
    pet = next(t for t in data["themes"] if any("pet" in term for term in t["terms"]))
    assert pet["ownership"] == "client_gap" and pet["per_website"]["rival.com"]["pages"] == 3
