"""URL history: the latest capture of every URL across runs, used as the next baseline."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from src.modules.content_processor.enums import ChangeStatus, UrlBadge
from src.modules.content_processor.export.project_report import action, page_rows
from src.modules.content_processor.recrawl.history import UrlHistory, describe
from tests.modules.content_processor.test_recrawl_units import PRIOR, T0, _record


def test_remembered_urls_are_found_in_any_spelling_and_survive_a_reload(tmp_path: Path):
    history = UrlHistory(tmp_path, "t1")
    assert history.lookup("https://x.example/fees") is None
    assert history.remember([PRIOR], run_name="jh_full") == 1
    again = UrlHistory(tmp_path, "t1")  # a later run reads it from disk
    entry = again.lookup("https://x.example/fees?utm_source=other")
    assert entry is not None
    assert entry.last_scraped_at == T0 and entry.run_name == "jh_full"
    assert entry.page_modified_at == T0 and entry.badge == "PARTIAL"
    baseline = again.baseline_for(["https://x.example/fees", "https://x.example/new"])
    prior = baseline.lookup("https://x.example/fees")
    assert len(baseline) == 1 and prior is not None
    assert prior.content_fingerprint == PRIOR.content_fingerprint


def test_a_failed_or_older_capture_never_replaces_a_good_newer_one(tmp_path: Path):
    history = UrlHistory(tmp_path, "t1")
    newer = _record(run_id="run-2", captured_at=T0 + timedelta(days=5))
    history.remember([newer])
    blocked = _record(run_id="run-3", captured_at=T0 + timedelta(days=9), badge=UrlBadge.BLOCKED)
    older = _record(run_id="run-0", captured_at=T0 - timedelta(days=5))
    assert history.remember([blocked, older]) == 0
    entry = history.lookup("https://x.example/fees")
    assert entry is not None and entry.run_id == "run-2"


def test_other_tenants_and_a_damaged_index_line_are_ignored(tmp_path: Path):
    UrlHistory(tmp_path, "t1").remember([PRIOR])
    assert UrlHistory(tmp_path, "t2").lookup("https://x.example/fees") is None
    index = tmp_path / "t1" / "index.jsonl"
    index.write_text(index.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
    assert len(UrlHistory(tmp_path, "t1")) == 1


def test_pre_scrape_columns_say_whether_and_when_a_url_was_scraped(tmp_path: Path):
    history = UrlHistory(tmp_path, "t1")
    history.remember([PRIOR], run_name="jh_full")
    assert describe(history.lookup("https://x.example/fees")) == {
        "Scraped before": "Yes",
        "Last scraped": T0.date().isoformat(),
        "Site last updated": T0.date().isoformat(),
        "Run": "jh_full",
    }
    assert describe(None)["Scraped before"] == "No"


def test_report_action_column_says_new_reused_or_rescraped():
    assert action(PRIOR) == "New: scraped in full"
    reused = _record(change_status=ChangeStatus.UNCHANGED)
    assert action(reused).startswith("Reused: unchanged")
    assert action(_record(change_status=ChangeStatus.CHANGED)) == (
        "Re-scraped: changed since the last capture"
    )
    row = page_rows([PRIOR])[0]
    assert row["Last checked"] == T0.date().isoformat()
    assert row["Site last updated"] == T0.date().isoformat()
    assert row["Action"] == "New: scraped in full"
