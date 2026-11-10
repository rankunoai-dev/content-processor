"""A stopped scrape keeps what it captured and continues without asking for it again."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from fixtures.server import app
from src.integrations.browser_client import RenderResult
from src.integrations.evidence_store import LocalEvidenceStore
from src.integrations.http_client import FetchLimits, FetchResult, HttpFetcher
from src.modules.content_processor.enums import AccessTier, EntityStatus
from src.modules.content_processor.explore.model import (
    ControlRecord,
    ExplorationResult,
    ExploredState,
)
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.projects.runner import CAPTURES_DIR, ScrapeRunner
from src.modules.content_processor.projects.store import ProjectStore, SiteSpec
from src.modules.content_processor.scheduler.capture_journal import (
    CaptureJournal,
    saved_capture_count,
)
from src.modules.content_processor.workers.fetch_worker import FetchWorker
from src.modules.content_processor.workers.render_worker import RenderWorker
from tests.modules.content_processor.test_fetch_worker import InMemoryLedger
from tests.modules.content_processor.test_fetch_worker import _scope as fetch_scope
from tests.modules.content_processor.test_render_worker import (
    RENDERED_HTML,
    FakeRenderer,
    _scope,
    _seed,
)

URL = "https://site.example/a"
T1 = "T1_POLITE_HTTP"
T3 = "T3_FIDELITY_BROWSER"
PASSWORD = "correct horse battery"


def _ok(url: str = URL, **changes: Any) -> FetchResult:
    body = b"<html><body><p>Real page text.</p></body></html>"
    return FetchResult(
        requested_url=url,
        final_url=url,
        http_status=200,
        headers={"content-type": "text/html"},
        body=body,
        sniffed_mime="text/html",
        received_bytes=len(body),
    ).model_copy(update=changes)


@pytest.fixture
def evidence(tmp_path: Path) -> LocalEvidenceStore:
    return LocalEvidenceStore(tmp_path / "evidence", "t1")


@pytest.fixture
def journal(tmp_path: Path, evidence: LocalEvidenceStore) -> CaptureJournal:
    return CaptureJournal(tmp_path / "captures", evidence)


# -- the journal --------------------------------------------------------------


def test_a_saved_download_comes_back_exactly_with_its_real_time(
    tmp_path: Path, evidence: LocalEvidenceStore
):
    when = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    first = CaptureJournal(tmp_path / "c", evidence, clock=lambda: when)
    first.save_fetch(_ok(), T1)

    # A new process: only what is on disk is left.
    again = CaptureJournal(tmp_path / "c", evidence, clock=lambda: when + timedelta(hours=1))
    saved = again.find_fetch(URL, ["T2_HTTP_IMPERSONATED", T1])

    assert saved is not None and saved.tier == T1
    assert saved.result.body == _ok().body
    assert saved.result.headers == _ok().headers
    assert saved.result.captured_earlier_at == when
    assert again.stats["fetch_replayed"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"http_status": 500},
        {"http_status": 404},
        {"http_status": 304, "body": b""},
        {"http_status": None, "transport_error": "reset"},
        {"truncated": True},
        {"limit_hit": True},
        {"egress_denied": "private"},
        {"spend_refused": "cap"},
    ],
)
def test_only_complete_successful_downloads_are_kept(journal: CaptureJournal, changes: dict):
    journal.save_fetch(_ok(**changes), T1)
    assert journal.find_fetch(URL, [T1]) is None


def test_an_old_capture_is_not_reused(tmp_path: Path, evidence: LocalEvidenceStore):
    when = datetime(2026, 11, 1, tzinfo=UTC)
    CaptureJournal(tmp_path / "c", evidence, clock=lambda: when).save_fetch(_ok(), T1)
    later = CaptureJournal(tmp_path / "c", evidence, clock=lambda: when + timedelta(days=8))
    assert later.find_fetch(URL, [T1]) is None
    assert later.stats["not_reused"] == 1


def test_a_half_written_capture_is_not_reused(tmp_path: Path, journal: CaptureJournal):
    journal.save_fetch(_ok(), T1)
    capture = next((tmp_path / "captures").rglob("*.json"))
    capture.write_bytes(capture.read_bytes()[:40])
    assert journal.find_fetch(URL, [T1]) is None
    assert not capture.exists(), "dropped, so the page is simply requested again"


def test_a_capture_whose_bytes_are_gone_or_changed_is_not_reused(tmp_path: Path):
    class Store:
        def __init__(self) -> None:
            self.real = LocalEvidenceStore(tmp_path / "e", "t1")
            self.mode = ""

        def put(self, data: bytes, media_type: str) -> Any:
            return self.real.put(data, media_type)

        def exists(self, sha256: str) -> bool:
            return self.real.exists(sha256)

        def get(self, sha256: str) -> bytes:
            if self.mode == "gone":
                raise FileNotFoundError(sha256)
            return b"other bytes" if self.mode == "changed" else self.real.get(sha256)

    for mode in ("gone", "changed"):
        store = Store()
        journal = CaptureJournal(tmp_path / mode, store)
        journal.save_fetch(_ok(), T1)
        store.mode = mode
        assert journal.find_fetch(URL, [T1]) is None, mode


def test_a_failed_save_never_stops_the_scrape(tmp_path: Path):
    class Broken:
        def put(self, data: bytes, media_type: str) -> Any:
            raise OSError("disk full")

    journal = CaptureJournal(tmp_path / "c", Broken())  # type: ignore[arg-type]
    journal.save_fetch(_ok(), T1)
    assert journal.stats["save_failed"] == 1


def test_a_render_comes_back_with_what_exploring_found(journal: CaptureJournal):
    result = RenderResult(
        requested_url=URL,
        final_url=URL,
        http_status=200,
        dom_html=RENDERED_HTML,
        inner_text="Hydrated content arrived.",
        screenshot_jpeg=b"\xff\xd8 not text \xff",
    )
    exploration = ExplorationResult(
        controls=[ControlRecord(control_id="c1", kind="tab", label="Pricing", status="visited")],
        states=[
            ExploredState(
                index=1,
                control_id="c1",
                kind="tab",
                inner_text="Pricing text",
                dom_html=b"<p>Pricing text</p>",
                text_delta_chars=12,
            )
        ],
        actions_used=1,
    )
    journal.save_render(result, exploration, T3)

    saved = journal.find_render(URL, T3)

    assert saved is not None and saved.exploration is not None
    assert saved.result.dom_html == RENDERED_HTML
    assert saved.result.screenshot_jpeg == b"\xff\xd8 not text \xff"
    assert saved.exploration.states[0].dom_html == b"<p>Pricing text</p>"
    assert saved.exploration.controls[0].label == "Pricing"


# -- the workers: a saved response means no request -----------------------------


class CountingFetcher:
    """The fixture app behind a fetcher that counts page requests (not robots.txt)."""

    tier_name = T1

    def __init__(self) -> None:
        self.pages: list[str] = []
        self.inner = HttpFetcher(
            host_checker=lambda _h: (True, "test"),
            user_agent="test/1.0",
            transport=httpx.ASGITransport(app=app),
            limits=FetchLimits(max_bytes=100_000, total_timeout_s=5),
        )

    async def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResult:
        if not url.endswith("/robots.txt"):
            self.pages.append(url)
        return await self.inner.fetch(url, headers=headers)


async def _attempt(fetcher: CountingFetcher, evidence: Any, journal: CaptureJournal, url: str):
    ledger = InMemoryLedger()
    ledger.add(url)
    worker = FetchWorker(
        ledger=ledger,
        fetcher=fetcher,  # type: ignore[arg-type]
        evidence=evidence,
        scope=fetch_scope(),
        run_id="run-t",
        worker_id="w1",
        journal=journal,
    )
    await worker.run_until_idle(idle_polls=1, poll_s=0, max_wait_s=0)
    return ledger, ledger.resources[url]


async def test_the_next_attempt_gets_the_page_without_a_request(
    tmp_path: Path, evidence: LocalEvidenceStore
):
    url = "http://testserver/pages/static_ok"
    when = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    root = tmp_path / "c"
    fetcher = CountingFetcher()
    earlier, first = await _attempt(
        fetcher, evidence, CaptureJournal(root, evidence, clock=lambda: when), url
    )
    assert fetcher.pages == [url]

    later = when + timedelta(hours=2)
    ledger, second = await _attempt(
        fetcher, evidence, CaptureJournal(root, evidence, clock=lambda: later), url
    )

    assert fetcher.pages == [url], "no second request for the page"
    assert second.status is first.status
    assert second.payload_sha256 == first.payload_sha256
    assert second.acquired_at == when, "it keeps the time it was really captured"
    assert second.acquired_at_tier is AccessTier.T1_POLITE_HTTP
    assert ledger.summaries[second.resource_id]["fetch_replay"]["tier"] == T1
    assert [b.normalized_text for b in ledger.blocks[second.resource_id]] == [
        b.normalized_text for b in earlier.blocks[first.resource_id]
    ], "extracted again, with the same result"
    assert ledger.blocks[second.resource_id]


async def test_a_saved_response_the_checks_reject_is_requested_again(
    evidence: LocalEvidenceStore, journal: CaptureJournal
):
    url = "http://testserver/pages/static_ok"
    challenge = (
        b"<html><head><title>Just a moment...</title></head><body>challenge-platform</body></html>"
    )
    journal.save_fetch(
        FetchResult(
            requested_url=url,
            final_url=url,
            http_status=200,
            headers={"content-type": "text/html"},
            body=challenge,
            sniffed_mime="text/html",
        ),
        T1,
    )
    fetcher = CountingFetcher()

    _, blocked = await _attempt(fetcher, evidence, journal, url)
    assert fetcher.pages == [] and blocked.status is EntityStatus.BLOCKED

    _, clean = await _attempt(fetcher, evidence, journal, url)
    assert fetcher.pages == [url], "the block was not kept: the next attempt asks again"
    assert clean.status is EntityStatus.EXTRACTED_UNVERIFIED


async def test_the_next_attempt_does_not_open_the_browser_again(
    evidence: LocalEvidenceStore, journal: CaptureJournal
):
    url = "http://testserver/app"
    result = RenderResult(
        requested_url=url,
        final_url=url,
        http_status=200,
        headers={"content-type": "text/html"},
        dom_html=RENDERED_HTML,
        inner_text="Hydrated content arrived.",
    )
    finals = []
    renderer = FakeRenderer(result)
    for _ in range(2):
        ledger = MemoryLedger()
        resource = _seed(ledger)
        worker = RenderWorker(
            ledger=ledger,
            renderer=renderer,
            evidence=evidence,
            scope=_scope(),
            run_id="run-t",
            worker_id="r1",
            journal=journal,
        )
        await worker.run_until_idle(idle_polls=1, poll_s=0, max_wait_s=0)
        finals.append(ledger.get_resource(resource.resource_id))

    assert renderer.calls == [url], "rendered once"
    assert finals[0].status is finals[1].status
    assert finals[1].status_reason == finals[0].status_reason


# -- the runner: continuing a stopped scrape --------------------------------------


Script = Callable[[list[str], Path, threading.Event], int]


def _write(out: Path, badges: dict[str, str]) -> None:
    out.with_suffix(".records.jsonl").write_text(
        "".join(json.dumps({"requested_url": u, "badge": b}) + "\n" for u, b in badges.items()),
        encoding="utf-8",
    )


def _report(records: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.csv").write_text("url\n", encoding="utf-8")
    return 0


def _wait(check: Callable[[], bool], timeout_s: float = 15) -> None:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


class Setup:
    def __init__(
        self,
        tmp_path: Path,
        urls: int = 4,
        report: Callable[[Path, Path], int] = _report,
        knowledge: Callable[[Path, str], tuple[bool, str]] = lambda _r, _n: (True, "ok"),
    ) -> None:
        self.store = ProjectStore(tmp_path / "app_data")
        self.calls: list[list[str]] = []
        self.script: Script = lambda _u, _o, _c: 0
        record = self.store.create(
            name="Alpha",
            owner_name="Owner",
            client_name="Alpha",
            password=PASSWORD,
            client_site=SiteSpec("Alpha", "alpha.com"),
        )
        self.project = record.project_id
        self.site = record.sites[0].site_id
        self.urls = [f"https://alpha.com/{i}" for i in range(urls)]
        self.store.add_urls(self.project, self.site, self.urls)
        self.runner = ScrapeRunner(
            self.store,
            command=self._command,
            report=report,
            knowledge=knowledge,
            batch_size=2,
            attempts=2,
        )
        self.runner.start()

    def folder(self, run_id: str) -> Path:
        return self.store.run_dir(self.project, self.site, run_id)

    def merged(self, run_id: str) -> dict[str, dict[str, Any]]:
        text = (self.folder(run_id) / f"{run_id}.records.jsonl").read_text("utf-8")
        return {r["requested_url"]: r for r in map(json.loads, text.splitlines())}

    def _command(self, scope: Path, out: Path, cancel: threading.Event) -> int:
        urls = json.loads(scope.read_text("utf-8"))["input_urls"]
        self.calls.append(urls)
        return self.script(urls, out, cancel)

    def state(self, run_id: str):  # type: ignore[no-untyped-def]
        return self.store.run(self.project, self.site, run_id)

    def wait_status(self, run_id: str, *statuses: str) -> None:
        _wait(lambda: self.state(run_id).status in statuses and self.runner.wait_idle(0.01))


def _done(urls: list[str], out: Path, _cancel: threading.Event) -> int:
    _write(out, dict.fromkeys(urls, "VERIFIED_WITHIN_SCOPE"))
    return 0


def _cancel_on_second_batch(setup: Setup, run_id_holder: dict[str, str]) -> Script:
    def script(urls: list[str], out: Path, cancel: threading.Event) -> int:
        if urls[0] == setup.urls[2]:
            setup.runner.cancel(setup.project, setup.site, run_id_holder["id"])
            return -1  # killed mid-batch: no records for this batch
        return _done(urls, out, cancel)

    return script


def test_continuing_repeats_nothing_that_finished(tmp_path: Path):
    setup = Setup(tmp_path)
    holder: dict[str, str] = {}
    release = threading.Event()

    def script(urls: list[str], out: Path, cancel: threading.Event) -> int:
        release.wait(10)
        return _cancel_on_second_batch(setup, holder)(urls, out, cancel)

    setup.script = script
    run = setup.runner.submit(setup.project, setup.site, started_by="G")
    holder["id"] = run.run_id
    release.set()
    setup.wait_status(run.run_id, "cancelled")
    assert setup.state(run.run_id).pages_done == 2
    assert "can be continued" in setup.state(run.run_id).message

    setup.calls.clear()
    setup.script = _done
    resumed = setup.runner.resume(setup.project, setup.site, run.run_id)
    assert resumed.status == "queued" and resumed.resume_count == 1 and resumed.finished_at is None
    setup.wait_status(run.run_id, "finished")

    assert setup.calls == [setup.urls[2:]], "only the batch that never finished"
    final = setup.state(run.run_id)
    assert final.pages_done == 4 and final.reports_ready
    folder = setup.store.run_dir(setup.project, setup.site, run.run_id)
    merged = (folder / f"{run.run_id}.records.jsonl").read_text("utf-8")
    assert sorted(json.loads(x)["requested_url"] for x in merged.splitlines()) == setup.urls


def test_a_page_that_ended_blocked_gets_one_more_try_and_the_better_result_is_kept(tmp_path: Path):
    setup = Setup(tmp_path, urls=2)
    setup.script = lambda urls, out, _c: (
        _write(out, {urls[0]: "BLOCKED", urls[1]: "VERIFIED_WITHIN_SCOPE"}) or 0
    )
    run = setup.runner.submit(setup.project, setup.site, started_by="G")
    setup.wait_status(run.run_id, "finished")
    state = setup.state(run.run_id)
    # A failed last step leaves it continuable.
    setup.store.save_run(state.model_copy(update={"status": "cancelled"}))

    setup.calls.clear()
    setup.script = lambda urls, out, _c: _write(out, dict.fromkeys(urls, "BLOCKED")) or 0
    setup.runner.resume(setup.project, setup.site, run.run_id)
    setup.wait_status(run.run_id, "finished")
    assert setup.calls == [[setup.urls[0]]], "one more try, once, and only for the blocked page"

    setup.store.save_run(setup.state(run.run_id).model_copy(update={"status": "cancelled"}))
    setup.calls.clear()
    setup.script = _done
    setup.runner.resume(setup.project, setup.site, run.run_id)
    setup.wait_status(run.run_id, "finished")
    assert setup.state(run.run_id).results == {"VERIFIED_WITHIN_SCOPE": 2}


def test_saved_responses_are_removed_only_when_the_scrape_finishes(tmp_path: Path):
    setup = Setup(tmp_path, urls=2)
    gate = threading.Event()
    holder: dict[str, str] = {}

    def stop(urls: list[str], out: Path, _c: threading.Event) -> int:
        gate.wait(10)
        setup.runner.cancel(setup.project, setup.site, holder["id"])
        return -1

    setup.script = stop
    run = setup.runner.submit(setup.project, setup.site, started_by="G")
    holder["id"] = run.run_id
    folder = setup.store.run_dir(setup.project, setup.site, run.run_id)
    saved = folder / CAPTURES_DIR / "fetch" / "ab" / "x.json"
    saved.parent.mkdir(parents=True)
    saved.write_text("{}", encoding="utf-8")
    gate.set()
    setup.wait_status(run.run_id, "cancelled")
    assert saved.exists()
    assert saved_capture_count(folder / CAPTURES_DIR) == 1

    setup.script = _done
    resumed = setup.runner.resume(setup.project, setup.site, run.run_id)
    assert "1 saved responses" in resumed.message
    setup.wait_status(run.run_id, "finished")
    assert not (folder / CAPTURES_DIR).exists()


def test_a_running_or_finished_scrape_cannot_be_continued(tmp_path: Path):
    setup = Setup(tmp_path, urls=2)
    setup.script = _done
    run = setup.runner.submit(setup.project, setup.site, started_by="G")
    setup.wait_status(run.run_id, "finished")
    with pytest.raises(ValueError, match="can be continued"):
        setup.runner.resume(setup.project, setup.site, run.run_id)


def test_a_damaged_results_file_counts_as_pages_not_done(tmp_path: Path):
    setup = Setup(tmp_path, urls=2)
    attempts: list[int] = []

    def script(urls: list[str], out: Path, _c: threading.Event) -> int:
        attempts.append(1)
        if len(attempts) == 1:
            good = json.dumps({"requested_url": urls[0], "badge": "VERIFIED_WITHIN_SCOPE"})
            out.with_suffix(".records.jsonl").write_text(
                good + '\n{"requested_url": "https://alpha', encoding="utf-8"
            )
            return 1
        return _done(urls, out, _c)

    setup.script = script
    run = setup.runner.submit(setup.project, setup.site, started_by="G")
    setup.wait_status(run.run_id, "finished")
    assert setup.calls[1] == [setup.urls[1]], "only the page whose line was damaged"
    assert setup.state(run.run_id).results == {"VERIFIED_WITHIN_SCOPE": 2}


def _rows(out: Path, rows: list[dict[str, Any]]) -> None:
    out.with_suffix(".records.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


def _out_of_time(url: str) -> dict[str, Any]:
    """The record of a page whose batch hit its runtime budget (as the live batch wrote it)."""
    return {
        "requested_url": url,
        "badge": "PARTIAL",
        "resource_status": "BUDGET_EXHAUSTED",
        "resource_status_reason": "budget_exhausted:runtime",
    }


def test_a_page_its_batch_ran_out_of_time_for_is_tried_again(tmp_path: Path):
    setup = Setup(tmp_path, urls=2)
    first, second = setup.urls
    done_before_retry: list[int] = []

    def script(urls: list[str], out: Path, _c: threading.Event) -> int:
        if out.name.endswith("_a1.json"):
            _rows(
                out,
                [_out_of_time(first), {"requested_url": second, "badge": "VERIFIED_WITHIN_SCOPE"}],
            )
            return 0
        done_before_retry.append(setup.state(out.parent.name).pages_done)
        return _done(urls, out, _c)

    setup.script = script
    run = setup.runner.submit(setup.project, setup.site, started_by="G")
    setup.wait_status(run.run_id, "finished")
    assert setup.calls == [[first, second], [first]], "the page out of time gets the next try"
    assert done_before_retry == [1], "not counted as done while a try is left"
    assert setup.state(run.run_id).results == {"VERIFIED_WITHIN_SCOPE": 2}
    kept = setup.merged(run.run_id)[first]
    assert kept["badge"] == "VERIFIED_WITHIN_SCOPE" and "resource_status" not in kept


def test_a_page_out_of_time_on_every_try_is_kept_as_it_is_and_retried_when_continued(
    tmp_path: Path,
):
    setup = Setup(tmp_path, urls=2)
    first, second = setup.urls
    setup.script = lambda urls, out, _c: (
        _rows(
            out,
            [
                _out_of_time(u) if u == first else {"requested_url": u, "badge": "PARTIAL"}
                for u in urls
            ],
        )
        or 0
    )
    run = setup.runner.submit(setup.project, setup.site, started_by="G")
    setup.wait_status(run.run_id, "finished")
    state = setup.state(run.run_id)
    assert setup.calls == [[first, second], [first]]
    assert state.pages_done == 2 and state.results == {"PARTIAL": 1, "BUDGET_EXHAUSTED": 1}
    assert setup.merged(run.run_id)[first]["resource_status"] == "BUDGET_EXHAUSTED"

    setup.store.save_run(state.model_copy(update={"status": "cancelled"}))
    setup.calls.clear()
    setup.script = _done
    setup.runner.resume(setup.project, setup.site, run.run_id)
    setup.wait_status(run.run_id, "finished")
    assert setup.calls == [[first]], "continuing gives it another try"
    assert setup.state(run.run_id).results == {"PARTIAL": 1, "VERIFIED_WITHIN_SCOPE": 1}


def test_batches_done_never_goes_back_when_a_scrape_is_continued(tmp_path: Path):
    setup = Setup(tmp_path, urls=4)
    setup.script = lambda urls, out, _c: (
        _write(
            out,
            dict.fromkeys(urls, "BLOCKED" if urls[0] == setup.urls[0] else "VERIFIED_WITHIN_SCOPE"),
        )
        or 0
    )
    run = setup.runner.submit(setup.project, setup.site, started_by="G")
    setup.wait_status(run.run_id, "finished")
    assert setup.state(run.run_id).batches_done == 2
    setup.store.save_run(setup.state(run.run_id).model_copy(update={"status": "cancelled"}))

    seen: list[int] = []
    real_save = setup.store.save_run

    def recording(state: Any) -> None:
        seen.append(state.batches_done)
        real_save(state)

    setup.store.save_run = recording  # type: ignore[method-assign]
    setup.script = _done
    setup.runner.resume(setup.project, setup.site, run.run_id)
    setup.wait_status(run.run_id, "finished")
    assert setup.calls[-1] == setup.urls[:2], "batch 1 was tried again"
    assert min(seen) == 2, f"batches done went back: {seen}"
    assert setup.state(run.run_id).results == {"VERIFIED_WITHIN_SCOPE": 4}


def test_a_page_found_on_the_way_keeps_its_best_try_too(tmp_path: Path):
    setup = Setup(tmp_path, urls=2)
    first, second = setup.urls
    found = "https://alpha.com/brochure.pdf"

    def script(urls: list[str], out: Path, _c: threading.Event) -> int:
        if out.name.endswith("_a1.json"):
            _rows(
                out,
                [
                    {"requested_url": first, "badge": "VERIFIED_WITHIN_SCOPE"},
                    {"requested_url": second, "badge": "RUNNING"},
                    {"requested_url": found, "badge": "FAILED"},
                ],
            )
        else:
            _rows(
                out,
                [
                    {"requested_url": second, "badge": "VERIFIED_WITHIN_SCOPE"},
                    {"requested_url": found, "badge": "VERIFIED_WITHIN_SCOPE"},
                ],
            )
        return 0

    setup.script = script
    run = setup.runner.submit(setup.project, setup.site, started_by="G")
    setup.wait_status(run.run_id, "finished")
    merged = setup.merged(run.run_id)
    assert merged[found]["badge"] == "VERIFIED_WITHIN_SCOPE"
    assert list(merged) == [first, second, found], "supplied pages first, then the found ones"


def test_a_finished_scrape_leaves_nothing_behind_in_memory(tmp_path: Path):
    setup = Setup(tmp_path, urls=2)
    setup.script = _done
    run = setup.runner.submit(setup.project, setup.site, started_by="G")
    setup.wait_status(run.run_id, "finished")
    folder = setup.folder(run.run_id)
    assert run.run_id not in setup.runner._cancel  # noqa: SLF001 - what the fix is about
    assert not [p for p in setup.runner._badge_cache if p.parent == folder]  # noqa: SLF001


def test_a_cancel_while_the_reports_are_written_ends_the_scrape_cancelled(tmp_path: Path):
    holder: dict[str, str] = {}
    cancelled_once = threading.Event()

    def report(records: Path, out_dir: Path) -> int:
        if not cancelled_once.is_set():
            cancelled_once.set()
            setup.runner.cancel(setup.project, setup.site, holder["id"])
        return _report(records, out_dir)

    setup = Setup(tmp_path, urls=2, report=report)
    setup.script = _done
    run = setup.runner.submit(setup.project, setup.site, started_by="G")
    holder["id"] = run.run_id
    setup.wait_status(run.run_id, "cancelled", "finished")
    state = setup.state(run.run_id)
    assert state.status == "cancelled" and not state.reports_ready
    assert "can be continued" in state.message

    setup.runner.resume(setup.project, setup.site, run.run_id)
    setup.wait_status(run.run_id, "finished")
    assert setup.state(run.run_id).reports_ready


def test_a_cancel_while_the_knowledge_space_is_built_ends_the_scrape_cancelled(tmp_path: Path):
    holder: dict[str, str] = {}

    def knowledge(_records: Path, _namespace: str) -> tuple[bool, str]:
        setup.runner.cancel(setup.project, setup.site, holder["id"])
        return True, "ok"

    setup = Setup(tmp_path, urls=2, knowledge=knowledge)
    setup.script = _done
    run = setup.runner.submit(setup.project, setup.site, started_by="G")
    holder["id"] = run.run_id
    setup.wait_status(run.run_id, "cancelled", "finished")
    state = setup.state(run.run_id)
    assert state.status == "cancelled" and state.reports_ready
    assert state.knowledge_status == "" and "cancelled" in state.knowledge_message


# -- the report and knowledge steps run like batches: watched, stopped, logged ----

_STEP = """
import sys, time
sys.stderr.write("step warning line\\n")
sys.stderr.flush()
if "--fast" in sys.argv:
    sys.exit(3)
