"""Reviewers approve or reject page verdicts; scrape again and exclude are followed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.modules.accounts.store import AccountStore
from src.modules.content_processor.export.decisions import (
    Decision,
    read_decisions,
    review_decisions,
    verdict_decisions,
)
from src.modules.content_processor.projects import project_reports
from src.modules.content_processor.projects.runner import FORCE_FILE, ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.verdicts import (
    excluded_urls,
    page_verdicts,
    rescrape_urls,
    save_verdict,
)
from src.modules.content_processor.webapi.app import create_app
from tests.modules.analyst.conftest import CLIENT, World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture

WRITE = {"X-Requested-With": "content-processor"}
TERM = f"{CLIENT}/life/term"
ABOUT = f"{CLIENT}/about"


def test_a_rejection_needs_a_reason_and_a_next_step() -> None:
    ok = Decision(reviewer="Rita", kind="verdict", key="u", decision="approved")
    assert ok.next_step == ""
    for bad, why in [
        ({"decision": "rejected", "note": "text cut"}, "needs a next step"),
        ({"decision": "rejected", "next_step": "exclude"}, "needs a reason"),
        ({"decision": "approved", "next_step": "exclude"}, "only a rejected verdict"),
        ({"decision": "maybe"}, "unknown verdict decision"),
        ({"decision": "rejected", "next_step": "later", "note": "x"}, "needs a next step"),
    ]:
        with pytest.raises(ValidationError, match=why):
            Decision(reviewer="Rita", kind="verdict", key="u", **bad)
    log = [
        Decision(reviewer="R", kind="verdict", key="a", decision="approved"),
        Decision(
            reviewer="R",
            kind="verdict",
            key="b",
            decision="rejected",
            next_step="scrape_again",
            note="cut",
        ),
        Decision(
            reviewer="R",
            kind="verdict",
            key="c",
            decision="rejected",
            next_step="exclude",
            note="dup",
        ),
        Decision(reviewer="R", kind="review", key="c", decision="resolved"),
    ]
    assert review_decisions(log) == {
        "a": ("accept_as_is", ""),
        "b": ("retry_later", "cut"),
        "c": ("resolved", ""),  # a review line wins over the verdict's action
    }
    assert set(verdict_decisions(log)) == {"a", "b", "c"}


def _client_run(w: World) -> tuple[str, str, str]:
    return w.project_id, w.site_ids["client"], "run-c1"


def test_listing_saving_and_the_follow_ups(world: World) -> None:
    pid, sid, rid = _client_run(world)
    listed = page_verdicts(world.store, pid, sid, rid)
    assert {p.url for p in listed.pages} >= {TERM, ABOUT}
    assert listed.counts["undecided"] == len([p for p in listed.pages if p.is_page])
    save_verdict(
        world.store,
        pid,
        sid,
        rid,
        url=ABOUT,
        decision="approved",
        next_step="",
        note="",
        reviewer="Rita",
    )
    save_verdict(
        world.store,
        pid,
        sid,
        rid,
        url=TERM,
        decision="rejected",
        next_step="scrape_again",
        note="the rate table is missing",
        reviewer="Rita",
    )
    save_verdict(
        world.store,
        pid,
        sid,
        rid,
        url=TERM,
        decision="rejected",
        next_step="exclude",
        note="duplicate of the product page",
        reviewer="Eddy",
    )
    again = {p.url: p for p in page_verdicts(world.store, pid, sid, rid).pages}
    term = again[TERM]
    assert term.decision is not None and term.decision.next_step == "exclude"
    assert [h.reviewer for h in term.history] == ["Eddy", "Rita"]  # newest first
    assert again[ABOUT].decision is not None and again[ABOUT].decision.decision == "approved"
    folder = world.store.run_dir(pid, sid, rid)
    assert excluded_urls(folder / f"{rid}.records.jsonl", rid) == {TERM}
    assert rescrape_urls(world.store, pid, sid) == []  # the latest decision is exclude now
    save_verdict(
        world.store,
        pid,
        sid,
        rid,
        url=TERM,
        decision="rejected",
        next_step="scrape_again",
        note="try once more",
        reviewer="Eddy",
    )
    assert rescrape_urls(world.store, pid, sid) == [TERM]
    with pytest.raises(KeyError):
        save_verdict(
            world.store,
            pid,
            sid,
            rid,
            url=f"{CLIENT}/nope",
            decision="approved",
            next_step="",
            note="",
            reviewer="R",
        )
    assert len(read_decisions(folder / f"{rid}.decisions.jsonl")) == 4  # every line kept

    # the next scrape of the website fetches the sent-back page in full
    world.store.add_urls(pid, sid, [TERM, ABOUT])
    state = ScrapeRunner(world.store).submit(pid, sid, started_by="test", skip_fetch=[TERM, ABOUT])
    new_folder = world.store.run_dir(pid, sid, state.run_id)
    assert json.loads((new_folder / FORCE_FILE).read_text(encoding="utf-8")) == {"urls": [TERM]}
    skipped = json.loads((new_folder / "skip_fetch.json").read_text(encoding="utf-8"))
    assert skipped["urls"] == [ABOUT]  # never skipped when a reviewer sent it back


def test_excluded_pages_are_left_out_of_the_project_reports(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pid, sid, rid = _client_run(world)
    save_verdict(
        world.store,
        pid,
        sid,
        rid,
        url=TERM,
        decision="rejected",
        next_step="exclude",
        note="dup",
        reviewer="R",
    )
    records = world.store.run_dir(pid, sid, rid) / f"{rid}.records.jsonl"

    class Record:
        def __init__(self, url: str) -> None:
            self.requested_url = url

    monkeypatch.setattr(project_reports, "load_records", lambda _p: [Record(TERM), Record(ABOUT)])
    source: Any = type("S", (), {"records": records, "run_id": rid, "site_name": "Example"})()
    loaded, websites = project_reports._load([source])  # noqa: SLF001
    assert [r.requested_url for r in loaded] == [ABOUT] and websites == {ABOUT: "Example"}


def _app(w: World, tmp_path: Path, signer: TokenSigner) -> TestClient:
    exports = tmp_path / "exports"
    exports.mkdir(exist_ok=True)
    app = create_app(exports, projects=(w.store, ScrapeRunner(w.store), signer), knowledge=w.access)
    return TestClient(app, base_url="http://127.0.0.1")


def test_api_without_accounts_needs_the_unlock_and_a_name(world: World, tmp_path: Path) -> None:
    signer = TokenSigner(b"k" * 32)
    client = _app(world, tmp_path, signer)
    pid, sid, rid = _client_run(world)
    base = f"/api/projects/{pid}/sites/{sid}/runs/{rid}/verdicts"
    assert any(p["url"] == TERM for p in client.get(base).json()["pages"])
    body = {"url": TERM, "decision": "approved"}
    assert client.post(base, json=body, headers=WRITE).status_code == 401
    token, _ = signer.issue(pid)
    auth = {**WRITE, "X-Project-Token": token}
    assert (
        client.post(base, json=body, headers=auth).json()["detail"]
        == "Say who is deciding (your name)."
    )
    saved = client.post(base, json={**body, "reviewer": "Rita"}, headers=auth).json()
    assert saved["reviewer"] == "Rita" and saved["decision"] == "approved"
    no_reason = client.post(
        base,
        json={"url": TERM, "decision": "rejected", "next_step": "exclude", "reviewer": "R"},
        headers=auth,
    )
    assert (
        no_reason.status_code == 422
        and no_reason.json()["detail"] == "Not saved: a rejected verdict needs a reason."
    )
    missing = client.post(
        base, json={"url": f"{CLIENT}/nope", "decision": "approved", "reviewer": "R"}, headers=auth
    )
    assert missing.status_code == 404
    assert client.get(f"/api/projects/{pid}/sites/{sid}/runs/run-nope/verdicts").status_code == 404


def test_api_with_accounts_takes_the_reviewer_from_the_account(
    world: World, tmp_path: Path
) -> None:
    accounts = AccountStore(world.store.root / "accounts")
    accounts.create(
        "rita", "Rita Reviewer", "reviewer", "rita-password-1", must_change_password=False
    )
    accounts.create("vera", "Vera Viewer", "viewer", "vera-password-1", must_change_password=False)
    client = _app(world, tmp_path, TokenSigner(b"k" * 32))
    pid, sid, rid = _client_run(world)
    base = f"/api/projects/{pid}/sites/{sid}/runs/{rid}/verdicts"
    login = {"username": "vera", "password": "vera-password-1"}
    assert client.post("/api/auth/login", json=login, headers=WRITE).status_code == 200
    assert client.get(base).status_code == 200
    body = {
        "url": TERM,
        "decision": "rejected",
        "next_step": "obtain_manually",
        "note": "PDF behind a form",
        "reviewer": "someone else",
    }
    assert client.post(base, json=body, headers=WRITE).status_code == 403
    client.post(
        "/api/auth/login", json={"username": "rita", "password": "rita-password-1"}, headers=WRITE
    )
    saved = client.post(base, json=body, headers=WRITE).json()
    assert saved["reviewer"] == "Rita Reviewer" and saved["next_step"] == "obtain_manually"
