"""App module 1 (Content Scraper): projects with websites, passwords, URLs, runs, the bot."""

from __future__ import annotations

import base64
import io
import json
import threading
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.modules.content_processor.projects.runner import ScrapeRunner, build_scope
from src.modules.content_processor.projects.security import (
    AttemptLimiter,
    TokenSigner,
    hash_password,
    verify_password,
)
from src.modules.content_processor.projects.store import ProjectStore, SiteSpec, slug
from src.modules.content_processor.projects.urls import (
    UnreadableFileError,
    candidates_from_file,
    candidates_from_text,
    merge_urls,
)
from src.modules.content_processor.webapi.app import WRITE_HEADER, WRITE_HEADER_VALUE, create_app

PASSWORD = "correct horse 42"


# -- passwords and tokens ---------------------------------------------------------


def test_passwords_are_hashed_and_checked():
    stored = hash_password(PASSWORD)
    assert PASSWORD not in stored and stored.startswith("scrypt$")
    assert verify_password(PASSWORD, stored)
    assert not verify_password("wrong password", stored)
    assert not verify_password(PASSWORD, "garbage")
    assert hash_password(PASSWORD) != stored, "salted"


def test_tokens_are_per_project_signed_and_expire(tmp_path: Path):
    signer = TokenSigner.from_file(tmp_path / "secret.key", ttl_s=60)
    token, expires = signer.issue("proj-a", now=1000.0)
    assert expires == 1060.0
    assert signer.check(token, "proj-a", now=1059.0)
    assert not signer.check(token, "proj-a", now=1061.0), "expired"
    assert not signer.check(token, "proj-b", now=1001.0), "another project"
    body, mac = token.split(".")
    assert not signer.check(f"{body}.{mac[:-2]}xx", "proj-a", now=1001.0), "tampered"
    assert not signer.check(None, "proj-a") and not signer.check("nonsense", "proj-a")
    again = TokenSigner.from_file(tmp_path / "secret.key", ttl_s=60)
    assert again.check(token, "proj-a", now=1001.0), "the secret survives a restart"


def test_wrong_passwords_slow_down_after_five():
    limiter = AttemptLimiter(free=5, base_wait_s=2.0)
    for _ in range(5):
        assert limiter.wait_s("p", now=100.0) == 0
        limiter.failed("p", now=100.0)
    assert limiter.wait_s("p", now=100.0) == pytest.approx(2.0)
    assert limiter.wait_s("p", now=103.0) == 0
    limiter.succeeded("p")
    assert limiter.wait_s("p", now=100.0) == 0


# -- URL lists ----------------------------------------------------------------------


def test_pasted_lines_and_checks_with_reasons():
    text = (
        "https://www.example.com/a\n\n https://www.example.com/a?utm_source=mail \n"
        "ftp://example.com/file\nnot a url\nhttps://example.com/b https://example.com/c\n"
    )
    result = merge_urls(["https://www.example.com/c"], candidates_from_text(text))
    assert result.added == [
        "https://www.example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ], "example.com/c is a different address from www.example.com/c"
    assert result.duplicates_in_input == 1, "the utm copy of /a"
    reasons = dict(result.rejected)
    assert reasons["ftp://example.com/file"] == "only http and https addresses can be scraped"
    assert reasons["not a url"] == "address contains spaces"
    again = merge_urls(result.added, ["https://example.com/b?utm_source=news"])
    assert again.already_present == 1 and again.added == []


def test_files_from_the_device(tmp_path: Path):
    csv_text = 'Page,Notes\nhttps://example.com/x,first\n"https://example.com/y",second\n'
    assert candidates_from_file("list.csv", csv_text.encode()) == [
        "https://example.com/x",
        "https://example.com/y",
    ]
    assert candidates_from_file("list.txt", b"\xef\xbb\xbfhttps://example.com/z\n") == [
        "https://example.com/z"
    ]
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["URL", "Owner"])
    book.active.append(["https://example.com/sheet", "me"])
    raw = io.BytesIO()
    book.save(raw)
    assert candidates_from_file("urls.xlsx", raw.getvalue()) == ["https://example.com/sheet"]
    with pytest.raises(UnreadableFileError, match=r"\.xlsx or \.csv"):
        candidates_from_file("old.xls", b"x")
    with pytest.raises(UnreadableFileError, match="Excel"):
        candidates_from_file("broken.xlsx", b"not a zip")
    with pytest.raises(UnreadableFileError, match=r"\.txt, \.csv or \.xlsx"):
        candidates_from_file("photo.png", b"x")


# -- the store: projects with websites --------------------------------------------


def _flynns(store: ProjectStore):
    return store.create(
        name="Print security",
        owner_name="Gaurav",
        client_name="Flynn's",
        password=PASSWORD,
        client_site=SiteSpec("Flynn's", "https://www.flynns.com"),
        competitors=[SiteSpec("Xerox", "xerox.com"), SiteSpec("Canon", None)],
    )


