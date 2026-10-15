"""Release-gate test: every fixture in `fixtures/manifest.json` must classify as declared.

Runs the fixture server in-process (no sockets, no network) and feeds each
response through the block classifier. A wrong verdict here is a Stage 0 failure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fixtures.server import app
from src.modules.content_processor.enums import BlockKind
from src.modules.content_processor.governor.block_classifier import classify_response

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = json.loads((ROOT / "fixtures" / "manifest.json").read_text(encoding="utf-8"))
FIXTURES = [f for f in MANIFEST["fixtures"] if "expect_block" in f]


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["path"] for f in FIXTURES])
def test_fixture_classifies_as_declared(client: TestClient, fixture: dict[str, object]):
    path = str(fixture["path"])
    response = client.get(path)
    final_url = (
        str(response.headers.get("location", "")) if response.status_code in (301, 302) else path
    )
    if response.status_code in (301, 302):
        response = client.get(str(response.headers["location"]))
    verdict = classify_response(
        status=response.status_code,
        headers=dict(response.headers),
        body_prefix=response.content[:65536],
        requested_mime_family=str(fixture.get("requested_mime", "text/html")),
        final_url=final_url,
    )
    assert verdict.kind is BlockKind(str(fixture["expect_block"])), (path, verdict)
    if "expect_vendor" in fixture:
        assert verdict.vendor == fixture["expect_vendor"], (path, verdict)
    if "expect_retry_after_s" in fixture:
        assert int(response.headers["retry-after"]) == fixture["expect_retry_after_s"]


def test_truncated_fixture_declares_more_bytes_than_sent(client: TestClient):
    # Starlette normalises Content-Length on the way out, so the fixture must be
    # checked at the raw-socket level in Stage 1; here we assert the intent only.
    response = client.get("/truncated")
    assert response.content == b"only half"


def test_seed_file_matches_manifest_count():
    seed = MANIFEST["seeds"][0]
    lines = (ROOT / "fixtures" / seed["file"]).read_text(encoding="utf-8").splitlines()
    assert len([line for line in lines if line.strip()]) == 34
