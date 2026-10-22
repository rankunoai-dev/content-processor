"""Site utilities (sign-in, account, country/language selectors, web chat) are chrome."""

from __future__ import annotations

from pathlib import Path

from src.modules.content_processor.adapters.html_source import extract_html_source
from src.modules.content_processor.analysis.overlap import OverlapConfig, docs_from_records
from src.modules.content_processor.enums import ContentLayer
from src.modules.content_processor.export.content import whole_content
from tests.modules.content_processor.l5_factory import record

PAGE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "pages" / "site_utilities.html"
).read_bytes()


def _blocks():
    return extract_html_source(
        PAGE, base_url="https://x.example/", evidence_id="e", resource_id="r"
    ).blocks


def test_utilities_are_chrome_and_chat_is_third_party() -> None:
    by_text = {b.normalized_text: b for b in _blocks()}
    assert by_text["Austria"].role == "chrome"  # a dropdown choice, not content
    assert by_text["United States"].role == "chrome"
    assert by_text["I'm an individual I'm a financial professional"].role == "chrome"
    assert (
        by_text["Hi, I am the virtual assistant. Need help?"].layer
        is ContentLayer.THIRD_PARTY_CHROME
    )
    assert (
        by_text["The fund declared its monthly distribution of $0.0825 per share."].role == "main"
    )


def test_utilities_stay_out_of_the_article_text_and_matching() -> None:
    blocks = _blocks()
    main_text = whole_content(blocks, include_boilerplate=False)
    assert "monthly distribution" in main_text
    for gone in ("Austria", "I'm an individual", "virtual assistant"):
        assert gone not in main_text
    rec = record("https://x.example/notice", blocks=blocks)
    (doc,) = docs_from_records([rec], OverlapConfig())[0]
    assert "Austria" not in doc.text and "virtual assistant" not in doc.text
    # the full content keeps the utilities after the page content, without the chat
    full = whole_content(blocks)
    assert "Austria" in full and "virtual assistant" not in full


def test_bare_hidden_attribute_is_hidden_in_the_source_channel() -> None:
    page = (
        b"<html><body><main><p>Shown.</p><div hidden><p>Closed answer.</p></div>"
        b'<div hidden=""><p>Empty value.</p></div>'
        b'<div hidden="until-found"><p>Found on search.</p></div></main></body></html>'
    )
    layers = {
        b.normalized_text: b.layer
        for b in extract_html_source(
            page, base_url="https://x.example/", evidence_id="e", resource_id="r"
        ).blocks
    }
    assert layers["Shown."] is ContentLayer.RENDERED_VISIBLE
    assert layers["Closed answer."] is ContentLayer.HIDDEN
    assert layers["Empty value."] is ContentLayer.HIDDEN
    assert layers["Found on search."] is ContentLayer.COLLAPSED_PUBLISHED
