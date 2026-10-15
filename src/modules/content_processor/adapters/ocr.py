"""Multilingual OCR routing and engine execution (ARCHITECTURE.md §6.1).

Routing happens per region, not per document:
script detection -> language pack selection -> Tesseract execution -> confidence gate.
When Tesseract is missing or an unsupported script pack is requested, an explicit
UNSUPPORTED gap is emitted with the crop evidence preserved. Never falls back
to English silently.
"""

from __future__ import annotations

import importlib
import io
import shutil
import unicodedata
from collections import Counter
from collections.abc import Sequence
from typing import Any

from PIL import Image
from pydantic import Field

from src.core.logger import get_logger
from src.core.schemas import StrictModel

__all__ = [
    "OCR_CONFIDENCE_FLOOR",
    "OcrResult",
    "OcrRoute",
    "OcrSpan",
    "SCRIPT_TO_PACKS",
    "detect_script_from_text",
    "installed_packs",
    "is_ocr_available",
    "route_ocr",
    "run_ocr",
    "select_ocr_packs",
]

_logger = get_logger("content_processor.ocr")

# Mean word confidence below this triggers the second-opinion path (ADR-0003).
# Provisional: ARCHITECTURE.md 6.1 says to set it from Stage 0 fixtures, not guess.
OCR_CONFIDENCE_FLOOR = 0.60

# Tesseract OSD script names -> the SCRIPT_TO_PACKS keys.
_OSD_SCRIPTS: dict[str, str] = {
    "Latin": "LATIN",
    "Devanagari": "DEVANAGARI",
    "Arabic": "ARABIC",
    "Han": "CJK",
    "Hangul": "CJK",
    "Japanese": "CJK",
    "Korean": "CJK",
    "Cyrillic": "CYRILLIC",
    "Greek": "GREEK",
    "Hebrew": "HEBREW",
    "Thai": "THAI",
    "Bengali": "BENGALI",
    "Gurmukhi": "GURMUKHI",
    "Gujarati": "GUJARATI",
    "Tamil": "TAMIL",
    "Telugu": "TELUGU",
    "Kannada": "KANNADA",
    "Malayalam": "MALAYALAM",
    "Oriya": "ORIYA",
}
# Unicode character-name prefixes that belong to one of the keys above.
_UNICODE_SCRIPT_ALIASES: dict[str, str] = {
    "HIRAGANA": "CJK",
    "KATAKANA": "CJK",
    "HANGUL": "CJK",
    "BOPOMOFO": "CJK",
}

SCRIPT_TO_PACKS: dict[str, tuple[str, ...]] = {
    "LATIN": ("eng",),
    "DEVANAGARI": ("hin", "mar", "san"),
    "ARABIC": ("ara", "fas", "urd"),
    "CJK": ("chi_sim", "chi_tra", "jpn", "kor"),
    "BENGALI": ("ben",),
    "GURMUKHI": ("pan",),
    "GUJARATI": ("guj",),
    "TAMIL": ("tam",),
    "TELUGU": ("tel",),
    "KANNADA": ("kan",),
    "MALAYALAM": ("mal",),
    "ORIYA": ("ori",),
    "CYRILLIC": ("rus", "ukr"),
    "GREEK": ("ell",),
    "HEBREW": ("heb",),
    "THAI": ("tha",),
}

LOCALE_PACK_HINTS: dict[str, str] = {
    "de": "deu",
    "fr": "fra",
    "es": "spa",
    "it": "ita",
    "nl": "nld",
    "pt": "por",
    "ru": "rus",
    "zh": "chi_sim",
    "ja": "jpn",
    "ko": "kor",
    "hi": "hin",
    "ar": "ara",
}


class OcrSpan(StrictModel):
    """One text span recognized by OCR with its bounding box and confidence."""

    text: str
    bbox: tuple[float, float, float, float]
    confidence: float = Field(ge=0.0, le=1.0)


class OcrResult(StrictModel):
    """Result of an OCR operation on an image or region crop."""

    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    spans: list[OcrSpan] = Field(default_factory=list)
    engine_available: bool = True
    packs_used: list[str] = Field(default_factory=list)
    error: str | None = None


def detect_script_from_text(sample: str) -> str:
    """Identify the dominant script using Unicode character properties."""
    script_counts: Counter[str] = Counter()
    for ch in sample:
        if ch.isalnum():
            try:
                name = unicodedata.name(ch)
                script = name.split()[0]
                script = _UNICODE_SCRIPT_ALIASES.get(script, script)
                script_counts[script] += 1
            except ValueError:
                continue
    if not script_counts:
        return "LATIN"
    return script_counts.most_common(1)[0][0]


def select_ocr_packs(
    script: str | None = None,
    *,
    locale: str | None = None,
    allowed_packs: Sequence[str] | None = None,
) -> list[str]:
    """Select up to 3 OCR language packs based on script, locale, and policy."""
    candidates: list[str] = []
    normalized_script = (script or "LATIN").upper()
    packs_for_script = SCRIPT_TO_PACKS.get(normalized_script)

    # An unknown script yields no candidates: it must surface as an UNSUPPORTED gap,
    # never be read as English.
    if packs_for_script:
        candidates.extend(packs_for_script)

    # If Latin and locale hints an alternate Latin-based language (e.g. deu, fra, spa)
    if normalized_script == "LATIN" and locale:
        lang_code = locale.split("-")[0].lower()
        hint_pack = LOCALE_PACK_HINTS.get(lang_code)
        if hint_pack and hint_pack not in candidates:
            candidates.insert(0, hint_pack)

    # Restrict to allowed packs if specified by RunScope
    if allowed_packs is not None:
        allowed_set = set(allowed_packs)
        candidates = [p for p in candidates if p in allowed_set]

    return candidates[:3]