def test_each_website_is_stored_on_its_own(tmp_path: Path):
    store = ProjectStore(tmp_path)
    record = _flynns(store)
    pid = record.project_id
    raw = (tmp_path / "projects" / pid / "project.json").read_text("utf-8")
    assert PASSWORD not in raw
    assert [(s.site_id, s.role, s.domains) for s in record.sites] == [
        ("flynn-s", "client", ["flynns.com"]),
        ("xerox", "competitor", ["xerox.com"]),
        ("canon", "competitor", []),
    ]
    for site in record.sites:
        assert (tmp_path / "projects" / pid / "sites" / site.site_id / "urls.json").is_file()

    added = store.add_urls(pid, "flynn-s", ["https://blog.flynns.com/a", "https://www.xerox.com/x"])
    assert added.added == ["https://blog.flynns.com/a"], "a subdomain is the same website"
    assert dict(added.rejected) == {"https://www.xerox.com/x": "belongs to Xerox"}
    elsewhere = store.add_urls(pid, "xerox", ["https://www.hp.com/p"])
    assert dict(elsewhere.rejected) == {"https://www.hp.com/p": "not this website (hp.com)"}
    learned = store.add_urls(pid, "canon", ["https://www.usa.canon.com/printers"])
    assert learned.added and store.site(pid, "canon").domains == ["usa.canon.com"]

    routed = store.add_urls_routed(
        pid,
        [
            "https://www.flynns.com/b",
            "https://www.xerox.com/security",
            "https://www.xerox.com/security",
            "https://www.hp.com/p",
            "not a url",
        ],
    )
    assert {sid: r.added for sid, r in routed.per_site.items()} == {
        "flynn-s": ["https://www.flynns.com/b"],
        "xerox": ["https://www.xerox.com/security"],
    }
    assert routed.per_site["xerox"].duplicates_in_input == 1
    assert routed.unknown_domains == {"hp.com": 1}
    assert routed.rejected == [("not a url", "not a usable web address")]
    assert store.url_count(pid) == 4 and store.url_count(pid, "xerox") == 1

    with pytest.raises(ValueError, match="cannot be removed"):
        store.remove_site(pid, "flynn-s")
    store.remove_site(pid, "canon")
    assert [s.site_id for s in store.sites(pid)] == ["flynn-s", "xerox"]
    assert not (tmp_path / "projects" / pid / "sites" / "canon").exists()
    hp = store.add_site(pid, SiteSpec("HP", "https://www.hp.com"))
    assert hp.role == "competitor" and hp.domains == ["hp.com"]
    with pytest.raises(ValueError, match="already the website of 'Xerox'"):
        store.add_site(pid, SiteSpec("Xerox again", "https://xerox.com"))
    with pytest.raises(ValueError, match="Two websites are called"):
        store.add_site(pid, SiteSpec("HP", None))
    with pytest.raises(ValueError, match="not a website address"):
        store.add_site(pid, SiteSpec("Bad", "not a site"))
    assert slug("  ") == "project"


