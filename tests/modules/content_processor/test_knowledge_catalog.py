"""RFC 0002 step 2: offering catalog from menus, addresses and structured data, with evidence."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from selectolax.parser import HTMLParser

from src.modules.content_processor import cli
from src.modules.content_processor.knowledge.assist import (
    AssistReport,
    LocalChat,
    LocalEmbedder,
    LocalOnlyError,
    extract_attributes,
    propose_merges,
    quote_in,
)
from src.modules.content_processor.knowledge.catalog import (
    build_catalog,
    classify_label,
    merge_offerings,
    name_key,
    url_key,
)
from src.modules.content_processor.knowledge.catalog_models import Catalog, Offering
from src.modules.content_processor.knowledge.catalog_report import catalog_tables, write_catalog
from src.modules.content_processor.knowledge.chunking import (
    TARGET_TOKENS,
    PageContext,
    chunk_blocks,
)
from src.modules.content_processor.knowledge.html_facts import parse_menus
from src.modules.content_processor.knowledge.models import (
    Chunk,
    Cta,
    MenuItem,
    PageStamp,
    SchemaFacts,
    SiteMenu,
    SiteStamp,
)
from tests.modules.content_processor.l5_factory import block

BASE = "https://www.example.com"
WHEN = datetime(2026, 10, 27, tzinfo=UTC)


def item(label: str, href: str | None = None, *children: MenuItem) -> MenuItem:
    return MenuItem(label=label, href=BASE + href if href else None, children=list(children))


HEADER = SiteMenu(
    menu_id="menu-h",
    kind="header",
    items=[
        item("Individuals", "/individual"),
        item("About Us", "/about-us"),
        item(
            "Life insurance",
            None,
            item("Life insurance overview", "/individual/life-insurance"),
            item(
                "Products & services",
                None,
                item("Term life insurance", "/individual/life-insurance/term"),
                item("Whole life insurance", "/individual/life-insurance/whole"),
            ),
            item("Service your policy", None, item("File a claim", "/claims")),
            item("Tools & calculators", None, item("Coverage calculator", "/calc")),
        ),
        item("Insights", "/insights", item("Articles", "/insights/articles")),
    ],
    item_count=12,
    page_count=5,
)
FOOTER = SiteMenu(
    menu_id="menu-f",
    kind="footer",
    items=[
        item("Term life", "/individual/life-insurance/term/"),
        item("Travel insurance", "/travel"),
        item("Do Not Sell or Share My Personal Information"),
        item("Careers", "/careers"),
    ],
    item_count=4,
    page_count=5,
)


def stamp(
    path: str,
    *,
    eligible: bool = True,
    meta: str | None = None,
    crumbs: list[str] | None = None,
    schema: SchemaFacts | None = None,
    ctas: list[Cta] | None = None,
) -> PageStamp:
    url = BASE + path
    return PageStamp(
        run_id="run-1",
        url=url,
        final_url=url,
        page_key="pg-" + path.strip("/").replace("/", "-"),
        site="example.com",
        host="www.example.com",
        stamped_at=WHEN,
        knowledge_eligible=eligible,
        meta_description=meta,
        breadcrumbs=crumbs or [],
        schema_org=schema or SchemaFacts(),
        ctas=ctas or [],
        badge="VERIFIED_WITHIN_SCOPE",
    )


def chunk(path: str, n: int, text: str, kind: str = "text") -> Chunk:
    return Chunk(
        chunk_id=f"chk-{path.strip('/').replace('/', '-')}-{n}",
        run_id="run-1",
        url=BASE + path,
        page_key="pg",
        order=n,
        kind=kind,  # type: ignore[arg-type]
        parent_section_id="sec",
        text=text,
        context_header="Example",
        embed_text=text,
        block_ids=["b"],
        word_count=len(text.split()),
        token_estimate=len(text) // 4,
    )


STAMPS = [
    stamp(
        "/individual/life-insurance/term",
        meta="Cover for a set number of years.",
        schema=SchemaFacts(
            types=["Service", "FAQPage"],
            services=["Term Life Insurance"],
            faq_questions=["How long can the term be?"],
            offers=["Plan A: 49 USD"],
        ),
        ctas=[
            Cta(text="Get a quote", url=BASE + "/quote", strength="strong"),
            Cta(text="Learn more", url=BASE + "/more", strength="soft"),
        ],
    ),
    stamp(
        "/individual/life-insurance/term/rates",
        schema=SchemaFacts(types=["Product"], products=["Plan A"]),
    ),
    stamp("/individual/life-insurance/final-expense/one"),
    stamp("/individual/life-insurance/final-expense/two"),
    stamp("/about-us/news/2026/record-year"),
    stamp("/campaign/landing", crumbs=["Home", "Insights"]),
    stamp("/somewhere/else"),
    stamp("/individual/life-insurance/whole", eligible=False),
]
CHUNKS = [
    chunk("/individual/life-insurance/term", 0, "Pay $49 a month for 20 years of cover."),
    chunk(
        "/individual/life-insurance/term", 1, "Can I convert my policy?\nYes, before age 70.", "faq"
    ),
    chunk("/individual/life-insurance/term/rates", 0, "Rates start at $12 per month."),
]
SITE = SiteStamp(
    run_id="run-1",
    site="example.com",
    site_name="Example Life",
    hosts=["www.example.com"],
    menus=[HEADER, FOOTER],
    pages=len(STAMPS),
    knowledge_pages=7,
    chunks=len(CHUNKS),
)


def _catalog() -> Catalog:
    return build_catalog(SITE, STAMPS, CHUNKS, now=WHEN)


def _by_name(catalog: Catalog) -> dict[str, Offering]:
    return {o.name: o for o in catalog.offerings}


# -- labels and keys -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "role"),
    [
        ("Products & services", "group"),
        ("Life insurance overview", "landing"),
        ("Coverage calculator", "tool"),
        ("File a claim", "task"),
        ("Lost or unclaimed policy", "task"),
        ("Insights", "content"),
        ("Careers", "corporate"),
        ("Do Not Sell or Share My Personal Information", "corporate"),
        ("Financial professionals", "audience"),
        ("Term life insurance", "unknown"),
    ],
)
def test_classify_label(label: str, role: str) -> None:
    assert classify_label(label) == role


def test_name_and_url_keys() -> None:
    assert name_key("Products & Services") == "product service"
    assert name_key("The IRAs") == "ira"
    assert url_key("https://WWW.Example.com/a/b/?x=1#top") == "example.com/a/b"
    assert url_key(None) is None


# -- tree ------------------------------------------------------------------------------


def test_menu_tree_becomes_families_offerings_and_other_entries() -> None:
    catalog = _catalog()
    by = _by_name(catalog)
    life = by["Life insurance"]
    assert life.level == "family" and life.parent_id is None
    assert life.href == BASE + "/individual/life-insurance"  # from its overview link
    assert life.tools == ["File a claim", "Coverage calculator"]
    term = by["Term life insurance"]
    assert (term.level, term.parent_id, term.family) == (
        "offering",
        life.offering_id,
        "Life insurance",
    )
    # The footer's "Term life" is the same offering: same address.
    assert term.aliases == ["Term life"]
    assert term.signals == ["header_menu", "footer_menu", "schema"]
    assert by["Travel insurance"].level == "offering" and by["Travel insurance"].signals == [
        "footer_menu"
    ]
    roles = {e.label: e.role for e in catalog.other_entries}
    assert roles == {
        "About Us": "corporate",
        "Service your policy": "task",
        "File a claim": "task",
        "Tools & calculators": "tool",
        "Coverage calculator": "tool",
        "Insights": "content",
        "Articles": "content",
        "Do Not Sell or Share My Personal Information": "corporate",
        "Careers": "corporate",
    }
    assert "Individuals" not in by  # an audience, not an offering
    assert [a.value for a in term.audiences] == ["Individuals"]


def test_pages_map_to_offerings_sections_or_unmapped() -> None:
    catalog = _catalog()
    by = _by_name(catalog)
    term = by["Term life insurance"]
    assert [(p.url.removeprefix(BASE), p.basis) for p in term.pages] == [
        ("/individual/life-insurance/term", "same_url")
    ]
    plan = by["Plan A"]  # declared in structured data on a page under Term life
    assert plan.level == "sub_offering" and plan.parent_id == term.offering_id
    assert plan.signals == ["schema"]
    assert [p.basis for p in plan.pages] == ["schema_name"]
    final = by["Final expense"]  # a folder shared by two pages
    assert final.signals == ["url_family"] and final.parent_id == by["Life insurance"].offering_id
    assert len(final.pages) == 2
    sections = {e.label: e.pages for e in catalog.other_entries if e.pages}
    assert sections == {
        "About Us": [BASE + "/about-us/news/2026/record-year"],
        "Insights": [BASE + "/campaign/landing"],
    }
    assert catalog.unmapped_pages == [BASE + "/somewhere/else"]
    assert catalog.stats["pages"] == 7
    assert (
        catalog.stats["pages_mapped"]
        + catalog.stats["pages_in_sections"]
        + catalog.stats["pages_unmapped"]
        == 7
    )


def test_cards_carry_evidence_for_every_value() -> None:
    term = _by_name(_catalog())["Term life insurance"]
    assert term.description is not None
    assert term.description.value == "Cover for a set number of years."
    assert term.description.evidence[0].detail == "meta_description"
    assert [n.value for n in term.numbers] == ["$49", "20 years"]
    assert term.numbers[0].evidence[0].ref == "chk-individual-life-insurance-term-0"
    assert term.numbers[0].evidence[0].detail == "Pay $49 a month for 20 years of cover."
    assert [q.value for q in term.questions] == [
        "How long can the term be?",
        "Can I convert my policy?",
    ]
    assert [p.value for p in term.pricing] == ["Plan A: 49 USD"]
    assert [c.value for c in term.ctas] == ["Get a quote"]  # strong calls to action only
    for attr in [term.description, *term.numbers, *term.questions, *term.pricing, *term.ctas]:
        assert attr.evidence and attr.source == "code"


def test_merge_offerings_moves_everything() -> None:
    catalog = _catalog()
    by = _by_name(catalog)
    merge_offerings(
        catalog, by["Term life insurance"].offering_id, by["Whole life insurance"].offering_id
    )
    term = _by_name(catalog)["Term life insurance"]
    assert "Whole life insurance" in term.aliases
    assert "Whole life insurance" not in _by_name(catalog)
    assert _by_name(catalog)["Plan A"].parent_id == term.offering_id


# -- model help (mock local endpoints) ---------------------------------------------------


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_model_endpoints_must_be_local() -> None:
    with pytest.raises(LocalOnlyError):
        LocalEmbedder("https://api.example.ai/v1", "m")
    with pytest.raises(LocalOnlyError):
        LocalChat("https://api.example.ai/v1", "m")
    LocalChat("http://10.0.0.5:11434/v1", "m").close()  # private network is allowed


def test_embedding_merges_need_a_second_signal() -> None:
    catalog = _catalog()
    names = [o.name for o in catalog.offerings]
    vectors = {
        "Term life insurance": [1.0, 0.0, 0.0],
        "Whole life insurance": [0.0, 1.0, 0.0],
        "Final expense": [0.0, 0.0, 1.0],
        "Travel insurance": [0.0, 0.98, 0.2],  # close to Whole life, other family
    }

    def handler(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["input"]
        data = [
            {"index": i, "embedding": vectors.get(t.split(" (")[0], [0.5, 0.5, 0.5])}
            for i, t in enumerate(texts)
        ]
        return httpx.Response(200, json={"data": data})

    report = AssistReport()
    propose_merges(
        catalog,
        LocalEmbedder("http://localhost:11434/v1", "bge-m3", client=_client(handler)),
        report,
    )
    assert report.merges_applied == 0
    assert report.merge_suggestions >= 1
    travel = next(s for s in catalog.suggestions if "Travel insurance" in s.proposal)
    assert travel.kind == "merge" and travel.score is not None
    assert len(catalog.offerings) == len(names)
    assert catalog.engines["embedding"] == "ollama:bge-m3"

    # Same family and shared words: merged.
    vectors["Whole life insurance"] = [0.99, 0.05, 0.0]
    catalog2 = _catalog()
    report2 = AssistReport()
    propose_merges(
        catalog2,
        LocalEmbedder("http://localhost:11434/v1", "bge-m3", client=_client(handler)),
        report2,
    )
    assert report2.merges_applied == 1
    assert "Whole life insurance" in _by_name(catalog2)["Term life insurance"].aliases


def test_embedding_failure_changes_nothing() -> None:
    catalog = _catalog()
    report = AssistReport()
    embedder = LocalEmbedder(
        "http://localhost:11434/v1", "m", client=_client(lambda r: httpx.Response(500))
    )
    propose_merges(catalog, embedder, report)
    assert report.failed_calls == 1 and embedder.errors == 1
    assert len(catalog.offerings) == len(_catalog().offerings)


def test_model_attributes_kept_only_with_a_matching_quote() -> None:
    catalog = _catalog()
    answer = {
        "features": [
            {
                "text": "Monthly premium",
                "chunk_id": "chk-individual-life-insurance-term-0",
                "quote": "pay $49 a  month",
            },
            {
                "text": "Invented",
                "chunk_id": "chk-individual-life-insurance-term-0",
                "quote": "free forever for everyone",
            },
            {"text": "Wrong chunk", "chunk_id": "chk-other", "quote": "Pay $49 a month"},
            "not an object",
        ],
        "deliverables": [],
        "audiences": [
            {
                "text": "Families",
                "chunk_id": "chk-individual-life-insurance-term-1",
                "quote": "Can I convert my policy?",
            }
        ],
        "pricing": "not a list",
        "offering_type": "service",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        user = json.loads(body["messages"][1]["content"])
        reply = (
            answer if user["offering"] == "Term life insurance" else {"offering_type": "product"}
        )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Here: " + json.dumps(reply)}}]}
        )

    report = AssistReport()
    chat = LocalChat("http://localhost:11434/v1", "qwen2.5:14b", client=_client(handler))
    extract_attributes(catalog, {c.chunk_id: c for c in CHUNKS}, chat, report)
    term = _by_name(catalog)["Term life insurance"]
    assert [(f.value, f.source, f.model) for f in term.features] == [
        ("Monthly premium", "ollama", "qwen2.5:14b")
    ]
    assert term.features[0].evidence[0].detail == "pay $49 a  month"
    assert [a.value for a in term.audiences] == ["Individuals", "Families"]
    assert report.attributes_accepted == 2 and report.attributes_rejected == 3
    # Term's page declares a Service: the proposed type is confirmed and applied.
    assert term.offering_type == "service" and report.types_applied == 2
    # Plan A's page declares a Product: "product" applied. Final expense has no
    # chunks, so it was never asked.
    assert _by_name(catalog)["Plan A"].offering_type == "product"
    assert catalog.engines["attributes"] == "ollama:qwen2.5:14b"


def test_unconfirmed_type_is_only_a_suggestion_and_bad_replies_are_counted() -> None:
    catalog = _catalog()
    replies = iter(['{"offering_type": "platform"}', "no json at all", "[1, 2]"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}]})

    report = AssistReport()
    chat = LocalChat("http://localhost:11434/v1", "m", client=_client(handler))
    extract_attributes(catalog, {c.chunk_id: c for c in CHUNKS}, chat, report)
    assert report.type_suggestions == 1 and report.failed_calls == 1
    (suggestion,) = catalog.suggestions
    assert (suggestion.kind, suggestion.proposal) == ("offering_type", "platform")
    assert _by_name(catalog)["Term life insurance"].offering_type == "unspecified"


def test_quote_check() -> None:
    assert quote_in("Pay  $49 a MONTH", "Pay $49 a month for cover.")
    assert quote_in("old ~~price~~ here", "old price here now")
    assert not quote_in("short", "short text")
    assert not quote_in("Pay $50 a month", "Pay $49 a month for cover.")


# -- outputs and CLI ---------------------------------------------------------------------


def test_catalog_workbook_and_json(tmp_path: Path) -> None:
    catalog = _catalog()
    tables = {t.name: t for t in catalog_tables(catalog)}
    offerings = tables["Offerings"].rows
    assert offerings[0]["Offering"] == "Life insurance"
    assert any(str(r["Offering"]).startswith("    Term life insurance") for r in offerings)
    kinds = [r["Kind"] for r in tables["Page mapping"].rows]
    assert kinds.count("unmapped") == 1 and "section (corporate)" in kinds
    files = write_catalog(catalog, tmp_path, "jh")
    data = json.loads(Path(files["catalog"]).read_text(encoding="utf-8"))
    assert data["catalog_version"] == "catalog-v1" and data["site_name"] == "Example Life"
    with zipfile.ZipFile(files["workbook"]) as book:
        assert "xl/worksheets/sheet5.xml" in book.namelist()


def _write_inputs(folder: Path) -> Path:
    stamps = folder / "jh.stamps.jsonl"
    stamps.write_text("".join(s.model_dump_json() + "\n" for s in STAMPS), encoding="utf-8")
    (folder / "jh.chunks.jsonl").write_text(
        "".join(c.model_dump_json() + "\n" for c in CHUNKS), encoding="utf-8"
    )
    (folder / "jh.site.json").write_text(SITE.model_dump_json(), encoding="utf-8")
    return stamps


def test_cli_catalog(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stamps = _write_inputs(tmp_path)
    assert cli.main(["catalog", "--stamps", str(stamps)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["stats"]["families"] == 1 and summary["engines"] == {"catalog": "code"}
    assert (tmp_path / "jh.catalog.json").exists()
    assert (tmp_path / "jh_offering_catalog.xlsx").exists()


def test_cli_catalog_refusals(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stamps = _write_inputs(tmp_path)
    assert cli.main(["catalog", "--stamps", str(tmp_path / "x.stamps.jsonl")]) == 2
    assert (
        cli.main(["catalog", "--stamps", str(stamps), "--llm-endpoint", "http://localhost:1/v1"])
        == 2
    )
    remote = ["--embed-endpoint", "https://api.example.ai/v1", "--embed-model", "m"]
    assert cli.main(["catalog", "--stamps", str(stamps), *remote]) == 2
    err = capsys.readouterr().err
    assert "not found" in err and "needs its model" in err and "refused model endpoint" in err


# -- step 1 fixes found while building the catalog ----------------------------------------


def test_script_button_links_are_not_menu_destinations() -> None:
    html = (
        "<footer><ul><li><a href='#'>Do not sell</a></li>"
        "<li><a href='javascript:void(0)'>Cookies</a></li>"
        "<li><a href='/page'>This page</a></li><li><a href='/life'>Life</a></li></ul></footer>"
    )
    (menu,) = parse_menus(HTMLParser(html), BASE + "/page")
    assert [(i.label, i.href) for i in menu.items] == [
        ("Do not sell", None),
        ("Cookies", None),
        ("This page", None),
        ("Life", BASE + "/life"),
    ]


def test_a_sentence_longer_than_the_target_is_cut_between_words() -> None:
    sentence = " ".join(f"word{i}" for i in range(900)) + "."
    blocks = [block("r", 0, sentence).model_copy(update={"heading_path": ["H"]})]
    ctx = PageContext(run_id="r", url="u", page_key="p", site="s", title=None, summary=None)
    chunks = chunk_blocks(ctx, blocks)
    assert len(chunks) > 1
    assert all(c.token_estimate <= TARGET_TOKENS for c in chunks)
    assert " ".join(c.text for c in chunks) == sentence
