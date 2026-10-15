"""PDF structures beyond running text: forms, annotations, attachments, layers, ghost text.

Everything here takes PyMuPDF objects typed as Any (the library ships no usable
type information) and returns plain data, so `pdf.py` owns block construction.
Cases covered: PDF forms, annotations, embedded files, optional-content layers,
and invisible (render mode 3) text such as a stale OCR layer.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "PdfAnnotation",
    "PdfAttachment",
    "PdfFormField",
    "annotations_of",
    "attachments_of",
    "form_fields_of",
    "hidden_layer_names",
    "invisible_text_of",
]

# Annotation kinds that carry no reader-visible content of their own.
_SKIP_ANNOT_KINDS = {"Link", "Popup", "Widget"}


class PdfFormField(StrictModel):
    """One AcroForm field with its current value."""

    name: str
    field_type: str
    value: str
    bbox: tuple[float, float, float, float]


class PdfAnnotation(StrictModel):
    """A comment, note or markup annotation with text content."""

    kind: str
    content: str
    bbox: tuple[float, float, float, float]


class PdfAttachment(StrictModel):
    """An embedded file. Bytes are kept so the caller can schedule it as a child resource."""

    name: str
    size: int = Field(ge=0)
    description: str = ""
    data: bytes = b""


def _rect(r: Any) -> tuple[float, float, float, float]:
    return (float(r[0]), float(r[1]), float(r[2]), float(r[3]))


def form_fields_of(page: Any) -> list[PdfFormField]:
    """Form widgets on a page that currently hold a value."""
    fields: list[PdfFormField] = []
    for widget in page.widgets() or []:
        value = widget.field_value
        text = "" if value in (None, False, "Off") else str(value).strip()
        if text:
            fields.append(
                PdfFormField(
                    name=str(widget.field_name or ""),
                    field_type=str(widget.field_type_string or ""),
                    value=text,
                    bbox=_rect(widget.rect),
                )
            )
    return fields


def annotations_of(page: Any) -> list[PdfAnnotation]:
    """Annotations with non-empty text content, excluding links and widgets."""
    found: list[PdfAnnotation] = []
    for annot in page.annots() or []:
        kind = str(annot.type[1])
        content = str((annot.info or {}).get("content") or "").strip()
        if kind in _SKIP_ANNOT_KINDS or not content:
            continue
        found.append(PdfAnnotation(kind=kind, content=content, bbox=_rect(annot.rect)))
    return found


def attachments_of(doc: Any, *, max_bytes: int = 25 * 1024 * 1024) -> list[PdfAttachment]:
    """Embedded files; payloads over `max_bytes` are listed without their bytes."""
    out: list[PdfAttachment] = []
    for name in doc.embfile_names() or []:
        info = doc.embfile_info(name)
        size = int(info.get("size", 0))
        data = bytes(doc.embfile_get(name)) if size <= max_bytes else b""
        out.append(
            PdfAttachment(
                name=str(name), size=size, description=str(info.get("description") or ""), data=data
            )
        )
    return out


def invisible_text_of(page: Any) -> str:
    """Whitespace-normalised text drawn in render mode 3 (not painted), e.g. a ghost OCR layer."""
    parts: list[str] = []
    for span in page.get_texttrace() or []:
        if span.get("type") == 3:
            parts.append("".join(chr(c[0]) for c in span.get("chars", ())))
    return " ".join(" ".join(parts).split())


def hidden_layer_names(doc: Any) -> list[str]:
    """Names of optional-content layers that are switched off by default."""
    try:
        layers = doc.get_ocgs() or {}
    except Exception:  # noqa: BLE001 - some malformed files raise on the OCG catalogue
        return []
    return [str(v.get("name", "")) for v in layers.values() if not v.get("on", True)]
