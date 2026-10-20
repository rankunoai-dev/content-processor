"""Image extraction adapter: metadata analysis, visual classification, and OCR routing.

ARCHITECTURE.md §6 and §6.1:
- Extracts image format, dimensions, mode, and EXIF metadata.
- Routes image to multilingual OCR engine (Tesseract) per-region.
- If OCR engine or required script pack is missing: emits an explicit UNSUPPORTED gap
  with the image evidence reference retained. Never claims 100% extraction without OCR.
- If OCR runs and finds zero text, classifies as VERIFIED_NONCONTENT (e.g. decorative).
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Sequence
from typing import Any

from PIL import ExifTags, Image
from pydantic import Field

from src.core.logger import get_logger
from src.core.schemas import StrictModel
from src.modules.content_processor.adapters.html_source import normalize_text
from src.modules.content_processor.adapters.ocr import (
    OCR_CONFIDENCE_FLOOR,
    installed_packs,
    route_ocr,
    run_ocr,
)
from src.modules.content_processor.contracts import ContentBlock, Gap, Provenance
from src.modules.content_processor.enums import ContentLayer, EntityStatus, GapType

__all__ = ["ImageExtraction", "extract_image"]

_logger = get_logger("content_processor.image")

TOOL_VERSION = "image/1.0"


class ImageExtraction(StrictModel):
    """Extraction output for an image resource."""

    blocks: list[ContentBlock] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    width: int = Field(ge=0)
    height: int = Field(ge=0)
    image_format: str
    is_visual_only: bool = False
    status: EntityStatus
    warnings: list[str] = Field(default_factory=list)


def extract_image(
    image_bytes: bytes,
    *,
    resource_id: str,
    run_id: str,
    evidence_id: str,
    media_type: str = "image/png",
    ocr_languages: Sequence[str] | None = None,
    ocr_runner: Any = None,
    locale: str | None = None,
    script_hint: str | None = None,
    osd_runner: Any = None,
) -> ImageExtraction:
    """Analyze image metadata and extract text via multilingual OCR."""
    try:
        pil_img = Image.open(io.BytesIO(image_bytes))
        width, height = pil_img.size
        img_format = pil_img.format or media_type.split("/")[-1].upper()
    except Exception as exc:
        _logger.error("image_open_error", extra={"error": str(exc), "resource_id": resource_id})
        return ImageExtraction(
            blocks=[],
            gaps=[
                Gap(
                    gap_id=f"gap-{hashlib.sha256(f'{resource_id}|corrupt'.encode()).hexdigest()[:12]}",
                    run_id=run_id,
                    resource_id=resource_id,
                    gap_type=GapType.UNSUPPORTED_FORMAT,
                    status=EntityStatus.FAILED,
                    reason=f"corrupt_image_payload:{exc}",
                    evidence_ids=[evidence_id],
                )
            ],
            metadata={},
            width=0,
            height=0,
            image_format="UNKNOWN",
            is_visual_only=False,
            status=EntityStatus.FAILED,
            warnings=[f"corrupt_image:{exc}"],
        )

    metadata: dict[str, str] = {
        "format": str(img_format),
        "dimensions": f"{width}x{height}",
        "mode": str(pil_img.mode),
    }

    # Extract EXIF if present
    try:
        exif = pil_img.getexif()
        if exif:
            for tag_id, value in exif.items():
                tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                if isinstance(value, (str, int, float)):
                    metadata[f"exif_{tag_name}"] = str(value)
    except Exception as exc:
        _logger.debug("exif_read_error", extra={"error": str(exc)})

    blocks: list[ContentBlock] = []
    gaps: list[Gap] = []
    warnings: list[str] = []

    # Metadata block
    meta_text = f"Image {img_format} {width}x{height}"
    norm_meta, ops_meta = normalize_text(meta_text)
    meta_hash = hashlib.sha256(f"{resource_id}|image|meta".encode()).hexdigest()
    blocks.append(
        ContentBlock(
            block_id=f"blk-{meta_hash[:12]}",
            block_key=f"{resource_id}|image|metadata",
            resource_id=resource_id,
            state_id=f"{run_id}|image",
            region_id="reg-image-metadata",
            reading_order=0,
            layer=ContentLayer.METADATA,
            role="unknown",
            raw_text=meta_text,
            normalized_text=norm_meta,
            normalization_ops=ops_meta,
            method="pillow/metadata",
            tool_version=TOOL_VERSION,
            confidence=1.0,
            status=EntityStatus.VERIFIED_EXTRACTED,
            evidence_ids=[evidence_id],
            provenance=Provenance(frame_path="image"),
        )
    )

    # Small icons or tracking pixels (< 16x16) are verified non-content
    if width <= 16 and height <= 16:
        return ImageExtraction(
            blocks=blocks,
            gaps=[],
            metadata=metadata,
            width=width,
            height=height,
            image_format=img_format,
            is_visual_only=True,
            status=EntityStatus.VERIFIED_NONCONTENT,
            warnings=["icon_or_tracker_dimension"],
        )

    # Route the region: script from OSD or hint, packs limited to scope and installed set.
    route = route_ocr(
        image_bytes,
        script_hint=script_hint,
        locale=locale,
        allowed_packs=ocr_languages,
        available_packs=installed_packs() if ocr_runner is None else None,
        osd_runner=osd_runner,
    )
    if route.unsupported_reason is not None:
        gaps.append(
            Gap(
                gap_id=f"gap-{hashlib.sha256(f'{resource_id}|{route.unsupported_reason}'.encode()).hexdigest()[:12]}",
                run_id=run_id,
                resource_id=resource_id,
                gap_type=GapType.UNSUPPORTED_FORMAT,
                status=EntityStatus.UNSUPPORTED,
                reason=f"{route.unsupported_reason};image_content_unextracted",
                evidence_ids=[evidence_id],
            )
        )
        return ImageExtraction(
            blocks=blocks,
            gaps=gaps,
            metadata=metadata,
            width=width,
            height=height,
            image_format=img_format,
            is_visual_only=False,
            status=EntityStatus.UNSUPPORTED,
            warnings=[route.unsupported_reason],
        )
    packs = route.packs

    # Execute OCR
    ocr_res = run_ocr(image_bytes, packs=packs, ocr_runner=ocr_runner)

    if not ocr_res.engine_available:
        # Honest gap: Tesseract is missing
        gap_id = f"gap-{hashlib.sha256(f'{resource_id}|ocr_engine'.encode()).hexdigest()[:12]}"
        gaps.append(
            Gap(
                gap_id=gap_id,
                run_id=run_id,
                resource_id=resource_id,
                gap_type=GapType.UNSUPPORTED_FORMAT,
                status=EntityStatus.UNSUPPORTED,
                reason="ocr_engine_missing:tesseract;image_content_unextracted",
                evidence_ids=[evidence_id],
            )
        )
        return ImageExtraction(
            blocks=blocks,
            gaps=gaps,
            metadata=metadata,
            width=width,
            height=height,
            image_format=img_format,
            is_visual_only=False,
            status=EntityStatus.UNSUPPORTED,
            warnings=["ocr_engine_missing"],
        )

    if ocr_res.error:
        # The engine ran but failed: nothing was read, so this is not "no content".
        gaps.append(
            Gap(
                gap_id=f"gap-{hashlib.sha256(f'{resource_id}|ocr_error'.encode()).hexdigest()[:12]}",
                run_id=run_id,
                resource_id=resource_id,
                gap_type=GapType.UNSUPPORTED_FORMAT,
                status=EntityStatus.PARTIAL,
                reason=f"ocr_error:{ocr_res.error[:120]};image_content_unextracted",
                evidence_ids=[evidence_id],
                retry_eligible=True,
            )
        )
        return ImageExtraction(
            blocks=blocks,
            gaps=gaps,
            metadata=metadata,
            width=width,
            height=height,
            image_format=img_format,
            is_visual_only=False,
            status=EntityStatus.PARTIAL,
            warnings=["ocr_error"],
        )

    if ocr_res.text.strip():
        status = EntityStatus.VERIFIED_EXTRACTED
        if ocr_res.confidence < OCR_CONFIDENCE_FLOOR:
            # Trigger only (ADR-0003): record the weak reading and ask for a second opinion.
            status = EntityStatus.EXTRACTED_UNVERIFIED
            warnings.append("ocr_low_confidence:second_opinion_recommended")
            gaps.append(
                Gap(
                    gap_id=f"gap-{hashlib.sha256(f'{resource_id}|ocr_low_conf'.encode()).hexdigest()[:12]}",
                    run_id=run_id,
                    resource_id=resource_id,
                    gap_type=GapType.LOW_CONFIDENCE,
                    status=EntityStatus.EXTRACTED_UNVERIFIED,
                    reason=f"ocr_confidence={ocr_res.confidence:.2f}<{OCR_CONFIDENCE_FLOOR}",
                    evidence_ids=[evidence_id],
                    retry_eligible=True,
                )
            )
        # Text found in image
        norm_ocr, ops_ocr = normalize_text(ocr_res.text)
        txt_hash = hashlib.sha256(f"{resource_id}|image|ocr|{norm_ocr}".encode()).hexdigest()
        blocks.append(
            ContentBlock(
                block_id=f"blk-{txt_hash[:12]}",
                block_key=f"{resource_id}|image|ocr_body",
                resource_id=resource_id,
                state_id=f"{run_id}|image",
                region_id="reg-image-ocr-body",
                reading_order=1,
                bbox=(0.0, 0.0, float(width), float(height)),
                layer=ContentLayer.RENDERED_VISIBLE,
                role="main",
                raw_text=ocr_res.text,
                normalized_text=norm_ocr,
                normalization_ops=ops_ocr,
                method="tesseract/ocr",
                tool_version=TOOL_VERSION,
                confidence=ocr_res.confidence,
                status=status,
                evidence_ids=[evidence_id],
                provenance=Provenance(
                    ocr_params={"packs": ocr_res.packs_used},
                    frame_path="image",
                ),
            )
        )
        return ImageExtraction(
            blocks=blocks,
            gaps=gaps,
            metadata=metadata,
            width=width,
            height=height,
            image_format=img_format,
            is_visual_only=False,
            status=status,
            warnings=warnings,
        )

    # Zero text recognized: purely photographic or graphical content
    return ImageExtraction(
        blocks=blocks,
        gaps=[],
        metadata=metadata,
        width=width,
        height=height,
        image_format=img_format,
        is_visual_only=True,
        status=EntityStatus.VERIFIED_NONCONTENT,
        warnings=warnings,
    )
