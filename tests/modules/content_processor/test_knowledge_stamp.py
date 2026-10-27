"""RFC 0002 step 1: page stamps, menu trees, JSON-LD facts and context-preserving chunks."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from selectolax.parser import HTMLParser

from src.integrations.evidence_store import LocalEvidenceStore
from src.modules.content_processor import cli
from src.modules.content_processor.adapters.html_source import DiscoveredLink
from src.modules.content_processor.analysis.claims import quantities_in, split_sentences
from src.modules.content_processor.contracts import ContentBlock, UrlRecord
from src.modules.content_processor.enums import ContentLayer, GapType, UiRegion, UrlBadge
from src.modules.content_processor.knowledge.chunking import (
    HARD_CAP_TOKENS,
    MIN_TOKENS,
    TARGET_TOKENS,
    PageContext,
    chunk_blocks,
    main_corpus,
)
from src.modules.content_processor.knowledge.html_facts import (
    ParsedMenu,
    dom_links,
    is_asset_url,
    is_tracking_url,
    menu_id,
    merge_menus,
    page_ctas,
    parse_menus,
    schema_facts,
)
from src.modules.content_processor.knowledge.models import MenuItem
from src.modules.content_processor.knowledge.stamp import page_key, reading_ease, stamp_records
from tests.modules.content_processor.l5_factory import block, gap, record, words

# -- helpers -------------------------------------------------------------------------


def blk(
    rid: str,
    order: int,
    text: str,
    *,
    heading: list[str] | None = None,
    region: UiRegion | None = UiRegion.MAIN_CONTENT,
    role: str = "main",
    channel: str = "rendered",
    layer: ContentLayer = ContentLayer.RENDERED_VISIBLE,
    dedupe_of: str | None = None,
    evidence: str | None = None,
) -> ContentBlock:
    base = block(rid, order, text, role=role, layer=layer, heading=heading, dedupe_of=dedupe_of)  # type: ignore[arg-type]
    return base.model_copy(
        update={
            "block_id": f"{rid}:{channel}:{order}",
            "state_id": f"run|{channel}",
            "ui_region": region,
            "evidence_ids": [f"evidence:{evidence or rid}"],
        }
    )


def ctx(
    title: str | None = "Term life", summary: str | None = "Cover for a set term."
) -> PageContext:
    return PageContext(
        run_id="run-l5",
        url="https://example.com/term",
        page_key="pg-1",
        site="Example Life",
        title=title,
        summary=summary,
    )


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- JSON-LD ---------------------------------------------------------------------------


def test_schema_facts_reads_graph_services_offers_faq_breadcrumbs_and_dates() -> None:
    graph = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "Organization", "name": "Example Life"},
            {"@type": ["Service", "FinancialProduct"], "name": "Term life insurance"},
            {
                "@type": "Product",
                "name": "Plan A",
                "offers": {"@type": "Offer", "price": "49", "priceCurrency": "USD"},
            },
            {
                "@type": "FAQPage",
                "mainEntity": [{"@type": "Question", "name": "How long is the term?"}],
            },
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 2, "name": "Life"},
                    {"@type": "ListItem", "position": 1, "item": {"name": "Home"}},
                ],
            },
            {
                "@type": "Article",
                "headline": "Why term?",
                "datePublished": "2024-04-30",
                "dateModified": "2025-01-02",
            },
        ],
    }
    facts = schema_facts(["<!-- " + json.dumps(graph) + " -->", "{not json"])
    assert facts.organization == "Example Life"
    assert facts.services == ["Term life insurance"]
    assert facts.products == ["Plan A"]
    assert facts.offers == ["offer: 49 USD"]
    assert facts.faq_questions == ["How long is the term?"]
    assert facts.breadcrumbs == ["Home", "Life"]
    assert facts.headline == "Why term?"
    assert (facts.date_published, facts.date_modified) == ("2024-04-30", "2025-01-02")
    assert {"Organization", "Service", "FinancialProduct", "FAQPage"} <= set(facts.types)
    assert facts.unreadable_blocks == 1


# -- menus -----------------------------------------------------------------------------

MEGA = """
<html><body>
<header>
  <nav class="desktop"><ul>
    <li><a href="/individual">Individuals</a></li>
    <li><button aria-controls="p1">Life insurance</button>
      <div id="p1" role="dialog">
        <ul><li>Products &amp; services
          <ul>
            <li><a href="/life/term">Term life <svg><title>icon</title></svg></a></li>
            <li><a href="/calc" target="_blank">Calculator (opens in a new tab)</a></li>
          </ul></li></ul>
      </div>
    </li>
  </ul></nav>
  <nav class="mobile"><ul><li><a href="/individual">Individuals</a></li></ul></nav>
