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


def _gap(rid: str, gap_type: GapType, reason: str):
    return gap(rid, gap_type, reason)


def test_pages_say_completeness_confidence_and_confirmed_share():
    full = record("https://x.example/full", ["One two three four.", "Five six."])
    rid = "res-partial"
    partial = record(
        "https://x.example/partial",
        badge=UrlBadge.PARTIAL,
        gaps=[
            _gap(rid, GapType.UNVISITED_STATE, "unvisited_tab:policy_disallows:hover_info:Pricing"),
            _gap(rid, GapType.DISCOVERY_OPEN, "discovery_open:unvisited_controls=1"),
        ],
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
        "Extraction completeness": "Complete",
        "Confidence": "High",
        "Confirmed text %": 100.0,
        "Reading confidence %": 100.0,
        "Words": 6,
        "Status": "Fully captured",
        "What is missing": "",
    }

    p = rows["https://x.example/partial"]
    assert p["Fully extracted"] == "No"
    assert p["Extraction completeness"] == "Incomplete: 1 tab not opened (Pricing)"
    assert p["Confidence"] == "Low"
    assert p["Confirmed text %"] == 80.0  # 8 of 10 words confirmed
    assert p["Reading confidence %"] == 90.0  # (8 x 1.0 + 2 x 0.5) / 10

    img = rows["https://x.example/logo.png"]
    assert img["Confirmed text %"] is None and img["Confidence"] == "Low"


def test_volatile_placeholder_skipped_and_counted_controls():
    rid = "res-fund"
    volatile = record(
        "https://x.example/fund",
        ["The fund pays monthly."],
        badge=UrlBadge.NEEDS_REVIEW,
        gaps=[_gap(rid, GapType.VOLATILE, "settle_window_exhausted:polls=18")],
    )
    holder = record(
        "https://x.example/errors",
        ["Lorem ipsum dolor sit amet."],
        badge=UrlBadge.NEEDS_REVIEW,
        gaps=[_gap("res-errors", GapType.DEGRADED_VARIANT, "placeholder_content_suspected")],
    )
    skipped = record("https://x.example/skip", [], badge=UrlBadge.EXCLUDED)
    rid2 = "res-faq"
    faq = record(
        "https://x.example/faq",
        ["Questions."],
        badge=UrlBadge.PARTIAL,
        gaps=[
            _gap(rid2, GapType.UNVISITED_STATE, "unvisited_accordion:time_budget_exhausted:Region"),
            _gap(rid2, GapType.UNVISITED_STATE, "unvisited_accordion:click_failed:Sign in"),
            _gap(rid2, GapType.UNVISITED_STATE, "unvisited_dialog:time_budget_exhausted:Insights"),
            _gap(rid2, GapType.VOLATILE, "settle_window_exhausted:polls=9"),
        ],
    )
    rows = _by_url(page_rows([volatile, holder, skipped, faq]))
    v = rows["https://x.example/fund"]
    assert (v["Extraction completeness"], v["Confidence"], v["Confirmed text %"]) == (
        "Complete, but the page kept changing",
        "Medium",
        100.0,
    )
    h = rows["https://x.example/errors"]
    assert (h["Extraction completeness"], h["Confidence"]) == (
        "Placeholder page (Lorem ipsum)",
        "Low",
    )
    k = rows["https://x.example/skip"]
    assert (k["Extraction completeness"], k["Confidence"]) == ("Skipped by rule", "Not rated")
    assert rows["https://x.example/faq"]["Extraction completeness"] == (
        "Incomplete: 2 sections not opened (Region, Sign in); 1 pop-up not opened (Insights); "
        "the page also kept changing"
    )


def test_completeness_and_confidence_never_contradict_the_status():
    """Only a fully captured page can read 'Complete' or rate High."""
    for badge in UrlBadge:
        for gap_type in [None, *GapType]:
            rid = f"res-{badge.value}"
            gaps = [_gap(rid, gap_type, "x")] if gap_type is not None else []
            row = page_rows(
                [record(f"https://x.example/{badge.value}", ["Text here."], badge=badge, gaps=gaps)]
            )[0]
            if badge is not UrlBadge.VERIFIED_WITHIN_SCOPE:
                assert row["Extraction completeness"] != "Complete", (badge, gap_type)
                assert row["Confidence"] != "High", (badge, gap_type)
            if badge in (UrlBadge.PARTIAL, UrlBadge.BLOCKED, UrlBadge.FAILED):
                assert row["Confidence"] == "Low", (badge, gap_type)


def test_content_column_wraps_and_has_a_width(tmp_path: Path):
    recs = [record("https://x.example/one", ["Heading", "Paragraph under it."])]
    manifest = write_run_reports(recs, tmp_path / "demo_reports")
    path = report_path(tmp_path / "demo_reports", "project_report.xlsx")
    assert path.name in manifest["files"]
    with zipfile.ZipFile(path) as z:
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
        styles = z.read("xl/styles.xml").decode()
    assert 'wrapText="1"' in styles
    assert '<c r="B2" t="inlineStr" s="2">' in sheet  # Content cell wraps
    assert '<c r="A2" t="inlineStr">' in sheet  # URL cell does not
    assert '<col min="2" max="2" width="100" customWidth="1"/>' in sheet
    assert "Heading\nParagraph under it." in sheet


def test_each_pair_is_listed_once_with_both_directions():
    short_text = words(1, 100)
    short = record("https://x.example/short", [short_text])
    long = record("https://x.example/long", [short_text, words(2, 300)])
    other = record("https://x.example/other", [words(3, 120)])
    rows = match_rows([short, long, other])
    pairs = [(r["URL"], r["Matching URL"]) for r in rows]
    # one row for the pair, under the URL that sorts first ("long" < "short")
    assert pairs.count(("https://x.example/long", "https://x.example/short")) == 1
    assert ("https://x.example/short", "https://x.example/long") not in pairs
    row = next(r for r in rows if r["Matching URL"] == "https://x.example/short")
    assert 24.0 <= row["URL's text found on Matching URL %"] <= 26.0  # long in short
    assert row["Matching URL's text found on URL %"] == 100.0  # short in long
    assert row["Example of the shared text"]
    # a page in no pair still gets one row; a page that is in a pair gets none of its own
    assert ("https://x.example/other", NO_MATCH) in pairs
    assert not any(u == "https://x.example/short" for u, _ in pairs)


def test_three_pages_with_the_same_text_make_three_rows_not_six():
    shared = words(4, 120)
    recs = [record(f"https://x.example/{n}", [shared]) for n in ("a", "b", "c")]
    pairs = [
        (r["URL"].rsplit("/", 1)[1], r["Matching URL"].rsplit("/", 1)[1]) for r in match_rows(recs)
    ]
    assert sorted(pairs) == [("a", "b"), ("a", "c"), ("b", "c")]


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
    # they share only a few words: under 10% both ways, so no pair; each says so
    assert {(r["URL"], r["Matching URL"]) for r in rows} == {
        ("https://x.example/a", NO_MATCH),
        ("https://x.example/b", NO_MATCH),
    }


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
