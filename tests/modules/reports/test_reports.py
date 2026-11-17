"""Downloads: every report in Excel, CSV, Word, PDF and JSON, one by one or all in a zip."""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfReader

from src.modules.content_processor.export.tabular import Table
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.webapi.app import create_app
from src.modules.gaps.service import build_project_gaps
from src.modules.monitoring.service import run_project_check
from src.modules.reports import writers
from src.modules.reports.catalog import REPORTS, build_report
from src.modules.reports.writers import Report, markdown_to_docx, write_report
from src.modules.research.service import pull_project
from src.modules.theming.service import build_project_themes
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.research.test_research import ON, semrush
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture

WRITE = {"X-Requested-With": "content-processor"}


def _doc_text(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    cells = [c.text for t in doc.tables for r in t.rows for c in r.cells]
    return "\n".join([p.text for p in doc.paragraphs] + cells)


def test_writers_keep_every_row_in_excel_and_csv_and_say_what_word_shortens(
    tmp_path: Path,
) -> None:
    long = "x" * (writers.WORD_CELL + 50)
    rows = [{"Name": f"row {i}", "Text": long if i == 0 else "short"} for i in range(400)]
    wide = Table("Wide", [f"C{i}" for i in range(8)], [{f"C{i}": i for i in range(8)}])
    report = Report(
        "demo",
        "Demo report",
        "Built today",
        ["One line."],
        [Table("Rows", ["Name", "Text"], rows), wide],
    )

    xlsx = write_report(report, "xlsx", tmp_path, "demo")
    with zipfile.ZipFile(xlsx) as book:
        assert "Rows" in book.read("xl/workbook.xml").decode()
        assert "row 399" in book.read("xl/worksheets/sheet1.xml").decode()
    csvs = write_report(report, "csv", tmp_path, "demo")
    assert csvs.suffix == ".zip"
    with zipfile.ZipFile(csvs) as archive:
        assert archive.namelist() == ["01_Rows.csv", "02_Wide.csv"]
        assert archive.read("01_Rows.csv").decode("utf-8-sig").count("\n") == 401
    single = Report("one", "One", "now", tables=[Table("Only", ["A"], [{"A": "=1+1"}])])
    one = write_report(single, "csv", tmp_path, "one")
    assert one.suffix == ".csv" and "'=1+1" in one.read_text(encoding="utf-8-sig")

    docx = write_report(report, "docx", tmp_path, "demo")
    text = _doc_text(docx.read_bytes())
    assert "Demo report" in text and "One line." in text and "row 299" in text
    assert "row 300" not in text
    assert f"first {writers.WORD_ROWS:,} of 400 rows" in text and "1 long cell(s) shortened" in text
    ids = Report(
        "ids",
        "Ids",
        "now",
        tables=[Table("T", ["Theme", "Theme id"], [{"Theme": "Pet", "Theme id": "th-1"}])],
    )
    id_text = _doc_text(write_report(ids, "docx", tmp_path, "ids").read_bytes())
    assert "Pet" in id_text and "th-1" not in id_text and "Theme id" not in id_text
    section = Document(str(docx)).sections[0]
    assert section.page_width > section.page_height  # an 8-column table turns the page


def _pdf_text(path: Path) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def test_pdf_matches_word_limits_and_json_keeps_everything(tmp_path: Path) -> None:
    long = "y" * (writers.WORD_CELL + 50)
    rows = [{"Name": f"row {i}", "Text": long if i == 0 else "short €"} for i in range(400)]
    wide = Table("Wide", [f"C{i}" for i in range(8)], [{f"C{i}": i for i in range(8)}])
    report = Report(
        "demo",
        "Demo report",
        "Built today",
        ["One line."],
        [Table("Rows", ["Name", "Text", "Row id"], rows), wide, Table("Empty", ["A"], [])],
        data={"raw": [1, 2, 3]},
    )
    pdf = write_report(report, "pdf", tmp_path, "demo")
    assert pdf.suffix == ".pdf"
    reader = PdfReader(str(pdf))
    box = reader.pages[0].mediabox
    assert float(box.width) > float(box.height)  # the 8-column table turns the page
    text = _pdf_text(pdf)
    assert "Demo report" in text and "One line." in text and "row 299" in text
    assert "row 300" not in text and "Row id" not in text and "Nothing to list." in text
    assert f"first {writers.WORD_ROWS:,} of 400 rows" in text and "1 long cell(s)" in text
    assert "Page 2" in text

    body = json.loads(write_report(report, "json", tmp_path, "demo").read_text(encoding="utf-8"))
    assert body["report"] == "demo" and body["summary"] == ["One line."]
    assert body["data"] == {"raw": [1, 2, 3]} and body["generated_at"]
    first = body["tables"][0]
    assert first["columns"] == ["Name", "Text", "Row id"] and len(first["rows"]) == 400
    assert first["rows"][0]["Text"] == long and first["rows"][1]["Text"] == "short €"
    assert "data" not in json.loads(
        write_report(Report("x", "X", "now"), "json", tmp_path, "x").read_text(encoding="utf-8")
    )


def test_pdf_without_a_unicode_font_still_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(writers, "_PDF_FONTS", (("/nowhere", "a.ttf", "b.ttf"),))
    report = Report("u", "Ünïcode ✓ 日本", "now", tables=[Table("T", ["A"], [{"A": "✓ ok"}])])
    text = _pdf_text(write_report(report, "pdf", tmp_path, "u"))
    assert "Ünïcode" in text and "? ok" in text


def test_markdown_becomes_a_word_document(tmp_path: Path) -> None:
    md = (
        "# Executive briefing\n\n> **In short:** the client trails on pet insurance [1].\n\n"
        "## Findings\n- **Pet** cover missing\n1. Write a pillar\n\n"
        "| Theme | Pages |\n|---|---|\n| Pet | 3 |\n\n"
        "Plain text with a [link](https://example.com/pet) and `code`.\n---\n"
    )
    path = markdown_to_docx(md, tmp_path / "brief", "Briefing")
    doc = Document(str(path))
    styles = [p.style.name for p in doc.paragraphs]
    assert "Heading 1" in styles and "List Bullet" in styles and "List Number" in styles
    text = _doc_text(path.read_bytes())
    assert "link (https://example.com/pet)" in text and "Pet" in text and "3" in text
    assert "**" not in text and "|" not in text


def test_catalog_builds_from_each_module_and_says_what_is_missing(themed: World) -> None:
    pid = themed.project_id
    assert build_report("themes", themed.store, pid) is None
    overview = build_report("overview", themed.store, pid)
    assert overview is not None and {t.name for t in overview.tables} == {"Websites", "Scrapes"}
    assert {r["Website"] for r in overview.tables[0].rows} == {"Example Life", "Rival"}
    build_project_themes(themed.store, themed.access, pid)
    pull_project(themed.store, themed.access, pid, ON, http=semrush)
    run_project_check(themed.store, themed.access, pid)
    build_project_gaps(themed.store, themed.access, pid)
    for key in ("themes", "research", "competitors", "monitoring", "monitoring_history", "gaps"):
        report = build_report(key, themed.store, pid)
        assert report is not None and report.tables and report.subtitle, key
    competitors = build_report("competitors", themed.store, pid)
    assert competitors is not None
    assert {r["Website"] for r in competitors.tables[0].rows} >= {"bigsure.com", "rival.com"}
    monitoring = build_report("monitoring", themed.store, pid)
    assert monitoring is not None and monitoring.data["check_id"]
    names = [t.name for t in monitoring.tables]
    for sheet in ("Competitor moves", "Price changes", "Template changes"):
        assert sheet in names
    run_project_check(themed.store, themed.access, pid)
    history = build_report("monitoring_history", themed.store, pid)
    assert history is not None
    assert [t.name for t in history.tables] == ["Checks", "All alerts", "Recurring alerts"]
    assert len(history.tables[0].rows) == 2 and "2 check(s)" in history.summary[0]
    assert build_report("analyst", themed.store, pid) is None  # no runner given
    with pytest.raises(KeyError):
        build_report("nope", themed.store, pid)
    assert all(d.missing for d in REPORTS if d.key != "overview")


def test_api_downloads(themed: World, tmp_path: Path) -> None:
    signer = TokenSigner(b"k" * 32)
    exports = tmp_path / "exports"
    exports.mkdir()
    app = create_app(
        exports,
        projects=(themed.store, ScrapeRunner(themed.store), signer),
        knowledge=themed.access,
        analyst=themed.runner(enabled=False),
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    pid = themed.project_id
    base = f"/api/projects/{pid}/exports"
    listed = client.get(base).json()
    assert [f["key"] for f in listed["formats"]] == ["xlsx", "csv", "docx", "pdf", "json"]
    by = {r["key"]: r for r in listed["reports"]}
    assert set(by) == {d.key for d in REPORTS}
    assert by["overview"]["available"] and not by["themes"]["available"]
    assert by["themes"]["missing"].startswith("Build the theme map")
    missing = client.get(f"{base}/themes.xlsx")
    assert missing.status_code == 404 and "Build the theme map" in missing.json()["detail"]
    assert client.get(f"{base}/nope.xlsx").status_code == 404
    assert client.get(f"{base}/overview.txt").status_code == 404
    assert client.get("/api/projects/zzz/exports").status_code == 404

    build_project_themes(themed.store, themed.access, pid)
    excel = client.get(f"{base}/themes.xlsx")
    assert excel.status_code == 200 and excel.content[:2] == b"PK"
    assert "_themes_" in excel.headers["content-disposition"]
    word = client.get(f"{base}/themes.docx")
    assert word.status_code == 200 and "themes" in _doc_text(word.content).lower()
    pdf = client.get(f"{base}/themes.pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content[:5] == b"%PDF-"
    data = client.get(f"{base}/themes.json")
    assert data.status_code == 200 and data.headers["content-type"] == "application/json"
    assert data.json()["report"] == "themes" and data.json()["tables"]
    csv = client.get(f"{base}/overview.csv")
    assert csv.status_code == 200 and csv.headers["content-type"] == "application/zip"

    token, _ = signer.issue(pid)
    started = client.post(
        f"/api/projects/{pid}/analyst/runs",
        json={"card": "eeat-trust-audit"},
        headers={**WRITE, "X-Project-Token": token},
    ).json()
    run_base = f"/api/projects/{pid}/analyst/runs/{started['run_id']}"
    for _ in range(100):
        if client.get(run_base).json()["record"]["status"] == "finished":
            break
        time.sleep(0.05)
    md = client.get(f"{run_base}/result/result.md")
    assert md.status_code == 200 and b"# Context pack" in md.content
    card_word = client.get(f"{run_base}/result/result.docx")
    assert card_word.status_code == 200 and "Context pack" in _doc_text(card_word.content)
    assert client.get(f"{run_base}/result/result.pdf").status_code == 404

    everything = client.get(f"{base}/all.zip")
    assert everything.status_code == 200
    with zipfile.ZipFile(io.BytesIO(everything.content)) as archive:
        names = archive.namelist()
        readme = archive.read("README.txt").decode()
    assert sum(1 for n in names if "_themes_" in n) == 5  # xlsx, csv, docx, pdf, json
    assert sum(1 for n in names if "_analyst_" in n) == 5
    assert "Not included yet:" in readme and "Gaps: Build the gap analysis" in readme
