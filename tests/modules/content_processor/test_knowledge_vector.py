"""RFC 0002 stages D and E: vector store, chunk labels, hybrid search and checked answers."""

from __future__ import annotations

import json
import os
import zipfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.modules.content_processor import cli
from src.modules.content_processor.knowledge import embedders as embedders_module
from src.modules.content_processor.knowledge import kb_commands
from src.modules.content_processor.knowledge.assist import LocalChat, LocalOnlyError
from src.modules.content_processor.knowledge.catalog_models import (
    Catalog,
    Evidence,
    Offering,
    PageLink,
)
from src.modules.content_processor.knowledge.embedders import (
    HASHING_MODEL,
    HashingEmbedder,
    ModelServer,
    check_model_server,
    is_semantic,
    open_embedder,
)
from src.modules.content_processor.knowledge.indexing import (
    ModelMismatchError,
    RunInputs,
    embed_hash,
    index_run,
    load_run,
    write_label_review,
)
from src.modules.content_processor.knowledge.labels import (
    Labeller,
    decide_offering,
    decide_role,
    structure_role,
    text_role,
)
from src.modules.content_processor.knowledge.models import Chunk, Cta, PageStamp, SchemaFacts
from src.modules.content_processor.knowledge.retrieval import (
    answer_question,
    check_statement,
    fragment_link,
    hybrid_search,
    offering_scope,
    render_answer,
    render_hits,
    rrf,
    sentences,
)
from src.modules.content_processor.knowledge.vector_store import (
    ChunkRow,
    PgVectorStore,
    SqliteVectorStore,
    open_store,
    probe_pgvector,
    query_tokens,
)

BASE = "https://www.example.com"
WHEN = datetime(2026, 10, 30, tzinfo=UTC)
RUN = "run-1"


def stamp(page_key: str, path: str, **extra: Any) -> PageStamp:
    return PageStamp(
        run_id=RUN,
        url=BASE + path,
        page_key=page_key,
        site="example.com",
        host="www.example.com",
        stamped_at=WHEN,
        knowledge_eligible=extra.pop("knowledge_eligible", True),
        badge="VERIFIED",
        **extra,
    )


def chunk(
    chunk_id: str,
    page_key: str,
    path: str,
    text: str,
    *,
    order: int = 0,
    headings: list[str] | None = None,
    kind: str = "text",
    regions: list[str] | None = None,
    prev: str | None = None,
    nxt: str | None = None,
) -> Chunk:
    heading_path = headings or []
    header = " > ".join(["Example Life", *heading_path])
    return Chunk(
        chunk_id=chunk_id,
        run_id=RUN,
        url=BASE + path,
        page_key=page_key,
        order=order,
        kind=kind,
        heading_path=heading_path,
        parent_section_id=f"sec-{page_key}",
        prev_chunk_id=prev,
        next_chunk_id=nxt,
        text=text,
        context_header=header,
        embed_text=f"{header}\n{text}",
        block_ids=[f"blk-{chunk_id}"],
        evidence_ids=[f"evidence:{chunk_id}"],
        ui_regions=regions or ["main_content"],
        word_count=len(text.split()),
        token_estimate=len(text.split()) * 2,
    )


TERM_TEXT = (
    "Term life insurance covers you for 10, 20 or 30 years. "
    "You can convert your term policy to permanent coverage before age 70 without a medical exam."
)
FAQ_TEXT = "Can I convert my term policy?\nYes. Conversion is allowed before age 70."
PRICE_TEXT = "Coverage starting at $25 per month for a healthy 35-year-old. Form ICC20-1234."
DISCLAIMER_TEXT = (
    "Insurance products are issued by Example Life Insurance Company. Guarantees are based "
    "on the claims-paying ability of the issuer. Not FDIC insured."
)
NEWS_TEXT = "Example Life announced a new partnership with a fitness brand in Boston today."

STAMPS = {
    "pg-term": stamp(
        "pg-term",
        "/life-insurance/term",
        title="Term life insurance",
        breadcrumbs=["Home", "Life insurance", "Term life insurance"],
        schema_org=SchemaFacts(faq_questions=["Can I convert my term policy?"]),
        ctas=[Cta(text="Get a quote", url=BASE + "/quote", strength="strong")],
    ),
    "pg-news": stamp("pg-news", "/news/partnership", title="Partnership"),
    "pg-old": stamp("pg-old", "/old", knowledge_eligible=False, exclusion_reason="duplicate"),
}
CHUNKS = [
    chunk(
        "c1",
        "pg-term",
        "/life-insurance/term",
        TERM_TEXT,
        headings=["Term life insurance"],
        nxt="c2",
    ),
    chunk(
        "c2",
        "pg-term",
        "/life-insurance/term",
        FAQ_TEXT,
        order=1,
        headings=["Term life insurance", "Frequently asked questions"],
        kind="faq",
        regions=["main_content", "accordion_panel"],
        prev="c1",
        nxt="c3",
    ),
    chunk(
        "c3",
        "pg-term",
        "/life-insurance/term",
        PRICE_TEXT,
        order=2,
        headings=["Term life insurance", "Pricing"],
        prev="c2",
        nxt="c4",
    ),
    chunk(
        "c4",
        "pg-term",
        "/life-insurance/term",
        DISCLAIMER_TEXT,
        order=3,
        headings=["Term life insurance", "Important disclosures"],
        prev="c3",
    ),
    chunk("c5", "pg-news", "/news/partnership", NEWS_TEXT, headings=["Partnership"]),
    chunk("c6", "pg-old", "/old", "An old page that is excluded.", headings=["Old"]),
]


