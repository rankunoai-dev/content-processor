"""OCR routing tests: script detection, pack selection, engine-missing behaviour."""

from __future__ import annotations

import pytest

from src.modules.content_processor.adapters import ocr
from src.modules.content_processor.adapters.ocr import (
    detect_script_from_text,
    run_ocr,
    select_ocr_packs,
)


def test_detect_script() -> None:
    assert detect_script_from_text("hello world") == "LATIN"
    assert detect_script_from_text("नमस्ते दुनिया") == "DEVANAGARI"
    assert detect_script_from_text("") == "LATIN"


def test_pack_selection_follows_script_and_locale() -> None:
    assert select_ocr_packs("DEVANAGARI")[0] == "hin"
    assert select_ocr_packs("LATIN", locale="de-DE")[0] == "deu"
    assert len(select_ocr_packs("CJK")) <= 3


def test_pack_selection_respects_allowed_packs() -> None:
    assert select_ocr_packs("DEVANAGARI", allowed_packs=["eng"]) == ["eng"]
    assert select_ocr_packs("DEVANAGARI", allowed_packs=["tam"]) == []


def test_missing_engine_is_explicit_not_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "is_ocr_available", lambda: False)
    result = run_ocr(b"irrelevant", packs=["hin"])
    assert result.engine_available is False
    assert result.packs_used == ["hin"]
    assert "ocr_engine_missing" in (result.error or "")


def test_engine_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "is_ocr_available", lambda: True)
    result = run_ocr(b"not an image", packs=["eng"])
    assert result.engine_available is True
    assert (result.error or "").startswith("ocr_execution_error")
