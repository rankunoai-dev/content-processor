"""RFC 0003 step A: SEO and GEO inventory of links, images, media, head tags and schema."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.integrations.evidence_store import LocalEvidenceStore
from src.modules.content_processor import cli
from src.modules.content_processor.enums import UrlBadge
from src.modules.content_processor.knowledge.inventory import (
    check_structured,
    inventory_page,
    inventory_records,
    media_provider,
)
from src.modules.content_processor.knowledge.inventory_report import inventory_tables, seen_label
from tests.modules.content_processor.l5_factory import block, record

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
URL = "https://shop.example.com/printers/xerox-c405"

SOURCE = """<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<title>Xerox C405 colour multifunction printer for busy offices | Example</title>
<meta name="description" content="Short.">
<meta name="robots" content="index, follow"><meta name="googlebot" content="max-snippet:-1">
<meta property="og:title" content="Xerox C405"><meta name="twitter:card" content="summary">
<link rel="canonical" href="https://shop.example.com/printers/xerox-c405/">
<link rel="alternate" hreflang="en-gb" href="https://shop.example.com/uk/printers/xerox-c405">
<link rel="icon" href="/favicon.ico">
<link rel="alternate" type="application/rss+xml" href="/feed.xml">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"Product","name":"Xerox C405","offers":{"@type":"Offer","price":"899"}},
 {"@type":"BreadcrumbList","itemListElement":[]}]}</script>
