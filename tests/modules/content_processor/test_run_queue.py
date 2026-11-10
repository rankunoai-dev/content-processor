"""The shared scrape queue: one scrape at a time, in order, with "your turn" emails.

Projects and URLs are unlimited; runs are not simultaneous. A queued scrape either
starts by itself when its turn comes, or emails its person and waits for them.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.modules.content_processor.projects.models import RUN_ACTIVE, RunState
from src.modules.content_processor.projects.notify import Notifier, check_email, hide_email
from src.modules.content_processor.projects.runner import SECONDS_PER_URL, ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import ProjectStore, SiteSpec
from src.modules.content_processor.webapi.app import WRITE_HEADER, WRITE_HEADER_VALUE, create_app

PASSWORD = "correct horse battery"


class GatedBatches:
    """Writes finished records; each batch waits for `release` and counts overlap."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.order: list[str] = []
        self.active = 0
        self.most_at_once = 0
        self._lock = threading.Lock()

    def __call__(self, scope: Path, out: Path, cancel: threading.Event) -> int:
        urls = json.loads(scope.read_text("utf-8"))["input_urls"]
        with self._lock:
            self.active += 1
            self.most_at_once = max(self.most_at_once, self.active)
            self.order.append(urls[0])
        self.release.wait(20)
        out.with_suffix(".records.jsonl").write_text(
            "\n".join(
                json.dumps({"requested_url": u, "badge": "VERIFIED_WITHIN_SCOPE"}) for u in urls
            )
            + "\n",
            encoding="utf-8",
        )
        with self._lock:
            self.active -= 1
        return 0