def test_an_older_project_becomes_one_client_website(tmp_path: Path):
    """Projects saved before websites existed: list and runs move under the client site."""
    folder = tmp_path / "projects" / "old-print-abc123"
    (folder / "runs" / "old-print_20261030-120000").mkdir(parents=True)
    (folder / "project.json").write_text(
        json.dumps(
            {
                "project_id": "old-print-abc123",
                "name": "Old print",
                "owner_name": "Gaurav",
                "client_name": "Flynn's",
                "password_hash": hash_password(PASSWORD),
                "created_at": "2026-10-30T09:00:00Z",
                "updated_at": "2026-10-30T09:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (folder / "urls.json").write_text(
        json.dumps([{"url": "https://www.flynns.com/a", "added_at": "2026-10-30T09:00:00Z"}]),
        encoding="utf-8",
    )
    (folder / "runs" / "old-print_20261030-120000" / "state.json").write_text(
        json.dumps(
            {
                "run_id": "old-print_20261030-120000",
                "project_id": "old-print-abc123",
                "status": "finished",
                "created_at": "2026-10-30T12:00:00Z",
                "started_by": "Gaurav",
                "url_count": 1,
                "batch_size": 25,
                "batches_total": 1,
            }
        ),
        encoding="utf-8",
    )
    store = ProjectStore(tmp_path)
    record = store.get("old-print-abc123")
    assert [(s.site_id, s.role, s.domains) for s in record.sites] == [
        ("flynn-s", "client", ["flynns.com"])
    ]
    assert [u.url for u in store.urls("old-print-abc123", "flynn-s")] == [
        "https://www.flynns.com/a"
    ]
    [run] = store.runs("old-print-abc123")
    assert run.site_id == "flynn-s" and run.status == "finished"
    assert (folder / "urls.json.converted").is_file() and not (folder / "urls.json").exists()
    assert store.get("old-print-abc123").sites == record.sites, "converted once"


def test_scope_covers_each_site_free_tiers_only():
    from datetime import UTC, datetime

    from src.modules.content_processor.contracts import RunScope
    from src.modules.content_processor.projects.models import ProjectRecord

    project = ProjectRecord(
        project_id="p-1",
        name="P",
        owner_name="Gaurav",
        client_name="C",
        password_hash="x",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    scope = RunScope.model_validate(
        build_scope(project, ["https://www.flynns.com/blog/a", "https://shop.example.org/b"])
    )
    assert scope.hosts_in_scope == ["flynns.com", "shop.example.org"]
    assert scope.access_tier_ceiling.value == "T3_FIDELITY_BROWSER"
    assert scope.budgets.cost_ceiling_usd == 0.0
    assert scope.authorization.approved_by == "Gaurav"


# -- the runner with fake batches, reports and knowledge steps ----------------------


class FakeBatches:
    """Writes records like `cli dryrun`; URLs with 'slow' stay unfinished on try 1."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.gate: threading.Event | None = None

    def __call__(self, scope: Path, out: Path, cancel: threading.Event) -> int:
        urls = json.loads(scope.read_text("utf-8"))["input_urls"]
        self.calls.append(urls)
        if self.gate is not None:
            self.gate.wait(10)
        first_try = out.name.endswith("_a1.json")
        rows = []
        for url in urls:
            badge = "RUNNING" if ("slow" in url and first_try) else "VERIFIED_WITHIN_SCOPE"
            rows.append({"requested_url": url, "badge": badge})
        rows.append({"requested_url": urls[0] + "/brochure.pdf", "badge": "PARTIAL"})
        out.with_suffix(".records.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        return 0


def _fake_report(records: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = records.name.removesuffix(".records.jsonl")
    lines = records.read_text("utf-8").splitlines()
    (out_dir / f"{stem}_project_report.xlsx").write_bytes(b"xlsx" + str(len(lines)).encode())
    (out_dir / f"{stem}_r1_url_content.csv").write_text("url,content\n", encoding="utf-8")
    return 0


class FakeKnowledge:
    """Records which records file went into which namespace."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def __call__(self, records: Path, namespace: str) -> tuple[bool, str]:
        self.calls.append((records.name, namespace))
        if self.fail:
            return False, "the index step failed: no model"
        return True, f"searchable by the bot (knowledge space {namespace})"


@pytest.fixture
def app_env(tmp_path: Path):
    store = ProjectStore(tmp_path / "app_data")
    batches = FakeBatches()
    knowledge = FakeKnowledge()
    runner = ScrapeRunner(
        store, command=batches, report=_fake_report, knowledge=knowledge, batch_size=2
    )
    runner.start()
    signer = TokenSigner.from_file(tmp_path / "app_data" / "secret.key")
    exports = tmp_path / "exports"
    exports.mkdir()
    app = create_app(exports, projects=(store, runner, signer))
    client = TestClient(app, base_url="http://127.0.0.1")
    return client, store, runner, batches, knowledge


def _post(client: TestClient, path: str, body: object, token: str | None = None):
    headers = {WRITE_HEADER: WRITE_HEADER_VALUE}
    if token:
        headers["X-Project-Token"] = token
    return client.post(path, json=body, headers=headers)


def _create(client: TestClient) -> tuple[str, str]:
    reply = _post(
        client,
        "/api/projects",
        {
            "name": "Print security",
            "owner_name": "Gaurav",
            "client_name": "Flynn's",
            "password": PASSWORD,
            "client_website": "https://www.flynns.com",
            "competitors": [{"name": "Xerox", "website": "xerox.com"}],
        },
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()
    return body["project"]["project_id"], body["token"]


def test_create_with_competitors_and_only_the_password_holder_can_change(app_env):
    client, _, _, _, _ = app_env
    project_id, token = _create(client)
    [listing] = client.get("/api/projects").json()
    assert listing["project_id"] == project_id and "password_hash" not in listing
    assert [(s["name"], s["role"], s["domains"]) for s in listing["sites"]] == [
        ("Flynn's", "client", ["flynns.com"]),
        ("Xerox", "competitor", ["xerox.com"]),
    ]
    assert listing["sites"][1]["namespace"] == f"rankuno-dev.{project_id}.xerox"

    add = {"text": "https://www.xerox.com/a"}
    site_urls = f"/api/projects/{project_id}/sites/xerox/urls"
    assert _post(client, site_urls, add).status_code == 401
    assert _post(client, site_urls, add, "forged.token").status_code == 401
    no_header = client.post(site_urls, json=add, headers={"X-Project-Token": token})
    assert no_header.status_code == 403, "writes come only from the app"
    assert _post(client, site_urls, add, token).json()["added"] == 1

    bad = _post(
        client,
        "/api/projects",
        {
            "name": "x",
            "owner_name": "y",
            "client_name": "z",
            "password": PASSWORD,
            "client_website": "flynns.com",
            "competitors": [{"name": "Dup", "website": "flynns.com"}],
        },
    )
    assert bad.status_code == 422 and "already the website" in bad.json()["detail"]

    wrong = _post(client, f"/api/projects/{project_id}/unlock", {"password": "nope nope"})
    assert wrong.status_code == 401
    for _ in range(5):
        _post(client, f"/api/projects/{project_id}/unlock", {"password": "nope nope"})
    locked_out = _post(client, f"/api/projects/{project_id}/unlock", {"password": PASSWORD})
    assert locked_out.status_code == 429

    hp = _post(
        client, f"/api/projects/{project_id}/sites", {"name": "HP", "website": "hp.com"}, token
    )
    assert hp.status_code == 200 and hp.json()["role"] == "competitor"
    gone = _post(client, f"/api/projects/{project_id}/sites/hp/delete", {}, token)
    assert gone.status_code == 200
    client_gone = _post(client, f"/api/projects/{project_id}/sites/flynn-s/delete", {}, token)
    assert client_gone.status_code == 409


def test_one_list_for_all_websites_then_scrape_each_on_its_own(app_env):
    client, store, runner, batches, knowledge = app_env
    project_id, token = _create(client)
    mixed = (
        "URL\nhttps://www.flynns.com/a\nhttps://www.flynns.com/slow\nhttps://www.flynns.com/c\n"
        "https://www.xerox.com/security\nhttps://www.hp.com/p\n"
    )
    reply = _post(
        client,
        f"/api/projects/{project_id}/urls/import",
        {"file_name": "all.csv", "content_base64": base64.b64encode(mixed.encode()).decode()},
        token,
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert {s["site_id"]: s["added"] for s in body["sites"]} == {"flynn-s": 3, "xerox": 1}
    assert body["unknown_domains"] == {"hp.com": 1}

    started = _post(client, f"/api/projects/{project_id}/runs", {}, token)
    assert started.status_code == 200, started.text
    assert sorted(r["site_id"] for r in started.json()) == ["flynn-s", "xerox"]
    assert runner.wait_idle(20)
    runs = {r["site_id"]: r for r in client.get(f"/api/projects/{project_id}/runs").json()}
    flynns = runs["flynn-s"]
    assert flynns["status"] == "finished" and flynns["reports_ready"]
    assert flynns["results"] == {"VERIFIED_WITHIN_SCOPE": 3}
    assert flynns["knowledge_status"] == "ready"
    assert ["https://www.flynns.com/slow"] in batches.calls, "unfinished page re-run alone"
    assert sorted(ns for _, ns in knowledge.calls) == [
        f"rankuno-dev.{project_id}.flynn-s",
        f"rankuno-dev.{project_id}.xerox",
    ], "each website indexed into its own knowledge space"

    run_id = flynns["run_id"]
    folder = store.run_dir(project_id, "flynn-s", run_id)
    assert folder.parent.parent.name == "flynn-s", "the run lives in its website's folder"
    base = f"/api/projects/{project_id}/sites/flynn-s/runs/{run_id}"
    names = [f["name"] for f in client.get(f"{base}/downloads").json()]
    assert f"{run_id}_project_report.xlsx" in names
    assert client.get(f"{base}/downloads/{names[0]}").status_code == 200
    assert client.get(f"{base}/downloads/..%2Fstate.json").status_code == 404
    bundle = client.get(f"{base}/downloads.zip")
    assert sorted(zipfile.ZipFile(io.BytesIO(bundle.content)).namelist()) == sorted(names)
    xerox_urls = client.get(f"/api/projects/{project_id}/sites/xerox/urls").json()
    assert [u["url"] for u in xerox_urls["items"]] == ["https://www.xerox.com/security"]

    again = _post(client, f"/api/projects/{project_id}/sites/xerox/runs", {}, token)
    assert again.status_code == 200
    assert runner.wait_idle(20)
    removed = _post(
        client, f"/api/projects/{project_id}/sites/xerox/urls/remove", {"all": True}, token
    )
    assert removed.json() == {"removed": 1, "url_count": 0}
    empty = _post(client, f"/api/projects/{project_id}/sites/xerox/runs", {}, token)
    assert empty.status_code == 409 and "Add at least one URL to Xerox" in empty.json()["detail"]


def test_a_failed_knowledge_step_keeps_the_reports(tmp_path: Path):
    store = ProjectStore(tmp_path)
    project = _flynns(store)
    store.add_urls(project.project_id, "xerox", ["https://www.xerox.com/1"])
    runner = ScrapeRunner(
        store, command=FakeBatches(), report=_fake_report, knowledge=FakeKnowledge(fail=True)
    )
    runner.start()
    state = runner.submit(project.project_id, "xerox", started_by="G")
    assert runner.wait_idle(20)
    final = store.run(project.project_id, "xerox", state.run_id)
    assert final.status == "finished" and final.reports_ready
    assert final.knowledge_status == "failed" and "no model" in final.knowledge_message


def test_cancel_and_delete_need_the_owner(app_env):
    client, store, runner, batches, _ = app_env
    project_id, token = _create(client)
    site_urls = f"/api/projects/{project_id}/sites/flynn-s/urls"
    _post(
        client,
        site_urls,
        {"text": "https://flynns.com/1\nhttps://flynns.com/2\nhttps://flynns.com/3"},
        token,
    )
    batches.gate = threading.Event()
    run = _post(client, f"/api/projects/{project_id}/sites/flynn-s/runs", {}, token).json()
    again = _post(client, f"/api/projects/{project_id}/sites/flynn-s/runs", {}, token)
    assert again.status_code == 409, "one run of a website at a time"
    cancel = f"/api/projects/{project_id}/sites/flynn-s/runs/{run['run_id']}/cancel"
    assert _post(client, cancel, {}).status_code == 401
    assert _post(client, cancel, {}, token).status_code == 200
    batches.gate.set()
    assert runner.wait_idle(20)
    assert store.run(project_id, "flynn-s", run["run_id"]).status == "cancelled"
    assert len(batches.calls) == 1, "no batch starts after a cancel"

    delete = f"/api/projects/{project_id}/delete"
    assert _post(client, delete, {"password": PASSWORD}).status_code == 401
    assert _post(client, delete, {"password": "nope nope"}, token).status_code == 401
    assert _post(client, delete, {"password": PASSWORD}, token).status_code == 200
    assert client.get("/api/projects").json() == []


def test_renaming_to_nothing_is_refused_not_an_error(app_env):
    client, _, _, _, _ = app_env
    project_id, token = _create(client)
    blank = _post(client, f"/api/projects/{project_id}/update", {"name": "   "}, token)
    assert blank.status_code == 422 and "name" in blank.json()["detail"]
    ok = _post(client, f"/api/projects/{project_id}/update", {"name": " Print 2 "}, token)
    assert ok.status_code == 200 and ok.json()["name"] == "Print 2"


class FakeSpaceStore:
    """A website's knowledge space: built or not, and whether it was emptied."""

    reset_spaces: list[str] = []
    closed_spaces: list[str] = []

    def __init__(self, namespace: str, built: bool) -> None:
        self.namespace = namespace
        self.built = built

    def meta(self) -> dict[str, str]:
        return {"model": "bge-m3"} if self.built else {}

    def reset(self) -> None:
        FakeSpaceStore.reset_spaces.append(self.namespace)

    def close(self) -> None:
        FakeSpaceStore.closed_spaces.append(self.namespace)


def test_deleting_a_project_removes_the_other_modules_data_and_its_knowledge(app_env, tmp_path):
    from src.modules.content_processor.webapi.projects_api import KnowledgeAccess

    client, store, runner, _, _ = app_env
    project_id, _ = _create(client)
    other_id, _ = _create(client)
    root = Path(store.root)
    for module in ("theming", "monitoring", "gaps", "research"):
        for pid in (project_id, other_id):
            (root / module / pid).mkdir(parents=True)
            (root / module / pid / "latest.json").write_text("{}", encoding="utf-8")
    (root / "mcp" / "ledgers").mkdir(parents=True)
    (root / "mcp" / "ledgers" / f"{project_id}.json").write_text("{}", encoding="utf-8")
    built = runner.namespace(project_id, "flynn-s")
    access = KnowledgeAccess(
        open_store=lambda space: FakeSpaceStore(space.namespace, space.namespace == built),  # type: ignore[arg-type,return-value]
        embedder_for=lambda model: None,  # type: ignore[arg-type,return-value]
    )
    exports = tmp_path / "exports3"
    exports.mkdir()
    signer = TokenSigner.from_file(tmp_path / "secret3.key")
    app = TestClient(
        create_app(exports, projects=(store, runner, signer), knowledge=access),
        base_url="http://127.0.0.1",
    )
    token = signer.issue(project_id)[0]
    FakeSpaceStore.reset_spaces.clear()
    FakeSpaceStore.closed_spaces.clear()

    gone = _post(app, f"/api/projects/{project_id}/delete", {"password": PASSWORD}, token)
    assert gone.status_code == 200, gone.text

    for module in ("theming", "monitoring", "gaps", "research"):
        assert not (root / module / project_id).exists(), module
        assert (root / module / other_id / "latest.json").is_file(), "other projects untouched"
    assert not (root / "mcp" / "ledgers" / f"{project_id}.json").exists()
    assert FakeSpaceStore.reset_spaces == [built], "only a space that was built is emptied"
    assert sorted(FakeSpaceStore.closed_spaces) == sorted(
        [built, runner.namespace(project_id, "xerox")]
    )
    assert [p["project_id"] for p in app.get("/api/projects").json()] == [other_id]


def test_two_downloads_at_once_build_the_same_archive_safely(tmp_path: Path):
    from src.modules.content_processor.webapi.projects_api import _zip

    files = {}
    for n in range(3):
        path = tmp_path / f"report_{n}.csv"
        path.write_text("url\n" * 1000, encoding="utf-8")
        files[path.name] = path
    archive = tmp_path / "run" / "reports.zip"
    archive.parent.mkdir()
    errors: list[Exception] = []

    def go() -> None:
        try:
            _zip(archive, files)
        except Exception as exc:  # noqa: BLE001 - the test collects every failure
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors, errors
    assert sorted(zipfile.ZipFile(archive).namelist()) == sorted(files)
    assert [p.name for p in archive.parent.iterdir()] == ["reports.zip"], "no temporary file left"


def test_a_stopped_run_is_continued_by_its_owner(app_env):
    client, store, runner, batches, _ = app_env
    project_id, token = _create(client)
    site = f"/api/projects/{project_id}/sites/flynn-s"
    _post(client, f"{site}/urls", {"text": "https://flynns.com/1\nhttps://flynns.com/2"}, token)
    batches.gate = threading.Event()
    run = _post(client, f"{site}/runs", {}, token).json()
    resume = f"{site}/runs/{run['run_id']}/resume"
    assert _post(client, resume, {}, token).status_code == 409, "it is still running"
    assert _post(client, f"{site}/runs/{run['run_id']}/cancel", {}, token).status_code == 200
    batches.gate.set()
    assert runner.wait_idle(20)
    assert store.run(project_id, "flynn-s", run["run_id"]).status == "cancelled"

    assert _post(client, resume, {}).status_code == 401, "only with the project's password"
    assert _post(client, resume, {}, token).status_code == 200
    assert runner.wait_idle(20)
    assert store.run(project_id, "flynn-s", run["run_id"]).status == "finished"
    assert _post(client, resume, {}, token).status_code == 409, "a finished run has nothing left"


def test_a_run_left_running_resumes_after_a_restart(tmp_path: Path):
    store = ProjectStore(tmp_path)
    batches = FakeBatches()
    project = _flynns(store)
    pid = project.project_id
    store.add_urls(pid, "flynn-s", ["https://flynns.com/1", "https://flynns.com/2"])
    first = ScrapeRunner(
        store, command=batches, report=_fake_report, knowledge=FakeKnowledge(), batch_size=1
    )
    state = first.submit(pid, "flynn-s", started_by="G")  # queued, never worked
    store.save_run(state.model_copy(update={"status": "running"}))  # the app died mid-run
    folder = store.run_dir(pid, "flynn-s", state.run_id)
    (folder / "b1_a1.records.jsonl").write_text(
        json.dumps({"requested_url": "https://flynns.com/1", "badge": "VERIFIED_WITHIN_SCOPE"})
        + "\n",
        encoding="utf-8",
    )
    second = ScrapeRunner(
        store, command=batches, report=_fake_report, knowledge=FakeKnowledge(), batch_size=1
    )
    second.start()
    assert second.wait_idle(20)
    assert store.run(pid, "flynn-s", state.run_id).status == "finished"
    assert batches.calls == [["https://flynns.com/2"]]


def test_a_run_whose_batches_all_crash_says_so_plainly(tmp_path: Path):
    store = ProjectStore(tmp_path)
    project = _flynns(store)
    store.add_urls(project.project_id, "flynn-s", ["https://flynns.com/1"])

    def crashing(scope: Path, out: Path, cancel: threading.Event) -> int:
        out.with_suffix(".log").write_text("Traceback: boom", encoding="utf-8")
        return 1

    runner = ScrapeRunner(
        store, command=crashing, report=_fake_report, knowledge=FakeKnowledge(), attempts=2
    )
    runner.start()
    state = runner.submit(project.project_id, "flynn-s", started_by="G")
    assert runner.wait_idle(20)
    final = store.run(project.project_id, "flynn-s", state.run_id)
    assert final.status == "failed" and not final.reports_ready
    assert "No page could be scraped" in final.message and "b1_a2.log" in final.message


# -- the bot: one search over several websites, each in its own store ---------------


def _space_store(root: Path, namespace: str, passages: dict[str, str]) -> None:
    from src.modules.content_processor.knowledge.embedders import HashingEmbedder
    from src.modules.content_processor.knowledge.indexing import embed_hash
    from src.modules.content_processor.knowledge.vector_store import ChunkRow, SqliteVectorStore

    embedder = HashingEmbedder()
    store = SqliteVectorStore(root / namespace / "vectors.sqlite")
    vectors = embedder.embed(list(passages.values())) or []
    store.upsert_chunks(
        [
            ChunkRow(
                chunk_id=f"{namespace}-{n}",
                run_id="r",
                url=url,
                page_key=f"pg-{n}",
                order=0,
                kind="text",
                heading_path=["Security"],
                context_header=url,
                verbatim_text=text,
                embed_text=text,
                embed_hash=embed_hash(text),
                block_ids=["b1"],
                evidence_ids=["evidence:1"],
                embedding=vector,
            )
            for n, ((url, text), vector) in enumerate(zip(passages.items(), vectors, strict=True))
        ]
    )
    store.set_meta({"model": embedder.model, "dims": str(len(vectors[0]))})
    store.close()


def test_the_bot_searches_several_websites_and_says_where_each_passage_came_from(
    app_env, tmp_path: Path
):
    from src.modules.content_processor.knowledge.embedders import HashingEmbedder
    from src.modules.content_processor.knowledge.vector_store import SqliteVectorStore
    from src.modules.content_processor.webapi.projects_api import KnowledgeAccess

    client, store, runner, _, _ = app_env
    project_id, _token = _create(client)
    root = tmp_path / "knowledge"
    flynns_ns = runner.namespace(project_id, "flynn-s")
    xerox_ns = runner.namespace(project_id, "xerox")
    _space_store(
        root,
        flynns_ns,
        {
            "https://www.flynns.com/secure-print": (
                "Secure print release keeps documents in the tray until the owner signs in."
            ),
            "https://www.flynns.com/lease": "Lease a Xerox printer with managed print services.",
        },
    )
    _space_store(
        root,
        xerox_ns,
        {
            "https://www.xerox.com/security": (
                "Xerox secure print release and encrypted hard drives protect documents."
            )
        },
    )
    access = KnowledgeAccess(
        open_store=lambda space: SqliteVectorStore(root / space.namespace / "vectors.sqlite"),
        embedder_for=lambda model: HashingEmbedder(),
    )
    exports = tmp_path / "exports2"
    exports.mkdir()
    signer = TokenSigner.from_file(tmp_path / "secret2.key")
    bot = TestClient(
        create_app(exports, projects=(store, runner, signer), knowledge=access),
        base_url="http://127.0.0.1",
    )

    spaces = {s["site_name"]: s for s in bot.get("/api/knowledge/spaces").json()}
    assert spaces["Xerox"]["ready"] and spaces["Xerox"]["role"] == "competitor"
    assert spaces["Flynn's"]["namespace"] == flynns_ns

    both = bot.get(
        "/api/knowledge/search", params={"q": "secure print release", "project": project_id}
    ).json()
    assert {h["site_name"] for h in both["hits"]} == {"Flynn's", "Xerox"}
    assert sorted(both["searched"]) == sorted([flynns_ns, xerox_ns])
    first = both["hits"][0]
    assert first["project_name"] == "Print security" and first["url"].startswith("https://www.")
    assert first["evidence_ids"] == ["evidence:1"]

    only_xerox = bot.get(
        "/api/knowledge/search", params={"q": "secure print", "space": xerox_ns}
    ).json()
    assert {h["site_name"] for h in only_xerox["hits"]} == {"Xerox"}, "the scope is respected"

    side_by_side = bot.get(
        "/api/knowledge/search",
        params={"q": "secure print release", "project": project_id, "per_site": 1},
    ).json()
    assert sorted(h["site_name"] for h in side_by_side["hits"]) == ["Flynn's", "Xerox"]

    other_id, _ = _create(client)  # a second project: never indexed
    across = bot.get(
        "/api/knowledge/search",
        params={"q": "secure print", "project": [project_id, other_id]},
    ).json()
    skipped = {s["space"]: s["reason"] for s in across["skipped"]}
    assert skipped[runner.namespace(other_id, "xerox")].startswith("not indexed yet")
    assert {h["project_id"] for h in across["hits"]} == {project_id}
    assert bot.get("/api/knowledge/search", params={"q": "x y", "space": "nope"}).status_code == 404


# -- unique websites across projects, and the competitive scope ----------------------


def _knowledge_built(store: ProjectStore, project_id: str, site_id: str, when: str) -> None:
    """Record a finished run whose knowledge space was built at `when`."""
    from datetime import datetime

    from src.modules.content_processor.projects.models import RunState

    at = datetime.fromisoformat(when)
    store.save_run(
        RunState(
            run_id=f"{site_id}_{at:%Y%m%d-%H%M%S}",
            project_id=project_id,
            site_id=site_id,
            status="finished",
            created_at=at,
            finished_at=at,
            started_by="G",
            url_count=1,
            batch_size=25,
            batches_total=1,
            reports_ready=True,
            knowledge_status="ready",
        )
    )


def _two_projects(store: ProjectStore) -> tuple[str, str]:
    first = store.create(
        name="Print security",
        owner_name="G",
        client_name="Flynn's",
        password=PASSWORD,
        client_site=SiteSpec("Flynn's", "flynns.com"),
        competitors=[SiteSpec("Xerox", "xerox.com"), SiteSpec("Canon", "canon.com")],
    ).project_id
    second = store.create(
        name="HP refresh",
        owner_name="G",
        client_name="HP",
        password=PASSWORD,
        client_site=SiteSpec("HP", "hp.com"),
        competitors=[
            SiteSpec("Flynns NYC", "https://www.flynns.com"),
            SiteSpec("Xerox US", "https://www.xerox.com"),
            SiteSpec("Canon USA", "usa.canon.com"),
            SiteSpec("Brother", "brother.com"),
        ],
    ).project_id
    return first, second


def test_websites_are_unique_across_projects_and_competitors_are_collected(tmp_path: Path):
    from src.modules.content_processor.projects.websites import WebsiteGraph

    store = ProjectStore(tmp_path)
    first, second = _two_projects(store)
    _knowledge_built(store, first, "xerox", "2026-10-01T10:00:00+00:00")
    _knowledge_built(store, second, "xerox-us", "2026-10-20T10:00:00+00:00")
    graph = WebsiteGraph.build(store, lambda p, s: f"t.{p}.{s}")

    assert sorted(w.key for w in graph.websites()) == [
        "brother.com",
        "canon.com",
        "flynns.com",
        "hp.com",
        "xerox.com",
    ], "Flynn's, Xerox and Canon (usa.canon.com) are each one website across both projects"
    xerox = graph.find("https://www.xerox.com/en-us/printers")
    assert xerox is not None and len(xerox.copies) == 2
    fresh = xerox.freshest()
    assert fresh is not None and fresh.project_id == second, "the copy indexed last is used"
    canon = graph.find("canon.com")
    assert canon is not None and canon.domains == {"canon.com", "usa.canon.com"}

    flynns = graph.find("flynns.com")
    assert flynns is not None
    scope = [w.key for w in graph.competitive_scope(flynns)]
    assert scope[0] == "flynns.com"
    assert sorted(scope[1:]) == ["canon.com", "hp.com", "xerox.com"], (
        "competitors from its own project plus HP, which lists Flynn's as a competitor"
    )
    hp = graph.find("hp.com")
    assert hp is not None
    assert sorted(w.key for w in graph.competitive_scope(hp))[0] == "brother.com"
    assert graph.find("nowhere.example") is None and graph.find("not a site") is None


def test_a_question_about_a_website_covers_its_competitors_in_every_project(
    app_env, tmp_path: Path
):
    from src.modules.content_processor.knowledge.embedders import HashingEmbedder
    from src.modules.content_processor.knowledge.vector_store import SqliteVectorStore
    from src.modules.content_processor.webapi.projects_api import KnowledgeAccess

    _client, store, runner, _, _ = app_env
    first, second = _two_projects(store)
    root = tmp_path / "knowledge"
    spaces = {
        (first, "flynn-s"): {"https://www.flynns.com/a": "Flynn's secure print release for banks."},
        (first, "xerox"): {
            "https://www.xerox.com/old": "Old Xerox page about secure print release."
        },
        (second, "xerox-us"): {
            "https://www.xerox.com/new": "New Xerox page about secure print release."
        },
        (second, "hp"): {"https://www.hp.com/p": "HP secure print release for offices."},
    }
    for (pid, sid), passages in spaces.items():
        _space_store(root, runner.namespace(pid, sid), passages)
    _knowledge_built(store, first, "flynn-s", "2026-10-02T10:00:00+00:00")
    _knowledge_built(store, first, "xerox", "2026-10-01T10:00:00+00:00")
    _knowledge_built(store, second, "xerox-us", "2026-10-20T10:00:00+00:00")
    _knowledge_built(store, second, "hp", "2026-10-21T10:00:00+00:00")
    access = KnowledgeAccess(
        open_store=lambda space: SqliteVectorStore(root / space.namespace / "vectors.sqlite"),
        embedder_for=lambda model: HashingEmbedder(),
    )
    exports = tmp_path / "exports3"
    exports.mkdir()
    bot = TestClient(
        create_app(
            exports,
            projects=(store, runner, TokenSigner.from_file(tmp_path / "s3.key")),
            knowledge=access,
        ),
        base_url="http://127.0.0.1",
    )
    result = bot.get(
        "/api/knowledge/search",
        params={"q": "secure print release", "website": "https://www.flynns.com", "per_site": 1},
    ).json()
    assert result["scope"][0] == "Flynn's"
    assert sorted(result["scope"][1:]) == ["Canon USA", "HP", "Xerox US"]
    found = {h["website"]: h for h in result["hits"]}
    assert set(found) == {"flynns.com", "xerox.com", "hp.com"}
    assert found["xerox.com"]["url"] == "https://www.xerox.com/new", "freshest copy of Xerox"
    assert found["hp.com"]["project_name"] == "HP refresh", "regardless of the project asked"
    assert {s["site"]: s["reason"] for s in result["skipped"]} == {
        "Canon USA": "not scraped yet in any project"
    }
    websites = {w["key"]: w for w in bot.get("/api/knowledge/websites").json()}
    assert websites["xerox.com"]["projects"] == ["HP refresh", "Print security"]
    assert websites["xerox.com"]["freshest_project"] == "HP refresh"
    assert websites["canon.com"]["searchable"] is False
    unknown = bot.get("/api/knowledge/search", params={"q": "print", "website": "nowhere.example"})
    assert unknown.status_code == 404
