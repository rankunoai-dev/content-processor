"""App module 1 (Content Scraper): projects, passwords, URL import, runs and downloads."""

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
from src.modules.content_processor.projects.store import ProjectStore, slug
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


# -- the store ----------------------------------------------------------------------


def test_store_keeps_projects_urls_and_never_the_password(tmp_path: Path):
    store = ProjectStore(tmp_path)
    record = store.create(
        name="Flynn's Print Q4", owner_name="Gaurav", client_name="Flynn's", password=PASSWORD
    )
    assert record.project_id.startswith("flynn-s-print-q4-")
    raw = (tmp_path / "projects" / record.project_id / "project.json").read_text("utf-8")
    assert PASSWORD not in raw
    assert store.check_password(record.project_id, PASSWORD)
    assert [p.project_id for p in store.projects()] == [record.project_id]
    result = store.add_urls(record.project_id, ["https://a.example/1", "https://a.example/2"])
    assert len(result.added) == 2 and len(store.urls(record.project_id)) == 2
    assert store.remove_urls(record.project_id, ["https://a.example/1"]) == 1
    assert store.remove_urls(record.project_id, None) == 1
    with pytest.raises(ValueError, match="at least 8"):
        store.create(name="x", owner_name="y", client_name="z", password="short")
    store.delete(record.project_id)
    assert store.projects() == []
    assert slug("  ") == "project"


def test_scope_covers_each_site_free_tiers_only():
    store_record = ProjectStore(Path(".")).__class__  # noqa: F841 - type only
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


# -- the runner with a fake batch command -------------------------------------------


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


@pytest.fixture
def app_env(tmp_path: Path):
    store = ProjectStore(tmp_path / "app_data")
    batches = FakeBatches()
    runner = ScrapeRunner(store, command=batches, report=_fake_report, batch_size=2)
    runner.start()
    signer = TokenSigner.from_file(tmp_path / "app_data" / "secret.key")
    exports = tmp_path / "exports"
    exports.mkdir()
    app = create_app(exports, projects=(store, runner, signer))
    client = TestClient(app, base_url="http://127.0.0.1")
    return client, store, runner, batches


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
        },
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()
    return body["project"]["project_id"], body["token"]


def test_create_view_and_only_the_password_holder_can_change(app_env):
    client, _, _, _ = app_env
    project_id, token = _create(client)
    listing = client.get("/api/projects").json()
    assert listing[0]["project_id"] == project_id and "password_hash" not in listing[0]
    assert listing[0]["owner_name"] == "Gaurav" and listing[0]["client_name"] == "Flynn's"

    add = {"text": "https://www.example.com/a"}
    assert _post(client, f"/api/projects/{project_id}/urls", add).status_code == 401
    assert _post(client, f"/api/projects/{project_id}/urls", add, "forged.token").status_code == 401
    no_header = client.post(
        f"/api/projects/{project_id}/urls", json=add, headers={"X-Project-Token": token}
    )
    assert no_header.status_code == 403, "writes come only from the app"
    assert _post(client, f"/api/projects/{project_id}/urls", add, token).json()["added"] == 1

    wrong = _post(client, f"/api/projects/{project_id}/unlock", {"password": "nope nope"})
    assert wrong.status_code == 401
    unlocked = _post(client, f"/api/projects/{project_id}/unlock", {"password": PASSWORD})
    assert unlocked.status_code == 200 and unlocked.json()["token"]
    for _ in range(5):
        _post(client, f"/api/projects/{project_id}/unlock", {"password": "nope nope"})
    assert (
        _post(client, f"/api/projects/{project_id}/unlock", {"password": PASSWORD}).status_code
        == 429
    )
    short = _post(
        client,
        "/api/projects",
        {"name": "x", "owner_name": "y", "client_name": "z", "password": "short"},
    )
    assert short.status_code == 422