def _report(records: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.csv").write_text("url\n", encoding="utf-8")
    return 0


def _knowledge(records: Path, namespace: str) -> tuple[bool, str]:
    return True, "ok"


def _projects(store: ProjectStore) -> dict[str, str]:
    """Three projects of different people, each with one website and its URLs."""
    ids = {}
    for name, domain in (("Alpha", "alpha.com"), ("Beta", "beta.com"), ("Gamma", "gamma.com")):
        record = store.create(
            name=name,
            owner_name=f"{name} owner",
            client_name=name,
            password=PASSWORD,
            client_site=SiteSpec(name, domain),
        )
        store.add_urls(record.project_id, record.sites[0].site_id, [f"https://{domain}/a"])
        ids[name] = record.project_id
    return ids


def _site(store: ProjectStore, project_id: str) -> str:
    return store.get(project_id).sites[0].site_id


def _wait_for(check, timeout: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


@pytest.fixture
def env(tmp_path: Path):
    store = ProjectStore(tmp_path / "app_data")
    batches = GatedBatches()
    runner = ScrapeRunner(
        store,
        command=batches,
        report=_report,
        knowledge=_knowledge,
        notifier=Notifier(outbox=tmp_path / "outbox", base_url="http://app.example"),
    )
    runner.start()
    yield store, runner, batches, tmp_path / "outbox"
    batches.release.set()


def _status(store: ProjectStore, state) -> str:  # type: ignore[no-untyped-def]
    return store.run(state.project_id, state.site_id, state.run_id).status


def test_emails_are_checked_and_shown_partly_hidden():
    assert check_email(" gaurav.doshi@rankuno.com ") == "gaurav.doshi@rankuno.com"
    assert hide_email("gaurav.doshi@rankuno.com") == "g***@rankuno.com"
    for bad in ("gaurav", "a@b", "a b@c.com", "x@y.com, z@w.com"):
        with pytest.raises(ValueError, match="not an email address"):
            check_email(bad)


def test_one_scrape_at_a_time_in_the_order_queued(env):
    store, runner, batches, _ = env
    ids = _projects(store)
    first = runner.submit(ids["Alpha"], _site(store, ids["Alpha"]), started_by="A")
    _wait_for(lambda: _status(store, first) == "running")
    second = runner.submit(ids["Beta"], _site(store, ids["Beta"]), started_by="B")
    third = runner.submit(ids["Gamma"], _site(store, ids["Gamma"]), started_by="C")
    assert second.message == "Waiting for its turn in the queue."

    running, waiting, ready = runner.queue()
    assert [e.state.run_id for e in running] == [first.run_id]
    assert [(e.site_name, e.position) for e in waiting] == [("Beta", 1), ("Gamma", 2)]
    assert waiting[0].wait_s == pytest.approx(30.0), "1 URL left ahead at 30 s per URL"
    assert waiting[1].wait_s == pytest.approx(60.0)
    assert ready == []

    batches.release.set()
    assert runner.wait_idle(20)
    assert batches.most_at_once == 1, "never two scrapes at the same time"
    assert batches.order == ["https://alpha.com/a", "https://beta.com/a", "https://gamma.com/a"]
    assert {_status(store, s) for s in (first, second, third)} == {"finished"}
    assert runner.seconds_per_url() >= 1.0
    assert store.run(first.project_id, first.site_id, first.run_id).active_seconds > 0


def test_two_requests_at_once_queue_one_scrape_of_a_website(env):
    store, runner, _, _ = env
    ids = _projects(store)
    pid, sid = ids["Alpha"], _site(store, ids["Alpha"])
    meet = threading.Barrier(2)
    real = runner._queue_time  # noqa: SLF001 - the moment between the check and the write

    def held() -> datetime:
        # Both requests here at once would mean both passed the check.
        with contextlib.suppress(threading.BrokenBarrierError):
            meet.wait(timeout=0.5)
        return real()

    runner._queue_time = held  # type: ignore[method-assign]  # noqa: SLF001
    results: list[RunState | ValueError] = []

    def go() -> None:
        try:
            results.append(runner.submit(pid, sid, started_by="A"))
        except ValueError as exc:
            results.append(exc)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sum(isinstance(r, RunState) for r in results) == 1, results
    assert sum(isinstance(r, ValueError) for r in results) == 1, results
    assert len([r for r in store.runs(pid, sid) if r.status in RUN_ACTIVE]) == 1


def test_a_scrape_cancelled_before_it_starts_keeps_no_flag(env):
    store, runner, batches, _ = env
    ids = _projects(store)
    first = runner.submit(ids["Alpha"], _site(store, ids["Alpha"]), started_by="A")
    _wait_for(lambda: _status(store, first) == "running")
    second = runner.submit(ids["Beta"], _site(store, ids["Beta"]), started_by="B")
    runner.cancel(second.project_id, second.site_id, second.run_id)
    assert second.run_id not in runner._cancel  # noqa: SLF001
    batches.release.set()
    assert runner.wait_idle(20)
    assert first.run_id not in runner._cancel  # noqa: SLF001


def test_wait_estimates_count_only_the_time_a_scrape_ran(tmp_path: Path):
    store = ProjectStore(tmp_path)
    ids = _projects(store)
    runner = ScrapeRunner(store, command=GatedBatches(), report=_report, knowledge=_knowledge)
    assert runner.seconds_per_url() == SECONDS_PER_URL
    now = datetime.now(UTC)
    # Stopped for three days, then continued: it ran for 40 s in all.
    state = runner.submit(ids["Alpha"], _site(store, ids["Alpha"]), started_by="A")
    store.save_run(
        state.model_copy(
            update={
                "status": "finished",
                "started_at": now - timedelta(days=3),
                "finished_at": now,
                "url_count": 2,
                "active_seconds": 40.0,
            }
        )
    )
    assert runner.seconds_per_url() == 20.0
    # A scrape from before running time was kept says nothing about the rate.
    legacy = runner.submit(ids["Beta"], _site(store, ids["Beta"]), started_by="B")
    store.save_run(
        legacy.model_copy(
            update={
                "status": "finished",
                "started_at": now - timedelta(hours=1),
                "finished_at": now,
                "active_seconds": 0.0,
            }
        )
    )
    assert runner.seconds_per_url() == 20.0


def test_your_turn_email_then_start_runs_before_the_waiting_line(env):
    store, runner, batches, outbox = env
    ids = _projects(store)
    first = runner.submit(ids["Alpha"], _site(store, ids["Alpha"]), started_by="A")
    _wait_for(lambda: _status(store, first) == "running")
    notify = runner.submit(
        ids["Beta"],
        _site(store, ids["Beta"]),
        started_by="B",
        on_turn="notify",
        email="beta.owner@example.com",
    )
    assert notify.notify_to == "b***@example.com"
    assert "beta.owner@example.com" not in notify.model_dump_json(), (
        "the full address stays private"
    )
    auto = runner.submit(ids["Gamma"], _site(store, ids["Gamma"]), started_by="C")

    batches.release.set()
    assert runner.wait_idle(20)
    assert _status(store, notify) == "ready", "its turn came: it waits for its person"
    assert _status(store, auto) == "finished", "the queue went on without waiting"
    [mail] = sorted(outbox.glob("*turn_*.eml"))
    message = message_from_bytes(mail.read_bytes())
    assert message["To"] == "beta.owner@example.com"
    assert message["Subject"].startswith("Your scrape can start now: Beta (Beta)")
    assert "http://app.example/projects/" in message.get_payload()
    held = store.run(notify.project_id, notify.site_id, notify.run_id)
    assert held.turn_at is not None and "saved as" in held.message, "no SMTP: saved to outbox"

    # Started while another scrape runs: it goes before scrapes queued earlier.
    batches.release.clear()
    again = runner.submit(ids["Alpha"], _site(store, ids["Alpha"]), started_by="A")
    _wait_for(lambda: _status(store, again) == "running")
    later = runner.submit(ids["Gamma"], _site(store, ids["Gamma"]), started_by="C")
    started = runner.start_ready(notify.project_id, notify.site_id, notify.run_id)
    assert started.jump and started.message == "Starts as soon as the current scrape ends."
    _, waiting, _ = runner.queue()
    assert [e.state.run_id for e in waiting] == [notify.run_id, later.run_id]
    batches.release.set()
    assert runner.wait_idle(20)
    assert batches.order[-2:] == ["https://beta.com/a", "https://gamma.com/a"]
    with pytest.raises(ValueError, match="turn has come"):
        runner.start_ready(notify.project_id, notify.site_id, notify.run_id)


def test_email_when_done_and_rules_for_addresses(env):
    store, runner, batches, outbox = env
    ids = _projects(store)
    site = _site(store, ids["Alpha"])
    with pytest.raises(ValueError, match="Give an email address"):
        runner.submit(ids["Alpha"], site, started_by="A", on_turn="notify")
    with pytest.raises(ValueError, match="not an email address"):
        runner.submit(ids["Alpha"], site, started_by="A", email="nope", email_when_done=True)
    state = runner.submit(
        ids["Alpha"],
        site,
        started_by="A",
        on_turn="notify",
        email="alpha@example.com",
        email_when_done=True,
    )
    assert state.on_turn == "auto", "nothing to wait for: it starts at once"
    batches.release.set()
    assert runner.wait_idle(20)
    folder = store.run_dir(state.project_id, state.site_id, state.run_id)
    assert json.loads((folder / "notify.json").read_text("utf-8")) == {"email": "alpha@example.com"}
    [mail] = sorted(outbox.glob("*done_*.eml"))
    assert message_from_bytes(mail.read_bytes())["Subject"] == "Your scrape finished: Alpha (Alpha)"


def test_a_scrape_not_started_after_its_turn_is_cancelled(tmp_path: Path):
    store = ProjectStore(tmp_path)
    ids = _projects(store)
    runner = ScrapeRunner(store, command=GatedBatches(), report=_report, knowledge=_knowledge)
    state = runner.submit(
        ids["Beta"], _site(store, ids["Beta"]), started_by="B", on_turn="auto"
    ).model_copy(update={"status": "ready", "turn_at": datetime.now(UTC) - timedelta(hours=73)})
    store.save_run(state)
    runner._expire_ready()  # noqa: SLF001 - the worker calls it on every turn
    expired = store.run(state.project_id, state.site_id, state.run_id)
    assert expired.status == "cancelled" and "not started" in expired.message


def test_the_queue_survives_a_restart_in_order(tmp_path: Path):
    store = ProjectStore(tmp_path)
    ids = _projects(store)
    batches = GatedBatches()
    before = ScrapeRunner(store, command=batches, report=_report, knowledge=_knowledge)
    # Not started: the runs stay queued on disk, as after a crash.
    states = [before.submit(ids[n], _site(store, ids[n]), started_by=n) for n in ("Gamma", "Alpha")]
    held = before.submit(ids["Beta"], _site(store, ids["Beta"]), started_by="B")
    store.save_run(
        store.run(held.project_id, held.site_id, held.run_id).model_copy(
            update={"status": "ready", "turn_at": datetime.now(UTC)}
        )
    )
    after = ScrapeRunner(store, command=batches, report=_report, knowledge=_knowledge)
    batches.release.set()
    after.start()
    assert after.wait_idle(20)
    assert batches.order == ["https://gamma.com/a", "https://alpha.com/a"]
    assert all(_status(store, s) == "finished" for s in states)
    assert _status(store, held) == "ready", "a scrape waiting for its person is not started"


def test_the_app_shows_the_shared_queue_without_addresses(tmp_path: Path):
    store = ProjectStore(tmp_path / "app_data")
    batches = GatedBatches()
    runner = ScrapeRunner(store, command=batches, report=_report, knowledge=_knowledge)
    runner.start()
    signer = TokenSigner.from_file(tmp_path / "app_data" / "secret.key")
    exports = tmp_path / "exports"
    exports.mkdir()
    client = TestClient(
        create_app(exports, projects=(store, runner, signer)), base_url="http://127.0.0.1"
    )
    ids = _projects(store)
    tokens = {n: signer.issue(pid)[0] for n, pid in ids.items()}

    def start(name: str, body: object | None) -> object:
        pid = ids[name]
        return client.post(
            f"/api/projects/{pid}/sites/{_site(store, pid)}/runs",
            json=body,
            headers={WRITE_HEADER: WRITE_HEADER_VALUE, "X-Project-Token": tokens[name]},
        )

    try:
        assert start("Alpha", {}).status_code == 200  # type: ignore[attr-defined]
        _wait_for(lambda: client.get("/api/queue").json()["running"] != [])
        reply = start("Beta", {"on_turn": "notify", "email": "beta.owner@example.com"})
        assert reply.status_code == 200  # type: ignore[attr-defined]
        bad = start("Gamma", {"on_turn": "notify"})
        assert bad.status_code == 409 and "email" in bad.json()["detail"]  # type: ignore[attr-defined]
        queue = client.get("/api/queue")
        assert "beta.owner@example.com" not in queue.text
        body = queue.json()
        assert body["running"][0]["project_name"] == "Alpha"
        assert body["waiting"][0]["run"]["notify_to"] == "b***@example.com"
        assert body["email_sends"] is False

        batches.release.set()
        assert runner.wait_idle(20)
        [held] = client.get("/api/queue").json()["ready"]
        path = (
            f"/api/projects/{ids['Beta']}/sites/{held['run']['site_id']}"
            f"/runs/{held['run']['run_id']}/start"
        )
        refused = client.post(path, json={}, headers={WRITE_HEADER: WRITE_HEADER_VALUE})
        assert refused.status_code == 401, "only the project's password holder starts it"
        ok = client.post(
            path,
            json={},
            headers={WRITE_HEADER: WRITE_HEADER_VALUE, "X-Project-Token": tokens["Beta"]},
        )
        assert ok.status_code == 200
        assert runner.wait_idle(20)
        assert client.get("/api/queue").json() == {
            "running": [],
            "waiting": [],
            "ready": [],
            "seconds_per_url": pytest.approx(client.get("/api/queue").json()["seconds_per_url"]),
            "email_sends": False,
            "ready_hours": 72.0,
        }
    finally:
        batches.release.set()