</header>
<nav aria-label="Breadcrumbs"><a href="/">Home</a> <a href="/life">Life</a></nav>
<div id="onetrust-consent"><nav><ul><li>Strictly necessary cookies</li></ul></nav></div>
<main><a href="/quote">Get a quote</a> <a href="/more">Learn more</a>
  <a href="/x" hidden>Apply now</a></main>
<footer><a href="/privacy">Privacy</a><a href="/contact">Contact us</a></footer>
</body></html>
"""


def _labels(items: list[MenuItem]) -> list[object]:
    return [(i.label, _labels(i.children)) if i.children else i.label for i in items]


def test_parse_menus_builds_mega_menu_tree_and_skips_consent() -> None:
    menus = {m.kind: m for m in parse_menus(HTMLParser(MEGA), "https://example.com/")}
    assert set(menus) == {"header", "breadcrumb", "footer"}
    header = menus["header"]
    # The panel's lists sit under the tab that opens it; the mobile copy adds nothing.
    assert _labels(header.items) == [
        "Individuals",
        ("Life insurance", [("Products & services", ["Term life", "Calculator"])]),
    ]
    term = header.items[1].children[0].children[0]
    assert term.href == "https://example.com/life/term"
    assert [i.label for i in menus["breadcrumb"].items] == ["Home", "Life"]
    assert [i.label for i in menus["footer"].items] == ["Privacy", "Contact us"]
    assert header.item_count == 5


def test_merge_menus_unites_states_and_tab_without_link() -> None:
    a = ParsedMenu("header", "", [MenuItem(label="Life", children=[MenuItem(label="Term")])])
    b = ParsedMenu(
        "header",
        "",
        [
            MenuItem(label="Life", href="https://e.com/life", children=[MenuItem(label="Whole")]),
            MenuItem(label="Annuities"),
        ],
    )
    (merged,) = merge_menus([a, b])
    assert _labels(merged.items) == [("Life", ["Term", "Whole"]), "Annuities"]
    assert merged.items[0].href == "https://e.com/life"
    assert a.items[0].children == [MenuItem(label="Term")]  # inputs untouched
    assert menu_id(merged) == menu_id(a)


def test_dom_links_leave_out_chrome_and_ctas_rank_strong_first() -> None:
    links = dom_links(HTMLParser(MEGA), "https://example.com/")
    assert [link.text for link in links] == ["Get a quote", "Learn more", "Apply now"]
    assert links[-1].hidden
    ctas = page_ctas(
        [
            *links,
            DiscoveredLink(url="https://example.com/more", text="Learn more"),
            DiscoveredLink(url="https://example.com/long", text="Contact " + "x" * 80),
            DiscoveredLink(url="https://example.com/n", text="Our history"),
        ]
    )
    assert [(c.text, c.strength) for c in ctas] == [
        ("Get a quote", "strong"),
        ("Learn more", "soft"),
    ]


def test_asset_and_tracking_urls() -> None:
    assert is_asset_url("https://e.com/logo.png", None)
    assert is_asset_url("https://e.com/adobe/dynamicmedia/deliver/x", "image/jpeg")
    assert is_asset_url("https://e.com/content/dam/hero", None)
    assert not is_asset_url("https://e.com/content/dam/brochure.pdf", "application/pdf")
    assert not is_asset_url("https://e.com/content/dam/page", "text/html")
    assert not is_asset_url("https://e.com/life/term", "text/html")
    assert is_tracking_url("https://e.com/akam/13/pixel_35f2?a=1")
    assert not is_tracking_url("https://e.com/life/term")


# -- chunking --------------------------------------------------------------------------


def test_main_corpus_keeps_content_and_drops_chrome_copies_and_metadata() -> None:
    rid = "r"
    blocks = [
        blk(rid, 0, "Menu", role="nav", region=UiRegion.NAV_MENU),
        blk(rid, 1, "Country list", role="unknown", region=UiRegion.POPOVER),
        blk(rid, 2, "Body text here", heading=["Term"]),
        blk(rid, 3, "Body text here", heading=["Term"], dedupe_of="r:rendered:2"),
        blk(rid, 4, "meta", layer=ContentLayer.METADATA),
        blk(rid, 5, "Terms apply.", role="legal", region=UiRegion.MAIN_CONTENT),
        blk(rid, 6, "Explored panel", channel="state2", region=UiRegion.TAB_PANEL, role="unknown"),
        blk(rid, 7, "Footer", role="main", region=UiRegion.FOOTER),
        blk(rid, 1, "Base view after", region=UiRegion.MAIN_CONTENT),
    ]
    picked = [b.normalized_text for b in main_corpus(blocks)]
    assert picked == ["Base view after", "Body text here", "Terms apply.", "Explored panel"]


def test_chunks_keep_context_links_and_verbatim_text() -> None:
    rid = "r"
    blocks = [
        blk(rid, 0, "By Example Life"),  # before any heading: joins the first section
        blk(rid, 1, "Term life", heading=["Term life"]),
        blk(rid, 2, words(1, 120), heading=["Term life", "How it works"]),
        blk(rid, 3, words(2, 120), heading=["Costs"]),
    ]
    chunks = chunk_blocks(ctx(), blocks)
    assert len(chunks) == 2
    first, second = chunks
    assert first.text.startswith("By Example Life\nTerm life\n")
    assert first.heading_path == ["Term life"]
    # The page title is not repeated when the heading is the same.
    assert first.context_header == "Example Life › Term life\nPage summary: Cover for a set term."
    assert first.embed_text.endswith(first.text)
    assert first.embed_text.startswith(first.context_header)
    assert second.context_header.startswith("Example Life › Term life › Costs")
    assert (first.prev_chunk_id, first.next_chunk_id) == (None, second.chunk_id)
    assert second.prev_chunk_id == first.chunk_id
    assert first.parent_section_id != second.parent_section_id
    assert first.block_ids == ["r:rendered:0", "r:rendered:1", "r:rendered:2"]
    assert first.evidence_ids == ["evidence:r"]
    assert first.ui_regions == ["main_content"]
    # Stable ids: the same input gives the same chunks.
    assert [c.chunk_id for c in chunk_blocks(ctx(), blocks)] == [c.chunk_id for c in chunks]


def test_long_section_splits_between_sentences_with_overlap() -> None:
    sentences = [f"Sentence {i} " + words(i, 40) + "." for i in range(30)]
    blocks = [blk("r", 0, " ".join(sentences), heading=["Guide"])]
    chunks = chunk_blocks(ctx(), blocks)
    assert len(chunks) > 1
    assert all(c.token_estimate <= TARGET_TOKENS for c in chunks)
    joined = " ".join(c.text for c in chunks)
    assert joined == " ".join(sentences)  # nothing lost, nothing cut inside a sentence
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        last = prev.text[split_sentences(prev.text)[-1][0] :]
        assert nxt.overlap_before == last
        assert "… " + last in nxt.embed_text
        assert nxt.parent_section_id == prev.parent_section_id


def test_tables_stay_whole_and_oversized_units_are_flagged() -> None:
    cells = [
        blk("r", i, f"cell {i} value", heading=["Rates"], region=UiRegion.TABLE) for i in range(40)
    ]
    intro = blk("r", 100, "Rates as of today.", heading=["Rates"])
    (table,) = chunk_blocks(ctx(), [intro, *cells])
    assert table.kind == "table"  # the short caption joined the table
    assert len(table.block_ids) == 41
    big = [blk("r", i, words(i, 80), heading=["Big"], region=UiRegion.TABLE) for i in range(60)]
    chunks = chunk_blocks(ctx(), big)
    assert all(c.kind == "table" and c.oversized_atomic for c in chunks)
    assert all(c.token_estimate <= HARD_CAP_TOKENS for c in chunks)
    assert sum(len(c.block_ids) for c in chunks) == 60


def test_accordions_are_faq_only_when_phrased_as_questions() -> None:
    faq = [
        blk("r", 0, "How long is the term?", heading=["FAQ"], region=UiRegion.ACCORDION_PANEL),
        blk("r", 1, "Ten to thirty years.", heading=["FAQ"], region=UiRegion.ACCORDION_PANEL),
    ]
    panel = [
        blk(
            "r",
            0,
            "Important disclosures " + words(3, 40),
            heading=["Legal"],
            region=UiRegion.ACCORDION_PANEL,
        )
    ]
    assert [c.kind for c in chunk_blocks(ctx(), faq)] == ["faq"]
    assert [c.kind for c in chunk_blocks(ctx(), panel)] == ["panel"]


def test_small_chunks_do_not_cross_top_level_headings() -> None:
    blocks = [
        blk("r", 0, words(5, 150), heading=["A"]),
        blk("r", 1, "tiny", heading=["B"]),
    ]
    chunks = chunk_blocks(ctx(title=None, summary=None), blocks)
    assert [c.heading_path for c in chunks] == [["A"], ["B"]]
    assert chunks[1].token_estimate < MIN_TOKENS
    assert chunks[1].context_header == "Example Life › B"


# -- stamping --------------------------------------------------------------------------

PAGE = """<html lang="en"><head>
<title>Term life insurance | Example Life</title>
<meta name="description" content="Affordable cover for a set number of years.">
<meta property="og:title" content="Term life">
<link rel="canonical" href="https://example.com/life/term">
<link rel="alternate" hreflang="es" href="https://example.com/es/life/term">
<script type="application/ld+json">{"@type": "Service", "name": "Term life insurance"}</script>
</head><body><header><nav><ul><li><a href="/life">Life</a></li></ul></nav></header>
<h1>Term life insurance</h1><main><a href="/quote">Get a quote</a></main>
<footer><a href="/privacy">Privacy</a></footer></body></html>"""

RENDERED = """<html><body><header><nav><ul>
<li><a href="/life">Life</a><ul><li><a href="/life/term">Term</a></li></ul></li>
</ul></nav></header><main><h1>Term life insurance</h1></main></body></html>"""


def _page(
    url: str,
    texts: list[str],
    *,
    html: bytes | None = PAGE.encode(),
    mime: str = "text/html",
    heading: str = "Term life insurance",
    **kw: object,
) -> UrlRecord:
    rid = "res-" + url.rsplit("/", 1)[-1]
    blocks = [
        blk(rid, i, t, heading=[heading], evidence=sha(RENDERED.encode()))
        for i, t in enumerate(texts)
    ]
    rec = record(url, blocks=blocks, **kw)  # type: ignore[arg-type]
    return rec.model_copy(
        update={
            "sniffed_mime": mime,
            "payload_sha256": sha(html) if html is not None else None,
            "page_published_at": datetime(2024, 4, 30, tzinfo=UTC),
        }
    )


def _loader(*bodies: bytes) -> dict[str, bytes]:
    return {sha(b): b for b in bodies}


def test_stamp_records_end_to_end() -> None:
    store = _loader(PAGE.encode(), RENDERED.encode())
    english = " ".join(
        f"The plan covers you for {i} years. You pay a fixed premium of $49." for i in range(20)
    )
    other_page = PAGE.replace(
        "https://example.com/life/term", "https://example.com/life/whole"
    ).encode()
    store.update(_loader(other_page))
    records = [
        _page("https://example.com/life/term", [english]),
        _page("https://example.com/life/term-copy", [english]),  # identical text
        _page("https://example.com/life/whole", [words(9, 200)], html=other_page),
        _page("https://example.com/logo.png", [], html=None, mime="image/png"),
        _page("https://example.com/blocked", ["x"], badge=UrlBadge.BLOCKED),
        _page(
            "https://example.com/gone",
            ["Some text here"],
            gaps=[gap("g", GapType.SOFT_404_SUSPECTED)],
        ),
        _page(
            "https://example.com/errors",
            ["An error happened"],
            html=PAGE.replace("<title>Term", "<title>Errors - Term").encode(),
        ),
        _page("https://example.com/draft", ["Lorem ipsum dolor sit amet " + words(1, 20)]),
        _page("https://example.com/missing", ["Real words " + words(4, 30)], html=b"never stored"),
    ]
    store.update(_loader(PAGE.replace("<title>Term", "<title>Errors - Term").encode()))
    when = datetime(2026, 10, 26, tzinfo=UTC)
    result = stamp_records(records, store.get, now=when)
    by_url = {s.url.rsplit("/", 1)[-1]: s for s in result.stamps}

    term = by_url["term"]
    assert term.knowledge_eligible and term.exclusion_reason is None
    assert term.title == "Term life insurance | Example Life"
    assert term.meta_description == "Affordable cover for a set number of years."
    assert term.page_summary == term.meta_description
    assert term.canonical == "https://example.com/life/term"
    assert term.hreflang == {"es": "https://example.com/es/life/term"}
    assert term.og == {"og:title": "Term life"}
    assert term.lang == "en"
    assert term.h1 == "Term life insurance"
    assert term.schema_org.services == ["Term life insurance"]
    assert (
        term.path_segments == ["life", "term"] and term.path_l1 == "life" and term.path_depth == 2
    )
    assert term.page_format == "html" and term.metadata_source == "stored_html"
    assert term.ctas[0].text == "Get a quote"
    assert "$49" in term.numbers
    assert term.reading_ease is not None
    assert term.published_at == datetime(2024, 4, 30, tzinfo=UTC)
    assert term.page_key == page_key("https://example.com/life/term/")
    assert term.chunk_count == 1
    assert term.stamped_at == when

    assert by_url["term-copy"].duplicate_of == "https://example.com/life/term"
    assert by_url["term-copy"].exclusion_reason == "duplicate"
    assert by_url["logo.png"].is_asset and not by_url["logo.png"].knowledge_eligible
    assert by_url["logo.png"].metadata_source == "not_html"
    assert by_url["blocked"].exclusion_reason == "blocked"
    assert by_url["gone"].exclusion_reason == "soft_404"
    assert by_url["errors"].exclusion_reason == "error_page"
    assert by_url["draft"].exclusion_reason == "placeholder_text"
    assert by_url["missing"].metadata_source == "missing_evidence"
    assert by_url["missing"].knowledge_eligible

    site = result.site
    assert site.site == "example.com" and site.site_name == "Example Life"
    header = next(m for m in site.menus if m.kind == "header")
    # Source and rendered views merged: the rendered sub-menu is in the tree.
    assert _labels(header.items) == [("Life", ["Term"])]
    assert header.page_count == 7  # every page with stored HTML, eligible or not
    assert term.menus[0].menu_id == header.menu_id
    assert site.excluded["duplicate"] == 1
    assert site.knowledge_pages == sum(s.knowledge_eligible for s in result.stamps)
    assert site.chunks == len(result.chunks)
    assert {c.url for c in result.chunks} <= {
        "https://example.com/life/term",
        "https://example.com/life/whole",
        "https://example.com/missing",
    }
    assert site.metadata_coverage["title"] == 1.0
    assert site.schema_types == {"Service": 7}
    # Chunk context uses the short title and the site name.
    term_chunk = next(c for c in result.chunks if c.url.endswith("/life/term"))
    assert term_chunk.context_header.startswith("Example Life › Term life insurance\n")


def test_near_duplicates_are_noted_not_dropped() -> None:
    base = words(7, 300)
    other = base.rsplit(" ", 3)[0] + " w1 w2 w3"
    records = [_page("https://example.com/a", [base]), _page("https://example.com/b", [other])]
    result = stamp_records(records, _loader(PAGE.encode(), RENDERED.encode()).get)
    a, b = result.stamps
    assert a.knowledge_eligible and b.knowledge_eligible
    assert a.near_duplicates == ["https://example.com/b"]
    assert b.near_duplicates == ["https://example.com/a"]


def test_canonical_pointing_at_another_page_marks_duplicate() -> None:
    pointing = PAGE.replace("https://example.com/life/term", "https://example.com/a").encode()
    records = [
        _page("https://example.com/a", [words(1, 100)], html=pointing),
        _page("https://example.com/b", [words(2, 100)], html=pointing),
    ]
    result = stamp_records(records, _loader(pointing, RENDERED.encode()).get)
    assert [s.duplicate_of for s in result.stamps] == [None, "https://example.com/a"]


def test_stamp_needs_records_and_reading_ease_needs_words() -> None:
    with pytest.raises(ValueError, match="no records"):
        stamp_records([], lambda _sha: None)
    assert reading_ease("Too short.") is None
    assert sorted(q.text for q in quantities_in("Save 10% or $5.")) == ["$5", "10%"]


def test_site_name_falls_back_to_organization_then_host() -> None:
    bare = b"<html><head><title>Plain</title></head><body></body></html>"
    org = bare.replace(
        b"</head>",
        b'<script type="application/ld+json">{"@type":"Organization","name":"Org Co"}</script>'
        b"</head>",
    )
    r1 = _page("https://www.example.org/x", [words(1, 50)], html=bare)
    assert stamp_records([r1], _loader(bare).get).site.site_name == "example.org"
    r2 = _page("https://www.example.org/x", [words(1, 50)], html=org)
    site = stamp_records([r2], _loader(org).get).site
    assert (site.site_name, site.organization) == ("Org Co", "Org Co")


# -- CLI -------------------------------------------------------------------------------


def test_cli_stamp_writes_three_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = LocalEvidenceStore(tmp_path / "evidence", "t1")
    store.put(PAGE.encode(), "text/html")
    store.put(RENDERED.encode(), "text/html")
    rec = _page(
        "https://example.com/life/term", ["Term life pays out if you die in the term. " * 5]
    )
    records_path = tmp_path / "out" / "jh.records.jsonl"
    records_path.parent.mkdir()
    records_path.write_text(rec.model_dump_json() + "\n\n", encoding="utf-8")
    code = cli.main(
        [
            "stamp",
            "--records",
            str(records_path),
            "--tenant",
            "t1",
            "--evidence-root",
            str(tmp_path / "evidence"),
        ]
    )
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["knowledge_pages"] == 1 and summary["chunks"] == 1
    for name in ("jh.stamps.jsonl", "jh.chunks.jsonl", "jh.site.json"):
        assert (records_path.parent / name).exists()
    site = json.loads((records_path.parent / "jh.site.json").read_text(encoding="utf-8"))
    assert site["site_name"] == "Example Life"


def test_cli_stamp_rejects_empty_records(tmp_path: Path) -> None:
    empty = tmp_path / "x.records.jsonl"
    empty.write_text("\n", encoding="utf-8")
    code = cli.main(
        ["stamp", "--records", str(empty), "--tenant", "t1", "--evidence-root", str(tmp_path)]
    )
    assert code == 2
