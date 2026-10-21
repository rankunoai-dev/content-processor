"""Project report: pages with extraction and confidence, and directional content matches."""

from __future__ import annotations

import zipfile
from pathlib import Path

from src.modules.content_processor.enums import EntityStatus, GapType, UrlBadge
from src.modules.content_processor.export.bundle import write_run_reports
from src.modules.content_processor.export.project_report import (
    NO_MATCH,
    match_rows,
    page_rows,
    project_report_tables,
)
from tests.modules.content_processor.l5_factory import block, gap, record, report_path, words


def _by_url(rows):
    return {r["URL"]: r for r in rows}


def test_pages_say_fully_extracted_extracted_share_and_confidence():
    full = record("https://x.example/full", ["One two three four.", "Five six."])
    rid = "res-partial"
    partial = record(
        "https://x.example/partial",
        badge=UrlBadge.PARTIAL,
        gaps=[gap(rid, GapType.UNVISITED_STATE)],
        blocks=[
            block(rid, 0, "a b c d e f"),  # 6 words, confirmed
            block(rid, 1, "g h").model_copy(  # 2 words, not confirmed
                update={"status": EntityStatus.EXTRACTED_UNVERIFIED}
            ),
            block(rid, 2, "i j").model_copy(update={"confidence": 0.5}),  # OCR-like
        ],
    )
    image = record("https://x.example/logo.png", [], badge=UrlBadge.PARTIAL)
    rows = _by_url(page_rows([full, partial, image]))

    assert rows["https://x.example/full"] | {"Content": None} == {
        "URL": "https://x.example/full",
        "Content": None,
        "Fully extracted": "Yes",
        "Extracted %": 100.0,
        "Confidence %": 100.0,
        "Words": 6,
        "Status": "Fully captured",
        "What is missing": "",
    }
    assert "One two three four." in str(rows["https://x.example/full"]["Content"])

    p = rows["https://x.example/partial"]
    assert p["Fully extracted"] == "No"
    assert p["Extracted %"] == 80.0  # 8 of 10 words confirmed
    assert p["Confidence %"] == 90.0  # (8 x 1.0 + 2 x 0.5) / 10
    assert p["What is missing"] == "a tab, menu or button was not opened"

    img = rows["https://x.example/logo.png"]
    assert img["Extracted %"] is None and img["Confidence %"] is None and img["Words"] == 0


def test_matches_are_directional_and_every_url_gets_a_row():
    short_text = words(1, 100)
    short = record("https://x.example/short", [short_text])
    long = record("https://x.example/long", [short_text, words(2, 300)])
    other = record("https://x.example/other", [words(3, 120)])
    rows = match_rows([short, long, other])
    got = {(r["URL"], r["Matching URL"]): r["Same content %"] for r in rows}

    assert got[("https://x.example/short", "https://x.example/long")] == 100.0
    assert 24.0 <= got[("https://x.example/long", "https://x.example/short")] <= 26.0
    assert got[("https://x.example/other", NO_MATCH)] is None
    shared = next(r for r in rows if r["URL"] == "https://x.example/short")
    assert shared["Example of the shared text"]


def test_text_shared_only_in_menus_and_footers_is_not_a_match():
    nav = words(9, 40)

    def page(url: str, seed: int):
        rid = "res-" + url.rsplit("/", 1)[-1]
        return record(
            url,
            blocks=[block(rid, 0, nav, role="nav"), block(rid, 1, words(seed, 80))],
        )

    rows = match_rows([page("https://x.example/a", 4), page("https://x.example/b", 5)])
    assert {r["Matching URL"] for r in rows} == {NO_MATCH}


def test_small_overlaps_below_the_minimum_are_not_listed():
    a = record("https://x.example/a", [words(6, 200)])
    b = record("https://x.example/b", [words(6, 200)[:60] + " " + words(7, 300)])
    rows = match_rows([a, b], min_match_pct=10.0)
    pct = {(r["URL"], r["Matching URL"]): r["Same content %"] for r in rows}
    assert ("https://x.example/a", NO_MATCH) in pct  # a shares under 10% with b


def test_workbook_is_written_with_three_sheets(tmp_path: Path):
    recs = [record("https://x.example/one", [words(1, 50)])]
    assert [t.name for t in project_report_tables(recs)] == [
        "Pages",
        "Matching content",
        "How to read",
    ]
    manifest = write_run_reports(recs, tmp_path / "demo_reports")
    path = report_path(tmp_path / "demo_reports", "project_report.xlsx")
    assert path.name == "demo_project_report.xlsx"
    assert path.name in manifest["files"]
    with zipfile.ZipFile(path) as z:
        workbook = z.read("xl/workbook.xml").decode()
    for sheet in ("Pages", "Matching content", "How to read"):
        assert f'name="{sheet}"' in workbook
