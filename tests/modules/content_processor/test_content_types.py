"""Content-type section: catalogue, scope filter, command, read-only API, and Office detection."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.integrations.sniff import refine_zip_media_type, sniff_media_type
from src.modules.content_processor.content_types import (
    CONTENT_TYPES,
    catalogue,
    scope_fragment,
)
from src.modules.content_processor.webapi.app import create_app
from tests.modules.content_processor.office_builders import docx_full, pptx_full, xlsx_full

BASE = "http://127.0.0.1"


def _zip(**members: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name.replace("__", "/"), data)
    return buf.getvalue()


# -- catalogue -----------------------------------------------------------------------


def test_catalogue_ids_are_unique_and_every_type_has_a_status() -> None:
    ids = [t.id for t in catalogue()]
    assert len(ids) == len(set(ids))
    assert {t.status for t in CONTENT_TYPES} == {"supported", "not_supported"}


def test_office_and_pdf_are_supported_legacy_and_media_are_not() -> None:
    status = {t.id: t.status for t in catalogue()}
    assert status["pdf"] == status["word"] == status["excel"] == status["powerpoint"] == "supported"
    assert status["legacy_office"] == status["audio_video"] == status["archives"] == "not_supported"


# -- scope fragment --------------------------------------------------------------------


def test_selection_builds_the_media_filter_and_always_keeps_pages() -> None:
    frag = scope_fragment(["pdf"])
    assert frag.allowed_mime_families == ["text/html", "application/pdf"]
    assert frag.unsupported_selected == []


def test_word_selection_uses_the_office_media_type_the_sniffer_reports() -> None:
    frag = scope_fragment(["word"])
    sniffed = refine_zip_media_type(docx_full(), sniff_media_type(docx_full(), None))
    assert sniffed in frag.allowed_mime_families


def test_unsupported_selection_is_flagged_not_hidden() -> None:
    frag = scope_fragment(["pdf", "legacy_office", "audio_video"])
    assert frag.unsupported_selected == ["legacy_office", "audio_video"]
    assert "application/msword" in frag.allowed_mime_families


def test_command_names_the_scope_file_and_the_dry_run() -> None:
    frag = scope_fragment(["pdf"], scope_file="my scope.json")
    assert "cli dryrun --scope my scope.json" in frag.command


@pytest.mark.parametrize("selected", [[], ["nope"], ["pdf", "typo"]])
def test_empty_or_unknown_selection_is_rejected(selected: list[str]) -> None:
    with pytest.raises(ValueError):
        scope_fragment(selected)


# -- Office detection through the sniffer ---------------------------------------------------


def test_each_office_file_is_refined_to_its_own_media_type() -> None:
    assert "wordprocessingml" in refine_zip_media_type(docx_full(), "application/zip")
    assert "spreadsheetml" in refine_zip_media_type(xlsx_full(), "application/zip")
    assert "presentationml" in refine_zip_media_type(pptx_full(), "application/zip")


def test_a_plain_zip_stays_a_zip() -> None:
    body = _zip(**{"notes.txt": b"hello"})
    assert refine_zip_media_type(body, "application/zip") == "application/zip"


def test_refinement_leaves_other_types_alone() -> None:
    assert refine_zip_media_type(b"%PDF-1.4", "application/pdf") == "application/pdf"


# -- read-only API ----------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path), base_url=BASE)


def test_api_lists_the_catalogue(client: TestClient) -> None:
    body = client.get("/api/content-types").json()
    assert {t["id"] for t in body["types"]} == {t.id for t in CONTENT_TYPES}


def test_api_returns_scope_for_a_selection(client: TestClient) -> None:
    body = client.get("/api/content-types/scope", params={"types": "pdf,word"}).json()
    assert body["selected"] == ["pdf", "word"]
    assert "application/pdf" in body["allowed_mime_families"]


def test_api_rejects_unknown_types_with_422(client: TestClient) -> None:
    assert client.get("/api/content-types/scope", params={"types": "pdf,bogus"}).status_code == 422
    assert client.get("/api/content-types/scope", params={"types": ""}).status_code == 422


def test_api_is_read_only(client: TestClient) -> None:
    assert client.post("/api/content-types/scope", json={}).status_code in (404, 405)
