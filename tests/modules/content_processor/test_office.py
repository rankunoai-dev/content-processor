"""Office (C-9): DOCX, XLSX and PPTX extraction, safety limits, and worker routing."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from src.integrations.evidence_store import LocalEvidenceStore
from src.modules.content_processor.adapters import office
from src.modules.content_processor.adapters.office import (
    OfficeExtraction,
    OfficeLimitError,
    OfficeParseError,
    detect_office_kind,
    extract_office,
)
from src.modules.content_processor.contracts import Resource, Task
from src.modules.content_processor.enums import (
    ContentLayer,
    EntityStatus,
    GapType,
    PriorityClass,
    TaskKind,
)
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.workers.doc_worker import DocWorker
from tests.modules.content_processor.office_builders import (
    docx_full,
    pptx_full,
    xlsx_full,
    zip_bomb_like,
)

RUN = "run-office"
RES = "res-office"


def _extract(data: bytes) -> OfficeExtraction:
    return extract_office(data, resource_id=RES, run_id=RUN, evidence_id="evidence:e")


def _by_method(result: OfficeExtraction, method: str) -> list[str]:
    return [b.raw_text for b in result.blocks if b.method == method]


# -- detection and limits ------------------------------------------------------


def test_detects_each_ooxml_flavour() -> None:
    assert detect_office_kind(docx_full()) == "docx"
    assert detect_office_kind(xlsx_full()) == "xlsx"
    assert detect_office_kind(pptx_full()) == "pptx"


def test_plain_zip_is_not_office() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hello")
    assert detect_office_kind(buf.getvalue()) is None


def test_zip_bomb_is_refused_before_parsing() -> None:
    with pytest.raises(OfficeLimitError, match="zip_ratio"):
        _extract(zip_bomb_like())


def test_too_many_members_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(office, "MAX_MEMBERS", 2)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<w:document/>")
        zf.writestr("a", "1")
        zf.writestr("b", "2")
    with pytest.raises(OfficeLimitError, match="zip_members"):
        office.check_zip_limits(buf.getvalue())


def test_truncated_ooxml_is_a_parse_error() -> None:
    with pytest.raises(OfficeParseError):
        _extract(docx_full()[:200])


def test_non_ooxml_bytes_are_rejected() -> None:
    with pytest.raises(OfficeParseError, match="not_ooxml"):
        _extract(b"plain text, not office")


# -- DOCX ----------------------------------------------------------------------


def test_docx_body_tables_and_heading_path() -> None:
    result = _extract(docx_full())
    assert result.kind == "docx"
    assert "Revenue grew 12% in Q3." in _by_method(result, "docx/paragraph")
    assert _by_method(result, "docx/table") == ["| Region | Sales |\n| --- | --- |\n| North | 40 |"]
    revenue = next(b for b in result.blocks if b.raw_text == "Revenue grew 12% in Q3.")
    assert revenue.heading_path == ["Quarterly review"]


def test_docx_hidden_and_tracked_deleted_text_stay_out_of_body() -> None:
    result = _extract(docx_full())
    body = " ".join(b.raw_text for b in result.blocks if b.layer is ContentLayer.RENDERED_VISIBLE)
    assert "secret internal note" not in body
    assert "10 dollars" not in body
    assert "Price is 12 dollars" in body  # the insertion is visible text
    hidden = {b.method: b.raw_text for b in result.blocks if b.layer is ContentLayer.HIDDEN}
    assert hidden["docx/hidden_text"] == "secret internal note"
    assert hidden["docx/tracked_deletion"] == "10 dollars"


def test_docx_header_is_chrome_and_comments_are_collapsed_layer() -> None:
    result = _extract(docx_full())
    header = next(b for b in result.blocks if b.method == "docx/header")
    assert header.role == "chrome"
    comment = next(b for b in result.blocks if b.method == "docx/comments")
    assert comment.layer is ContentLayer.COLLAPSED_PUBLISHED
    assert "check with legal" in comment.raw_text


# -- XLSX ----------------------------------------------------------------------


def test_xlsx_sheet_rendered_as_table_with_title() -> None:
    result = _extract(xlsx_full())
    assert result.unit_count == 2
    costs = [b for b in result.blocks if b.page_label == "Costs" and b.method == "xlsx/table"]
    assert costs and "| hosting | 120 |" in costs[0].raw_text


def test_xlsx_hidden_sheet_is_hidden_layer() -> None:
    result = _extract(xlsx_full())
    scratch = [b for b in result.blocks if b.page_label == "Scratch"]
    assert scratch and all(b.layer is ContentLayer.HIDDEN for b in scratch)


def test_xlsx_formula_without_cached_value_is_reported() -> None:
    result = _extract(xlsx_full())
    assert "formula_without_cached_value:1" in result.warnings


def test_xlsx_row_cap_marks_sheet_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(office, "MAX_SHEET_ROWS", 2)
    result = _extract(xlsx_full())
    assert result.status is EntityStatus.PARTIAL
    assert any(w.startswith("sheet_truncated:Costs") for w in result.warnings)


# -- PPTX ----------------------------------------------------------------------


def test_pptx_slide_text_table_and_notes() -> None:
    result = _extract(pptx_full())
    assert result.unit_count == 3
    texts = {b.raw_text for b in result.blocks if b.page_label == "1" and b.method == "pptx/text"}
    assert {"Roadmap", "Launch in October"} <= texts
    notes = [b for b in result.blocks if b.method == "pptx/notes"]
    assert notes[0].layer is ContentLayer.SOURCE_ONLY
    assert "Mention the pilot customers." in notes[0].raw_text
    table = [b for b in result.blocks if b.method == "pptx/table"]
    assert table[0].page_label == "2"


def test_pptx_hidden_slide_is_hidden_layer() -> None:
    result = _extract(pptx_full())
    appendix = [b for b in result.blocks if b.page_label == "3"]
    assert appendix and all(b.layer is ContentLayer.HIDDEN for b in appendix)
    assert "hidden_slides_present" in result.warnings


# -- worker routing ------------------------------------------------------------


class _Env:
    def __init__(self, tmp_path: Path) -> None:
        self.ledger = MemoryLedger(base_delay_s=0)
        self.store = LocalEvidenceStore(tmp_path, "t")
        self.worker = DocWorker(
            ledger=self.ledger,
            evidence=self.store,
            run_id=RUN,
            worker_id="w1",
            isolate=False,
        )

    def submit(self, data: bytes, ext: str) -> Task:
        ref = self.store.put(data, "application/octet-stream")
        self.ledger.upsert_resource(
            Resource(
                resource_id=RES,
                run_id=RUN,
                requested_url=f"http://h/x.{ext}",
                schedule_key=f"http://h/x.{ext}",
                extension_hint=ext,
                status=EntityStatus.IN_PROGRESS,
            ),
            "h",
        )
        self.ledger.enqueue(
            [
                Task(
                    run_id=RUN,
                    kind=TaskKind.EXTRACT_DOC,
                    idempotency_key="k1",
                    host="h",
                    priority=PriorityClass.HTML_PAGE,
                    payload={"resource_id": RES, "sha256": ref.sha256},
                )
            ]
        )
        task = self.ledger.lease("w1", [TaskKind.EXTRACT_DOC])
        assert task is not None
        return task

    @property
    def resource(self) -> Resource:
        res = self.ledger.get_resource(RES)
        assert res is not None
        return res


def test_worker_extracts_docx_and_marks_unverified(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    env.worker.process(env.submit(docx_full(), "docx"))
    assert env.resource.status is EntityStatus.EXTRACTED_UNVERIFIED
    assert any(b.method == "docx/table" for b in env.ledger.get_blocks(RES))


def test_worker_partial_when_xlsx_sheet_is_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(office, "MAX_SHEET_ROWS", 2)
    env = _Env(tmp_path)
    env.worker.process(env.submit(xlsx_full(), "xlsx"))
    assert env.resource.status is EntityStatus.PARTIAL


def test_worker_zip_bomb_is_limit_gap_not_retry(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    env.worker.process(env.submit(zip_bomb_like(), "docx"))
    assert env.resource.status is EntityStatus.PARTIAL
    assert [g.gap_type for g in env.ledger.get_gaps(RES)] == [GapType.LIMIT_HIT]


def test_worker_corrupt_ooxml_fails_without_retry(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    env.worker.process(env.submit(docx_full()[:200], "docx"))
    assert env.resource.status is EntityStatus.FAILED


def test_worker_legacy_doc_is_unsupported_with_libreoffice_reason(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    legacy = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
    env.worker.process(env.submit(legacy, "doc"))
    assert env.resource.status is EntityStatus.UNSUPPORTED
    assert env.resource.status_reason == "legacy_office_requires_libreoffice:doc"
    assert [g.gap_type for g in env.ledger.get_gaps(RES)] == [GapType.UNSUPPORTED_FORMAT]