def test_import_run_progress_and_downloads(app_env):
    client, store, runner, batches = app_env
    project_id, token = _create(client)
    csv_file = "URL\nhttps://www.example.com/a\nhttps://www.example.com/slow\nhttps://www.example.com/c\nmailto:x@y.z\n"
    reply = _post(
        client,
        f"/api/projects/{project_id}/urls/import",
        {"file_name": "urls.csv", "content_base64": base64.b64encode(csv_file.encode()).decode()},
        token,
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["added"] == 3 and reply.json()["url_count"] == 3
    bad = _post(
        client,
        f"/api/projects/{project_id}/urls/import",
        {"file_name": "notes.txt", "content_base64": base64.b64encode(b"nothing here").decode()},
        token,
    )
    assert bad.status_code == 200 and bad.json()["rejected"] == 1

    started = _post(client, f"/api/projects/{project_id}/runs", {}, token)
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]
    assert runner.wait_idle(20)
    state = client.get(f"/api/projects/{project_id}/runs/{run_id}").json()
    assert state["status"] == "finished" and state["reports_ready"]
    assert state["batches_total"] == 2 and state["batches_done"] == 2
    assert state["results"] == {"VERIFIED_WITHIN_SCOPE": 3}
    # the 'slow' page was unfinished on try 1 and re-run alone
    assert batches.calls == [
        ["https://www.example.com/a", "https://www.example.com/slow"],
        ["https://www.example.com/slow"],
        ["https://www.example.com/c"],
    ]
    merged = store.run_dir(project_id, run_id) / f"{run_id}.records.jsonl"
    urls = [json.loads(line)["requested_url"] for line in merged.read_text("utf-8").splitlines()]
    assert urls[:3] == [
        "https://www.example.com/a",
        "https://www.example.com/slow",
        "https://www.example.com/c",
    ], "supplied URLs first, then the pages found on them"

    files = client.get(f"/api/projects/{project_id}/runs/{run_id}/downloads").json()
    names = [f["name"] for f in files]
    assert f"{run_id}_project_report.xlsx" in names
    one = client.get(f"/api/projects/{project_id}/runs/{run_id}/downloads/{names[0]}")
    assert one.status_code == 200 and "attachment" in one.headers["content-disposition"]
    assert (
        client.get(
            f"/api/projects/{project_id}/runs/{run_id}/downloads/..%2Fstate.json"
        ).status_code
        == 404
    )
    bundle = client.get(f"/api/projects/{project_id}/runs/{run_id}/downloads.zip")
    assert bundle.status_code == 200
    assert sorted(zipfile.ZipFile(io.BytesIO(bundle.content)).namelist()) == sorted(names)

    removed = _post(client, f"/api/projects/{project_id}/urls/remove", {"all": True}, token)
    assert removed.json() == {"removed": 3, "url_count": 0}
    empty = _post(client, f"/api/projects/{project_id}/runs", {}, token)
    assert empty.status_code == 409 and "Add at least one URL" in empty.json()["detail"]


def test_cancel_and_delete_need_the_owner(app_env):
    client, store, runner, batches = app_env
    project_id, token = _create(client)
    _post(
        client,
        f"/api/projects/{project_id}/urls",
        {"text": "https://example.com/1\nhttps://example.com/2\nhttps://example.com/3"},
        token,
    )
    batches.gate = threading.Event()
    run_id = _post(client, f"/api/projects/{project_id}/runs", {}, token).json()["run_id"]
    again = _post(client, f"/api/projects/{project_id}/runs", {}, token)
    assert again.status_code == 409, "one run of a project at a time"
    assert _post(client, f"/api/projects/{project_id}/runs/{run_id}/cancel", {}).status_code == 401
    assert (
        _post(client, f"/api/projects/{project_id}/runs/{run_id}/cancel", {}, token).status_code
        == 200
    )
    batches.gate.set()
    assert runner.wait_idle(20)
    assert store.run(project_id, run_id).status == "cancelled"
    assert len(batches.calls) == 1, "no batch starts after a cancel"

    assert (
        _post(client, f"/api/projects/{project_id}/delete", {"password": PASSWORD}).status_code
        == 401
    )
    wrong = _post(client, f"/api/projects/{project_id}/delete", {"password": "nope nope"}, token)
    assert wrong.status_code == 401
    done = _post(client, f"/api/projects/{project_id}/delete", {"password": PASSWORD}, token)
    assert done.status_code == 200 and client.get("/api/projects").json() == []
    assert client.get(f"/api/projects/{project_id}").status_code == 404


def test_a_run_left_running_resumes_after_a_restart(tmp_path: Path):
    store = ProjectStore(tmp_path)
    batches = FakeBatches()
    project = store.create(name="Resume", owner_name="G", client_name="C", password=PASSWORD)
    store.add_urls(project.project_id, ["https://example.com/1", "https://example.com/2"])
    first = ScrapeRunner(store, command=batches, report=_fake_report, batch_size=1)
    state = first.submit(project.project_id, started_by="G")  # queued, never worked
    store.save_run(state.model_copy(update={"status": "running"}))  # as if the app died mid-run
    # batch 1 already finished before the crash: it must not run again
    folder = store.run_dir(project.project_id, state.run_id)
    (folder / "b1_a1.records.jsonl").write_text(
        json.dumps({"requested_url": "https://example.com/1", "badge": "VERIFIED_WITHIN_SCOPE"})
        + "\n",
        encoding="utf-8",
    )
    second = ScrapeRunner(store, command=batches, report=_fake_report, batch_size=1)
    second.start()
    assert second.wait_idle(20)
    assert store.run(project.project_id, state.run_id).status == "finished"
    assert batches.calls == [["https://example.com/2"]]
