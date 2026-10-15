from __future__ import annotations

from src.modules.content_processor.adapters.html_source import extract_html_source
from src.modules.content_processor.enums import EntityStatus
from src.modules.content_processor.verify.reconcile import reconcile_source_vs_rendered

SOURCE = b"""<html><body><main><h1>Title</h1><p>Server paragraph.</p>
<p id="shell">Loading...</p></main><footer>Footer</footer></body></html>"""
RENDERED = b"""<html><body><main><h1>Title</h1><p>Server paragraph.</p>
<p>Hydrated content arrived.</p><p data-cp-hidden="1">Hidden by CSS</p></main>
<footer>Footer</footer></body></html>"""
INNER_TEXT = "Title\nServer paragraph.\nHydrated content arrived.\nFooter"


def _blocks(html: bytes, state: str):
    return extract_html_source(
        html, base_url="http://t/", evidence_id="e", resource_id="r", state_id=state
    ).blocks


def test_reconcile_marks_verified_hydrated_and_source_only():
    src = _blocks(SOURCE, "run|source")
    ren = _blocks(RENDERED, "run|rendered")
    updated, result = reconcile_source_vs_rendered(src, ren, INNER_TEXT)
    verified = {b.normalized_text for b in updated if b.status is EntityStatus.VERIFIED_EXTRACTED}
    assert {"Title", "Server paragraph.", "Hydrated content arrived.", "Footer"} <= verified
    assert result.fully_verified
    assert result.rendered_only_keys == 1  # hydrated paragraph
    assert result.source_only_keys == 1  # the "Loading..." shell text
    hidden = [b for b in updated if b.layer.value == "hidden"]
    assert hidden and hidden[0].normalized_text == "Hidden by CSS"
    assert (
        hidden[0].status is EntityStatus.EXTRACTED_UNVERIFIED
    )  # hidden blocks are not verified as visible


def test_reconcile_flags_unverified_when_text_is_not_visible():
    ren = _blocks(RENDERED, "run|rendered")
    updated, result = reconcile_source_vs_rendered([], ren, "Title\nFooter")
    assert not result.fully_verified
    assert result.unverified_blocks == 2
    unverified = {
        b.normalized_text
        for b in updated
        if b.status is EntityStatus.EXTRACTED_UNVERIFIED
        and b.layer.value != "hidden"
        and b.layer.value != "metadata"
    }
    assert {"Server paragraph.", "Hydrated content arrived."} <= unverified


def test_reconcile_is_whitespace_and_case_tolerant():
    ren = _blocks(
        b"<html><body><main><p>Annual  Plans Only</p></main></body></html>", "run|rendered"
    )
    _, result = reconcile_source_vs_rendered([], ren, "annual plans only")
    assert result.fully_verified