def offering(
    oid: str, name: str, level: str, parent: str | None, href: str, **extra: Any
) -> Offering:
    return Offering(
        offering_id=oid,
        name=name,
        level=level,
        parent_id=parent,
        family=None if parent is None else "Life insurance",
        href=BASE + href,
        signals=["header_menu"],
        evidence=[Evidence(kind="menu", ref="menu-h", detail=name)],
        **extra,
    )


CATALOG = Catalog(
    run_id=RUN,
    site="example.com",
    site_name="Example Life",
    built_at=WHEN,
    offerings=[
        offering("off-life", "Life insurance", "family", None, "/life-insurance"),
        offering(
            "off-term",
            "Term life insurance",
            "offering",
            "off-life",
            "/life-insurance/term",
            pages=[PageLink(url=BASE + "/life-insurance/term", basis="same_url", chunk_ids=["c1"])],
        ),
        offering("off-annuity", "Annuities", "family", None, "/annuities"),
    ],
)


def run_inputs(chunks: list[Chunk] | None = None) -> RunInputs:
    return RunInputs("jh", Path("."), chunks or list(CHUNKS), dict(STAMPS), CATALOG)


class CountingEmbedder(HashingEmbedder):
    """The hashing embedder, counting how many texts it was asked to embed."""

    def __init__(self, model: str = HASHING_MODEL) -> None:
        super().__init__()
        self.model = model
        self.texts = 0

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        self.texts += len(texts)
        return super().embed(texts)


