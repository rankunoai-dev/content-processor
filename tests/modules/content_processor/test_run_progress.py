"""Live progress of a scrape: batches, the pages of a batch and the batch log, read from files."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.modules.content_processor.projects.models import RunState
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.projects.store import ProjectStore, SiteSpec
from src.modules.content_processor.scheduler.capture_journal import capture_path
from src.modules.content_processor.webapi.app import create_app
from src.modules.content_processor.webapi.run_progress import (
    LOG_LINES_PER_CALL,
    RecordsCache,
    batch_progress,
    batch_urls,
    read_log,
    summarize_record,
)

URLS = [f"https://www.example.com/page-{i}" for i in range(1, 8)]  # 7 pages, batches of 3


def _record(url: str, badge: str, *, main: int = 4, verified: int = 4, gaps: int = 0) -> str:
    blocks = [{"role": "nav", "status": "VERIFIED_EXTRACTED", "raw_text": "Menu"}]
    blocks += [
        {"role": "main", "status": "VERIFIED_EXTRACTED" if i < verified else "EXTRACTED_UNVERIFIED"}
        for i in range(main)
    ]
    return json.dumps(
        {
            "requested_url": url,
            "badge": badge,
            "resource_status": badge,
            "resource_status_reason": "rendered;all_rendered_blocks_verified;hydrated_blocks=2",
            "blocks": blocks,
            "gaps": [{"gap_type": "UNVISITED_STATE"}] * gaps,
            "summaries": {},
        }
    )


def _log_line(message: str, level: str = "INFO", **extra: object) -> str:
    return json.dumps(
        {"ts": "2026-11-10T06:33:52+00:00", "level": level, "logger": "x", "message": message}
        | extra
    )


def _write_scope(folder: Path, number: int, attempt: int, urls: list[str]) -> None:
    (folder / f"b{number}_a{attempt}_scope.json").write_text(
        json.dumps({"mode": "SUPPLIED_URLS_PLUS_EMBEDDED", "input_urls": urls}), "utf-8"
    )


def _capture(folder: Path, kind: str, url: str, tier: str = "T1_POLITE_HTTP") -> None:
    path = capture_path(folder / "captures", kind, url, tier)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 1, "url": url, "tier": tier}), "utf-8")


def _state(run_id: str, **changes: object) -> RunState:
    base: dict[str, object] = {
        "run_id": run_id,
        "project_id": "p",
        "site_id": "s",
        "status": "running",
        "created_at": datetime(2026, 11, 10, 12, 0, tzinfo=UTC),
        "started_by": "Gaurav",
        "url_count": len(URLS),
        "batch_size": 3,
        "batches_total": 3,
        "batches_done": 1,
        "current_batch": 2,
        "current_attempt": 2,
    }
    return RunState.model_validate(base | changes)


@pytest.fixture
def run_folder(tmp_path: Path) -> Path:
    """A run as the runner leaves it mid-way: batch 1 done, batch 2 on its second try.

    Batch 2's first try finished one page and is followed by a half-written line;
    its second try has downloaded one page and rendered another. Batch 3 has not started.
    """
    folder = tmp_path / "run"
    folder.mkdir()
    (folder / "urls.json").write_text(json.dumps(URLS), "utf-8")
    # batch 1: one attempt, all three pages finished
    _write_scope(folder, 1, 1, URLS[0:3])
    (folder / "b1_a1.records.jsonl").write_text(
        "\n".join(
            [
                _record(URLS[0], "VERIFIED_WITHIN_SCOPE"),
                _record(URLS[1], "PARTIAL", verified=3, gaps=2),
                _record(URLS[2], "EXCLUDED", main=0),
                json.dumps(
                    {"requested_url": "https://www.example.com/logo.svg", "badge": "EXCLUDED"}
                ),
            ]
        )
        + "\n",
        "utf-8",
    )
    (folder / "b1_a1.log").write_text(_log_line("run_execution_finished") + "\n", "utf-8")
    # batch 2: first attempt finished one page, failed another; the third page's line is cut
    _write_scope(folder, 2, 1, URLS[3:6])
    (folder / "b2_a1.records.jsonl").write_text(
        _record(URLS[3], "NEEDS_REVIEW", verified=2, gaps=1)
        + "\n"
        + _record(URLS[4], "FAILED", main=0)
        + "\n"
        + _record(URLS[5], "PARTIAL")[:80],
        "utf-8",
    )
    # second attempt: running, captures arriving
    _write_scope(folder, 2, 2, URLS[4:6])
    _capture(folder, "fetch", URLS[4])
    _capture(folder, "fetch", URLS[5], tier="T3_FIDELITY_BROWSER")
    _capture(folder, "render", URLS[5], tier="T3_FIDELITY_BROWSER")
    (folder / "b2_a2.log").write_text(
        _log_line("explore_click_failed", url=URLS[5], control="Show more")
        + "\n"
        + _log_line("image_open_error", "ERROR")
        + "\n",
        "utf-8",
    )
    return folder


# -- records ---------------------------------------------------------------------------


def test_completeness_is_the_verified_share_of_main_content():
    full = summarize_record(json.loads(_record("u", "PARTIAL", main=4, verified=4, gaps=2)))
    assert (full.completeness, full.gaps, full.final) == (100, 2, True)
    assert full.reason == "rendered", "first clause only"
    half = summarize_record(json.loads(_record("u", "PARTIAL", main=4, verified=1)))
    assert half.completeness == 25
    none = summarize_record(json.loads(_record("u", "BLOCKED", main=0)))
    assert none.completeness is None, "no main content: no percentage"
    assert summarize_record({"badge": "RUNNING"}).final is False


# -- batches ---------------------------------------------------------------------------


def test_batches_states_counts_and_captures_of_the_running_attempt(run_folder: Path):
    cache = RecordsCache()
    state = _state("r1")
    one = batch_progress(cache, run_folder, state, 1, URLS[0:3])
    assert (one.state, one.attempts, one.attempt, one.pages, one.pages_finished) == (
        "done",
        1,
        1,
        3,
        3,
    )
    assert one.by_badge == {"VERIFIED_WITHIN_SCOPE": 1, "PARTIAL": 1, "EXCLUDED": 1}
    assert one.started_at is not None and one.finished_at is not None
    assert one.in_progress is None

    two = batch_progress(cache, run_folder, state, 2, URLS[3:6])
    assert two.state == "retrying", "a second attempt is running"
    assert (two.attempts, two.attempt, two.pages_finished) == (2, 2, 1)
    assert two.by_badge == {"NEEDS_REVIEW": 1}, "a page being redone is not finished"
    assert two.in_progress is not None
    assert (two.in_progress.pages, two.in_progress.fetched, two.in_progress.rendered) == (2, 2, 1)
    assert two.finished_at is None

    three = batch_progress(cache, run_folder, state, 3, URLS[6:7])
    assert (three.state, three.attempts, three.attempt, three.started_at) == (
        "waiting",
        0,
        None,
        None,
    )

    _write_scope(run_folder, 3, 1, URLS[6:7])
    first_try = _state("r1", batches_done=2, current_batch=3, current_attempt=1)
    started = batch_progress(cache, run_folder, first_try, 3, URLS[6:7])
    assert (started.state, started.attempts, started.pages_finished) == ("running", 1, 0)
    assert started.in_progress is not None and started.in_progress.pages == 1


def test_records_are_summarised_once_until_the_file_changes(run_folder: Path):
    cache = RecordsCache()
    path = run_folder / "b1_a1.records.jsonl"
    first = cache.summaries(path)
    assert cache.summaries(path) is first, "same file, same object"
    assert set(first) == set(URLS[0:3]) | {"https://www.example.com/logo.svg"}
    path.write_text(_record(URLS[0], "BLOCKED") + "\n", "utf-8")
    assert cache.summaries(path)[URLS[0]].badge == "BLOCKED"
    assert cache.summaries(run_folder / "missing.records.jsonl") == {}


# -- the pages of a batch -------------------------------------------------------------


def test_urls_of_a_batch_show_stage_result_and_completeness(run_folder: Path):
    cache = RecordsCache()
    state = _state("r1")
    rows = batch_urls(cache, run_folder, state, 2, 0, 100)
    assert rows.total == 3 and [r.url for r in rows.items] == URLS[3:6]
    done, failed, cut = rows.items
    assert (done.stage, done.badge, done.completeness, done.gaps, done.attempt) == (
        "done",
        "NEEDS_REVIEW",
        50,
        1,
        1,
    )
    assert done.reason == "rendered"
    assert (failed.stage, failed.badge, failed.completeness) == ("downloaded", "FAILED", None)
    assert failed.attempt == 1, "the earlier try's result is shown while the new one runs"
    assert (cut.stage, cut.badge, cut.completeness) == ("rendered", None, None), (
        "the half-written line is skipped; the saved render says how far it came"
    )

    page = batch_urls(cache, run_folder, state, 2, 2, 1)
    assert (page.offset, [r.url for r in page.items]) == (2, [URLS[5]])
    waiting = batch_urls(cache, run_folder, state, 3, 0, 100)
    assert [(r.url, r.stage) for r in waiting.items] == [(URLS[6], "queued")]


# -- the log --------------------------------------------------------------------------


def test_log_tail_continues_across_calls_and_skips_unfinished_lines(tmp_path: Path):
    path = tmp_path / "b1_a1.log"
    path.write_text(_log_line("run_execution_started") + "\n" + '{"ts": "2026-', "utf-8")
    first = read_log(path, 0)
    assert first.exists and [line.message for line in first.lines] == ["run_execution_started"]
    assert first.offset == path.read_bytes().index(b"\n") + 1, "the cut line waits"

    with path.open("a", encoding="utf-8") as handle:
        handle.write('11-10T06:33:53+00:00", "level": "WARNING", "message": "capture_not_reused", ')
        handle.write('"url": "https://www.example.com/a", "why": "too_old"}\n')
        handle.write("Traceback (most recent call last):\n")
        handle.write('  File "x.py", line 1, in <module>\n')
        handle.write("{\n")
        handle.write('  "run_id": "dry-run",\n')
        handle.write('  "count": 85\n')
        handle.write("}\n")
    second = read_log(path, first.offset)
    assert [(line.level, line.message) for line in second.lines] == [
        ("WARNING", "capture_not_reused"),
        ("TEXT", "Traceback (most recent call last):"),
        ("TEXT", '  File "x.py", line 1, in <module>'),
    ], "the printed JSON report at the end of a log is not shown as log lines"
    assert second.lines[0].extra == {"url": "https://www.example.com/a", "why": "too_old"}
    assert second.lines[0].time == "2026-11-10T06:33:53+00:00"
    assert second.offset == second.size == path.stat().st_size
    assert read_log(path, second.offset).lines == [], "nothing new"
    assert read_log(path, second.offset + 999).offset == second.size, "a stale offset starts over"
    assert read_log(tmp_path / "none.log", 0).exists is False


def test_log_tail_is_capped_per_call(tmp_path: Path):
    path = tmp_path / "b1_a1.log"
    path.write_text(
        "".join(_log_line(f"m{i}") + "\n" for i in range(LOG_LINES_PER_CALL + 5)), "utf-8"
    )
    first = read_log(path, 0)
    assert len(first.lines) == LOG_LINES_PER_CALL
    rest = read_log(path, first.offset)
    assert [line.message for line in rest.lines] == [f"m{LOG_LINES_PER_CALL + i}" for i in range(5)]


# -- the routes -----------------------------------------------------------------------


@pytest.fixture
def api(tmp_path: Path, run_folder: Path):
    """The app over a store holding one project whose website has the fixture run."""
    store = ProjectStore(tmp_path / "app_data")
    record = store.create(
        name="Example",
        owner_name="Gaurav",
        client_name="Example Inc",
        password="correct horse 42",
        client_site=SiteSpec("Example", "https://www.example.com"),
        competitors=[],
    )
    site = record.sites[0]
    state = _state("example_20261110-120000", project_id=record.project_id, site_id=site.site_id)
    store.save_run(state)
    target = store.run_dir(record.project_id, site.site_id, state.run_id)
    for path in run_folder.rglob("*"):
        if path.is_file():
            dest = target / path.relative_to(run_folder)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(path.read_bytes())
    from src.modules.content_processor.projects.runner import ScrapeRunner

    runner = ScrapeRunner(store, command=lambda *_: 0, report=lambda *_: 0, knowledge=None)
    signer = TokenSigner.from_file(tmp_path / "app_data" / "secret.key")
    exports = tmp_path / "exports"
    exports.mkdir()
    app = create_app(exports, projects=(store, runner, signer))
    client = TestClient(app, base_url="http://127.0.0.1")
    return client, f"/api/projects/{record.project_id}/sites/{site.site_id}/runs/{state.run_id}"


def test_routes_answer_batches_urls_and_log(api: tuple[TestClient, str]):
    client, base = api
    batches = client.get(f"{base}/batches").json()
    assert (batches["total"], batches["current_batch"], batches["status"]) == (3, 2, "running")
    assert [b["state"] for b in batches["items"]] == ["done", "retrying", "waiting"]
    assert batches["items"][1]["in_progress"] == {"pages": 2, "fetched": 2, "rendered": 1}
    page = client.get(f"{base}/batches", params={"offset": 2, "limit": 1}).json()
    assert [b["number"] for b in page["items"]] == [3]

    urls = client.get(f"{base}/batches/2/urls").json()
    assert [(r["stage"], r["badge"]) for r in urls["items"]] == [
        ("done", "NEEDS_REVIEW"),
        ("downloaded", "FAILED"),
        ("rendered", None),
    ]
    assert client.get(f"{base}/batches/9/urls").status_code == 404
    assert client.get(f"{base}/batches/2/urls", params={"limit": 101}).status_code == 422

    log = client.get(f"{base}/log").json()
    assert (log["batch"], log["attempt"], log["exists"]) == (2, 2, True), (
        "defaults to the running try"
    )
    assert [line["message"] for line in log["lines"]] == [
        "explore_click_failed",
        "image_open_error",
    ]
    assert log["lines"][0]["extra"] == {"url": URLS[5], "control": "Show more"}
    again = client.get(
        f"{base}/log", params={"batch": 2, "attempt": 2, "after": log["offset"]}
    ).json()
    assert again["lines"] == [] and again["offset"] == log["offset"]
    first = client.get(f"{base}/log", params={"batch": 1, "attempt": 1}).json()
    assert [line["message"] for line in first["lines"]] == ["run_execution_finished"]
    assert client.get(f"{base}/log", params={"batch": 3, "attempt": 1}).json()["exists"] is False

    missing = client.get(f"{base}-nope/batches")
    assert missing.status_code == 404
