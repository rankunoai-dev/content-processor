"""Global URL history: every run remembered, newest good capture reused, old ones re-extracted.

Also covers `UrlHistory` (the tenant entry point with the API runs used before the
SQLite index) and the one-time import of the earlier file layout.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.modules.content_processor import cli
from src.modules.content_processor.enums import ChangeStatus, GapType, TaskKind, UrlBadge
from src.modules.content_processor.export.project_report import page_rows, scrape_action
from src.modules.content_processor.recrawl import (
    Baseline,
    HistoryBaseline,
    HistoryIndex,
    UrlHistory,
    describe,
    extraction_time,
    history_path,
)
from tests.modules.content_processor.l5_factory import block, gap, record
from tests.modules.content_processor.test_cli import _make_mini_scope, _patch_fetcher
from tests.modules.content_processor.test_recrawl_e2e import URL, _run
from tests.modules.content_processor.test_recrawl_units import PRIOR, _record
from tests.modules.content_processor.test_recrawl_units import T0 as UNITS_T0

T0 = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


def rec(url: str, run: str, when: datetime, **kw: object):  # type: ignore[no-untyped-def]
    base = record(url, ["Some page text here"], **kw)  # type: ignore[arg-type]
    return base.model_copy(update={"run_id": run, "captured_at": when, "payload_sha256": "ab" * 32})


def test_index_keeps_newest_good_capture_and_logs_every_run(tmp_path: Path) -> None:
    path = history_path(tmp_path, "t1")
    assert path == tmp_path / "t1" / "history.sqlite"
    url = "https://x.example/a"
    with HistoryIndex(path) as index:
        first = index.add_records([rec(url, "run-1", T0)])
        assert (first.captures_added, first.latest_updated) == (1, 1)
        # A failed capture later is logged but never replaces the good one.
        failed = rec(url, "run-2", T0 + timedelta(days=1), badge=UrlBadge.FAILED)
        second = index.add_records([failed])
        assert (second.captures_added, second.not_reusable, second.latest_updated) == (1, 1, 0)
        found = index.lookup(url)
        assert found is not None and found.run_id == "run-1"
        # A newer good capture replaces it; an older one imported later does not.
        index.add_records([rec(url, "run-3", T0 + timedelta(days=2))])
        late = index.add_records([rec(url, "run-0", T0 - timedelta(days=5))])
        assert late.kept_newer >= 1 and late.latest_updated == 0
        assert index.lookup(url + "?utm_source=mail").run_id == "run-3"  # type: ignore[union-attr]
        # Importing the same run again changes nothing.
        again = index.add_records([rec(url, "run-3", T0 + timedelta(days=2))])
        assert (again.captures_added, again.already_known) == (0, 1)
        assert index.lookup("https://x.example/never") is None
        captures = index.captures(url)
        assert [c.run_id for c in captures] == ["run-3", "run-2", "run-1", "run-0"]
        assert [c.is_latest for c in captures] == [True, False, False, False]
        assert captures[1].badge == "FAILED" and not captures[1].reusable
        assert index.stats() == {"captures": 4, "runs": 4, "urls_reusable": 1, "url_keys": 1}
    # Only the capture in use keeps its full record.
    db = sqlite3.connect(path)
    stored = db.execute("SELECT run_id FROM captures WHERE record IS NOT NULL").fetchall()
    db.close()
    assert stored == [("run-3",)]


def test_extraction_time_follows_the_original_extraction() -> None:
    fresh = rec("https://x.example/a", "run-1", T0)
    assert extraction_time(fresh) == T0
    old = T0 - timedelta(days=40)
    reused = fresh.model_copy(
        update={
            "reused_from_run_id": "run-0",
            "blocks": [b.model_copy(update={"extracted_at": old}) for b in fresh.blocks],
        }
    )
    assert extraction_time(reused) == old
    baseline = Baseline([], reuse_since=T0 - timedelta(days=30))
    assert baseline.reuse_allowed(fresh) and not baseline.reuse_allowed(reused)
    assert Baseline([]).reuse_allowed(reused)


def test_history_baseline_ignores_captures_whose_evidence_is_gone(tmp_path: Path) -> None:
    url = "https://x.example/a"
    with HistoryIndex(tmp_path / "h.sqlite") as index:
        index.add_records([rec(url, "run-1", T0)])
        gone = HistoryBaseline(index, evidence_exists=lambda _sha: False)
        assert gone.lookup(url) is None and gone.missing_evidence == 1
        kept = HistoryBaseline(index, evidence_exists=lambda _sha: True)
        assert kept.lookup(url) is not None and len(kept) == 1


@pytest.mark.parametrize(
    ("status", "basis", "reused", "action"),
    [
        (None, None, None, "Scraped (not compared with earlier runs)"),
        (ChangeStatus.NEW, "no_prior_capture", None, "New page"),
        (
            ChangeStatus.UNCHANGED,
            "http_304_not_modified",
            "run-1",
            "Reused (unchanged since 2026-10-16: server said not modified)",
        ),
        (
            ChangeStatus.UNCHANGED,
            "visible_text_identical;bytes_differ",
            "run-1",
            "Reused (unchanged since 2026-10-16: same visible text)",
        ),
        (
            ChangeStatus.UNCHANGED,
            "payload_identical;reuse_not_allowed:capture_too_old",
            None,
            "Re-scraped (unchanged, but the earlier capture was too old to reuse)",
        ),
        (
            ChangeStatus.CHANGED,
            "visible_text_changed;site_modified_date_not_updated",
            None,
            "Re-scraped (updated: visible text changed; the site's date was not updated)",
        ),
        (ChangeStatus.CHANGED, "something_else", None, "Re-scraped (updated)"),
        (ChangeStatus.UNKNOWN, None, None, "Scraped (change unknown)"),
    ],
)
def test_scrape_action_wording(
    status: ChangeStatus | None, basis: str | None, reused: str | None, action: str
) -> None:
    r = rec("https://x.example/a", "run-2", T0).model_copy(
        update={"change_status": status, "change_basis": basis, "reused_from_run_id": reused}
    )
    assert scrape_action(r) == action


def test_report_columns_show_extraction_date_and_site_date() -> None:
    r = rec("https://x.example/a", "run-2", T0).model_copy(
        update={
            "page_modified_at": datetime(2026, 10, 1, 8, 30, tzinfo=UTC),
            "change_status": ChangeStatus.NEW,
            "gaps": [gap("res-a", GapType.LOW_CONFIDENCE)],
            "blocks": [block("res-a", 0, "Some page text here")],
        }
    )
    (row,) = page_rows([r])
    assert row["Last scraped"] == "2026-10-16 09:00"
    assert row["Site last modified"] == "2026-10-01 08:30"
    assert row["Scrape action"] == "New page"


# -- end to end through the fetch worker (fixture server) ------------------------------


async def test_second_run_reuses_from_history_and_old_captures_are_re_extracted(
    tmp_path: Path,
) -> None:
    index = HistoryIndex(tmp_path / "history.sqlite")
    try:
        _, first, sent = await _run(tmp_path, "run-1", None)
        assert sent == [""]
        index.add_records([first])

        ledger, second, sent = await _run(tmp_path, "run-2", HistoryBaseline(index))
        assert sent == ['"v1"'], "history supplied the ETag of run-1"
        assert (
            second.change_status is ChangeStatus.UNCHANGED and second.reused_from_run_id == "run-1"
        )
        assert scrape_action(second).startswith("Reused (unchanged since ")
        assert scrape_action(second).endswith(": server said not modified)")
        assert not any(t.kind is TaskKind.RENDER for t in ledger._tasks.values())  # noqa: SLF001

        # A capture extracted before `reuse_since` is compared, but fetched and extracted again.
        too_old = HistoryBaseline(index, reuse_since=datetime.now(UTC) + timedelta(days=1))
        ledger, third, sent = await _run(tmp_path, "run-3", too_old)
        assert sent == [""], "no conditional request for a capture that may not be reused"
        assert third.change_status is ChangeStatus.UNCHANGED
        assert third.change_basis is not None
        assert third.change_basis.endswith("reuse_not_allowed:capture_too_old")
        assert third.reused_from_run_id is None
        assert any(t.kind is TaskKind.RENDER for t in ledger._tasks.values())  # noqa: SLF001
        assert scrape_action(third).startswith("Re-scraped (unchanged, but")

        # Evidence gone: the page counts as new and is captured in full.
        missing = HistoryBaseline(index, evidence_exists=lambda _sha: False)
        _, fourth, sent = await _run(tmp_path, "run-4", missing)
        assert sent == [""] and fourth.change_status is ChangeStatus.NEW
        assert fourth.requested_url == URL
    finally:
        index.close()


# -- CLI ---------------------------------------------------------------------------------


def test_reuse_since_flags() -> None:
    args = argparse.Namespace(reuse_since=None, reuse_max_age_days=None)
    assert cli._reuse_since(args, history=False) is None  # noqa: SLF001
    default = cli._reuse_since(args, history=True)  # noqa: SLF001
    assert default is not None
    expected = datetime.now(UTC) - timedelta(days=cli.HISTORY_MAX_AGE_DAYS)
    assert abs((default - expected).total_seconds()) < 60
    args = argparse.Namespace(reuse_since="2099-01-01", reuse_max_age_days=7)
    assert cli._reuse_since(args, history=True) == datetime(2099, 1, 1, tzinfo=UTC)  # noqa: SLF001


def test_cli_history_import_show_stats(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    url = "https://x.example/a"
    records = tmp_path / "x.records.jsonl"
    records.write_text(rec(url, "run-1", T0).model_dump_json() + "\n", encoding="utf-8")
    base = ["history", "--tenant", "t1", "--history-root", str(tmp_path / "h")]
    assert cli.main([base[0], "import", *base[1:], "--records", str(records)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert cli.main([base[0], "show", *base[1:], "--url", url]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert [c["run_id"] for c in shown["captures"]] == ["run-1"] and shown["captures"][0]["latest"]
    assert cli.main([base[0], "stats", *base[1:]]) == 0
    assert json.loads(capsys.readouterr().out)["stats"]["captures"] == 1
    assert cli.main([base[0], "import", *base[1:]]) == 2
    assert cli.main([base[0], "show", *base[1:]]) == 2
    assert (tmp_path / "h" / "t1" / "history.sqlite").exists()
    assert out["imported"][str(records)]["captures_added"] == 1


def test_dryruns_use_and_fill_history_automatically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    _patch_fetcher(monkeypatch)
    scope = _make_mini_scope(tmp_path)
    assert cli.main(["dryrun", "--scope", str(scope), "--no-render"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["baseline"]["source"] == "history" and first["baseline"]["urls"] == 0
    assert first["history"]["captures_added"] >= 1 and first["history"]["remembered"] >= 1
    assert first["changes"] == {"new": first["scheduled"]}
    assert cli.main(["dryrun", "--scope", str(scope), "--no-render"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["baseline"]["source"] == "history"
    assert second["baseline"]["urls"] >= 1 and second["baseline"]["reuse_since"]
    assert second["changes"].get("unchanged", 0) >= 1
    assert cli.main(["dryrun", "--scope", str(scope), "--no-render", "--no-history"]) == 0
    third = json.loads(capsys.readouterr().out)
    assert third["baseline"] is None and "history" not in third


# -- UrlHistory: the tenant entry point and the earlier file layout ------------------------


def test_url_history_finds_any_spelling_and_survives_a_reload(tmp_path: Path) -> None:
    history = UrlHistory(tmp_path, "t1")
    assert history.lookup("https://x.example/fees") is None and len(history) == 0
    assert not (tmp_path / "t1").exists(), "nothing is created by a lookup"
    assert history.remember([PRIOR], run_name="jh_full") == 1
    history.close()
    with UrlHistory(tmp_path, "t1") as again:
        entry = again.lookup("https://x.example/fees?utm_source=other")
        assert entry is not None
        assert entry.last_scraped_at == UNITS_T0 and entry.run_name == "jh_full"
        assert entry.page_modified_at == UNITS_T0 and entry.badge == "PARTIAL"
        baseline = again.baseline_for(["https://x.example/fees", "https://x.example/new"])
        prior = baseline.lookup("https://x.example/fees")
        assert len(baseline) == 1 and prior is not None
        assert prior.content_fingerprint == PRIOR.content_fingerprint
    assert UrlHistory(tmp_path, "t2").lookup("https://x.example/fees") is None
    assert len(UrlHistory(tmp_path / "none", "t1").baseline_for(["https://x.example/fees"])) == 0


def test_url_history_never_replaces_a_good_newer_capture(tmp_path: Path) -> None:
    with UrlHistory(tmp_path, "t1") as history:
        history.remember([_record(run_id="run-2", captured_at=UNITS_T0 + timedelta(days=5))])
        blocked = _record(
            run_id="run-3", captured_at=UNITS_T0 + timedelta(days=9), badge=UrlBadge.BLOCKED
        )
        older = _record(run_id="run-0", captured_at=UNITS_T0 - timedelta(days=5))
        assert history.remember([blocked, older]) == 0
        entry = history.lookup("https://x.example/fees")
        assert entry is not None and entry.run_id == "run-2"


def test_pre_scrape_columns_say_whether_and_when_a_url_was_scraped(tmp_path: Path) -> None:
    with UrlHistory(tmp_path, "t1") as history:
        history.remember([PRIOR], run_name="jh_full")
        assert describe(history.lookup("https://x.example/fees")) == {
            "Scraped before": "Yes",
            "Last scraped": UNITS_T0.date().isoformat(),
            "Site last modified": UNITS_T0.date().isoformat(),
            "Run": "jh_full",
        }
    assert describe(None)["Scraped before"] == "No"


def _legacy_layout(base: Path) -> None:
    """The file layout written before the SQLite index: index.jsonl + gzip records."""
    key = "https://x.example/fees"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    path = base / "records" / digest[:2] / f"{digest}.json.gz"
    path.parent.mkdir(parents=True)
    path.write_bytes(gzip.compress(PRIOR.model_dump_json().encode("utf-8")))
    entry = {
        "key": key,
        "url": PRIOR.requested_url,
        "last_scraped_at": PRIOR.captured_at.isoformat(),
        "run_id": PRIOR.run_id,
        "run_name": "jh_full",
        "badge": "PARTIAL",
    }
    missing = {**entry, "key": "https://x.example/gone"}
    (base / "index.jsonl").write_text(
        json.dumps(entry) + "\n{not json\n\n" + json.dumps(missing) + "\n", encoding="utf-8"
    )


def test_earlier_file_history_is_imported_once_and_kept(tmp_path: Path) -> None:
    _legacy_layout(tmp_path / "t1")
    history = UrlHistory(tmp_path, "t1")
    assert len(history) == 1 and history.migrated == 1
    entry = history.lookup("https://x.example/fees")
    assert entry is not None and entry.run_name == "jh_full"
    history.close()
    assert not (tmp_path / "t1" / "index.jsonl").exists()
    assert (tmp_path / "t1" / "index.jsonl.migrated").exists()
    assert (tmp_path / "t1" / "records").exists(), "the earlier files are not deleted"
    with UrlHistory(tmp_path, "t1") as again:
        assert len(again) == 1 and again.migrated == 0


def test_cli_history_check_lists_scraped_before_and_new(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _legacy_layout(tmp_path / "h" / "t1")
    urls = tmp_path / "urls.txt"
    urls.write_text("https://x.example/fees\n\nhttps://x.example/new\n", encoding="utf-8")
    out_csv = tmp_path / "check.csv"
    base = ["history", "--tenant", "t1", "--history-root", str(tmp_path / "h")]
    assert cli.main([*base, "--urls", str(urls), "--out", str(out_csv)]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert (checked["scraped_before"], checked["new"], checked["known_urls"]) == (1, 1, 1)
    assert checked["migrated_from_files"] == 1
    header = out_csv.read_text("utf-8-sig").splitlines()[0]
    assert header == "URL,Scraped before,Last scraped,Site last modified,Run"
    assert cli.main([*base, "check"]) == 2
    assert cli.main(["history", "stats", "--tenant", "t9", "--history-root", str(tmp_path)]) == 0
    assert "stats" not in json.loads(capsys.readouterr().out)


def test_old_index_file_gets_the_run_name_column(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE captures (id INTEGER PRIMARY KEY, run_id TEXT)")
    db.commit()
    db.close()
    with pytest.raises(sqlite3.OperationalError):
        HistoryIndex(path)  # an incompatible table is not silently used
    with HistoryIndex(tmp_path / "fresh.sqlite") as index:
        index.add_records([PRIOR], run_name="r")
    db = sqlite3.connect(tmp_path / "fresh.sqlite")
    assert db.execute("SELECT run_name FROM captures").fetchall() == [("r",)]
    db.close()