def chat_with(reply: Callable[[dict[str, Any]], dict[str, Any] | str]) -> LocalChat:
    """A LocalChat whose server answers with `reply(request_json)`."""

    def handler(request: httpx.Request) -> httpx.Response:
        answer = reply(json.loads(request.content))
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return LocalChat(
        "http://localhost:11434/v1",
        "qwen-test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteVectorStore]:
    s = SqliteVectorStore(tmp_path / "t" / "vectors.sqlite")
    yield s
    s.close()


@pytest.fixture
def indexed(store: SqliteVectorStore) -> SqliteVectorStore:
    index_run(run_inputs(), store, HashingEmbedder(), Labeller(STAMPS, CATALOG))
    return store


# -- embedders --------------------------------------------------------------------------


def test_hashing_embedder_is_repeatable_unit_length_and_word_sensitive() -> None:
    e = HashingEmbedder()
    a, b, c = e.embed(["term life policy", "term life policy", "annuity income rider"]) or []
    assert a == b and len(a) == 1024
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9
    near = e.embed(["convert term life policy"]) or []

    def dot(u: list[float], v: list[float]) -> float:
        return sum(x * y for x, y in zip(u, v, strict=True))

    assert dot(a, near[0]) > dot(a, c)
    assert e.embed([""]) == [[0.0] * 1024]
    assert HashingEmbedder(16).model == "hashing-16-v1"
    assert not is_semantic(HASHING_MODEL) and is_semantic("bge-m3")
    e.close()


def _server(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_model_server_check_lists_models_and_never_raises() -> None:
    ok = check_model_server(
        "http://localhost:11434/v1",
        client=_server(
            lambda r: httpx.Response(
                200, json={"data": [{"id": "bge-m3:latest"}, {"id": "qwen2.5:7b-instruct"}]}
            )
        ),
    )
    assert ok.reachable and ok.has("bge-m3") and ok.has("qwen2.5:7b-instruct")
    assert not ok.has("qwen2.5:14b")
    down = check_model_server(
        "http://localhost:11434/v1", client=_server(lambda r: httpx.Response(500))
    )
    assert not down.reachable and "HTTPStatusError" in down.detail
    remote = check_model_server("https://api.example.ai/v1")
    assert not remote.reachable and "refused" in remote.detail


def test_open_embedder_falls_back_or_insists() -> None:
    down = ModelServer("http://localhost:11434/v1", False, detail="refused")
    embedder, note = open_embedder("auto", "http://localhost:11434/v1", "bge-m3", server=down)
    assert isinstance(embedder, HashingEmbedder) and "no model server" in note
    missing = ModelServer("http://localhost:11434/v1", True, models=["llama3:latest"])
    _e, note = open_embedder("auto", "http://localhost:11434/v1", "bge-m3", server=missing)
    assert "ollama pull bge-m3" in note
    with pytest.raises(RuntimeError, match="not pulled"):
        open_embedder("ollama", "http://localhost:11434/v1", "bge-m3", server=missing)
    with pytest.raises(LocalOnlyError):
        open_embedder("auto", "https://api.example.ai/v1", "bge-m3", server=missing)
    ready = ModelServer("http://localhost:11434/v1", True, models=["bge-m3:latest"])
    real, note = open_embedder("auto", "http://localhost:11434/v1", "bge-m3", server=ready)
    assert real.model == "bge-m3" and note.startswith("ollama")
    real.close()
    assert open_embedder("hashing", "https://api.example.ai/v1", "bge-m3")[1].startswith("hashing")


# -- store ------------------------------------------------------------------------------


def _row(cid: str, text: str, vector: list[float], offering_id: str | None = None) -> ChunkRow:
    return ChunkRow(
        chunk_id=cid,
        run_id=RUN,
        url=f"{BASE}/{cid}",
        page_key="pg-a",
        order=0,
        kind="text",
        heading_path=["A"],
        context_header="Example",
        verbatim_text=text,
        embed_text=text,
        embed_hash=embed_hash(text),
        block_ids=["b1"],
        evidence_ids=["evidence:1"],
        offering_id=offering_id,
        labels={"role": None},
        embedding=vector,
    )


def test_sqlite_store_round_trip_search_prune_and_reset(store: SqliteVectorStore) -> None:
    assert store.vector_search([1.0, 0.0], 5, None) == []
    store.upsert_chunks(
        [
            _row("a", "Form ICC20-1234 for term conversion", [1.0, 0.0], "off-term"),
            _row("b", "Annuity income for retirement", [0.0, 1.0], "off-annuity"),
        ]
    )
    store.upsert_offerings([("off-term", "off-life", "Term"), ("off-life", None, "Life")])
    store.set_meta({"model": "m", "dims": "2"})
    assert store.meta() == {"model": "m", "dims": "2"}
    assert store.vector_search([0.9, 0.1], 5, None)[0][0] == "a"
    assert [i for i, _ in store.vector_search([0.9, 0.1], 5, {"off-annuity"})] == ["b"]
    assert store.keyword_search("ICC20 1234", 5, None)[0][0] == "a"
    assert store.keyword_search("retire", 5, {"off-term"}) == []
    assert store.keyword_search("!!!", 5, None) == []
    known = store.known_vectors([embed_hash("Annuity income for retirement"), "nope"])
    assert list(known) == [embed_hash("Annuity income for retirement")]
    got = store.get_chunks(["a", "zzz"])
    assert list(got) == ["a"] and got["a"].block_ids == ["b1"] and got["a"].heading_path == ["A"]
    assert store.offering_parents() == {"off-term": "off-life", "off-life": None}
    assert store.prune_page("pg-a", {"a"}) == 1
    assert store.counts()["chunk_vectors"] == 1
    assert store.keyword_search("annuity", 5, None) == []
    store.reset()
    assert store.counts() == {"chunk_vectors": 0, "page_vectors": 0, "offerings": 0}
    assert store.meta() == {}


def test_query_tokens_are_safe_and_unique() -> None:
    assert query_tokens('Form "ICC20-1234" OR form; DROP') == [
        "form",
        "icc20",
        "1234",
        "or",
        "drop",
    ]


def test_open_store_choices(tmp_path: Path) -> None:
    s, note = open_store("sqlite", tenant="t", sqlite_root=tmp_path, db_url=None, dims=4)
    assert note == "sqlite (asked for)" and s.location.endswith("vectors.sqlite")
    s.close()
    s, note = open_store("auto", tenant="t", sqlite_root=tmp_path, db_url=None, dims=4)
    assert "CP_DATABASE_URL is not set" in note
    s.close()
    with pytest.raises(RuntimeError, match="no database URL"):
        open_store("postgres", tenant="t", sqlite_root=tmp_path, db_url=None, dims=4)
    closed = "postgresql://u:p@127.0.0.1:1/none?connect_timeout=2"
    ok, detail = probe_pgvector(closed)
    assert not ok and "cannot connect" in detail
    s, note = open_store("auto", tenant="t", sqlite_root=tmp_path, db_url=closed, dims=4)
    assert note.startswith("sqlite fallback: postgres cannot connect")
    s.close()
    with pytest.raises(RuntimeError, match="cannot connect"):
        open_store("postgres", tenant="t", sqlite_root=tmp_path, db_url=closed, dims=4)


def test_probe_pgvector_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    mock_conn = MagicMock()
    monkeypatch.setattr("psycopg.connect", lambda *a, **kw: mock_conn)
    mock_conn.__enter__.return_value = mock_conn

    # 1. No vector extension found
    mock_conn.execute.return_value.fetchone.return_value = None
    ok, detail = probe_pgvector("postgresql://mock")
    assert not ok and "extension is not installed" in detail

    # 2. Available but not enabled
    mock_conn.execute.return_value.fetchone.return_value = ("0.8.0", None)
    ok, detail = probe_pgvector("postgresql://mock")
    assert ok and "not yet enabled" in detail

    # 3. Available and enabled
    mock_conn.execute.return_value.fetchone.return_value = ("0.8.0", "0.8.0")
    ok, detail = probe_pgvector("postgresql://mock")
    assert ok and "enabled 0.8.0" in detail


def test_pgvector_store_unit_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from src.modules.content_processor.knowledge.vector_store import (
        ChunkRow,
        PageRow,
        PgVectorStore,
    )

    mock_conn = MagicMock()
    monkeypatch.setattr("psycopg.connect", lambda *a, **kw: mock_conn)
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    store = PgVectorStore("postgresql://mock", "tenant1", 4)
    assert store.kind == "postgres"
    assert "tenant1" in store.location

    # meta
    mock_conn.execute.return_value.fetchall.return_value = [{"key": "model", "value": "test-model"}]
    assert store.meta() == {"model": "test-model"}

    # set_meta
    store.set_meta({"model": "test-model"})
    assert mock_cursor.executemany.called

    # reset
    store.reset()

    # known_vectors
    mock_conn.execute.return_value.fetchall.return_value = [
        {"embed_hash": "h1", "v": "[0.1,0.2,0.3,0.4]"}
    ]
    assert store.known_vectors(["h1"]) == {"h1": [0.1, 0.2, 0.3, 0.4]}

    # upsert_chunks
    chunk_row = ChunkRow(
        chunk_id="c1",
        run_id="r1",
        url="https://x.example/1",
        page_key="p1",
        order=0,
        kind="text",
        heading_path=["H1"],
        context_header="Header",
        verbatim_text="Text",
        embed_text="Embed",
        embed_hash="h1",
        block_ids=["b1"],
        evidence_ids=["e1"],
        embedding=[0.1, 0.2, 0.3, 0.4],
    )
    store.upsert_chunks([chunk_row])
    assert mock_cursor.executemany.called

    # prune_page
    mock_conn.execute.return_value.rowcount = 2
    assert store.prune_page("p1", {"c1"}) == 2

    # upsert_pages
    page_row = PageRow(
        page_key="p1",
        run_id="r1",
        url="https://x.example/1",
        title="Title",
        page_type="article",
        offering_id="off-1",
        embedding=[0.1, 0.2, 0.3, 0.4],
    )
    store.upsert_pages([page_row])

    # upsert_offerings & offering_parents
    store.upsert_offerings([("off-1", None, "Offering 1")])
    mock_conn.execute.return_value.fetchall.return_value = [
        {"offering_id": "off-1", "parent_id": None}
    ]
    assert store.offering_parents() == {"off-1": None}

    # vector_search
    mock_conn.execute.return_value.fetchall.return_value = [{"chunk_id": "c1", "score": 0.95}]
    assert store.vector_search([0.1, 0.2, 0.3, 0.4], 5, None) == [("c1", 0.95)]

    # keyword_search
    mock_conn.execute.return_value.fetchall.return_value = [{"chunk_id": "c1", "score": 1.5}]
    assert store.keyword_search("term", 5, {"off-1"}) == [("c1", 1.5)]
    assert store.keyword_search("", 5, None) == []

    # get_chunks
    mock_conn.execute.return_value.fetchall.return_value = [
        {
            "chunk_id": "c1",
            "run_id": "r1",
            "url": "https://x.example/1",
            "page_key": "p1",
            "order_no": 0,
            "kind": "text",
            "offering_id": "off-1",
            "chunk_role": "feature",
            "label_status": "accepted",
            "labels": "{}",
            "heading_path": "[]",
            "context_header": "Header",
            "verbatim_text": "Text",
            "embed_text": "Embed",
            "embed_hash": "h1",
            "block_ids": '["b1"]',
            "evidence_ids": '["e1"]',
            "prev_chunk_id": None,
            "next_chunk_id": None,
        }
    ]
    chunks = store.get_chunks(["c1"])
    assert "c1" in chunks and chunks["c1"].chunk_id == "c1"

    # counts
    mock_conn.execute.return_value.fetchone.return_value = {"n": 10}
    counts = store.counts()
    assert counts["chunk_vectors"] == 10

    # close
    store.close()
    assert mock_conn.close.called


# -- labels -----------------------------------------------------------------------------


def test_structure_and_text_role_signals() -> None:
    term = STAMPS["pg-term"]
    by_id = {c.chunk_id: c for c in CHUNKS}
    assert structure_role(by_id["c2"], term) == "faq"
    assert structure_role(by_id["c3"], term) == "pricing"
    assert structure_role(by_id["c4"], term) == "disclaimer"
    assert structure_role(by_id["c5"], STAMPS["pg-news"]) is None
    schema_faq = chunk("x", "pg-term", "/t", "Can I convert my term policy? Yes.", headings=["Q"])
    assert structure_role(schema_faq, term) == "faq"
    cta = chunk("y", "pg-term", "/t", "Ready? Get a quote today.", headings=["Next"])
    assert structure_role(cta, term) == "cta"
    form = chunk("z", "pg-term", "/t", "Step 1", kind="form_steps")
    assert structure_role(form, None) == "cta"
    under_questions = chunk(
        "q", "pg-term", "/t", "Yes.", headings=["Please review the questions below", "Is it free?"]
    )
    assert structure_role(under_questions, None) == "faq"

    assert text_role(PRICE_TEXT, 14) == "pricing"
    assert text_role(FAQ_TEXT, 12) == "faq"
    assert text_role(DISCLAIMER_TEXT, 24) == "disclaimer"
    assert text_role("Rated A+ by A.M. Best for financial strength.", 8) == "proof_point"
    assert text_role("Get a quote\nCall 1-800-555-0100", 6) == "cta"
    assert text_role("Get a quote " + "word " * 60, 62) is None  # too long to be a call to act
    assert text_role("The fund paid $0.0975 per share to shareholders.", 9) is None
    assert text_role(NEWS_TEXT, 13) is None
    assert text_role("It includes cover and you can add riders.", 8) == "feature"
    assert text_role("The leading and most trusted insurer.", 6) == "claim"


def test_decisions_need_two_agreeing_signals() -> None:
    assert decide_role({"structure": "faq", "text": "faq", "model": None}).status == "accepted"
    single = decide_role({"structure": None, "text": "pricing"})
    assert single.value is None and single.status == "single_signal"
    conflict = decide_role({"structure": "faq", "text": "disclaimer"})
    assert conflict.status == "review" and conflict.value is None
    assert decide_role({"structure": None}).status == "unlabelled"

    parents = {"off-term": "off-life", "off-life": None, "off-annuity": None}
    nested = decide_offering({"site": "off-life", "text": "off-term"}, parents)
    assert nested.value == "off-life" and nested.status == "accepted"
    same = decide_offering({"site": "off-term", "text": "off-term", "meaning": "off-life"}, parents)
    assert same.value == "off-term" and same.agreeing == ["site", "text"]
    assert decide_offering({"site": "off-term", "text": "off-annuity"}, parents).status == "review"
    assert decide_offering({"site": "off-term"}, parents).status == "single_signal"
    assert decide_offering({}, parents).status == "unlabelled"


def test_labeller_uses_site_text_meaning_and_model_signals() -> None:
    def reply(request: dict[str, Any]) -> dict[str, Any]:
        sections = json.loads(request["messages"][1]["content"])["sections"]
        return {
            "labels": [
                {
                    "id": s["id"],
                    "role": "pricing" if "$" in s["text"] else "bogus",
                    "offering": "off-term",
                }
                for s in sections
            ]
            + [{"id": "unknown", "role": "faq"}, "junk"]
        }

    chat = chat_with(reply)
    labeller = Labeller(STAMPS, CATALOG, chat=chat)
    labels = {lab.chunk_id: lab for lab in labeller.label(CHUNKS[:5])}
    assert labeller.model_calls == 1 and labeller.model_failures == 0
    assert labels["c3"].role.value == "pricing" and labels["c3"].role.agreeing == [
        "model",
        "structure",
        "text",
    ]
    assert labels["c2"].role.value == "faq"
    assert labels["c1"].offering.value == "off-term"
    assert labels["c1"].offering_votes["site"] == "off-term"
    assert labels["c5"].offering_votes == {"model": "off-term"}
    assert labels["c4"].as_dict()["role_status"] == "accepted"

    broken = Labeller(STAMPS, CATALOG, chat=chat_with(lambda r: "not json"))
    broken.label(CHUNKS[:2])
    assert broken.model_failures == 1

    labeller = Labeller(STAMPS, CATALOG)
    ids = [i for i, _ in labeller.offering_texts]
    labeller.set_offering_vectors(ids, [[1.0, 0.0], [0.0, 1.0], [0.6, 0.6]])
    meaning = labeller.label(CHUNKS[4:5], {"c5": [0.0, 1.0]})[0]
    assert meaning.offering_votes == {"meaning": "off-term"}
    assert labeller.label(CHUNKS[4:5], {"c5": [0.0, 0.0]})[0].offering_votes == {}
    assert Labeller(STAMPS, None).label(CHUNKS[:1])[0].offering.status == "unlabelled"


def test_site_signal_from_breadcrumb_and_url_section() -> None:
    labeller = Labeller(STAMPS, CATALOG)
    page = chunk("p", "pg-term", "/life-insurance/term/riders", "Riders add cover.")
    assert labeller.label([page])[0].offering_votes["site"] == "off-term"
    other_host = Chunk.model_validate(
        {**page.model_dump(), "url": "https://other.org/annuities/x", "page_key": "pg-x"}
    )
    assert "site" not in labeller.label([other_host])[0].offering_votes
    section = chunk("s", "pg-x", "/annuities/fixed", "Fixed annuities pay income.")
    assert labeller.label([section])[0].offering_votes["site"] == "off-annuity"
    repeated = chunk("r", "pg-x", "/x", "Annuities explained. Annuities can pay income for life.")
    assert labeller.label([repeated])[0].offering_votes["text"] == "off-annuity"


# -- indexing ---------------------------------------------------------------------------


def test_index_run_stores_labels_pages_and_reuses_vectors(store: SqliteVectorStore) -> None:
    embedder = CountingEmbedder()
    seen: list[tuple[int, int]] = []
    report = index_run(
        run_inputs(),
        store,
        embedder,
        Labeller(STAMPS, CATALOG),
        progress=lambda d, t: seen.append((d, t)),
    )
    assert report.chunks == 5 and report.pages == 2 and report.embedded == 5 and report.reused == 0
    assert seen == [(5, 5)]
    assert report.roles == {"faq": 1, "pricing": 1, "disclaimer": 1}
    assert report.offering_status["accepted"] >= 1
    assert store.counts() == {"chunk_vectors": 5, "page_vectors": 2, "offerings": 3}
    assert store.meta()["model"] == HASHING_MODEL and store.meta()["dims"] == "1024"
    row = store.get_chunks(["c3"])["c3"]
    assert row.chunk_role == "pricing" and row.label_status == "accepted"
    assert row.verbatim_text == PRICE_TEXT and row.evidence_ids == ["evidence:c3"]
    assert row.labels["role_votes"] == {"structure": "pricing", "text": "pricing"}

    changed = [*CHUNKS[:3], chunk("c4", "pg-term", "/life-insurance/term", "New wording.")]
    again = CountingEmbedder()
    second = index_run(run_inputs(changed), store, again, Labeller(STAMPS, CATALOG))
    assert second.reused == 3 and second.embedded == 1 and again.texts == 1
    assert store.counts()["chunk_vectors"] == 5  # c5 is on another page: kept
    third = index_run(run_inputs(CHUNKS[:2]), store, CountingEmbedder(), Labeller(STAMPS, CATALOG))
    assert third.removed == 2 and store.counts()["chunk_vectors"] == 3

    with pytest.raises(ModelMismatchError, match="--rebuild"):
        index_run(run_inputs(), store, CountingEmbedder("bge-m3"), Labeller(STAMPS, CATALOG))


def test_index_run_embeds_offerings_with_a_semantic_model(store: SqliteVectorStore) -> None:
    embedder = CountingEmbedder("bge-m3")
    report = index_run(run_inputs(), store, embedder, Labeller(STAMPS, CATALOG))
    assert embedder.texts == 5 + 3 and report.embed_calls == 2


def test_index_run_reports_a_failing_embedder(store: SqliteVectorStore) -> None:
    class Broken(HashingEmbedder):
        def embed(self, texts: list[str]) -> list[list[float]] | None:
            return None

    with pytest.raises(RuntimeError, match="failed twice"):
        index_run(run_inputs(), store, Broken(), Labeller(STAMPS, CATALOG))


def _write_run(folder: Path, *, catalog: bool = True, structured: bool = True) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "jh.chunks.jsonl").write_text(
        "".join(c.model_dump_json() + "\n" for c in CHUNKS), encoding="utf-8"
    )
    (folder / "jh.stamps.jsonl").write_text(
        "".join(s.model_dump_json() + "\n" for s in STAMPS.values()), encoding="utf-8"
    )
    if catalog:
        (folder / "jh.catalog.json").write_text(CATALOG.model_dump_json(), encoding="utf-8")
    if structured:
        (folder / "jh.structured.jsonl").write_text(
            json.dumps({"page_key": "pg-term", "page_type": {"page_type": "product"}})
            + "\n"
            + json.dumps({"page_key": "pg-news", "page_type": None})
            + "\n",
            encoding="utf-8",
        )


def test_load_run_and_label_review(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="run `stamp` first"):
        load_run(tmp_path, "jh")
    _write_run(tmp_path)
    run = load_run(tmp_path, "jh")
    assert (
        len(run.chunks) == 6
        and run.catalog is not None
        and run.page_types == {"pg-term": "product"}
    )
    _write_run(tmp_path / "bare", catalog=False, structured=False)
    assert load_run(tmp_path / "bare", "jh").catalog is None

    labels = Labeller(STAMPS, CATALOG).label(CHUNKS)
    files = write_label_review(labels, CHUNKS, tmp_path / "out", "jh")
    lines = Path(files["labels"]).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6 and json.loads(lines[0])["chunk_id"] == "c1"
    with zipfile.ZipFile(files["review"]) as book:
        names = book.read("xl/workbook.xml").decode()
    assert "Needs review" in names and "All chunks" in names and "How to read" in names


# -- search and answers -------------------------------------------------------------------


def test_rrf_rewards_items_found_by_both() -> None:
    fused = rrf([[("a", 0.9), ("b", 0.8)], [("b", 3.0), ("c", 2.0)]])
    assert max(fused, key=fused.__getitem__) == "b"
    assert fused["a"] == pytest.approx(1 / 61)


def test_hybrid_search_finds_exact_codes_and_filters_offerings(indexed: SqliteVectorStore) -> None:
    hits = hybrid_search(indexed, HashingEmbedder(), "ICC20-1234 form", top_k=3)
    assert hits[0].chunk.chunk_id == "c3" and hits[0].keyword_rank == 1
    assert hits[0].vector_rank is not None
    assert offering_scope(indexed, None) is None
    assert offering_scope(indexed, "off-life") == {"off-life", "off-term"}
    with pytest.raises(ValueError, match="unknown offering"):
        offering_scope(indexed, "off-nope")
    scoped = hybrid_search(indexed, HashingEmbedder(), "partnership", top_k=5, offering="off-life")
    assert all(h.chunk.offering_id in {"off-life", "off-term"} for h in scoped)
    page = render_hits("ICC20-1234 form", hits)
    assert "evidence:c3" in page and "keywords #1" in page and "> Coverage starting at $25" in page
    assert "No passage matches." in render_hits("x", [])

    class Mute(HashingEmbedder):
        def embed(self, texts: list[str]) -> list[list[float]] | None:
            return None

    with pytest.raises(RuntimeError, match="could not embed"):
        hybrid_search(indexed, Mute(), "x")


def test_check_statement_guards_against_unsupported_claims() -> None:
    source = TERM_TEXT
    quote = "convert your term policy to permanent coverage before age 70"
    assert (
        check_statement(
            "You can convert a term policy to permanent coverage before 70.", quote, source
        )
        is None
    )
    assert "not found" in (
        check_statement("Conversion is allowed.", "convert any policy at any age", source) or ""
    )
    assert "numbers" in (check_statement("You can convert before age 75.", quote, source) or "")
    assert "goes beyond" in (
        check_statement("Dividends grow tax free with guaranteed bonus returns.", quote, source)
        or ""
    )
    assert check_statement("  ", quote, source) == "empty statement"


def test_answer_with_model_keeps_only_verified_points(indexed: SqliteVectorStore) -> None:
    def reply(request: dict[str, Any]) -> dict[str, Any]:
        sources = json.loads(request["messages"][1]["content"])["sources"]
        term = next(s["id"] for s in sources if "convert your term policy" in s["text"])
        return {
            "points": [
                {
                    "statement": "You can convert a term policy to permanent cover before age 70.",
                    "source": term,
                    "quote": "convert your term policy to permanent coverage before age 70",
                },
                {
                    "statement": "Conversion costs nothing.",
                    "source": term,
                    "quote": "free conversion",
                },
                {"statement": "It is cheap.", "source": "S99", "quote": "x"},
                "junk",
            ]
        }

    answer = answer_question(
        indexed, HashingEmbedder(), "Can I convert my term policy?", chat=chat_with(reply)
    )
    assert answer.mode == "model" and len(answer.points) == 1 and len(answer.dropped) == 2
    data = answer.as_dict()
    point = data["points"][0]
    assert point["evidence_ids"] == ["evidence:c1"] and point["block_ids"] == ["blk-c1"]
    assert point["link"].startswith(BASE + "/life-insurance/term#:~:text=convert%20your")
    assert {d["reason"] for d in data["dropped"]} >= {"cites a source that was not given"}
    # neighbours of the matched passages are given to the model too
    assert any(not s["matched"] for s in data["sources"]) or len(data["sources"]) >= 2
    page = render_answer(answer)
    assert "## Sources" in page and "## Dropped" in page and "evidence:c1" in page


def test_answer_falls_back_to_quotes_when_nothing_verifies(indexed: SqliteVectorStore) -> None:
    bad = chat_with(
        lambda r: {"points": [{"statement": "Rates are 2%.", "source": "S1", "quote": "nope"}]}
    )
    answer = answer_question(indexed, HashingEmbedder(), "Can I convert my term policy?", chat=bad)
    assert answer.mode == "extractive" and "no statement of the model" in answer.note
    assert all(p.statement == p.quote for p in answer.points)
    empty = chat_with(lambda r: {"points": []})
    answer = answer_question(
        indexed, HashingEmbedder(), "Can I convert my term policy?", chat=empty
    )
    assert answer.mode == "extractive" and "found no answer" in answer.note
    silent = chat_with(lambda r: "no json here")
    answer = answer_question(
        indexed, HashingEmbedder(), "Can I convert my term policy?", chat=silent
    )
    assert "did not reply" in answer.note
    nothing = chat_with(lambda r: {"points": []})
    answer = answer_question(indexed, HashingEmbedder(), "zebra quantum", chat=nothing)
    assert answer.mode == "not_found"


def test_extractive_answer_and_not_found(indexed: SqliteVectorStore, tmp_path: Path) -> None:
    answer = answer_question(indexed, HashingEmbedder(), "Can I convert my term policy?")
    assert answer.mode == "extractive" and answer.points
    assert "convert" in answer.points[0].quote.casefold()
    assert "quoted from the pages" in render_answer(answer)
    assert answer_question(indexed, HashingEmbedder(), "zebra quantum").mode == "not_found"
    empty = SqliteVectorStore(tmp_path / "empty.sqlite")
    nothing = answer_question(empty, HashingEmbedder(), "anything at all")
    assert nothing.mode == "not_found" and "do not answer" in render_answer(nothing)
    empty.close()


def test_sentences_keep_abbreviations_and_links_highlight_quotes() -> None:
    text = "BOSTON, Nov. 30, 2022 - Example Inc. paid $0.10. Next sentence here!\nNew line."
    assert sentences(text) == [
        "BOSTON, Nov. 30, 2022 - Example Inc. paid $0.10.",
        "Next sentence here!",
        "New line.",
    ]
    assert fragment_link(BASE + "/a#top", "one two") == BASE + "/a#:~:text=one%20two"
    assert fragment_link(BASE + "/a", "") == BASE + "/a"


# -- commands -----------------------------------------------------------------------------


@pytest.fixture
def no_model_server(monkeypatch: pytest.MonkeyPatch) -> None:
    down = ModelServer("http://localhost:11434/v1", False, detail="ConnectError")
    monkeypatch.setattr(embedders_module, "check_model_server", lambda endpoint, **_: down)
    monkeypatch.setattr(kb_commands, "check_model_server", lambda endpoint, **_: down)


def test_cli_index_search_ask_and_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_model_server: None
) -> None:
    _write_run(tmp_path / "run")
    common = ["--store", "sqlite", "--store-path", str(tmp_path / "kb")]
    assert cli.main(["search", "term", *common]) == 2
    assert "store is empty" in capsys.readouterr().err

    assert cli.main(["index", "--run", "jh", "--dir", str(tmp_path / "run"), *common]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["chunks"] == 5 and summary["embedder"].startswith("hashing fallback")
    assert summary["store"] == "sqlite (asked for)" and Path(summary["files"]["review"]).exists()

    assert cli.main(["search", "ICC20-1234", *common, "--json"]) == 0
    hits = json.loads(capsys.readouterr().out)
    assert hits[0]["chunk_id"] == "c3" and hits[0]["evidence_ids"] == ["evidence:c3"]
    assert cli.main(["search", "ICC20-1234", *common]) == 0
    assert "# Search: ICC20-1234" in capsys.readouterr().out
    assert cli.main(["search", "x", *common, "--offering", "off-nope"]) == 2
    assert "unknown offering" in capsys.readouterr().err

    assert cli.main(["ask", "Can I convert my term policy?", *common]) == 0
    assert "## Sources" in capsys.readouterr().out
    assert cli.main(["ask", "Can I convert my term policy?", *common, "--json", "--no-model"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "extractive"

    assert cli.main(["kb-check", "--store-path", str(tmp_path / "kb")]) == 0
    check = json.loads(capsys.readouterr().out)
    assert check["index_would_use"]["embedder"] == "hashing fallback (words only)"
    assert check["model_server"]["reachable"] is False


def test_cli_index_refusals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_model_server: None
) -> None:
    _write_run(tmp_path / "run")
    common = ["--store", "sqlite", "--store-path", str(tmp_path / "kb")]
    assert cli.main(["index", "--run", "missing", "--dir", str(tmp_path), *common]) == 2
    run = ["index", "--run", "jh", "--dir", str(tmp_path / "run"), *common]
    assert cli.main([*run, "--embedder", "ollama"]) == 2
    assert cli.main([*run, "--llm-model", "qwen2.5:7b-instruct"]) == 2
    assert cli.main([*run, "--endpoint", "https://api.example.ai/v1"]) == 2
    assert (
        cli.main(
            [
                "index",
                "--run",
                "jh",
                "--dir",
                str(tmp_path / "run"),
                "--store",
                "postgres",
                "--db-url",
                "",
            ]
        )
        == 2
    )
    err = capsys.readouterr().err
    assert "not found" in err and "no model server" in err and "not available" in err
    assert "refused model endpoint" in err and "no database URL" in err

    assert cli.main(run) == 0
    capsys.readouterr()
    store = SqliteVectorStore(tmp_path / "kb" / "rankuno-dev" / "vectors.sqlite")
    store.set_meta({"model": "bge-m3"})
    store.close()
    assert cli.main(run) == 2
    assert "--rebuild" in capsys.readouterr().err
    assert cli.main([*run, "--rebuild"]) == 0
    assert cli.main(["ask", "q", *common, "--endpoint", "http://localhost:1/v1"]) == 0


def test_query_embedder_needs_the_stored_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_model_server: None
) -> None:
    store = SqliteVectorStore(tmp_path / "kb" / "rankuno-dev" / "vectors.sqlite")
    store.set_meta({"model": "bge-m3"})
    store.close()
    common = ["--store", "sqlite", "--store-path", str(tmp_path / "kb")]
    assert cli.main(["search", "term", *common]) == 2
    assert cli.main(["ask", "term", *common]) == 2
    assert "no model server" in capsys.readouterr().err


# -- Postgres (integration) -----------------------------------------------------------------


@pytest.mark.integration
def test_pgvector_store_round_trip(database_url: str) -> None:
    ok, detail = probe_pgvector(database_url)
    if not ok:
        pytest.skip(detail)
    tenant = f"test-{os.getpid()}"
    store = PgVectorStore(database_url, tenant, 1024)
    try:
        store.reset()
        report = index_run(run_inputs(), store, HashingEmbedder(), Labeller(STAMPS, CATALOG))
        assert report.chunks == 5 and store.counts()["chunk_vectors"] == 5
        assert store.meta()["model"] == HASHING_MODEL
        hits = hybrid_search(store, HashingEmbedder(), "ICC20-1234 form", top_k=3)
        assert hits[0].chunk.chunk_id == "c3"
        assert offering_scope(store, "off-life") == {"off-life", "off-term"}
        again = index_run(run_inputs(), store, CountingEmbedder(), Labeller(STAMPS, CATALOG))
        assert again.reused == 5 and again.embedded == 0
        assert store.prune_page("pg-term", {"c1"}) == 3
        assert store.get_chunks(["c1"])["c1"].evidence_ids == ["evidence:c1"]
    finally:
        store.reset()
        store.close()
