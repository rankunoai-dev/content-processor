"""The client's several websites (brands), notes and tags, and one domain split into websites.

The client's websites are one side: never competitors of one another, in a
project or across projects (docs/app/ARCHITECTURE_PROJECTS_AND_SITES.md, 6b).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.modules.analyst.playground import Playground
from src.modules.content_processor.knowledge.embedders import HashingEmbedder
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import ProjectStore, SiteSpec, clean_tags
from src.modules.content_processor.projects.urls import address_of, address_score
from src.modules.content_processor.projects.websites import WebsiteGraph
from src.modules.content_processor.webapi.app import WRITE_HEADER, WRITE_HEADER_VALUE, create_app
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess

PASSWORD = "correct horse battery"


def _acme(store: ProjectStore) -> str:
    """Acme's main website, two brands on the same domain, and one competitor."""
    return store.create(
        name="Acme growth",
        owner_name="G",
        client_name="Acme",
        password=PASSWORD,
        client_site=SiteSpec("Acme", "https://www.acme.com", note="Parent company site"),
        client_brands=[
            SiteSpec("Acme Pro", "pro.acme.com", note="B2B brand", tags=("brand", "B2B")),
            SiteSpec("Acme Outlet", "https://acme.com/outlet/", tags=("brand",)),
        ],
        competitors=[SiteSpec("Rival", "rival.com", note="Main competitor in the US")],
    ).project_id


def test_addresses_cover_urls_most_specific_first():
    assert address_of("https://www.Acme.com/Outlet/", keep_path=True) == "acme.com/outlet"
    assert address_of("https://www.Acme.com/Outlet/", keep_path=False) == "acme.com"
    assert address_score("https://acme.com/outlet/shoes", "acme.com/outlet") == (2, 1)
    assert address_score("https://acme.com/outlets", "acme.com/outlet") is None, (
        "a section covers only the URLs under it"
    )
    assert address_score("https://shop.pro.acme.com/x", "pro.acme.com") == (3, 0)
    assert address_score("https://rival.com/", "acme.com") is None


def test_the_client_can_have_several_websites_with_notes_and_tags(tmp_path: Path):
    store = ProjectStore(tmp_path)
    project_id = _acme(store)
    sites = {s.site_id: s for s in store.sites(project_id)}

    assert [s.role for s in sites.values()] == ["client", "client", "client", "competitor"]
    assert store.get(project_id).main_site().site_id == "acme"  # type: ignore[union-attr]
    assert sites["acme"].domains == ["acme.com"]
    assert sites["acme-pro"].domains == ["pro.acme.com"]
    assert sites["acme-outlet"].domains == ["acme.com/outlet"], (
        "the section is kept because acme.com is already another website of the project"
    )
    assert sites["acme-pro"].note == "B2B brand" and sites["acme-pro"].tags == ["brand", "B2B"]

    routed = store.add_urls_routed(
        project_id,
        [
            "https://www.acme.com/about",
            "https://pro.acme.com/pricing",
            "https://acme.com/outlet/shoes",
            "https://acme.com/outlets-near-you",
            "https://rival.com/x",
        ],
    )
    assert {k: len(v.added) for k, v in routed.per_site.items()} == {
        "acme": 2,
        "acme-pro": 1,
        "acme-outlet": 1,
        "rival": 1,
    }, "each URL goes to the most specific website that covers it"
    refused = store.add_urls(project_id, "acme", ["https://pro.acme.com/contact"])
    assert refused.rejected == [("https://pro.acme.com/contact", "belongs to Acme Pro")]


def test_a_domain_taken_by_another_website_needs_its_own_subdomain_or_section(tmp_path: Path):
    store = ProjectStore(tmp_path)
    project_id = _acme(store)
    with pytest.raises(ValueError, match="own subdomain or section"):
        store.add_site(project_id, SiteSpec("Acme again", "acme.com"), "client")
    other = store.add_site(project_id, SiteSpec("Other", "https://other.com/en-us/home"))
    assert other.domains == ["other.com"], "a page address of a free domain gives the domain"


def test_editing_a_website_and_what_cannot_change(tmp_path: Path):
    store = ProjectStore(tmp_path)
    project_id = _acme(store)
    changed = store.update_site(
        project_id, "rival", name="Rival Inc", note="  Rival, US only ", tags=["US", "us", " "]
    )
    assert (changed.name, changed.note, changed.tags) == ("Rival Inc", "Rival, US only", ["US"])
    assert store.update_site(project_id, "rival", role="client").role == "client"
    assert store.update_site(project_id, "rival", role="competitor").role == "competitor"
    with pytest.raises(ValueError, match="main website"):
        store.update_site(project_id, "acme", role="competitor")
    with pytest.raises(ValueError, match="already called"):
        store.update_site(project_id, "rival", name="acme pro")
    with pytest.raises(ValueError, match="main website"):
        store.remove_site(project_id, "acme")
    store.remove_site(project_id, "acme-outlet")
    assert [s.site_id for s in store.sites(project_id)] == ["acme", "acme-pro", "rival"]
    with pytest.raises(ValueError, match="at most 12"):
        clean_tags([f"t{i}" for i in range(13)])
    with pytest.raises(ValueError, match="longer than 40"):
        clean_tags(["x" * 41])


