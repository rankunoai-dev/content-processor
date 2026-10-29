"""CSV / XLSX writer: 32,767-character splitting, XML safety, formula guard."""

from __future__ import annotations

import csv
import json
import re
import zipfile
from pathlib import Path

import pytest

from src.modules.content_processor.export.tabular import (
    XLSX_CELL_LIMIT,
    XLSX_MAX_ROWS,
    Table,
    expand_split_column,
    split_cell,
    utf16_len,
    write_csv,
    write_jsonl,
    write_xlsx,
    xml_safe,
)


def _openpyxl():
    """XLSX read-back needs openpyxl (dev only); the writer itself does not."""
    return pytest.importorskip("openpyxl")


def test_split_counts_utf16_units_and_never_cuts_a_surrogate_pair():
    text = "a" * 5 + "\U0001f600" + "b" * 3
    assert utf16_len(text) == 10
    parts = split_cell(text, limit=6)
    assert parts == ["aaaaa", "\U0001f600bbb"]
    assert "".join(parts) == text
    assert all(utf16_len(p) <= 6 for p in parts)
    assert split_cell("") == [""]
    with pytest.raises(ValueError, match="at least 2"):
        split_cell("x", limit=1)


def test_split_at_the_real_limit_restores_exactly():
    text = "x" * (XLSX_CELL_LIMIT * 2 + 5)
    parts = split_cell(text)
    assert [len(p) for p in parts] == [XLSX_CELL_LIMIT, XLSX_CELL_LIMIT, 5]
    assert "".join(parts) == text


def test_expand_split_column_adds_parts_count_and_flag():
    rows = [{"id": 1, "body": "abcdefgh", "tail": "t"}, {"id": 2, "body": "ab", "tail": "u"}]
    columns = expand_split_column(rows, ["id", "body", "tail"], "body", limit=3)
    assert columns == [
        "id",
        "body",
        "body_part_2",
        "body_part_3",
        "tail",
        "body_parts",
        "body_split",
    ]
    assert rows[0] == {
        "id": 1,
        "body": "abc",
        "body_part_2": "def",
        "body_part_3": "gh",
        "tail": "t",
        "body_parts": 3,
        "body_split": True,
    }
    assert rows[1]["body_parts"] == 1 and rows[1]["body_split"] is False
    assert "body_part_2" not in rows[1]


def test_xml_safe_replaces_only_illegal_characters():
    assert xml_safe("tab\tnew\nline\r ok é") == ("tab\tnew\nline\r ok é", 0)
    assert xml_safe("a\x00b\x0bc￾") == ("a�b�c�", 3)


def test_xlsx_round_trips_types_and_text(tmp_path: Path):
    long_text = "y" * 40_000 + "\U0001f600"
    rows = [
        {"url": "=HYPERLINK(1)", "body": long_text, "n": 3, "f": 1.5, "ok": True, "none": None},
        {"url": "u2", "body": "ctl\x01", "n": 0, "f": float("nan"), "ok": False, "none": None},
    ]
    columns = expand_split_column(rows, ["url", "body", "n", "f", "ok", "none"], "body")
    path = tmp_path / "t.xlsx"
    replaced = write_xlsx(
        path, [Table("R1 [content]: urls?", columns, rows), Table("x" * 40, ["a"])]
    )
    assert replaced == 1
    wb = _openpyxl().load_workbook(path)
    assert wb.sheetnames == ["R1 _content__ urls_", "x" * 31]
    ws = wb.worksheets[0]
    assert ws.freeze_panes == "A2"
    values = [[c.value for c in row] for row in ws.iter_rows()]
    header, first, second = values
    assert header == columns
    got = dict(zip(header, first, strict=True))
    assert got["url"] == "=HYPERLINK(1)"  # a string, never a formula
    assert ws["A2"].data_type == "s"
    assert got["body"] + got["body_part_2"] == long_text
    assert got["n"] == 3 and got["f"] == 1.5 and got["ok"] is True and got["none"] is None
    other = dict(zip(header, second, strict=True))
    assert other["body"] == "ctl�" and other["f"] == "nan"
    assert ws["A1"].font.bold


def test_xlsx_refuses_cells_over_the_limit_and_empty_workbooks(tmp_path: Path):
    with pytest.raises(ValueError, match="split it first"):
        write_xlsx(tmp_path / "a.xlsx", [Table("t", ["c"], [{"c": "z" * (XLSX_CELL_LIMIT + 1)}])])
    with pytest.raises(ValueError, match="at least one"):
        write_xlsx(tmp_path / "b.xlsx", [])


def test_xlsx_is_a_valid_package_with_unique_sheet_names(tmp_path: Path):
    path = tmp_path / "s.xlsx"
    write_xlsx(path, [Table("Same", ["a"]), Table("same", ["a"]), Table("'", ["a"])])
    with zipfile.ZipFile(path) as archive:
        assert "[Content_Types].xml" in archive.namelist()
        assert len([n for n in archive.namelist() if n.startswith("xl/worksheets/")]) == 3
    assert _openpyxl().load_workbook(path).sheetnames == ["Same", "same (2)", "Sheet"]


def test_csv_has_bom_guards_formulas_and_keeps_text(tmp_path: Path):
    table = Table(
        "t",
        ["a", "b", "c", "d"],
        [{"a": "=1+1", "b": "-note", "c": True, "d": None}, {"a": "plain\nline", "b": 2.5}],
    )
    path = tmp_path / "t.csv"
    assert write_csv(path, table) == 2
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(path.open(encoding="utf-8-sig", newline="")))
    assert rows == [
        ["a", "b", "c", "d"],
        ["'=1+1", "'-note", "true", ""],
        ["plain\nline", "2.5", "", ""],
    ]
    write_csv(path, table, formula_guard=False)
    assert list(csv.reader(path.open(encoding="utf-8-sig", newline="")))[1][0] == "=1+1"


def test_jsonl_is_verbatim(tmp_path: Path):
    path = tmp_path / "t.jsonl"
    assert write_jsonl(path, [{"a": "=1\x01é"}, {"b": 1}]) == 2
    lines = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"a": "=1\x01é"}


def test_a_table_longer_than_a_sheet_continues_on_numbered_sheets(tmp_path: Path) -> None:
    assert XLSX_MAX_ROWS == 1_048_576
    rows = [{"n": i} for i in range(1, 8)]
    path = tmp_path / "long.xlsx"
    write_xlsx(path, [Table("Lines", ["n"], rows), Table("After", ["x"])], max_rows=4)
    with zipfile.ZipFile(path) as z:
        workbook = z.read("xl/workbook.xml").decode()
        sheets = [z.read(f"xl/worksheets/sheet{i}.xml").decode() for i in (1, 2, 3, 4)]
    names = ['name="Lines"', 'name="Lines (2)"', 'name="Lines (3)"', 'name="After"']
    assert all(n in workbook for n in names)
    # each part has the header and at most 3 rows; together they hold all 7, in order
    counts = [s.count("<row ") for s in sheets[:3]]
    assert counts == [4, 4, 2]
    assert all(">n<" in s for s in sheets[:3])
    found = [int(v) for s in sheets[:3] for v in re.findall(r"<v>(\d+)</v>", s)]
    assert found == list(range(1, 8))