def _tesseract() -> Any:
    """Load pytesseract lazily; it ships no type information."""
    return importlib.import_module("pytesseract")


def is_ocr_available() -> bool:
    """Check if the Tesseract OCR executable is available in PATH or system."""
    if shutil.which("tesseract") is not None:
        return True
    try:
        _tesseract().get_tesseract_version()
        return True
    except Exception:
        return False


def installed_packs() -> list[str] | None:
    """Language packs the local Tesseract can load, or None when the engine is unavailable."""
    if not is_ocr_available():
        return None
    try:
        langs = _tesseract().get_languages(config="")
    except Exception:  # noqa: BLE001 - treat any probe failure as "unknown"
        return None
    return [str(lang) for lang in langs if lang != "osd"]


def detect_script_osd(image_bytes: bytes, *, osd_runner: Any = None) -> str | None:
    """Detect the dominant script of a crop with Tesseract OSD; None when unavailable."""
    try:
        if osd_runner is not None:
            raw = osd_runner(image_bytes)
        else:
            if not is_ocr_available():
                return None
            raw = _tesseract().image_to_osd(Image.open(io.BytesIO(image_bytes)))
    except Exception:  # noqa: BLE001 - OSD fails on tiny or blank crops; fall back to hints
        return None
    text = str(raw)
    for line in text.splitlines():
        if line.startswith("Script:"):
            return _OSD_SCRIPTS.get(line.split(":", 1)[1].strip())
    return _OSD_SCRIPTS.get(text.strip())


class OcrRoute(StrictModel):
    """Per-region routing decision: which script, which packs, and how it was decided."""

    script: str
    packs: list[str] = Field(default_factory=list)
    source: str
    unsupported_reason: str | None = None


def route_ocr(
    image_bytes: bytes,
    *,
    script_hint: str | None = None,
    locale: str | None = None,
    allowed_packs: Sequence[str] | None = None,
    available_packs: Sequence[str] | None = None,
    osd_runner: Any = None,
) -> OcrRoute:
    """Decide script and packs for one region crop (ARCHITECTURE.md 6.1).

    Script comes from OSD on the crop when the engine can do it, else from the
    caller's hint (script of nearby native text), else Latin. Packs are limited
    to the scope's allowed set and to what is installed. When a non-Latin script
    has no usable pack the route carries `unsupported_reason="ocr_lang:<SCRIPT>"`
    instead of silently reading it as English.
    """
    osd = detect_script_osd(image_bytes, osd_runner=osd_runner)
    if osd is not None:
        script, source = osd, "osd"
    elif script_hint:
        script, source = script_hint.upper(), "hint"
    else:
        script, source = "LATIN", "default"

    allowed = None if allowed_packs is None else set(allowed_packs)
    if available_packs is not None:
        allowed = set(available_packs) if allowed is None else allowed & set(available_packs)
    packs = select_ocr_packs(
        script, locale=locale, allowed_packs=sorted(allowed) if allowed is not None else None
    )
    reason = None if packs else f"ocr_lang:{script}"
    return OcrRoute(script=script, packs=packs, source=source, unsupported_reason=reason)


def run_ocr(
    image_bytes: bytes,
    *,
    packs: Sequence[str] | None = None,
    psm: int = 6,
    ocr_runner: Any = None,
) -> OcrResult:
    """Execute OCR on raw image bytes.

    If an ocr_runner is supplied (for testing or alternate backends), it is used.
    Otherwise, invokes pytesseract if available, or returns engine_available=False.
    """
    if ocr_runner is not None:
        result: OcrResult = ocr_runner(image_bytes, packs=packs, psm=psm)
        return result

    if not is_ocr_available():
        return OcrResult(
            text="",
            confidence=0.0,
            engine_available=False,
            packs_used=list(packs or []),
            error="ocr_engine_missing:tesseract;install_tesseract_and_packs_for_ocr",
        )

    try:
        pytesseract = _tesseract()
        output_dict = pytesseract.Output.DICT

        image = Image.open(io.BytesIO(image_bytes))
        lang_str = "+".join(packs) if packs else "eng"
        config = f"--psm {psm}"

        data = pytesseract.image_to_data(
            image, lang=lang_str, config=config, output_type=output_dict
        )

        spans: list[OcrSpan] = []
        words: list[str] = []
        confidences: list[float] = []

        n_boxes = len(data["text"])
        for i in range(n_boxes):
            text = (data["text"][i] or "").strip()
            conf_val = float(data["conf"][i])
            if text and conf_val >= 0:
                conf = min(max(conf_val / 100.0, 0.0), 1.0)
                words.append(text)
                confidences.append(conf)
                x = float(data["left"][i])
                y = float(data["top"][i])
                w = float(data["width"][i])
                h = float(data["height"][i])
                spans.append(OcrSpan(text=text, bbox=(x, y, x + w, y + h), confidence=conf))

        mean_conf = sum(confidences) / len(confidences) if confidences else 1.0
        full_text = " ".join(words)

        return OcrResult(
            text=full_text,
            confidence=mean_conf,
            spans=spans,
            engine_available=True,
            packs_used=list(packs or ["eng"]),
        )
    except Exception as exc:
        _logger.warning("ocr_execution_error", extra={"error": str(exc)})
        return OcrResult(
            text="",
            confidence=0.0,
            engine_available=True,
            packs_used=list(packs or []),
            error=f"ocr_execution_error:{exc}",
        )
