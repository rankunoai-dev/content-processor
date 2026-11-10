"""R6 screenshot detection over several pages at once gives the same regions as one by one."""

from __future__ import annotations

import io
import random
import time
from typing import Any

import pytest
from PIL import Image, ImageDraw, ImageFont

from src.modules.content_processor.adapters import ocr
from src.modules.content_processor.verify.detectors import (
    DetectedRegion,
    OcrRegionDetector,
    detect_screenshot_regions,
)


class _JitteryDetector:
    """A deterministic detector that takes a random time per image."""

    name = "jittery"

    def __init__(self) -> None:
        self.calls = 0

    def detect(self, image: bytes) -> list[DetectedRegion] | None:
        self.calls += 1
        time.sleep(random.uniform(0, 0.02))  # noqa: S311 - timing jitter, not security
        if image == b"blank":
            return None
        return [DetectedRegion(text=image.decode(), bbox=(0, 0, 10, 10), confidence=0.9)]


def test_parallel_detection_keeps_order_and_results() -> None:
    images = [f"page {i}".encode() for i in range(9)] + [b"blank"]
    serial = detect_screenshot_regions(_JitteryDetector(), images, workers=1)
    parallel = detect_screenshot_regions(_JitteryDetector(), images, workers=4)
    assert serial == parallel
    assert [r[0].text if r else None for r in parallel] == [*(f"page {i}" for i in range(9)), None]
    assert detect_screenshot_regions(_JitteryDetector(), [], workers=4) == []


def test_parallel_detection_runs_each_image_once() -> None:
    detector = _JitteryDetector()
    detect_screenshot_regions(detector, [b"a", b"b", b"c"], workers=3)
    assert detector.calls == 3


def _screenshot(lines: list[str]) -> bytes:
    image = Image.new("RGB", (900, 80 + 60 * len(lines)), "white")
    font: Any
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except OSError:
        font = ImageFont.load_default(size=36)
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        draw.text((30, 30 + 60 * i), line, fill="black", font=font)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.skipif(not ocr.is_ocr_available(), reason="Tesseract not installed")
def test_real_detector_gives_identical_regions_in_parallel() -> None:
    shots = [
        _screenshot(["Annual plan costs 120 dollars", "Refunds within 30 days"]),
        _screenshot(["Premiums grew in every region"]),
        _screenshot(["Claims are paid within 30 days", "Contact us", "Menu"]),
    ]
    detector = OcrRegionDetector()
    serial = detect_screenshot_regions(detector, shots, workers=1)
    parallel = detect_screenshot_regions(detector, shots, workers=3)
    assert serial == parallel
    assert all(regions for regions in parallel), "every screenshot yielded text regions"
