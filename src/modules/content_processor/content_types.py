"""Content types a run can be told to look for: what the engine reads, and what it does not yet.

The analyst UI's content-type section is generated from this catalogue. Each type
maps to the media types the scope filters on (`RunScope.allowed_mime_families`)
and carries an honest status:

- `supported`: the engine extracts it (text, tables, or OCR text).
- `not_supported`: found files are recorded as not supported, with the reason, so they stay in
  the report's denominator and are never silently dropped.

Web pages are always read, whatever is selected, because pages are how links to
the other types are found. Selecting a type limits which linked files are
fetched and extracted; it does not hide the pages that link to them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = ["CONTENT_TYPES", "ContentType", "ScopeFragment", "catalogue", "scope_fragment"]

Status = Literal["supported", "not_supported"]


class ContentType(StrictModel):
    """One selectable content type."""

    id: str
    label: str
    families: list[str] = Field(min_length=1)
    extensions: list[str]
    status: Status
    note: str


class ScopeFragment(StrictModel):
    """What a selection means for a run: the scope filter and the command to run it."""

    selected: list[str]
    allowed_mime_families: list[str]
    unsupported_selected: list[str]
    command: str


CONTENT_TYPES: tuple[ContentType, ...] = (
    ContentType(
        id="html",
        label="Web pages",
        families=["text/html"],
        extensions=[".html", ".htm"],
        status="supported",
        note="Read, opened in a browser and explored. Always read: pages are how links are found.",
    ),
    ContentType(
        id="pdf",
        label="PDF documents",
        families=["application/pdf"],
        extensions=[".pdf"],
        status="supported",
        note="Text, tables, form fields and annotations. Scanned pages are read by OCR.",
    ),
    ContentType(
        id="images",
        label="Images (PNG, JPEG, GIF, WebP, TIFF, BMP)",
        families=["image/*"],
        extensions=[".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp"],
        status="supported",
        note="Text in images is read by OCR when the OCR engine is installed.",
    ),
    ContentType(
        id="word",
        label="Word documents (.docx)",
        families=["application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
        extensions=[".docx"],
        status="supported",
        note=(
            "Paragraphs, tables, headers, comments. Tracked deletions and hidden text "
            "are kept apart."
        ),
    ),
    ContentType(
        id="excel",
        label="Excel workbooks (.xlsx)",
        families=["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
        extensions=[".xlsx"],
        status="supported",
        note="Each sheet as a table. Hidden sheets are kept apart.",
    ),
    ContentType(
        id="powerpoint",
        label="PowerPoint presentations (.pptx)",
        families=["application/vnd.openxmlformats-officedocument.presentationml.presentation"],
        extensions=[".pptx"],
        status="supported",
        note="Slide text, tables, speaker notes and image descriptions.",
    ),
    ContentType(
        id="legacy_office",
        label="Older Office files (.doc, .xls, .ppt)",
        families=[
            "application/msword",
            "application/vnd.ms-excel",
            "application/vnd.ms-powerpoint",
        ],
        extensions=[".doc", ".xls", ".ppt"],
        status="not_supported",
        note=(
            "Needs LibreOffice, which is not installed. Found files are recorded as not "
            "supported, with the reason."
        ),
    ),
    ContentType(
        id="csv",
        label="Spreadsheet text files (.csv)",
        families=["text/csv"],
        extensions=[".csv"],
        status="not_supported",
        note="Not extracted yet. Found files are recorded as not supported, with the reason.",
    ),
    ContentType(
        id="archives",
        label="Zip archives",
        families=["application/zip"],
        extensions=[".zip"],
        status="not_supported",
        note=(
            "Archive contents are not opened yet. Found files are recorded as not "
            "supported, with the reason."
        ),
    ),
    ContentType(
        id="audio_video",
        label="Audio and video",
        families=["audio/*", "video/*"],
        extensions=[".mp3", ".wav", ".mp4", ".webm", ".mov"],
        status="not_supported",
        note=(
            "Captions, speech and frame reading are not built yet. Found files are "
            "recorded as not supported, with the reason."
        ),
    ),
    ContentType(
        id="email",
        label="Email files (.eml, .msg)",
        families=["message/rfc822"],
        extensions=[".eml", ".msg"],
        status="not_supported",
        note="Not extracted yet. Found files are recorded as not supported, with the reason.",
    ),
)

_BY_ID = {t.id: t for t in CONTENT_TYPES}
_ALWAYS = "text/html"


def catalogue() -> list[ContentType]:
    """All selectable content types, in display order."""
    return list(CONTENT_TYPES)


def scope_fragment(selected: list[str], *, scope_file: str = "scope.json") -> ScopeFragment:
    """The scope filter and the command for a selection of content-type ids.

    Raises ValueError for an unknown id or an empty selection, so a typo can never
    silently mean "everything".
    """
    if not selected:
        raise ValueError("select at least one content type")
    unknown = [s for s in selected if s not in _BY_ID]
    if unknown:
        raise ValueError(f"unknown content type(s): {', '.join(sorted(unknown))}")
    ordered = [t.id for t in CONTENT_TYPES if t.id in set(selected)]
    families: list[str] = [_ALWAYS]
    for type_id in ordered:
        for family in _BY_ID[type_id].families:
            if family not in families:
                families.append(family)
    unsupported = [i for i in ordered if _BY_ID[i].status == "not_supported"]
    return ScopeFragment(
        selected=ordered,
        allowed_mime_families=families,
        unsupported_selected=unsupported,
        command=(
            f"python -m src.modules.content_processor.cli dryrun --scope {scope_file} --out out"
        ),
    )
