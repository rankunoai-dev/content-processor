"""Doc-worker tests: PDF and image extraction, OCR gaps, isolation, failure paths."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from PIL import Image

from src.integrations.evidence_store import LocalEvidenceStore
from src.modules.content_processor.adapters.ocr import OcrResult
from src.modules.content_processor.contracts import Resource, Task
from src.modules.content_processor.enums import (
    EntityStatus,
    GapType,
    PriorityClass,
    TaskKind,
)
from src.modules.content_processor.ledger.memory import MemoryLedger
from src.modules.content_processor.workers.doc_worker import DocWorker, run_isolated

RUN = "run-doc"
RES = "res-doc"


def _text_pdf(text: str = "Quarterly report body text") -> bytes:
    doc: Any = pymupdf
    pdf = doc.open()
    page = pdf.new_page()
    page.insert_text((72, 100), text, fontsize=12)
    out: bytes = pdf.tobytes()
    pdf.close()
    return out


def _png(size: tuple[int, int] = (64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format="PNG")
    return buf.getvalue()


def _scanned_pdf() -> bytes:
    """A page whose only content is a full-page raster image (no native text)."""
    doc: Any = pymupdf
    pdf = doc.open()
    page = pdf.new_page()
    page.insert_image(page.rect, stream=_png((400, 500)))
    out: bytes = pdf.tobytes()
    pdf.close()
    return out


def _fake_ocr(text: str) -> Any:
    def runner(_img: bytes, *, packs: Any = None, psm: int = 6) -> OcrResult:
        return OcrResult(text=text, confidence=0.9, packs_used=list(packs or ["eng"]))

    return runner


def _missing_ocr(_img: bytes, *, packs: Any = None, psm: int = 6) -> OcrResult:
    return OcrResult(text="", confidence=0.0, engine_available=False, error="ocr_engine_missing")


class Env:
    def __init__(self, tmp_path: Path, **worker_kwargs: Any) -> None:
        self.ledger = MemoryLedger(base_delay_s=0)
        self.store = LocalEvidenceStore(tmp_path, "t")
        worker_kwargs.setdefault("isolate", False)
        self.worker = DocWorker(
            ledger=self.ledger,
            evidence=self.store,
            run_id=RUN,
            worker_id="w1",
            **worker_kwargs,
        )

    def submit(self, data: bytes, mime: str, kind: TaskKind) -> Task:
        ref = self.store.put(data, mime)
        self.ledger.upsert_resource(
            Resource(
                resource_id=RES,
                run_id=RUN,
                requested_url="http://h/x",
                schedule_key="http://h/x",
                sniffed_mime=mime,
                status=EntityStatus.IN_PROGRESS,
            ),
            "h",
        )
        self.ledger.enqueue(
            [
                Task(
                    run_id=RUN,
                    kind=kind,
                    idempotency_key="k1",
                    host="h",
                    priority=PriorityClass.HTML_PAGE,
                    payload={"resource_id": RES, "sha256": ref.sha256},
                )
            ]
        )
        task = self.ledger.lease("w1", [kind])
        assert task is not None
        return task

    @property
    def resource(self) -> Resource:
        res = self.ledger.get_resource(RES)
        assert res is not None
        return res


def test_native_pdf_extracts_text_blocks(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.worker.process(env.submit(_text_pdf(), "application/pdf", TaskKind.EXTRACT_DOC))
    assert env.resource.status is EntityStatus.EXTRACTED_UNVERIFIED
    texts = " ".join(b.normalized_text for b in env.ledger.get_blocks(RES))
    assert "Quarterly report body text" in texts
    assert env.ledger.get_gaps(RES) == []


def test_scanned_pdf_is_ocred_per_region(tmp_path: Path) -> None:
    env = Env(tmp_path, ocr_runner=_fake_ocr("Scanned clause 4.2"))
    env.worker.process(env.submit(_scanned_pdf(), "application/pdf", TaskKind.EXTRACT_DOC))
    blocks = env.ledger.get_blocks(RES)
    ocr_blocks = [b for b in blocks if b.method == "tesseract/ocr"]
    assert ocr_blocks and "Scanned clause 4.2" in ocr_blocks[0].normalized_text
    assert ocr_blocks[0].page_index == 0
    assert env.resource.status is EntityStatus.EXTRACTED_UNVERIFIED


def test_scanned_pdf_without_ocr_engine_is_unsupported_with_gap(tmp_path: Path) -> None:
    env = Env(tmp_path, ocr_runner=_missing_ocr)
    env.worker.process(env.submit(_scanned_pdf(), "application/pdf", TaskKind.EXTRACT_DOC))
    assert env.resource.status is EntityStatus.UNSUPPORTED
    gaps = env.ledger.get_gaps(RES)
    assert [g.gap_type for g in gaps] == [GapType.UNSUPPORTED_FORMAT]
    assert "ocr_engine_missing" in gaps[0].reason


def test_corrupt_pdf_fails_with_gap(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.worker.process(env.submit(b"%PDF-1.4 not really", "application/pdf", TaskKind.EXTRACT_DOC))
    assert env.resource.status is EntityStatus.FAILED
    assert env.ledger.get_gaps(RES)


def test_image_with_text_is_extracted(tmp_path: Path) -> None:
    env = Env(tmp_path, ocr_runner=_fake_ocr("SALE 50% OFF"))
    env.worker.process(env.submit(_png((200, 100)), "image/png", TaskKind.EXTRACT_IMAGE))
    assert env.resource.status is EntityStatus.EXTRACTED_UNVERIFIED
    assert any("SALE 50% OFF" in b.normalized_text for b in env.ledger.get_blocks(RES))


def test_textless_image_is_verified_noncontent(tmp_path: Path) -> None:
    env = Env(tmp_path, ocr_runner=_fake_ocr(""))
    env.worker.process(env.submit(_png((200, 100)), "image/png", TaskKind.EXTRACT_IMAGE))
    assert env.resource.status is EntityStatus.VERIFIED_NONCONTENT


def test_tracking_pixel_is_noncontent_without_ocr(tmp_path: Path) -> None:
    env = Env(tmp_path, ocr_runner=_missing_ocr)
    env.worker.process(env.submit(_png((1, 1)), "image/png", TaskKind.EXTRACT_IMAGE))
    assert env.resource.status is EntityStatus.VERIFIED_NONCONTENT
    assert env.ledger.get_gaps(RES) == []


def test_oversized_payload_is_partial_with_limit_gap(tmp_path: Path) -> None:
    env = Env(tmp_path, max_payload_bytes=10)
    env.worker.process(env.submit(_text_pdf(), "application/pdf", TaskKind.EXTRACT_DOC))
    assert env.resource.status is EntityStatus.PARTIAL
    assert [g.gap_type for g in env.ledger.get_gaps(RES)] == [GapType.LIMIT_HIT]


def test_missing_evidence_fails_resource(tmp_path: Path) -> None:
    env = Env(tmp_path)
    task = env.submit(_text_pdf(), "application/pdf", TaskKind.EXTRACT_DOC)
    task.payload["sha256"] = "0" * 64
    env.worker.process(task)
    assert env.resource.status is EntityStatus.FAILED
    assert env.resource.status_reason == "evidence_missing"


def test_unexpected_exception_becomes_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    task = env.submit(_text_pdf(), "application/pdf", TaskKind.EXTRACT_DOC)

    def boom(*_a: Any, **_k: Any) -> None:
        raise ValueError("kaboom")

    monkeypatch.setattr("src.modules.content_processor.workers.doc_worker.extract_pdf", boom)
    env.worker.process(task)
    assert env.resource.status is EntityStatus.RETRY_SCHEDULED
    assert "kaboom" in (env.resource.status_reason or "")


def _sleepy(seconds: float) -> str:
    import time

    time.sleep(seconds)
    return "done"


def _echo(value: str) -> str:
    return value


def _raiser() -> None:
    raise ValueError("bad input")


def test_run_isolated_returns_result() -> None:
    assert run_isolated(_echo, {"value": "hi"}, timeout_s=30) == "hi"


def test_run_isolated_kills_hung_child() -> None:
    with pytest.raises(TimeoutError):
        run_isolated(_sleepy, {"seconds": 60}, timeout_s=1)


def test_run_isolated_reports_child_error() -> None:
    with pytest.raises(RuntimeError, match="bad input"):
        run_isolated(_raiser, {}, timeout_s=30)


def test_isolated_pdf_extraction_end_to_end(tmp_path: Path) -> None:
    env = Env(tmp_path, isolate=True, timeout_s=60)
    env.worker.process(env.submit(_text_pdf(), "application/pdf", TaskKind.EXTRACT_DOC))
    assert env.resource.status is EntityStatus.EXTRACTED_UNVERIFIED
    assert env.ledger.get_blocks(RES)


def _low_conf_ocr(_img: bytes, *, packs: Any = None, psm: int = 6) -> OcrResult:
    return OcrResult(text="smudged words", confidence=0.31, packs_used=list(packs or ["eng"]))


def test_low_confidence_ocr_raises_retry_eligible_gap(tmp_path: Path) -> None:
    env = Env(tmp_path, ocr_runner=_low_conf_ocr)
    env.worker.process(env.submit(_scanned_pdf(), "application/pdf", TaskKind.EXTRACT_DOC))
    gaps = env.ledger.get_gaps(RES)
    assert [g.gap_type for g in gaps] == [GapType.LOW_CONFIDENCE]
    assert gaps[0].retry_eligible is True
    assert env.resource.status is EntityStatus.EXTRACTED_UNVERIFIED


def test_scope_without_latin_pack_flags_scanned_region_unsupported(tmp_path: Path) -> None:
    env = Env(tmp_path, ocr_runner=_fake_ocr("never used"), ocr_languages=["hin"])
    env.worker.process(env.submit(_scanned_pdf(), "application/pdf", TaskKind.EXTRACT_DOC))
    gaps = env.ledger.get_gaps(RES)
    assert [g.gap_type for g in gaps] == [GapType.UNSUPPORTED_FORMAT]
    assert "ocr_lang:LATIN" in gaps[0].reason
    assert env.resource.status is EntityStatus.UNSUPPORTED
