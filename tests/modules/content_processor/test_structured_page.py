"""RFC 0003 step B: each page as an ordered tree and Markdown, with its page type."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.integrations.evidence_store import LocalEvidenceStore
from src.modules.content_processor import cli
from src.modules.content_processor.enums import UiRegion, UrlBadge
from src.modules.content_processor.knowledge.page_types import (
    PageSignals,
    classify_page,
    site_profile,
)
from src.modules.content_processor.knowledge.structured_models import (
    PageTypeLabel,
    StructuredNode,
)
from src.modules.content_processor.knowledge.structured_page import (
    build_structured_page,
    markdown_name,
    structure_records,
    to_markdown,
)
from src.modules.content_processor.knowledge.structured_report import structure_tables
from tests.modules.content_processor.l5_factory import block, record

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)

SERVICE = """<!doctype html><html lang="en"><head><title>Managed print services | Acme</title>
<meta name="description" content="Print fleets run for you.">
<script type="application/ld+json">{"@type": "Service", "name": "Managed print"}</script>
</head><body>
<header><nav><a href="/">Home</a><a href="/services">Services</a></nav></header>
<div class="cookie-banner">We use cookies <button>Accept</button></div>
<nav aria-label="Breadcrumb"><ol><li>Home</li><li>Services</li><li>Managed print</li></ol></nav>
<main>
<h1>Managed print services</h1>
<p>We run your printers so <strong>you</strong> do not have to. Read the
<a href="/guide">buyer guide</a> first.</p>
<p><a href="/contact">Request a quote</a></p>
<h2>What is included</h2>
<ul><li>Supplies</li><li>Repairs</li></ul>
<div role="tablist"><button role="tab" aria-controls="t1">Overview</button>
<button role="tab" aria-controls="t2">Pricing</button></div>
<div id="t1" role="tabpanel"><p>All devices, one invoice.</p></div>
<div id="t2" role="tabpanel" hidden><table><tr><th>Plan</th><th>Price</th></tr>
<tr><td>Basic</td><td>$99</td></tr><tr><td>Pro | Plus</td><td>$199</td></tr></table></div>
<h2>Questions</h2>
<details><summary>How fast are repairs?</summary><p>Next business day.</p></details>
<details open><summary>Do you cover scanners?</summary><p>Yes.</p></details>
<button aria-expanded="false" aria-controls="a3">Is there a contract?</button>
<div id="a3" hidden><p>Twelve months.</p></div>
<h3>Customers</h3>
<div class="card"><h4>Bank</h4><p>Saved 30%.</p></div>
<div class="card"><h4>School</h4><p>Saved 20%.</p></div>
<figure><img src="/fleet.jpg" alt="Printer fleet"><figcaption>Our fleet</figcaption></figure>
<img src="/rt.gif" width="1" height="1">
<iframe src="https://www.youtube.com/embed/x" title="Tour"></iframe>
<div class="video-js vjs-paused"><video class="vjs-tech" src="/v.mp4"></video>
<div class="vjs-control-bar">Current Time 0:00</div></div>
<div role="progressbar">Loading 40%</div>
<form action="/send"><label>Name</label><input name="n"><label>Email</label><input name="e"></form>
<blockquote>Best service ever.</blockquote>
<pre>code  sample</pre>
<span>Loose text <div>block inside inline</div></span>
</main>
<footer><a href="/privacy">Privacy</a></footer>
</body></html>"""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _walk(nodes: list[StructuredNode]) -> list[StructuredNode]:
    out: list[StructuredNode] = []
    for n in nodes:
        out.append(n)
        out += _walk(n.children)
    return out


def _service_page():  # type: ignore[no-untyped-def]
    return build_structured_page(
        "https://acme.example/services/managed-print", SERVICE.encode(), run_id="r", now=NOW
    )


def test_tree_follows_the_page_and_leaves_out_chrome() -> None:
    page = _service_page()
    assert page.h1 == "Managed print services" and page.language == "en"
    assert page.breadcrumbs == ["Home", "Services", "Managed print"]
    assert page.description == "Print fleets run for you."
    (top,) = page.nodes
    assert (top.kind, top.title, top.level) == ("section", "Managed print services", 1)
    kinds = [n.kind for n in top.children]
    assert kinds[:3] == ["paragraph", "cta", "section"]
    intro, cta = top.children[0], top.children[1]
    assert intro.text == "We run your printers so you do not have to. Read the buyer guide first."
    assert [(lnk.text, lnk.url) for lnk in intro.links] == [
        ("buyer guide", "https://acme.example/guide")
    ]
    assert (cta.text, cta.url) == ("Request a quote", "https://acme.example/contact")
    texts = " ".join(n.text or n.title or "" for n in _walk(page.nodes))
    for chrome in ("Home Services", "cookies", "Privacy", "Current Time", "Loading 40%"):
        assert chrome not in texts


def test_components_tabs_accordions_cards_tables_media() -> None:
    nodes = _walk(_service_page().nodes)
    tabs = next(n for n in nodes if n.kind == "tabs")
    assert [t.title for t in tabs.children] == ["Overview", "Pricing"]
    overview, pricing = tabs.children
    assert overview.layer == "visible" and overview.children[0].text == "All devices, one invoice."
    assert pricing.layer == "collapsed"
    table = pricing.children[0]
    assert table.kind == "table" and table.header_row
    assert table.rows == [["Plan", "Price"], ["Basic", "$99"], ["Pro | Plus", "$199"]]
    faq = next(n for n in nodes if n.kind == "faq")
    assert [(i.title, i.layer) for i in faq.children] == [
        ("How fast are repairs?", "collapsed"),
        ("Do you cover scanners?", "visible"),
        ("Is there a contract?", "collapsed"),
    ]
    assert faq.children[2].children[0].text == "Twelve months."
    cards = next(n for n in nodes if n.kind == "card_group")
    assert [c.title for c in cards.children] == ["Bank", "School"]
    images = [n for n in nodes if n.kind == "image"]
    assert [(i.alt, i.caption) for i in images] == [("Printer fleet", "Our fleet")]
    media = [(n.control, n.title) for n in nodes if n.kind == "media"]
    assert media == [("iframe", "Tour"), ("video", "streamed on the page")] or media == [
        ("iframe", "Tour"),
        ("video", "acme.example"),
    ]
    form = next(n for n in nodes if n.kind == "form")
    assert form.items == ["Name", "Email"] and form.url == "https://acme.example/send"
    assert any(n.kind == "quote" for n in nodes) and any(n.kind == "code" for n in nodes)
    assert any(n.text == "block inside inline" for n in nodes)


def test_markdown_reads_like_the_page() -> None:
    page = _service_page()
    md = to_markdown(page)
    assert md.startswith("# Managed print services\n\n> Print fleets run for you.\n")
    assert "Page type: service" in md and "Breadcrumb: Home › Services › Managed print" in md
    assert "[buyer guide](https://acme.example/guide)" in md
    assert "[**Request a quote**](https://acme.example/contact)" in md
    assert "## What is included\n\n- Supplies\n- Repairs" in md
    assert "Tab: Pricing _(shown after a click)_" in md
    assert "| Plan | Price |\n|---|---|\n| Basic | $99 |\n| Pro \\| Plus | $199 |" in md
    assert "**Questions and answers**" in md
    assert "![Printer fleet](https://acme.example/fleet.jpg)\n_Our fleet_" in md
    assert "**Form** (Name, Email)" in md
    assert "[Iframe: Tour](https://www.youtube.com/embed/x)" in md
    assert "> Best service ever." in md and "```\ncode  sample\n```" in md
    assert "# Managed print services" not in md.split("---", 1)[1], "h1 is the title only"


def test_page_types_from_independent_signals() -> None:
    assert _service_page().page_type.page_type == "service"  # type: ignore[union-attr]
    product = classify_page(
        PageSignals(
            url="https://shop.example/products/c405",
            schema_types=["Product", "Offer"],
            cart_buttons=1,
            prices=1,
            variant_controls=1,
        )
    )
    assert product.page_type == "product" and product.confidence > 0.8
    assert any("add-to-cart" in s for s in product.signals)
    grid = classify_page(
        PageSignals(url="https://shop.example/collections/printers", product_cards=12, facets=3)
    )
    assert grid.page_type == "category" and grid.runner_up is None
    article = classify_page(
        PageSignals(
            url="https://acme.example/blog/print-security",
            schema_types=["BlogPosting"],
            has_author=True,
            has_published_date=True,
            has_article_tag=True,
            words=900,
        )
    )
    assert article.page_type == "article"
    assert classify_page(PageSignals(url="https://acme.example/")).page_type == "home"
    assert classify_page(PageSignals(url="https://acme.example/x/y")).page_type == "other"
    weak = classify_page(PageSignals(url="https://acme.example/x", prices=1))
    assert weak.page_type == "other" and weak.runner_up == "product"
    pdf = classify_page(PageSignals(url="https://acme.example/a.pdf", media_type="application/pdf"))
    assert pdf.page_type == "document"
    form = classify_page(
        PageSignals(url="https://acme.example/apply", forms=1, form_fields=6, words=80)
    )
    assert form.page_type == "form"
    faq = classify_page(
        PageSignals(url="https://acme.example/help-center/x", schema_types=["FAQPage"], faq_items=8)
    )
    assert faq.page_type == "faq"


def _label(kind: str) -> PageTypeLabel:
    return PageTypeLabel(page_type=kind, confidence=1.0)  # type: ignore[arg-type]


def test_site_profile() -> None:
    shop = [_label("product")] * 5 + [_label("category")] * 2 + [_label("article")] * 3
    assert site_profile(shop, cart_pages=4).profile == "ecommerce"
    b2b = [_label("service")] * 6 + [_label("case_study")] * 2 + [_label("article")] * 2
    assert site_profile(b2b).profile == "b2b_services"
    news = [_label("article")] * 9 + [_label("faq")]
    assert site_profile(news).profile == "publisher"
    mixed = [_label("service")] * 3 + [_label("article")] * 3 + [_label("product")] * 3
    assert site_profile(mixed).profile == "mixed"
    assert site_profile([_label("other"), _label("legal")]).profile == "unknown"


ARTICLE = """<html><head><title>Print security</title>
<script type="application/ld+json">{"@type": "BlogPosting", "headline": "Print security",
"author": {"@type": "Person", "name": "Brian Cantor"}, "datePublished": "2023-09-04"}</script>
</head><body><main><article><h1>Print security</h1><p>Printers are a risk.</p>
<button aria-expanded="false" aria-controls="lazy">Show more</button><div id="lazy"></div>
</article></main></body></html>"""


def _record(url: str, html: bytes, **kw: object):  # type: ignore[no-untyped-def]
    rid = "res-1"
    sha = _sha(html)
    base = block(rid, 0, "Printers are a risk.").model_copy(
        update={
            "block_id": f"{rid}:rendered:0",
            "state_id": "run|rendered",
            "evidence_ids": [f"evidence:{sha}"],
            "ui_region": UiRegion.MAIN_CONTENT,
        }
    )
    revealed = base.model_copy(
        update={
            "block_id": f"{rid}:state1:0",
            "state_id": "run|state:1",
            "raw_text": "Shared printers keep copies of scans.",
            "normalized_text": "Shared printers keep copies of scans.",
            "provenance": base.provenance.model_copy(
                update={"selector": "state:accordion:Show more"}
            ),
        }
    )
    menu = revealed.model_copy(
        update={
            "block_id": f"{rid}:state2:0",
            "state_id": "run|state:2",
            "raw_text": "Products menu entry",
            "normalized_text": "Products menu entry",
            "role": "nav",
            "provenance": base.provenance.model_copy(update={"selector": "state:dialog:Products"}),
        }
    )
    elsewhere = revealed.model_copy(
        update={
            "block_id": f"{rid}:state3:0",
            "state_id": "run|state:3",
            "raw_text": "Only in a pop-up.",
            "normalized_text": "Only in a pop-up.",
            "provenance": base.provenance.model_copy(update={"selector": "state:dialog:Details"}),
        }
    )
    rec = record(url, blocks=[base, revealed, menu, elsewhere], **kw)  # type: ignore[arg-type]
    return rec.model_copy(
        update={
            "sniffed_mime": "text/html",
            "payload_sha256": sha,
            "page_published_at": datetime(2023, 9, 4, tzinfo=UTC),
        }
    )


def test_records_cite_blocks_and_place_after_click_text() -> None:
    url = "https://acme.example/blog/print-security"
    payloads = {_sha(ARTICLE.encode()): ARTICLE.encode()}
    records = [
        _record(url, ARTICLE.encode()),
        _record("https://acme.example/blocked", ARTICLE.encode(), badge=UrlBadge.BLOCKED),
        _record("https://acme.example/gone", b"<html></html>").model_copy(
            update={"payload_sha256": None, "blocks": []}
        ),
        record("https://acme.example/a.pdf", ["x"]).model_copy(
            update={"sniffed_mime": "application/pdf"}
        ),
    ]
    result = structure_records(records, payloads.get, now=NOW)
    page, blocked, gone, pdf = result.pages
    assert (blocked.status, gone.status, pdf.status) == (
        "not_captured",
        "missing_evidence",
        "not_html",
    )
    assert pdf.page_type is not None and pdf.page_type.page_type == "document"
    assert page.status == "structured" and page.base_view == "rendered"
    assert page.author == "Brian Cantor" and page.page_type.page_type == "article"  # type: ignore[union-attr]
    nodes = _walk(page.nodes)
    para = next(n for n in nodes if n.text == "Printers are a risk.")
    assert para.block_ids == ["res-1:rendered:0"] and para.evidence_ids
    more = next(n for n in nodes if n.kind == "accordion_item")
    assert more.title == "Show more"
    assert [(c.text, c.layer) for c in more.children] == [
        ("Shared printers keep copies of scans.", "after_click")
    ]
    extra = next(n for n in nodes if n.kind == "revealed")
    assert (extra.title, extra.control) == ("Details", "dialog")
    assert "Products menu entry" not in " ".join(n.text or "" for n in nodes)
    md = to_markdown(page)
    assert "Author: Brian Cantor" in md and "Published: September 4, 2023" in md
    assert "After clicking 'Details': _(appears after a click)_" in md
    assert result.summary["structured"] == 1 and result.summary["text_nodes_cited"] >= 1
    tables = structure_tables(result.pages, result.summary, {url: markdown_name(url)})
    assert [t.name for t in tables] == ["Pages", "Site profile", "How to read"]
    assert tables[0].rows[0]["Page type"] == "article"
    assert tables[0].rows[0]["Markdown file"] == markdown_name(url)


def test_composed_view_is_the_base_when_there_is_one() -> None:
    url = "https://acme.example/blog/print-security"
    composed = ARTICLE.replace("Printers are a risk.", "Printers are a risk. Inside a component.")
    rec = _record(url, ARTICLE.encode())
    shadow = rec.blocks[0].model_copy(
        update={
            "block_id": "res-1:state9:0",
            "state_id": "run|state:9",
            "evidence_ids": [f"evidence:{_sha(composed.encode())}"],
            "provenance": rec.blocks[0].provenance.model_copy(
                update={"selector": "state:shadow_root:composed_tree"}
            ),
        }
    )
    rec = rec.model_copy(update={"blocks": [*rec.blocks, shadow]})
    payloads = {
        _sha(ARTICLE.encode()): ARTICLE.encode(),
        _sha(composed.encode()): composed.encode(),
    }
    (page,) = structure_records([rec], payloads.get, now=NOW).pages
    assert page.base_view == "state9"
    assert any("Inside a component" in (n.text or "") for n in _walk(page.nodes))


def test_markdown_name_is_short_safe_and_unique() -> None:
    a = markdown_name("https://acme.example/" + "x" * 300)
    b = markdown_name("https://acme.example/" + "x" * 301)
    assert a != b and len(a) <= 72 and a.endswith(".md")
    assert markdown_name("https://acme.example/").startswith("acme-example-")


def test_cli_structure_writes_jsonl_markdown_and_workbook(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = LocalEvidenceStore(tmp_path / "evidence", "t1")
    store.put(ARTICLE.encode(), "text/html")
    url = "https://acme.example/blog/print-security"
    records_path = tmp_path / "out" / "acme.records.jsonl"
    records_path.parent.mkdir()
    records_path.write_text(_record(url, ARTICLE.encode()).model_dump_json() + "\n", "utf-8")
    base = ["structure", "--records", str(records_path), "--tenant", "t1"]
    assert cli.main([*base, "--evidence-root", str(tmp_path / "evidence")]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["structured"] == 1 and summary["site_profile"]["profile"] == "publisher"
    folder = records_path.parent
    assert json.loads((folder / "acme.structured.jsonl").read_text("utf-8"))["url"] == url
    md = (folder / "acme_pages" / markdown_name(url)).read_text("utf-8")
    assert md.startswith("# Print security")
    assert (folder / "acme_page_types.xlsx").exists()
    empty = tmp_path / "e.records.jsonl"
    empty.write_text("\n", encoding="utf-8")
    assert cli.main(["structure", "--records", str(empty), "--tenant", "t1"]) == 2
