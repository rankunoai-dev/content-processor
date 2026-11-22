"""The unified retriever (RFC 0005 step 1): one pipeline for one website or a whole scope."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.modules.content_processor.knowledge.retrieve import (
    EmbedderMismatchError,
    Filters,
    Space,
    hybrid_search,
    mmr,
    retrieve,
)
from src.modules.content_processor.knowledge.vector_store import ChunkRow, SqliteVectorStore
from tests.modules.content_processor.test_retrieval_quality import (
    WIDTH,
    TopicEmbedder,
    WordReranker,
    row,
)


def _role(r: ChunkRow, role: str) -> ChunkRow:
    r.chunk_role = role
    return r


def _scope(tmp_path: Path, sites: dict[str, list[ChunkRow]]) -> tuple[list[Space], object]:
    spaces = []
    for name, rows in sites.items():
        store = SqliteVectorStore(tmp_path / name / "vectors.sqlite")
        store.set_meta({"model": "topic-model", "dims": str(WIDTH)})
        store.upsert_chunks(rows)
        store.close()
        spaces.append(Space(f"ns.{name}", "p", "P", name, name.upper(), "client"))

    def open_store(space: Space) -> SqliteVectorStore:
        return SqliteVectorStore(tmp_path / space.site_id / "vectors.sqlite")

    return spaces, open_store


def _go(spaces: list[Space], open_store: object, query: str, **kw: object):  # type: ignore[no-untyped-def]
    return retrieve(
        spaces,
        query,
        open_store=open_store,  # type: ignore[arg-type]
        embedder_for=lambda _m: TopicEmbedder(),
        **kw,  # type: ignore[arg-type]
    )


def test_one_copy_of_the_same_text_across_websites(tmp_path: Path) -> None:
    release = "Vitality members make an initial payment of $25 for an Apple Watch."
    spaces, open_store = _scope(
        tmp_path,
        {
            "a": [row("a1", release), row("a2", "Apple Watch payment plans run for two years.")],
            "b": [row("b1", release)],  # the same release, syndicated
        },
    )
    result = _go(spaces, open_store, "Apple Watch initial payment", k=5)
    texts = [h.hit.chunk.verbatim_text for h in result.hits]
    assert texts.count(release) == 1 and len(texts) == 2
    assert [s.site_id for s in result.searched] == ["a", "b"]


def test_role_filter_applies_before_scoring_in_every_website(tmp_path: Path) -> None:
    common = [row(f"c{i}", "Apple Watch payment details for members") for i in range(30)]
    rare = _role(row("p1", "Apple Watch pricing: $25 initial payment"), "pricing")
    spaces, open_store = _scope(tmp_path, {"a": [*common, rare]})
    found = _go(
        spaces,
        open_store,
        "Apple Watch payment",
        k=3,
        filters=Filters(roles=frozenset({"pricing"})),
    )
    assert [h.hit.chunk.chunk_id for h in found.hits] == ["p1"]
    none = _go(spaces, open_store, "Apple Watch payment", filters=Filters(roles=frozenset({"faq"})))
    assert none.hits == []


def test_per_site_quota_gives_every_website_its_best_passages(tmp_path: Path) -> None:
    spaces, open_store = _scope(
        tmp_path,
        {
            "big": [row(f"g{i}", f"Apple Watch payment option {i} for members") for i in range(25)],
            "small": [row("s1", "Our Apple Watch payment is lower")],
        },
    )
    result = _go(spaces, open_store, "Apple Watch payment", per_site=2)
    by_site = result.by_site()
    assert len(by_site["ns.big"]) == 2 and len(by_site["ns.small"]) == 1


def test_one_rerank_across_websites_with_date_banding(tmp_path: Path) -> None:
    def release(site: str, month: int) -> ChunkRow:
        return row(
            f"{site}{month}",
            f"The funds declared their monthly distribution per share of $0.{month}{site}",
            published=f"2026-{month:02d}-01",
        )

    spaces, open_store = _scope(
        tmp_path,
        {"a": [release("a", m) for m in range(1, 6)], "b": [release("b", m) for m in range(6, 11)]},
    )
    result = _go(
        spaces,
        open_store,
        "the latest monthly distribution per share",
        k=3,
        reranker=WordReranker(),
        min_score=-9,
        mmr_lambda=1.0,
    )
    # one scale for both websites, ties broken by date: the newest release of either site
    assert result.hits[0].hit.chunk.chunk_id == "b10" and result.reranked_by == "word-reranker"


def test_mmr_across_websites_uses_word_overlap() -> None:
    texts = {
        "a:1": "apple watch initial payment twenty five dollars",
        "b:1": "apple watch initial payment twenty five dollars plus tax",
        "c:1": "privacy notice personal information",
    }

    def sim(x: str, y: str) -> float:
        wx, wy = set(texts[x].split()), set(texts[y].split())
        return len(wx & wy) / len(wx | wy)

    picked = mmr(list(texts), {"a:1": 1.0, "b:1": 0.95, "c:1": 0.85}, {}, 2, 0.7, similarity=sim)
    assert picked == ["a:1", "c:1"]
    capped = mmr(
        ["a:1", "a:2", "b:1"],
        {"a:1": 1.0, "a:2": 0.9, "b:1": 0.5},
        {},
        3,
        1.0,
        group_of=lambda c: c.split(":")[0],
        quota=1,
    )
    assert capped == ["a:1", "b:1"]


def test_a_single_store_raises_where_a_scope_falls_back(tmp_path: Path) -> None:
    store = SqliteVectorStore(tmp_path / "x" / "vectors.sqlite")
    store.set_meta({"model": "other-model", "dims": str(WIDTH)})
    store.upsert_chunks([row("x1", "Apple Watch payment")])
    with pytest.raises(EmbedderMismatchError):
        hybrid_search(store, TopicEmbedder(), "Apple Watch")
    store.close()
    spaces = [Space("ns.x", "p", "P", "x", "X", "client")]
    result = _go(
        spaces, lambda s: SqliteVectorStore(tmp_path / "x" / "vectors.sqlite"), "Apple Watch"
    )
    assert [h.hit.chunk.chunk_id for h in result.hits] == ["x1"]  # by exact words
    assert any("exact words only" in why for _s, why in result.words_only)


def test_unindexed_websites_and_unknown_offerings_are_named(tmp_path: Path) -> None:
    spaces, open_store = _scope(tmp_path, {"a": [row("a1", "Apple Watch payment")]})
    empty = Space("ns.e", "p", "P", "e", "E", "competitor")
    (tmp_path / "e").mkdir()
    result = _go([*spaces, empty], open_store, "Apple Watch", filters=Filters(offering="nothing"))
    reasons = {s.site_id: why for s, why in result.skipped}
    assert "not indexed" in reasons["e"] and "no offering" in reasons["a"] and result.hits == []
    with pytest.raises(ValueError, match="unknown offering"):
        hybrid_search(
            SqliteVectorStore(tmp_path / "a" / "vectors.sqlite"),
            TopicEmbedder(),
            "x",
            offering="nothing",
        )