time.sleep(30)
"""


@pytest.fixture
def fake_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.modules.content_processor.projects import runner as module

    monkeypatch.setattr(module, "_CLI", [sys.executable, "-c", _STEP])


def test_a_steps_failure_is_kept_in_the_runs_log(tmp_path: Path, fake_cli: None):
    from src.modules.content_processor.projects.runner import _run_cli

    log = tmp_path / "run" / "reports.log"
    code, note = _run_cli(["report", "--fast"], cancel=None, timeout_s=60, log=log, step="report")
    assert code == 3 and note == "step warning line"
    text = log.read_text("utf-8")
    assert "== report: exit 3" in text and "step warning line" in text
    assert [p.name for p in log.parent.iterdir()] == ["reports.log"], "no part file left"


def test_a_cancel_stops_a_running_step(tmp_path: Path, fake_cli: None):
    from src.modules.content_processor.projects.runner import _run_cli

    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    code, note = _run_cli(
        ["stamp"], cancel=cancel, timeout_s=60, log=tmp_path / "knowledge.log", step="stamp"
    )
    assert code == -1 and "cancelled" in note
    assert time.monotonic() - started < 15
    assert "exit -1 stopped: the scrape was cancelled" in (tmp_path / "knowledge.log").read_text()


def test_a_step_that_runs_too_long_is_stopped(tmp_path: Path, fake_cli: None):
    from src.modules.content_processor.projects.runner import _run_cli

    code, note = _run_cli(["index"], cancel=None, timeout_s=0, log=tmp_path / "k.log", step="index")
    assert code == -2 and "stopped after 0 s" in note


def test_the_default_steps_are_watched_and_logged(
    tmp_path: Path, fake_cli: None, monkeypatch: pytest.MonkeyPatch
):
    from src.modules.content_processor.projects import runner as module

    monkeypatch.setattr(module, "REPORT_TIMEOUT_S", 0.0)
    records = tmp_path / "run" / "r.records.jsonl"
    records.parent.mkdir()
    records.write_text("", encoding="utf-8")
    assert module._report_subprocess(records, tmp_path / "run" / "reports") == -2
    assert "== report: exit -2" in (records.parent / "reports.log").read_text("utf-8")
    stopped = threading.Event()
    stopped.set()
    assert module._knowledge_subprocess(records, "ns", cancel=stopped) == (
        False,
        "not built: the scrape was cancelled",
    )


def test_progress_reads_each_results_file_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.modules.content_processor.projects import runner as module

    setup = Setup(tmp_path, urls=8)
    setup.script = _done
    reads: list[str] = []
    real = module._records

    def counted(path: Path) -> Any:
        if path.is_file():
            reads.append(path.name)
        return real(path)

    monkeypatch.setattr(module, "_records", counted)
    run = setup.runner.submit(setup.project, setup.site, started_by="G")
    setup.wait_status(run.run_id, "finished")
    # Once for the badges and once to build the final file: not once per progress update.
    assert max(reads.count(name) for name in set(reads)) <= 3


# -- a batch never outlives the app ---------------------------------------------

_BATCH = """
import pathlib, subprocess, sys, time
from src.modules.content_processor.cli import _exit_when_parent_stops
_exit_when_parent_stops()
subprocess.run([sys.executable, "-c", "pass"], check=True, timeout=30)
beat = pathlib.Path(sys.argv[1])
for n in range(1200):
    beat.write_text(str(n))
    time.sleep(0.1)
"""
_APP = """
import subprocess, sys
subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2]], stdin=subprocess.DEVNULL)
sys.stdin.read()
"""


def test_a_batch_can_start_programs_and_stops_when_the_app_is_gone(tmp_path: Path):
    beat = tmp_path / "beat"
    app = subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-c", _APP, _BATCH, str(beat)],
        stdin=subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[3],
    )
    try:
        _wait(beat.is_file, timeout_s=90)
    finally:
        app.kill()
    app.wait(timeout=10)
    time.sleep(1.5)  # the batch notices at once; give a last write time to land
    value = beat.read_text()
    time.sleep(1.5)
    assert beat.read_text() == value, "the batch kept running after the app was gone"
