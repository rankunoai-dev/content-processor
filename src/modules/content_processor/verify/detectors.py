"""Inputs for the extended rules that need evidence bytes (R6, R8, evidence integrity).

- R6 reads the rendered screenshot through a `TextRegionDetector`. The spec's
  baseline detector is Tesseract in sparse-text mode (`psm 11`), reached through
  `adapters.ocr.run_ocr`; `default_detector()` returns None when no engine is
  installed, and records then say `r6:detector_unavailable` (operator decision
  2026-10-19: record, do not block, until OCR is installed).
- R8 needs every image region of a PDF, including the small ones the document
  worker does not OCR; they are re-read from the stored PDF here.
- Evidence integrity re-hashes the stored payload and checks that every block's
  evidence still exists, so a record never cites bytes that are gone or changed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Literal, Protocol

from pydantic import Field

from src.core.schemas import StrictModel
from src.integrations.evidence_store import EvidenceStore
from src.modules.content_processor.adapters.ocr import is_ocr_available, run_ocr
from src.modules.content_processor.adapters.pdf import extract_pdf
from src.modules.content_processor.contracts import ContentBlock, Resource, RunEvent

__all__ = [
    "DetectedRegion",
    "EvidenceProblem",
    "OcrRegionDetector",
    "PdfRegionRef",
    "TextRegionDetector",
    "default_detector",
    "evidence_problems",
    "group_lines",
    "pdf_regions_from_evidence",
    "screenshot_sha",
]

_EVIDENCE = "evidence:"


class DetectedRegion(StrictModel):
    """A text region a detector found on a screenshot."""

    text: str
    bbox: tuple[float, float, float, float]
    confidence: float = Field(ge=0.0, le=1.0)


class TextRegionDetector(Protocol):
    """Finds text regions on an image; None means the detector could not run."""

    @property
    def name(self) -> str:
        """Recorded in warnings and gap reasons."""
        ...

    def detect(self, image: bytes) -> list[DetectedRegion] | None:
        """Text regions on `image`, or None when the engine is unavailable."""
        ...


class OcrRegionDetector:
    """Tesseract sparse-text detection (`psm 11`) through the OCR adapter."""

    name = "tesseract_psm11"

    def __init__(self, *, min_confidence: float = 0.6, ocr_runner: Any = None) -> None:
        """`ocr_runner` swaps the engine (tests, alternate backends)."""
        self._min_confidence = min_confidence
        self._runner = ocr_runner

    def detect(self, image: bytes) -> list[DetectedRegion] | None:
        """Text lines at or above the confidence floor; None when no engine ran.

        Tesseract reports single words; R6 judges phrases, so words are grouped
        into lines: same baseline band and a horizontal gap under two glyph
        heights. A line's confidence is its weakest word's.
        """
        result = run_ocr(image, psm=11, ocr_runner=self._runner)
        if not result.engine_available or result.error:
            return None
        words = [
            DetectedRegion(text=s.text.strip(), bbox=s.bbox, confidence=s.confidence)
            for s in result.spans
            if s.text.strip() and s.confidence >= self._min_confidence
        ]
        return group_lines(words)


def group_lines(words: Sequence[DetectedRegion]) -> list[DetectedRegion]:
    """Merge word boxes into line regions, in reading order.

    Two passes: words whose vertical centres fall inside a row's band form one
    row (glyph heights differ, so tops alone are unreliable); each row is then
    read left to right and split where the gap exceeds two glyph heights.
    """
    rows: list[list[DetectedRegion]] = []
    for word in sorted(words, key=lambda w: (w.bbox[1] + w.bbox[3]) / 2):
        centre = (word.bbox[1] + word.bbox[3]) / 2
        for row in rows:
            top = min(w.bbox[1] for w in row)
            bottom = max(w.bbox[3] for w in row)
            if top <= centre <= bottom:
                row.append(word)
                break
        else:
            rows.append([word])
    lines: list[list[DetectedRegion]] = []
    for row in rows:
        row.sort(key=lambda w: w.bbox[0])
        current = [row[0]]
        for word in row[1:]:
            height = max(word.bbox[3] - word.bbox[1], current[-1].bbox[3] - current[-1].bbox[1])
            if word.bbox[0] - current[-1].bbox[2] <= 2 * max(height, 1.0):
                current.append(word)
            else:
                lines.append(current)
                current = [word]
        lines.append(current)
    regions = [
        DetectedRegion(
            text=" ".join(w.text for w in line),
            bbox=(
                min(w.bbox[0] for w in line),
                min(w.bbox[1] for w in line),
                max(w.bbox[2] for w in line),
                max(w.bbox[3] for w in line),
            ),
            confidence=min(w.confidence for w in line),
        )
        for line in lines
    ]
    return sorted(regions, key=lambda r: (round(r.bbox[1]), r.bbox[0]))


def default_detector() -> TextRegionDetector | None:
    """The baseline detector when Tesseract is installed, else None."""
    return OcrRegionDetector() if is_ocr_available() else None


def screenshot_sha(history: Iterable[RunEvent], resource_id: str) -> str | None:
    """Screenshot hash from the render worker's final event for the resource.

    The render worker records `[dom, visible text, screenshot]` evidence on its
    `rendered;…` event; the latest such event wins.
    """
    found: str | None = None
    for event in history:
        if (
            event.entity_type == "resource"
            and event.entity_id == resource_id
            and (event.reason or "").startswith("rendered")
            and len(event.evidence_ids) >= 3
            and event.evidence_ids[2].startswith(_EVIDENCE)
        ):
            found = event.evidence_ids[2][len(_EVIDENCE) :]
    return found


class PdfRegionRef(StrictModel):
    """An image region of a PDF page (no bytes)."""

    page_index: int = Field(ge=0)
    bbox: tuple[float, float, float, float]
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    is_full_page: bool = False


def pdf_regions_from_evidence(
    store: EvidenceStore,
) -> Callable[[Resource], list[PdfRegionRef] | None]:
    """Image regions re-read from a resource's stored PDF; None when not a readable PDF."""

    def read(resource: Resource) -> list[PdfRegionRef] | None:
        sha = resource.payload_sha256
        if sha is None or resource.sniffed_mime != "application/pdf" or not store.exists(sha):
            return None
        extraction = extract_pdf(
            store.get(sha),
            resource_id=resource.resource_id,
            run_id=resource.run_id,
            evidence_id=f"{_EVIDENCE}{sha}",
        )
        return [
            PdfRegionRef(
                page_index=r.page_index,
                bbox=r.bbox,
                width=r.width,
                height=r.height,
                is_full_page=r.is_full_page,
            )
            for r in extraction.image_regions
        ]

    return read


class EvidenceProblem(StrictModel):
    """Evidence a record cites that is missing or no longer matches its hash."""

    kind: Literal["missing", "hash_mismatch"]
    sha256: str


def evidence_problems(
    store: EvidenceStore, resource: Resource, blocks: Sequence[ContentBlock]
) -> list[EvidenceProblem]:
    """Re-hash the payload; check that every cited evidence object exists."""
    problems: list[EvidenceProblem] = []
    payload = resource.payload_sha256
    if payload is not None:
        if not store.exists(payload):
            problems.append(EvidenceProblem(kind="missing", sha256=payload))
        elif hashlib.sha256(store.get(payload)).hexdigest() != payload:
            problems.append(EvidenceProblem(kind="hash_mismatch", sha256=payload))
    cited = {
        e[len(_EVIDENCE) :]
        for b in blocks
        for e in b.evidence_ids
        if e.startswith(_EVIDENCE) and len(e) == len(_EVIDENCE) + 64
    }
    for sha in sorted(cited - {payload}):
        if not store.exists(sha):
            problems.append(EvidenceProblem(kind="missing", sha256=sha))
    return problems
