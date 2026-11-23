"""The context builder (RFC 0005 step 2): each hit with its section, within a budget."""

from __future__ import annotations

from pathlib import Path

from src.modules.content_processor.knowledge.context import build_context, context_for
from src.modules.content_processor.knowledge.retrieval import answer_question
from src.modules.content_processor.knowledge.retrieve import Space, retrieve
from src.modules.content_processor.knowledge.vector_store import ChunkRow, SqliteVectorStore
from tests.modules.content_processor.test_retrieval_quality import WIDTH, TopicEmbedder, row

SPACE = Space("ns.a", "p", "P", "a", "A", "client")
WORDS = " ".join(["filler"] * 99)  # 100 words, about 150 tokens with the label


def _page(page: str, sections: list[tuple[str, int]]) -> list[ChunkRow]:
    """A page of passages: (top heading, how many passages under it), in order."""
    out: list[ChunkRow] = []
    for heading, count in sections:
        for _ in range(count):
            n = len(out)
            c = row(f"{page}-{n}", f"{page} passage {n}. {WORDS}", page=page, header=heading)
            c.order = n
            out.append(c)
    for i, c in enumerate(out):
        c.prev_chunk_id = out[i - 1].chunk_id if i else None
        c.next_chunk_id = out[i + 1].chunk_id if i + 1 < len(out) else None
    return out


def _lookup(*pages: list[ChunkRow]):  # type: ignore[no-untyped-def]
    by_url = {p[0].url: p for p in pages}
    calls: list[str] = []

    def page_chunks(_space: Space, url: str) -> list[ChunkRow]:
        calls.append(url)
        return list(by_url[url])

    return page_chunks, calls


def _ids(rows: list[ChunkRow]) -> list[str]:
    return [c.chunk_id for c in rows]


def test_a_hit_comes_with_its_whole_section_and_one_passage_across_each_edge() -> None:
    page = _page("p", [("Intro", 2), ("Fees", 3), ("Claims", 2)])
    page_chunks, _ = _lookup(page)
    found = build_context([(SPACE, page[3])], page_chunks, budget=10_000)
    (stretch,) = found.stretches
    # the Fees section (2, 3, 4) and one passage of Intro and of Claims, not the whole page
    assert _ids(stretch.chunks) == ["p-1", "p-2", "p-3", "p-4", "p-5"]
    assert stretch.complete and stretch.section == "Fees" and found.cut == 0
    before, after = stretch.around("p-3")
    assert _ids(before) == ["p-1", "p-2"] and _ids(after) == ["p-4", "p-5"]


def test_the_budget_is_shared_fairly_nearest_first() -> None:
    a = _page("a", [("Long", 9)])
    b = _page("b", [("Long", 9)])
    page_chunks, calls = _lookup(a, b)
    one = a[0].verbatim_text
    cost = -(-len(one) // 4)
    found = build_context([(SPACE, a[4]), (SPACE, b[4])], page_chunks, budget=cost * 6)
    first, second = found.stretches
    # six passages in all: each hit plus two neighbours, one after and one before
    assert _ids(first.chunks) == ["a-3", "a-4", "a-5"]
    assert _ids(second.chunks) == ["b-3", "b-4", "b-5"]
    assert found.cut == 2 and found.tokens <= found.budget and found.added == 4
    assert calls == [a[0].url, b[0].url], "each page is read once"


def test_hits_always_stay_even_over_budget_and_zero_means_hits_alone() -> None:
    page = _page("p", [("S", 5)])
    page_chunks, _ = _lookup(page)
    found = build_context([(SPACE, page[1]), (SPACE, page[3])], page_chunks, budget=0)
    assert [_ids(s.chunks) for s in found.stretches] == [["p-1"], ["p-3"]] and found.added == 0


def test_hits_on_one_page_that_meet_become_one_stretch() -> None:
    page = _page("p", [("S", 6)])
    page_chunks, _ = _lookup(page)
    found = build_context([(SPACE, page[3]), (SPACE, page[1])], page_chunks, budget=10_000)
    (stretch,) = found.stretches
    assert _ids(stretch.chunks) == [f"p-{i}" for i in range(6)]
    assert stretch.matched == ["p-3", "p-1"] and stretch.rank == 0


def test_a_hit_missing_from_its_page_is_kept_alone() -> None:
    page = _page("p", [("S", 3)])
    stale = row("gone", "an older passage", page="p")
    page_chunks, _ = _lookup(page)
    found = build_context([(SPACE, stale)], page_chunks, budget=10_000)
    assert _ids(found.stretches[0].chunks) == ["gone"]


def _store(tmp_path: Path, rows: list[ChunkRow]) -> SqliteVectorStore:
    store = SqliteVectorStore(tmp_path / "a" / "vectors.sqlite")
    store.set_meta({"model": "topic-model", "dims": str(WIDTH)})
    store.upsert_chunks(rows)
    return store


def test_answers_and_scope_searches_get_the_section(tmp_path: Path) -> None:
    lead = row("t-0", "Apple Watch offer for Vitality members:", page="t", header="Offer")
    rest = row("t-1", "- the initial payment is $25 plus tax", page="t", header="Offer")
    other = row("t-2", "Privacy notice for personal information", page="t", header="Legal")
    for i, c in enumerate((lead, rest, other)):
        c.order = i
    store = _store(tmp_path, [lead, rest, other])
    answer = answer_question(store, TopicEmbedder(), "Apple Watch offer", top_k=1)
    assert {s.chunk.chunk_id for s in answer.sources} >= {"t-0", "t-1"}
    assert [s.chunk.chunk_id for s in answer.sources if s.matched] == ["t-0"]
    store.close()

    result = retrieve(
        [SPACE],
        "Apple Watch offer",
        open_store=lambda _s: SqliteVectorStore(tmp_path / "a" / "vectors.sqlite"),
        embedder_for=lambda _m: TopicEmbedder(),
        k=1,
    )
    found = context_for(
        result, lambda _s: SqliteVectorStore(tmp_path / "a" / "vectors.sqlite"), budget=10_000
    )
    (stretch,) = found.stretches
    assert _ids(stretch.chunks) == ["t-0", "t-1", "t-2"]  # the section, then one across
    assert stretch.section == "Offer" and stretch.complete


def test_each_reader_has_its_own_budget() -> None:
    from src.core.config import get_settings
    from src.modules.content_processor.knowledge.context import context_budget

    settings = get_settings()
    assert context_budget() == settings.cp_context_tokens
    assert context_budget(reader="search") == settings.cp_search_context_tokens
    assert context_budget(-5) == 0 and context_budget(123, "search") == 123
