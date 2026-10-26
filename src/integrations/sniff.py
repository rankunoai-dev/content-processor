"""Payload type sniffing (case 106): the bytes decide, headers and names are recorded.

A small magic-byte table is enough for the formats in scope. Anything unknown
is reported as `application/octet-stream` so the adapter registry marks it
UNSUPPORTED rather than guessing.
"""

from __future__ import annotations

import io
import re
import zipfile
from email.message import Message

__all__ = [
    "OOXML_MEDIA_TYPES",
    "parse_content_disposition_name",
    "refine_zip_media_type",
    "sniff_media_type",
]

# Office Open XML files are zip containers. The magic bytes say "zip"; the part names say which
# Office format it is. These are the media types the content-type section filters on.
OOXML_MEDIA_TYPES: dict[str, str] = {
    "word/document.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xl/workbook.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt/presentation.xml": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
}

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
    (b"%!PS", "application/postscript"),
    (b"{\\rtf", "application/rtf"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage"),
)
_HTML_RE = re.compile(rb"^\s*(<!doctype\s+html|<html|<head|<body|<!--)", re.IGNORECASE)
_XML_RE = re.compile(rb"^\s*<\?xml")
_SVG_RE = re.compile(rb"<svg[\s>]", re.IGNORECASE)
_JSON_RE = re.compile(rb"^\s*[\[{]")


def sniff_media_type(body: bytes, declared: str | None = None) -> str:
    """Return the media type implied by the bytes.

    `declared` only breaks ties between text-like candidates (for example a
    declared `text/plain` body that happens to start with `{`).
    """
    head = body[:4096]
    if not head:
        return "application/x-empty"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:12] in (b"ftypavif", b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "image/avif" if head[8:12] == b"avif" else "image/heic"
    for magic, media_type in _MAGIC:
        if head.startswith(magic):
            return media_type
    if _XML_RE.match(head):
        return "image/svg+xml" if _SVG_RE.search(head) else "application/xml"
    if _HTML_RE.match(head):
        return "text/html"
    if _SVG_RE.search(head[:512]):
        return "image/svg+xml"
    declared_main = (declared or "").split(";", 1)[0].strip().lower()
    if _JSON_RE.match(head) and declared_main in ("application/json", "", "text/json"):
        return "application/json"
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    if declared_main.startswith("text/") and declared_main != "text/html":
        return declared_main
    return "text/plain"


def parse_content_disposition_name(header: str | None) -> str | None:
    """Extract the filename from a Content-Disposition header, if any."""
    if not header:
        return None
    msg = Message()
    msg["content-disposition"] = header
    name = msg.get_filename()
    return name or None


def refine_zip_media_type(body: bytes, sniffed: str) -> str:
    """Replace a generic `application/zip` with the Office type when the container holds one.

    Needs the whole body (the zip directory sits at the end of the file), so it runs where
    the full payload is in memory, not on the 4 KB sniffing head. Plain zips stay zip.
    """
    if sniffed != "application/zip":
        return sniffed
    try:
        names = set(zipfile.ZipFile(io.BytesIO(body)).namelist())
    except zipfile.BadZipFile:
        return sniffed
    for part, media_type in OOXML_MEDIA_TYPES.items():
        if part in names:
            return media_type
    return sniffed
