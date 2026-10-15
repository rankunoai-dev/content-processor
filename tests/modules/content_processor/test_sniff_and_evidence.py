from __future__ import annotations

from pathlib import Path

import pytest

from src.integrations.evidence_store import LocalEvidenceStore
from src.integrations.sniff import parse_content_disposition_name, sniff_media_type


@pytest.mark.parametrize(
    ("body", "declared", "expected"),
    [
        (b"%PDF-1.7 ...", "text/html", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n....", None, "image/png"),
        (b"\xff\xd8\xff\xe0", None, "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", None, "image/webp"),
        (b"PK\x03\x04", "application/vnd.openxmlformats", "application/zip"),
        (b"<!DOCTYPE html><html>", "application/pdf", "text/html"),
        (b"  <html><body>", None, "text/html"),
        (b'<?xml version="1.0"?><svg xmlns="x"></svg>', None, "image/svg+xml"),
        (b'<?xml version="1.0"?><root/>', None, "application/xml"),
        (b'{"a": 1}', "application/json", "application/json"),
        (b"plain words", "text/plain", "text/plain"),
        (b"\x00\x01\x02\xff\xfe", None, "application/octet-stream"),
        (b"", "text/html", "application/x-empty"),
    ],
)
def test_sniff(body: bytes, declared: str | None, expected: str):
    assert sniff_media_type(body, declared) == expected


def test_content_disposition_name():
    assert parse_content_disposition_name('attachment; filename="report.pdf"') == "report.pdf"
    assert parse_content_disposition_name("inline") is None
    assert parse_content_disposition_name(None) is None


def test_evidence_store_is_content_addressed_and_immutable(tmp_path: Path):
    store = LocalEvidenceStore(tmp_path, "tenant-a")
    ref1 = store.put(b"hello", "text/plain")
    ref2 = store.put(b"hello", "text/plain")
    assert ref1.sha256 == ref2.sha256
    assert ref1.reused is False and ref2.reused is True
    assert store.exists(ref1.sha256)
    assert store.get(ref1.sha256) == b"hello"
    assert ref1.uri.startswith("file:")
    other = LocalEvidenceStore(tmp_path, "tenant-b")
    assert not other.exists(ref1.sha256), "tenants must not share evidence"
