"""Password-protected PDFs and running headers/footers (page furniture) in PDFs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf

from src.integrations.evidence_store import LocalEvidenceStore
from src.modules.content_processor.adapters.pdf import (
    PASSWORD_PROTECTED,
    _running_key,
    extract_pdf,
)
from src.modules.content_processor.contracts import Resource, Task, UrlRecord
from src.modules.content_processor.enums import (
    EntityStatus,
    GapType,
    PriorityClass,
    TaskKind,
)
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.workers.doc_worker import DocWorker

RUN = "run-pdf"
RES = "res-pdf"
_FITZ: Any = pymupdf


def _body(page_no: int) -> str:
    return f"Section {page_no} explains how claims are paid within thirty days of approval."


def _report(pages: int, *, header: str = "Acme Annual Report 2025") -> bytes:
    """Pages with a running header, a body paragraph and a 'Page N of M' footer."""
    pdf = _FITZ.open()
    for n in range(1, pages + 1):
        page = pdf.new_page()  # A4-ish 595 x 842
        page.insert_text((72, 40), header, fontsize=9)
        page.insert_text((72, 300), _body(n), fontsize=11)
        page.insert_text((260, 820), f"Page {n} of {pages}", fontsize=9)
    out: bytes = pdf.tobytes()
    pdf.close()
    return out


def _encrypted(*, user_pw: str, owner_pw: str = "owner-secret") -> bytes:
    pdf = _FITZ.open()
    page = pdf.new_page()
    page.insert_text((72, 300), "Confidential pricing schedule for 2026.", fontsize=11)
    out: bytes = pdf.tobytes(
        encryption=_FITZ.PDF_ENCRYPT_AES_256,
        user_pw=user_pw,
        owner_pw=owner_pw,
        permissions=int(_FITZ.PDF_PERM_ACCESSIBILITY),
    )
    pdf.close()
    return out


def _extract(data: bytes) -> Any:
    return extract_pdf(data, resource_id=RES, run_id=RUN, evidence_id="ev-1")


# -- password-protected ---------------------------------------------------------------


def test_user_password_pdf_is_reported_as_password_protected() -> None:
    result = _extract(_encrypted(user_pw="open-sesame"))
    assert result.password_protected is True
    assert result.blocks == []
    assert result.image_regions == []  # nothing is sent to OCR
    assert result.warnings == [PASSWORD_PROTECTED]
    assert result.page_count == 1


def test_owner_password_only_pdf_stays_readable() -> None:
    result = _extract(_encrypted(user_pw=""))
    assert result.password_protected is False
    assert any("Confidential pricing schedule" in b.normalized_text for b in result.blocks)
    assert "pdf_encrypted_readable" in result.warnings


def test_doc_worker_blocks_a_locked_pdf_with_an_access_gap(tmp_path: Path) -> None:
    ledger = MemoryLedger(base_delay_s=0)
    store = LocalEvidenceStore(tmp_path, "t")
    worker = DocWorker(ledger=ledger, evidence=store, run_id=RUN, worker_id="w1", isolate=False)
    ref = store.put(_encrypted(user_pw="open-sesame"), "application/pdf")
    ledger.upsert_resource(
        Resource(
            resource_id=RES,
            run_id=RUN,
            requested_url="http://h/locked.pdf",
            schedule_key="http://h/locked.pdf",
            sniffed_mime="application/pdf",
            status=EntityStatus.IN_PROGRESS,
        ),
        "h",
    )
    ledger.enqueue(
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
    task = ledger.lease("w1", [TaskKind.EXTRACT_DOC])
    assert task is not None
    worker.process(task)
    resource = ledger.get_resource(RES)
    assert resource is not None and resource.status is EntityStatus.BLOCKED
    gaps = ledger.get_gaps(RES)
    assert [g.gap_type for g in gaps] == [GapType.ACCESS_BLOCKED]
    assert gaps[0].reason.startswith(PASSWORD_PROTECTED)


def test_report_says_the_file_is_password_protected() -> None:
    from src.modules.content_processor.contracts import Gap
    from src.modules.content_processor.enums import UrlBadge
    from src.modules.content_processor.export.project_report import completeness

    gap = Gap(
        gap_id="gap-1",
        run_id=RUN,
        resource_id=RES,
        gap_type=GapType.ACCESS_BLOCKED,
        status=EntityStatus.BLOCKED,
        reason=f"{PASSWORD_PROTECTED};pages=3",
    )
    record = UrlRecord.model_construct(badge=UrlBadge.BLOCKED, gaps=[gap])
    assert completeness(record, "", None) == "Not captured: the file is password-protected"


# -- running headers and footers ------------------------------------------------------


def test_page_numbers_are_made_alike_in_the_key() -> None:
    assert _running_key("Page 3 of 40") == _running_key("Page 17 of 40")
    assert _running_key("- iv -") == _running_key("- vii -")
    assert _running_key("Acme Report") != _running_key("Other Report")


def test_running_header_and_footer_are_furniture_not_content() -> None:
    result = _extract(_report(4))
    by_role: dict[str, list[str]] = {}
    for b in result.blocks:
        by_role.setdefault(b.role, []).append(b.normalized_text)
    assert by_role["chrome"] == ["Acme Annual Report 2025"] * 4
    assert sorted(by_role["footer"]) == [f"Page {n} of 4" for n in range(1, 5)]
    assert all(_body(n) in by_role["main"] for n in range(1, 5))
    assert result.running_text_blocks == 8
    assert "running_headers_footers:blocks=8" in result.warnings


def test_running_header_never_becomes_a_heading_path() -> None:
    # A large bold header would otherwise be taken as the heading of every page's body.
    pdf = _FITZ.open()
    for n in range(1, 4):
        page = pdf.new_page()
        page.insert_text((72, 40), "ACME", fontsize=20, fontname="hebo")
        page.insert_text((72, 300), _body(n), fontsize=11)
    result = _extract(pdf.tobytes())
    pdf.close()
    bodies = [b for b in result.blocks if b.role == "main"]
    assert bodies and all("ACME" not in b.heading_path for b in bodies)


def test_short_documents_keep_their_top_and_bottom_lines() -> None:
    result = _extract(_report(2))
    assert result.running_text_blocks == 0
    assert {b.role for b in result.blocks if b.layer.value != "metadata"} <= {"main", "legal"}


def test_a_line_on_a_minority_of_pages_is_not_furniture() -> None:
    pdf = _FITZ.open()
    for n in range(1, 7):
        page = pdf.new_page()
        if n <= 2:
            page.insert_text((72, 40), "Draft for review", fontsize=9)
        page.insert_text((72, 300), _body(n), fontsize=11)
    result = _extract(pdf.tobytes())
    pdf.close()
    assert result.running_text_blocks == 0


def test_furniture_is_left_out_of_chunks() -> None:
    from src.modules.content_processor.knowledge.chunking import _is_main

    result = _extract(_report(4))
    kept = [b.normalized_text for b in result.blocks if _is_main(b)]
    assert all("Page" not in t and "Annual Report" not in t for t in kept)
    assert any("claims are paid" in t for t in kept)