def _graph_projects(store: ProjectStore) -> None:
    _acme(store)
    # Rival's own project lists Acme Pro as a competitor: Acme Pro competes with Rival.
    store.create(
        name="Rival audit",
        owner_name="G",
        client_name="Rival",
        password=PASSWORD,
        client_site=SiteSpec("Rival", "rival.com"),
        competitors=[SiteSpec("Pro by Acme", "https://pro.acme.com")],
    )
    # A project entered by mistake that sets Acme against its own brand.
    store.create(
        name="Old mistake",
        owner_name="G",
        client_name="Acme",
        password=PASSWORD,
        client_site=SiteSpec("Acme", "acme.com"),
        competitors=[SiteSpec("Acme Pro", "pro.acme.com")],
    )


def test_sister_websites_are_never_competitors_in_any_project(tmp_path: Path):
    store = ProjectStore(tmp_path)
    _graph_projects(store)
    graph = WebsiteGraph.build(store, lambda p, s: f"t.{p}.{s}")

    assert sorted(w.key for w in graph.websites()) == [
        "acme.com",
        "acme.com/outlet",
        "pro.acme.com",
        "rival.com",
    ], "a project that lists them separately keeps one domain's websites apart everywhere"
    acme, pro = graph.find("acme.com"), graph.find("https://pro.acme.com/x")
    outlet = graph.find("https://acme.com/outlet/shoes")
    assert acme is not None and pro is not None and outlet is not None
    assert outlet.key == "acme.com/outlet" and graph.find("https://acme.com/x") is acme
    assert acme.competitors == {"rival.com"}, "the mistaken project cannot make a brand a rival"
    assert pro.competitors == {"rival.com"}
    assert {w.key for w in graph.sisters_of(acme)} == {"pro.acme.com", "acme.com/outlet"}
    assert [w.key for w in graph.competitive_scope(pro)] == [
        "pro.acme.com",
        "acme.com",
        "acme.com/outlet",
        "rival.com",
    ], "the website, then its sister websites, then their competitors"
    assert acme.note == "Parent company site" and pro.tags == ["brand", "B2B"]
    assert pro.name == "Acme Pro", "its name where it is the client's"


def test_the_analyst_treats_every_website_of_the_client_as_the_client(tmp_path: Path):
    store = ProjectStore(tmp_path)
    _graph_projects(store)
    access = KnowledgeAccess(
        open_store=lambda _space: None,  # type: ignore[arg-type,return-value]
        embedder_for=lambda _m: HashingEmbedder(),
    )
    acme_project = next(p for p in store.projects() if p.name == "Acme growth").project_id
    play = Playground(store, access, [acme_project])
    roles = {w.key: play.role_in_run(w) for w in play.websites()}
    assert roles == {
        "acme.com": "client",
        "acme.com/outlet": "client",
        "pro.acme.com": "client",
        "rival.com": "competitor",
    }
    rival_project = next(p for p in store.projects() if p.name == "Rival audit").project_id
    rival_play = Playground(store, access, [rival_project])
    assert rival_play.role_in_run(rival_play.website("pro.acme.com")) == "competitor", (
        "in Rival's own project, Acme's brands are the competitors"
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    store = ProjectStore(tmp_path / "app_data")
    runner = ScrapeRunner(store, command=lambda *_a: 0, report=lambda *_a: 0, batch_size=2)
    signer = TokenSigner.from_file(tmp_path / "app_data" / "secret.key")
    exports = tmp_path / "exports"
    exports.mkdir()
    app = create_app(exports, projects=(store, runner, signer))
    return TestClient(app, base_url="http://127.0.0.1")


def _post(c: TestClient, path: str, body: object, token: str | None = None):
    headers = {WRITE_HEADER: WRITE_HEADER_VALUE}
    if token:
        headers["X-Project-Token"] = token
    return c.post(path, json=body, headers=headers)


def test_the_app_creates_brands_and_edits_notes_and_tags(client: TestClient):
    reply = _post(
        client,
        "/api/projects",
        {
            "name": "Acme growth",
            "owner_name": "G",
            "client_name": "Acme",
            "password": PASSWORD,
            "client_website": "acme.com",
            "client_note": "Parent company",
            "client_tags": ["main"],
            "client_brands": [{"name": "Acme Pro", "website": "pro.acme.com", "note": "B2B"}],
            "competitors": [{"name": "Rival", "website": "rival.com", "tags": ["US"]}],
        },
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()
    project_id, token = body["project"]["project_id"], body["token"]
    sites = {s["site_id"]: s for s in body["project"]["sites"]}
    assert sites["acme"]["main"] and sites["acme"]["note"] == "Parent company"
    assert sites["acme-pro"]["role"] == "client" and not sites["acme-pro"]["main"]
    assert sites["rival"]["tags"] == ["US"]

    added = _post(
        client,
        f"/api/projects/{project_id}/sites",
        {"name": "Acme Kids", "website": "kids.acme.com", "role": "client", "tags": ["brand"]},
        token,
    )
    assert added.status_code == 200 and added.json()["role"] == "client"

    path = f"/api/projects/{project_id}/sites/rival/update"
    assert _post(client, path, {"note": "x"}).status_code == 401, "needs the password"
    edited = _post(client, path, {"note": "Rival in the US", "tags": ["US", "retail"]}, token)
    assert edited.status_code == 200 and edited.json()["tags"] == ["US", "retail"]
    refused = _post(
        client, f"/api/projects/{project_id}/sites/acme/update", {"role": "competitor"}, token
    )
    assert refused.status_code == 422 and "main website" in refused.json()["detail"]

    websites = {w["key"]: w for w in client.get("/api/knowledge/websites").json()}
    assert websites["acme.com"]["same_client"] == ["Acme Kids", "Acme Pro"]
    assert websites["acme.com"]["competitors"] == ["Rival"]
    assert websites["rival.com"]["note"] == "Rival in the US"
