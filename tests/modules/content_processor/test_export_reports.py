"""Report R1, the review queue, the report bundle and the `report` subcommand."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import pytest

from src.modules.content_processor.enums import ContentLayer, GapType, UrlBadge
from src.modules.content_processor.export.bundle import (
    bundle_file_name,
    report_file,
    run_file_prefix,
    write_run_reports,
)
from src.modules.content_processor.export.commands import (
    cmd_report,
    load_records,
    register,
    run_name_for,
)
from src.modules.content_processor.export.reports import (
    r1_rows,
    r1_table,
    review_rows,
    review_table,
    suggested_action,
)
from src.modules.content_processor.export.tabular import XLSX_CELL_LIMIT
from tests.modules.content_processor.l5_factory import block, gap, record, report_path, words

BASE = "https://example.com/"


def _records():
    return [
        record(
            BASE + "ok",
            blocks=[
                block("ok", 0, "Plans", heading=["Plans"]),
                block("ok", 1, "Annual plans renew yearly."),
                block("ok", 2, "Menu", role="nav"),
                block("ok", 3, "Collapsed answer.", layer=ContentLayer.COLLAPSED_PUBLISHED),
                block("ok", 4, "Hidden trap", layer=ContentLayer.HIDDEN),
            ],
            aliases=[BASE + "ok?utm_source=x"],
        ),
        record(
            BASE + "wall",
            [],
            badge=UrlBadge.BLOCKED,
            gaps=[gap("wall", GapType.ACCESS_BLOCKED, "lead_gen_wall")],
        ),
        record(
            BASE + "partial",
            ["Some text."],
            badge=UrlBadge.PARTIAL,
            gaps=[gap("partial", GapType.UNVISITED_STATE, "tab not opened")],
            warnings=["render_not_run"],
        ),
        record(BASE + "running", [], badge=UrlBadge.RUNNING),
        record(
            BASE + "failed", [], badge=UrlBadge.FAILED, gaps=[gap("failed", GapType.LOW_CONFIDENCE)]
        ),
    ]


def test_r1_has_one_row_per_requested_url_with_provenance():
    rows = r1_rows(_records())
    assert [r["url"] for r in rows] == [
        BASE + "ok",
        BASE + "wall",
        BASE + "partial",
        BASE + "running",
        BASE + "failed",
    ]
    ok = rows[0]
    assert ok["whole_content"] == "# Plans\nAnnual plans renew yearly.\nCollapsed answer.\nMenu"
    assert "Hidden trap" not in str(ok["whole_content"])
    assert ok["word_count"] == 9 and ok["block_count"] == 5
    assert ok["aliases"] == BASE + "ok?utm_source=x"
    assert ok["evidence_ids"] == "evidence:ok" and ok["run_id"] == "run-l5"
    assert str(ok["block_ids"]).startswith("ok:b0 ok:b1")
    assert ok["captured_at"] == "2026-10-16T09:00:00+00:00"
    wall = rows[1]
    assert wall["badge"] == "BLOCKED" and wall["gap_types"] == "ACCESS_BLOCKED"
    assert wall["evidence_ids"] == "evidence:gap-wall" and wall["whole_content"] == ""
    assert rows[2]["warnings"] == "render_not_run"
    no_boiler = r1_rows(_records()[:1], include_boilerplate=False)[0]
    assert "Menu" not in str(no_boiler["whole_content"])


def test_r1_table_splits_long_content_and_flags_it():
    long_text = words(1, 12_000)
    assert len(long_text) > XLSX_CELL_LIMIT
    rows = r1_rows([record(BASE + "long", [long_text]), record(BASE + "short", ["tiny"])])
    table = r1_table(rows)
    assert "whole_content_part_2" in table.columns
    long_row, short_row = table.rows
    assert long_row["whole_content_split"] is True and short_row["whole_content_split"] is False
    rebuilt = "".join(
        str(long_row[c])
        for c in table.columns
        if c == "whole_content" or c.startswith("whole_content_part_")
    )
    assert rebuilt == long_text
    assert rows[0]["whole_content"] == long_text  # the JSONL rows stay unsplit


def test_review_queue_keeps_decisions_most_severe_first():
    rows = review_rows(_records())
    assert [(r["url"], r["badge"]) for r in rows] == [
        (BASE + "failed", "FAILED"),
        (BASE + "wall", "BLOCKED"),
        (BASE + "partial", "PARTIAL"),
    ]
    wall = rows[1]
    assert wall["gaps"] == "ACCESS_BLOCKED: lead_gen_wall"
    assert wall["suggested_action"].startswith("obtain the asset manually")
    assert wall["operator_action"] == "" and wall["operator_note"] == ""
    assert rows[2]["suggested_action"] == "review the unexplored states"
    assert review_table(rows).columns[-2:] == ["gaps_parts", "gaps_split"]


def test_suggested_action_falls_back():
    assert suggested_action([]) == "review the record"


def test_bundle_writes_every_report_and_a_manifest(tmp_path: Path):
    shared = words(3, 200)
    records = [*_records(), record(BASE + "dup1", [shared]), record(BASE + "dup2", [shared])]
    manifest = write_run_reports(records, tmp_path / "out")
    out = tmp_path / "out"
    assert (
        sorted(p.name for p in out.iterdir())
        == manifest["files"]
        == [
            "run-l5_explorer.html",
            "run-l5_manifest.json",
            "run-l5_r1_url_content.csv",
            "run-l5_r1_url_content.jsonl",
            "run-l5_r1_url_content.xlsx",
            "run-l5_r2_overlap.xlsx",
            "run-l5_r2a_overlap_groups.csv",
            "run-l5_r2b_overlap_pairs.csv",
            "run-l5_r4_contradictions.csv",
            "run-l5_r4_contradictions.jsonl",
            "run-l5_r4_contradictions.xlsx",
            "run-l5_review_queue.csv",
            "run-l5_review_queue.jsonl",
            "run-l5_review_queue.xlsx",
        ]
    )
    assert manifest["requested_urls"] == 7 and manifest["r1_rows"] == 7
    assert manifest["review_rows"] == 3 and manifest["run_id"] == "run-l5"
    assert manifest["run_name"] == "run-l5", "no _reports folder: the run id names the files"
    assert manifest["badges"]["VERIFIED_WITHIN_SCOPE"] == 3
    assert manifest["overlap"]["pairs"] == 1 and manifest["overlap"]["groups"] == 1
    assert json.loads(report_path(out, "manifest.json").read_text(encoding="utf-8")) == manifest
    jsonl = [
        json.loads(x)
        for x in report_path(out, "r1_url_content.jsonl").read_text("utf-8").splitlines()
    ]
    assert len(jsonl) == 7
    pairs = list(
        csv.DictReader(report_path(out, "r2b_overlap_pairs.csv").open(encoding="utf-8-sig"))
    )
    assert [(p["url_a"], p["url_b"], p["jaccard_pct"]) for p in pairs] == [
        (BASE + "dup1", BASE + "dup2", "100.0")
    ]
    html = report_path(out, "explorer.html").read_text(encoding="utf-8")
    assert 'href="run-l5_r1_url_content.xlsx"' in html


def test_bundle_on_an_empty_run(tmp_path: Path):
    manifest = write_run_reports([], tmp_path / "empty", run_id="run-empty")
    assert manifest["requested_urls"] == 0 and manifest["run_id"] == "run-empty"


def test_report_subcommand_round_trips_records(tmp_path: Path, capsys):
    path = tmp_path / "records.jsonl"
    path.write_text("\n".join(r.model_dump_json() for r in _records()) + "\n\n", encoding="utf-8")
    assert len(load_records(path)) == 5
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        ["report", "--records", str(path), "--out", str(tmp_path / "rep"), "--threshold", "70"]
    )
    assert args.func(args) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["overlap"]["threshold"] == 0.7 and printed["requested_urls"] == 5
    assert cmd_report is args.func


def test_load_records_names_the_bad_line(tmp_path: Path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"run_id": "x"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"bad.jsonl:1: not a UrlRecord"):
        load_records(path)


def test_every_file_carries_the_run_name(tmp_path: Path):
    named = write_run_reports(_records(), tmp_path / "jh2_reports")
    assert named["run_name"] == "jh2"
    assert all(name.startswith("jh2_") for name in named["files"])
    assert sorted(p.name for p in (tmp_path / "jh2_reports").iterdir()) == named["files"]
    explicit = write_run_reports(_records(), tmp_path / "shared", run_name="Run 7/Oct")
    assert explicit["run_name"] == "Run_7_Oct"
    assert (tmp_path / "shared" / "Run_7_Oct_r1_url_content.xlsx").is_file()
    # Two runs written into one folder no longer overwrite each other.
    write_run_reports(_records(), tmp_path / "shared", run_name="other")
    assert (tmp_path / "shared" / "Run_7_Oct_manifest.json").is_file()
    assert (tmp_path / "shared" / "other_manifest.json").is_file()
    html = (tmp_path / "jh2_reports" / "jh2_explorer.html").read_text(encoding="utf-8")
    assert 'href="jh2_r1_url_content.xlsx"' in html


def test_naming_helpers(tmp_path: Path):
    assert run_file_prefix("jh2") == "jh2" and run_file_prefix(" ../x y ") == "x_y"
    assert run_file_prefix("///") == "run"
    assert bundle_file_name("jh2", "manifest.json") == "jh2_manifest.json"
    assert report_file(tmp_path, "jh2", "manifest.json") is None
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    assert report_file(tmp_path, "jh2", "manifest.json") == tmp_path / "manifest.json"
    (tmp_path / "jh2_manifest.json").write_text("{}", encoding="utf-8")
    assert report_file(tmp_path, "jh2", "manifest.json") == tmp_path / "jh2_manifest.json"
    assert run_name_for(Path("exports/jh2.records.jsonl")) == "jh2"
    assert run_name_for(Path("a.jsonl")) == "a" and run_name_for(Path("plain.txt")) == "plain"
