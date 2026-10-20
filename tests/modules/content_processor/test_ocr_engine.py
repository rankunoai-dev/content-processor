"""OCR engine discovery (C-1), line grouping for R6, and a real-engine check when installed."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image, ImageDraw, ImageFont

from src.modules.content_processor.adapters import ocr
from src.modules.content_processor.verify.detectors import (
    DetectedRegion,
    OcrRegionDetector,
    group_lines,
)


def test_tesseract_cmd_prefers_the_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    exe = tmp_path / "tesseract.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv("CP_TESSERACT_CMD", str(exe))
    assert ocr.tesseract_cmd() == str(exe)


def test_tesseract_cmd_is_none_when_nothing_exists(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CP_TESSERACT_CMD", raising=False)
    monkeypatch.setattr(ocr.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ocr, "_WINDOWS_ENGINE", Path("Z:/nowhere/tesseract.exe"))
    monkeypatch.setattr(ocr.Path, "is_file", lambda self: False)
    assert ocr.tesseract_cmd() is None
    assert ocr.is_ocr_available() is False
    assert ocr.ocr_engine_info() == {"available": False, "reason": "ocr_engine_missing:tesseract"}


def test_tessdata_dir_env_then_user_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    explicit = tmp_path / "packs"
    explicit.mkdir()
    monkeypatch.setenv("CP_TESSDATA_DIR", str(explicit))
    assert ocr.tessdata_dir() == explicit.as_posix()
    monkeypatch.delenv("CP_TESSDATA_DIR")
    local = tmp_path / "local"
    (local / "content-processor" / "tessdata").mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert ocr.tessdata_dir() == (local / "content-processor" / "tessdata").as_posix()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "missing"))
    assert ocr.tessdata_dir() is None


def test_config_adds_the_pack_folder_and_quotes_spaces(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ocr, "tessdata_dir", lambda: "C:/packs")
    assert ocr._config("--psm 6") == "--tessdata-dir C:/packs --psm 6"  # noqa: SLF001
    monkeypatch.setattr(ocr, "tessdata_dir", lambda: "C:/my packs")
    assert ocr._config() == '--tessdata-dir "C:/my packs"'  # noqa: SLF001
    monkeypatch.setattr(ocr, "tessdata_dir", lambda: None)
    assert ocr._config("--psm 11") == "--psm 11"  # noqa: SLF001


def test_engine_info_and_pytesseract_wiring(monkeypatch: pytest.MonkeyPatch):
    calls: dict[str, Any] = {}
    fake = SimpleNamespace(
        pytesseract=SimpleNamespace(tesseract_cmd="tesseract"),
        get_tesseract_version=lambda: "5.4.0",
        get_languages=lambda config: calls.setdefault("config", config) and ["eng", "hin", "osd"],
    )
    monkeypatch.setattr(ocr.importlib, "import_module", lambda _name: fake)
    monkeypatch.setattr(ocr, "tesseract_cmd", lambda: "C:/T/tesseract.exe")
    monkeypatch.setattr(ocr, "tessdata_dir", lambda: "C:/packs")
    info = ocr.ocr_engine_info()
    assert info == {
        "available": True,
        "engine": "C:/T/tesseract.exe",
        "version": "5.4.0",
        "tessdata": "C:/packs",
        "packs": ["eng", "hin"],
    }
    assert fake.pytesseract.tesseract_cmd == "C:/T/tesseract.exe"
    assert calls["config"] == "--tessdata-dir C:/packs"


def _word(
    text: str, x0: float, y0: float, x1: float, y1: float, conf: float = 0.9
) -> DetectedRegion:
    return DetectedRegion(text=text, bbox=(x0, y0, x1, y1), confidence=conf)


def test_group_lines_joins_a_row_despite_uneven_tops_and_splits_far_words():
    words = [
        _word("$120", 300, 28, 360, 70),  # taller glyph, higher top
        _word("Annual", 20, 32, 120, 70),
        _word("plan", 130, 32, 190, 70, conf=0.7),
        _word("costs", 200, 32, 290, 70),
        _word("Refunds", 20, 130, 150, 168),
        _word("are", 160, 130, 210, 168),
        _word("Menu", 620, 130, 700, 168),
    ]
    lines = group_lines(words)
    assert [r.text for r in lines] == ["Annual plan costs $120", "Refunds are", "Menu"]
    assert lines[0].bbox == (20, 28, 360, 70) and lines[0].confidence == 0.7
    assert group_lines([]) == []


def test_detector_groups_words_from_the_engine():
    def runner(image: bytes, *, packs: Any, psm: int) -> ocr.OcrResult:
        return ocr.OcrResult(
            text="x",
            confidence=0.9,
            spans=[
                ocr.OcrSpan(text="Hello", bbox=(0, 0, 50, 20), confidence=0.9),
                ocr.OcrSpan(text="world", bbox=(55, 0, 110, 20), confidence=0.9),
            ],
        )

    regions = OcrRegionDetector(ocr_runner=runner).detect(b"img")
    assert [r.text for r in regions or []] == ["Hello world"]


@pytest.mark.skipif(not ocr.is_ocr_available(), reason="Tesseract not installed")
def test_real_engine_reads_rendered_text():
    image = Image.new("RGB", (900, 120), "white")
    try:
        font: Any = ImageFont.truetype("arial.ttf", 40)
    except OSError:
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 40)
        except OSError:
            pytest.skip("no scalable font available to render test text")
    ImageDraw.Draw(image).text((20, 30), "Annual plan costs 120 dollars", fill="black", font=font)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    result = ocr.run_ocr(buf.getvalue(), packs=["eng"])
    assert result.engine_available and result.error is None
    assert "Annual plan costs 120 dollars" in result.text
    assert result.confidence > 0.6
    lines = OcrRegionDetector().detect(buf.getvalue())
    assert lines is not None and any("Annual plan costs" in r.text for r in lines)
