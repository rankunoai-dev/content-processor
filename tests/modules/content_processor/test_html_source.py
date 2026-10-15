"""Source-channel extraction against the fixture pages (false-positive traps included)."""

from __future__ import annotations

from pathlib import Path

from src.modules.content_processor.adapters.html_source import (
    SourceExtraction,
    extract_html_source,
    normalize_text,
)
from src.modules.content_processor.enums import ContentLayer
from src.modules.content_processor.export.content import whole_content

PAGES = Path(__file__).resolve().parents[3] / "fixtures" / "pages"


def _extract(name: str) -> SourceExtraction:
    return extract_html_source(
        (PAGES / f"{name}.html").read_bytes(),
        base_url=f"http://testserver/pages/{name}",
        evidence_id="ev-1",
        resource_id="res-1",
    )


def _texts(ex: SourceExtraction, layer: ContentLayer) -> list[str]:
    return [b.normalized_text for b in ex.blocks if b.layer is layer]


def test_static_page_blocks_roles_and_headings():
    ex = _extract("static_ok")
    main = [b for b in ex.blocks if b.role == "main" and b.layer is ContentLayer.RENDERED_VISIBLE]
    texts = [b.normalized_text for b in main]
    assert "Eligibility applies only to annual plans." in texts
    assert {"Plan", "Fee", "Annual", "$120"} <= set(texts)
    elig = next(b for b in main if b.normalized_text.startswith("Eligibility applies"))
    assert elig.heading_path == ["Product", "Eligibility"]
    nav = [b.normalized_text for b in ex.blocks if b.role == "nav"]
    assert nav == ["Home · Products · Help"]
    footer = [b.normalized_text for b in ex.blocks if b.role == "footer"]
    assert footer == ["© Example"]
    assert ex.metadata["title"] == "Product" and ex.metadata["lang"] == "en"
    assert "Product" in _texts(ex, ContentLayer.METADATA)
    assert not ex.placeholder_suspected and not ex.needs_render_hint
    assert len(ex.template_fingerprint) == 24


def test_icon_font_text_is_not_content():
    ex = _extract("icon_font")
    visible = " ".join(_texts(ex, ContentLayer.RENDERED_VISIBLE))
    assert "arrow_forward" not in visible and "menu" not in visible.split()
    assert "Open the statement to see your balance." in visible
    assert set(_texts(ex, ContentLayer.ICON_ARTIFACT)) == {"arrow_forward", "menu"}
    assert "Open the statement" in _texts(ex, ContentLayer.ACCESSIBILITY_ONLY)
    assert "arrow_forward" not in whole_content(ex.blocks)


def test_template_noscript_state_and_comments_are_source_only():
    ex = _extract("template_hidden")
    assert _texts(ex, ContentLayer.RENDERED_VISIBLE) == ["Visible paragraph."]
    source = " ".join(_texts(ex, ContentLayer.SOURCE_ONLY))
    assert "TEMPLATE-ONLY STRING" in source
    assert "SOURCE-ONLY STRING" in source
    assert "Enable JavaScript" in source
    assert "HTML comment that is not content" in source
    assert "TEMPLATE-ONLY" not in whole_content(ex.blocks)


def test_soft_hyphen_is_stripped_reversibly():
    ex = _extract("soft_hyphen")
    block = next(b for b in ex.blocks if b.layer is ContentLayer.RENDERED_VISIBLE)
    assert "Versicherungsbedingungen" in block.normalized_text
    assert "­" in block.raw_text
    assert "strip_soft_hyphen" in block.normalization_ops
    assert normalize_text("a­b  c")[0] == "ab c"


def test_third_party_widgets_are_kept_out_of_the_corpus():
    ex = _extract("third_party_widget")
    assert _texts(ex, ContentLayer.RENDERED_VISIBLE) == ["Main article body."]
    chrome = " ".join(_texts(ex, ContentLayer.THIRD_PARTY_CHROME))
    assert "Chat with us now!" in chrome and "We use cookies." in chrome
    assert "Chat with us" not in whole_content(ex.blocks)


def test_honeypot_links_are_marked_hidden_and_never_followed():
    ex = _extract("honeypot")
    by_url = {link.url: link for link in ex.links}
    assert by_url["http://testserver/trap/do-not-click"].hidden is True
    assert by_url["http://testserver/pages/static_ok"].hidden is False
    assert "hidden trap" in " ".join(_texts(ex, ContentLayer.HIDDEN))
    assert "hidden trap" not in whole_content(ex.blocks)


def test_collapsed_details_are_published_but_separate():
    ex = _extract("details_collapsed")
    assert "Is the fee refundable?" in _texts(ex, ContentLayer.RENDERED_VISIBLE)
    collapsed = _texts(ex, ContentLayer.COLLAPSED_PUBLISHED)
    assert "Collapsed but published answer." in collapsed
    assert "Find-in-page revealable note." in collapsed
    text = whole_content(ex.blocks)
    assert "Collapsed but published answer." in text
    assert "# FAQ" in text


def test_delayed_hydration_shell_hints_render():
    ex = _extract("delayed_hydration")
    assert _texts(ex, ContentLayer.RENDERED_VISIBLE) == ["Loading…"]
    assert ex.needs_render_hint is True


def test_placeholder_content_is_flagged():
    ex = extract_html_source(
        b"<html><body><main><h1>Lorem ipsum dolor sit amet</h1><p>x</p></main></body></html>",
        base_url="http://t/",
        evidence_id="e",
        resource_id="r",
    )
    assert ex.placeholder_suspected


def test_links_assets_and_action_detection():
    html = b"""<html><body><main>
    <a href="/logout">Log out</a><a href="/docs/a.pdf" rel="nofollow">PDF</a>
    <img src="/i.png" alt="Chart of fees"><iframe src="https://www.youtube.com/embed/x"></iframe>
    <script src="/app.js"></script><script type="application/ld+json">{"@type":"Thing"}</script>
    </main></body></html>"""
    ex = extract_html_source(html, base_url="http://t/page", evidence_id="e", resource_id="r")
    links = {link.url: link for link in ex.links}
    assert links["http://t/logout"].action_like and not links["http://t/docs/a.pdf"].action_like
    assert links["http://t/docs/a.pdf"].rel == "nofollow"
    kinds = {(a.kind, a.url) for a in ex.assets}
    assert ("image", "http://t/i.png") in kinds
    assert ("frame", "https://www.youtube.com/embed/x") in kinds
    assert ("script", "http://t/app.js") in kinds
    assert ex.structured_data == ['{"@type":"Thing"}']
    assert "Chart of fees" in _texts(ex, ContentLayer.ACCESSIBILITY_ONLY)


def test_duplicate_blocks_are_linked_not_dropped():
    html = b"<html><body><main><p>Same text</p><p>Same text</p></main></body></html>"
    ex = extract_html_source(html, base_url="http://t/", evidence_id="e", resource_id="r")
    visible = [b for b in ex.blocks if b.layer is ContentLayer.RENDERED_VISIBLE]
    assert len(visible) == 2 and visible[1].dedupe_of == visible[0].block_id
    assert whole_content(ex.blocks) == "Same text"
    assert whole_content(ex.blocks, dedupe=False).count("Same text") == 2
