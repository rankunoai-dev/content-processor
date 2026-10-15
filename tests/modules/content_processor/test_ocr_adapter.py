"""OCR routing tests: script detection, pack selection, engine-missing behaviour."""

from __future__ import annotations

import pytest

from src.modules.content_processor.adapters import ocr
from src.modules.content_processor.adapters.ocr import (
    detect_script_from_text,
    route_ocr,
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
    # A Devanagari region must never be read with the English pack just because it is allowed.
    assert select_ocr_packs("DEVANAGARI", allowed_packs=["eng"]) == []
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


def test_unknown_script_has_no_packs() -> None:
    assert select_ocr_packs("ETHIOPIC") == []


def test_cjk_kana_and_hangul_map_to_cjk() -> None:
    assert detect_script_from_text("ひらがなカタカナ") == "CJK"
    assert detect_script_from_text("한국어 텍스트") == "CJK"


def test_route_prefers_osd_over_hint() -> None:
    route = route_ocr(
        b"x",
        script_hint="LATIN",
        osd_runner=lambda _b: "Page number: 0\nScript: Devanagari\n",
        available_packs=["eng", "hin", "mar"],
    )
    assert (route.script, route.source, route.packs) == ("DEVANAGARI", "osd", ["hin", "mar"])
    assert route.unsupported_reason is None


def test_route_uses_hint_when_osd_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "is_ocr_available", lambda: False)
    route = route_ocr(b"x", script_hint="ARABIC", available_packs=["ara", "fas", "urd", "eng"])
    assert (route.source, route.packs) == ("hint", ["ara", "fas", "urd"])


def test_route_defaults_to_latin_with_locale_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "is_ocr_available", lambda: False)
    route = route_ocr(b"x", locale="de-DE", available_packs=["eng", "deu"])
    assert (route.source, route.packs) == ("default", ["deu", "eng"])


def test_route_reports_missing_pack_instead_of_falling_back_to_english() -> None:
    route = route_ocr(
        b"x",
        osd_runner=lambda _b: "Script: Tamil",
        available_packs=["eng", "hin"],
    )
    assert route.packs == []
    assert route.unsupported_reason == "ocr_lang:TAMIL"


def test_route_respects_scope_pinned_languages() -> None:
    route = route_ocr(
        b"x",
        osd_runner=lambda _b: "Script: Devanagari",
        allowed_packs=["mar"],
        available_packs=["eng", "hin", "mar"],
    )
    assert route.packs == ["mar"]


def test_route_ignores_osd_failure() -> None:
    def broken(_b: bytes) -> str:
        raise ValueError("too small")

    route = route_ocr(b"x", script_hint="GREEK", osd_runner=broken, available_packs=["ell"])
    assert (route.source, route.packs) == ("hint", ["ell"])