<script type="application/ld+json">{not json</script>
</head><body>
<header><nav><a href="/printers">Printers</a>
<a href="/">  <img src="/logo.svg" alt="Example"></a></nav></header>
<main>
<h1>Xerox C405</h1>
<h2>Overview</h2>
<p>Prints 36 pages a minute. <a href="/printers/compare">Read more</a>
<a href="/support" rel="nofollow">Support</a> <a href="http://old.example.org/manual.pdf">Manual</a>
<a href="https://partner.example.net/buy" target="_blank" rel="sponsored noopener">Buy</a>
<a href="#specs">Specs</a> <a href="mailto:sales@example.com">Email us</a></p>
<h4>Security</h4>
<img src="/img/c405.jpg">
<img src="/img/IMG_2041.jpg" alt="IMG_2041" width="400" height="300">
<figure><img data-src="/img/tray.webp" src="data:image/gif;base64,R0lGOD" alt="Paper tray"
 loading="lazy" width="10" height="10">
<figcaption>The 550-sheet tray</figcaption></figure>
<picture><source srcset="/img/a.avif 1x, /img/a2.avif 2x">
<img src="/img/a.jpg" alt="" width="1" height="1"></picture>
<img src="data:image/png;base64,AAAA" alt="">
<iframe src="https://www.youtube.com/embed/abc" title="C405 tour"></iframe>
<video src="/v/demo.mp4" poster="/v/demo.jpg"></video>
<a href="javascript:void(0)">Open</a>
<a href="/printers"></a>
<div itemscope itemtype="https://schema.org/Product"><span itemprop="name">C405</span>
<div itemscope itemtype="https://schema.org/Offer"><span itemprop="price">1</span></div></div>
<div typeof="schema:Organization"><span property="schema:name">Example</span></div>
<div hidden><a href="/secret">Secret</a></div>
</main>
<footer><a href="/privacy">Privacy</a></footer>
</body></html>"""

RENDERED = SOURCE.replace(
    "<title>Xerox C405 colour multifunction printer for busy offices | Example</title>",
    "<title>Xerox C405 | Example</title>",
).replace("<footer>", '<section><a href="/printers/c415">Xerox C415</a></section><footer>')

STATE = RENDERED.replace(
    "<footer>",
    '<div class="tab-panel"><a href="/printers/c405/specs.pdf">Spec sheet</a></div><footer>',
)


def _inventory():  # type: ignore[no-untyped-def]
    return inventory_page(
        URL,
        SOURCE.encode(),
        [("rendered", RENDERED.encode()), ("state1", STATE.encode())],
        run_id="run-1",
        now=NOW,
    )


def test_links_are_classified_and_tagged_with_where_they_were_found() -> None:
    inv = _inventory()
    assert inv.status == "inventoried" and inv.views == ["rendered", "state1", "src"]
    by_text = {link.anchor_text: link for link in inv.links}
    assert by_text["Printers"].kind == "internal" and by_text["Printers"].ui_region == "nav_menu"
    assert by_text["Printers"].seen_in == ["source", "rendered"]
    assert by_text["Support"].nofollow and by_text["Support"].heading == "Overview"
    manual = by_text["Manual"]
    assert manual.kind == "download" and manual.insecure
    buy = by_text["Buy"]
    assert (buy.kind, buy.sponsored, buy.new_tab, buy.rel) == (
        "external",
        True,
        True,
        ["noopener", "sponsored"],
    )
    assert by_text["Specs"].kind == "same_page"
    assert by_text["Email us"].kind == "mailto"
    assert by_text["Open"].kind == "javascript"
    logo = next(link for link in inv.links if link.url == "https://shop.example.com/")
    assert logo.image_alt == "Example"  # the image alt is the anchor of an image link
    assert by_text["Xerox C415"].seen_in == ["rendered"]
    assert by_text["Spec sheet"].seen_in == ["interaction"]
    assert by_text["Secret"].hidden
    assert by_text["Privacy"].ui_region == "footer"
    assert [link.order for link in inv.links] == list(range(len(inv.links)))


def test_images_media_and_alt_text() -> None:
    inv = _inventory()
    images = {i.url.rsplit("/", 1)[-1]: i for i in inv.images}
    assert images["c405.jpg"].alt_status == "missing" and images["c405.jpg"].heading == "Security"
    assert images["IMG_2041.jpg"].alt_status == "file_name"
    tray = images["tray.webp"]
    assert (tray.alt_status, tray.caption, tray.loading) == (
        "present",
        "The 550-sheet tray",
        "lazy",
    )
    assert images["a.jpg"].alt_status == "empty" and images["a.jpg"].srcset_candidates == 2
    assert images["logo.svg"].link_url == "https://shop.example.com/"
    assert not any(i.url.startswith("data:") for i in inv.images)
    media = {m.kind: m for m in inv.media}
    assert media["iframe"].provider == "YouTube" and media["iframe"].title == "C405 tour"
    assert media["video"].provider == "shop.example.com"
    assert media["video"].poster == "https://shop.example.com/v/demo.jpg"
    assert not media["video"].captions


def test_head_facts_and_outline() -> None:
    head = _inventory().head
    assert head.title is not None and head.title_length == len(head.title)
    assert head.title_rendered == "Xerox C405 | Example"
    assert head.canonical_status == "self"
    assert head.robots == {"robots": ["index", "follow"], "googlebot": ["max-snippet:-1"]}
    assert head.indexable and head.followable and head.lang == "en"
    assert head.hreflang == {"en-gb": "https://shop.example.com/uk/printers/xerox-c405"}
    assert head.open_graph == {"title": "Xerox C405"} and head.twitter == {"card": "summary"}
    assert head.favicon == "https://shop.example.com/favicon.ico"
    assert head.feeds == ["https://shop.example.com/feed.xml"]
    assert head.charset == "utf-8"
    assert head.h1 == ["Xerox C405"]
    assert head.skipped_heading_levels == ["h2 -> h4 at 'Security'"]


def test_structured_data_items_and_required_fields() -> None:
    inv = _inventory()
    by_type = {(s.syntax, s.types[0]): s for s in inv.structured_data}
    product = by_type[("json-ld", "Product")]
    assert product.name == "Xerox C405"
    assert product.missing_required == ["offers.priceCurrency or priceSpecification"]
    assert "image" in product.missing_recommended
    assert by_type[("json-ld", "BreadcrumbList")].missing_required == ["itemListElement"]
    micro = by_type[("microdata", "Product")]
    assert micro.properties == ["name", "price"]
    assert by_type[("rdfa", "Organization")].properties == ["name"]
    assert len(inv.structured_data_errors) == 1 and "block 2" in inv.structured_data_errors[0]


def test_issues_name_the_problems_with_examples() -> None:
    inv = _inventory()
    issues = {i.code: i for i in inv.issues}
    assert issues["title_too_long"].severity == "warning"
    assert "title_changed_by_scripts" in issues
    assert "description_too_short" in issues
    assert "heading_levels_skipped" in issues
    assert issues["hreflang_without_self"].severity == "notice"
    assert issues["open_graph_incomplete"].examples == ["description", "image"]
    assert issues["links_generic_text"].examples == [
        "Read more -> https://shop.example.com/printers/compare"
    ]
    assert issues["internal_links_nofollow"].count == 1
    assert issues["links_insecure"].count == 1
    assert issues["links_without_text"].examples == ["https://shop.example.com/printers"]
    assert issues["links_need_scripts"].examples == ["https://shop.example.com/printers/c415"]
    assert issues["links_behind_clicks"].count == 1
    assert issues["images_alt_missing"].count == 1
    assert issues["images_alt_file_name"].count == 1
    assert issues["images_alt_empty"].examples == ["https://shop.example.com/img/a.jpg"]
    assert issues["images_without_dimensions"].count == 2  # the logo and c405.jpg
    assert issues["videos_without_captions"].count == 1
    assert issues["structured_data_invalid"].severity == "error"
    assert issues["structured_data_required_missing"].count == 3
    assert "canonical_missing" not in issues and "noindex" not in issues
    assert inv.counts["links_need_scripts"] == 2 and inv.counts["links_behind_clicks"] == 1


def test_check_structured_follows_nested_items_and_alternatives() -> None:
    node = {
        "@type": ["Product"],
        "name": "X",
        "aggregateRating": {"@type": "AggregateRating", "ratingValue": 4.5},
        "offers": [{"@type": "Offer", "priceSpecification": {"price": 1}}],
    }
    required, recommended = check_structured(node)
    assert required == ["aggregateRating.ratingCount or reviewCount"]
    assert "offers.availability" in recommended
    assert check_structured({"@type": "Thing"}) == ([], [])


def test_media_provider_names() -> None:
    assert media_provider("https://player.vimeo.com/video/1") == "Vimeo"
    assert media_provider("https://www.google.com/maps/embed?pb=1") == "Google Maps"
    assert media_provider("https://www.other.org/widget") == "other.org"


def test_page_without_views_and_source_only_page() -> None:
    empty = inventory_page(URL, None, [], run_id="r", now=NOW)
    assert empty.status == "missing_evidence" and not empty.links
    plain = inventory_page(
        URL,
        b'<html><head><meta name="robots" content="noindex"></head><body>'
        b'<a href="/x">X</a></body></html>',
        [],
        run_id="r",
        now=NOW,
    )
    codes = {i.code for i in plain.issues}
    assert {"title_missing", "noindex", "lang_missing", "h1_missing"} <= codes
    assert "structured_data_missing" in codes and "canonical_missing" in codes
    assert "links_need_scripts" not in codes and plain.links[0].seen_in == ["source"]
    assert not plain.head.indexable


def test_rendered_only_page_reads_schema_from_the_browser_view() -> None:
    inv = inventory_page(URL, None, [("rendered", RENDERED.encode())], run_id="r", now=NOW)
    assert inv.views == ["rendered"] and inv.head.title == "Xerox C405 | Example"
    assert any(s.types == ["Product"] for s in inv.structured_data)
    assert inv.counts["links_need_scripts"] == 0


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record(url: str, html: bytes | None, *, mime: str = "text/html", **kw: object):  # type: ignore[no-untyped-def]
    rid = "res-1"
    b = block(rid, 0, "Prints 36 pages a minute.").model_copy(
        update={
            "block_id": f"{rid}:rendered:0",
            "state_id": "run|rendered",
            "evidence_ids": [f"evidence:{_sha(RENDERED.encode())}"],
        }
    )
    rec = record(url, blocks=[b], **kw)  # type: ignore[arg-type]
    return rec.model_copy(
        update={"sniffed_mime": mime, "payload_sha256": _sha(html) if html else None}
    )


def test_inventory_records_skips_what_was_not_captured() -> None:
    payloads = {_sha(SOURCE.encode()): SOURCE.encode(), _sha(RENDERED.encode()): RENDERED.encode()}
    records = [
        _record(URL, SOURCE.encode()),
        _record("https://shop.example.com/blocked", SOURCE.encode(), badge=UrlBadge.BLOCKED),
        _record("https://shop.example.com/a.pdf", b"%PDF", mime="application/pdf"),
    ]
    result = inventory_records(records, payloads.get, now=NOW)
    assert [p.status for p in result.pages] == ["inventoried", "not_captured", "not_html"]
    assert result.pages[0].views == ["rendered", "src"]
    summary = result.summary
    assert (summary["inventoried"], summary["not_captured"], summary["not_html"]) == (1, 1, 1)
    assert summary["pages_with_issue"]["images_alt_missing"] == 1
    tables = {t.name: t for t in inventory_tables(result.pages)}
    assert list(tables) == [
        "Issue summary",
        "Pages",
        "Issues",
        "Links",
        "Images",
        "Videos and embeds",
        "Structured data",
        "How to read",
    ]
    assert tables["Issue summary"].rows[0]["Severity"] == "error"
    assert len(tables["Pages"].rows) == 3
    assert tables["Issues"].rows[0]["Severity"] == "error"
    found = {r["Anchor text"]: r["Found in"] for r in tables["Links"].rows}
    assert found["Xerox C415"] == "Browser only (needs scripts)"
    assert found["Printers"] == "HTML and browser"


def test_seen_labels() -> None:
    assert seen_label(["interaction"]) == "After a click only"
    assert seen_label(["source"]) == "HTML only (not shown by the browser)"
    assert seen_label(["source", "interaction"]) == "HTML; shown after a click"


def test_empty_workbook_still_has_headers() -> None:
    tables = inventory_tables([])
    assert all(t.columns for t in tables)


def test_cli_inventory_writes_jsonl_and_workbook(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = LocalEvidenceStore(tmp_path / "evidence", "t1")
    store.put(SOURCE.encode(), "text/html")
    store.put(RENDERED.encode(), "text/html")
    records_path = tmp_path / "out" / "shop.records.jsonl"
    records_path.parent.mkdir()
    records_path.write_text(
        _record(URL, SOURCE.encode()).model_dump_json() + "\n", encoding="utf-8"
    )
    base = ["inventory", "--records", str(records_path), "--tenant", "t1"]
    assert cli.main([*base, "--evidence-root", str(tmp_path / "evidence")]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["inventoried"] == 1
    lines = (records_path.parent / "shop.inventory.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["url"] == URL
    assert (records_path.parent / "shop_seo_inventory.xlsx").exists()
    empty = tmp_path / "e.records.jsonl"
    empty.write_text("\n", encoding="utf-8")
    assert cli.main(["inventory", "--records", str(empty), "--tenant", "t1"]) == 2
