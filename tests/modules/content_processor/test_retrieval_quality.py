"""Retrieval quality: the stores, dates, duplicates, MMR, the reranker, answers, pgvector.

Covers the shared vector cache, stored widths, pre-filters, date hints, one copy of a
repeated text, diversity, the relevance threshold, answer sentences and prompt order.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from src.modules.content_processor.knowledge import reranker as reranker_module
from src.modules.content_processor.knowledge import vector_store as vs
from src.modules.content_processor.knowledge.federated import Space, federated_search
from src.modules.content_processor.knowledge.indexing import embed_hash
from src.modules.content_processor.knowledge.query_dates import date_intent
from src.modules.content_processor.knowledge.retrieval import (
    Source,
    _edges,
    _extractive,
    answer_question,
    hybrid_search,
    mmr,
)
from src.modules.content_processor.knowledge.vector_store import (
    ChunkRow,
    PageRow,
    SqliteVectorStore,
    keyword_terms,
)

# a tiny meaning model: one dimension per topic, the rest zero-padded to the store width
TOPICS = ["apple watch payment", "fund distribution share", "privacy personal information", "sleep"]
WIDTH = 8


class TopicEmbedder:
    model = "topic-model"

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        out = []
        for text in texts:
            low = text.lower()
            v = [float(sum(w in low for w in topic.split())) + 0.01 for topic in TOPICS]
            out.append(v + [0.0] * (WIDTH - len(v)))
        return out

    def close(self) -> None:
        """Nothing to release."""


class WordReranker:
    """Scores by how many of the question's long words a passage holds (minus 2)."""

    model = "word-reranker"

    def __init__(self) -> None:
        self.calls = 0

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        self.calls += 1
        words = {w.strip("?.,").lower() for w in query.split() if len(w) > 4}
        return [float(sum(w in p.lower() for w in words)) - 2.0 for p in passages]


def row(
    cid: str,
    text: str,
    *,
    page: str | None = None,
    published: str | None = None,
    offering: str | None = None,
    header: str = "Example page",
) -> ChunkRow:
    vector = TopicEmbedder().embed([text])
    assert vector is not None
    return ChunkRow(
        chunk_id=cid,
        run_id="r1",
        url=f"https://x.example/{page or cid}",
        page_key=page or cid,
        order=0,
        kind="text",
        heading_path=[header],
        context_header=header,
        verbatim_text=text,
        embed_text=text,
        embed_hash=embed_hash(text + cid),
        block_ids=["b1"],
        evidence_ids=["e1"],
        offering_id=offering,
        embedding=vector[0],
        published_at=published,
    )


@pytest.fixture
def store(tmp_path: Path) -> SqliteVectorStore:
    s = SqliteVectorStore(tmp_path / "t" / "vectors.sqlite")
    s.set_meta({"model": "topic-model", "dims": str(WIDTH)})
    return s


# -- store ------------------------------------------------------------------------------


def test_vectors_are_kept_in_memory_across_opens_until_the_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "s" / "vectors.sqlite"
    first = SqliteVectorStore(path)
    first.set_meta({"model": "topic-model", "dims": str(WIDTH)})
    first.upsert_chunks([row("c1", "Apple Watch initial payment of $25")])
    first.close()
    a = SqliteVectorStore(path)
    loaded = a._load_matrix()
    a.close()
    b = SqliteVectorStore(path)
    assert b._load_matrix() is loaded  # the second open reads nothing from disk
    b.upsert_chunks([row("c2", "Fund distribution per share $0.0883")])  # a write in this process
    assert len(b._load_matrix().ids) == 2
    b.close()
    # a write by another connection (indexing runs in its own process) changes the file
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM chunk_vectors WHERE chunk_id = 'c2'")
    c = SqliteVectorStore(path)
    assert c._load_matrix().ids == ["c1"]
    c.close()


def test_cache_limit_drops_the_least_recently_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vs, "_cache_limit", lambda: 260)
    stores = []
    for name in ("a", "b"):
        s = SqliteVectorStore(tmp_path / name / "vectors.sqlite")
        s.upsert_chunks([row(f"{name}1", "Apple Watch payment")])
        s._load_matrix()
        stores.append(s)
    assert list(vs._MATRICES) == [stores[1]._key]  # only the newest fits
    monkeypatch.setattr(vs, "_cache_limit", lambda: 0)
    stores[0]._changed()
    stores[0]._load_matrix()
    assert stores[0]._key not in vs._MATRICES  # 0 = never kept


def test_vectors_are_stored_without_padding_and_read_back_at_the_store_width(
    store: SqliteVectorStore,
) -> None:
    store.upsert_chunks([row("c1", "Apple Watch payment")])
    store.upsert_pages(
        [PageRow("p1", "r1", "https://x.example/p1", "P", None, None, [1.0, 0.0] + [0.0] * 6)]
    )
    blob = store._db.execute("SELECT embedding FROM chunk_vectors").fetchone()[0]
    assert len(blob) == 4 * len(TOPICS)  # the four real numbers, not the eight-wide padding
    assert (
        len(next(iter(store.known_vectors([embed_hash("Apple Watch paymentc1")]).values())))
        == WIDTH
    )
    assert len(store.pages()[0].embedding) == WIDTH
    assert store._load_matrix().matrix.shape[1] == len(TOPICS)  # searched at the real width
    query = TopicEmbedder().embed(["apple watch"])
    assert query is not None
    assert store.vector_search(query[0], 1, None)[0][0] == "c1"


def test_an_older_store_gets_the_date_column_and_old_padded_vectors_still_search(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old" / "vectors.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        schema = vs._SQLITE_SCHEMA.replace(",\n    published_at TEXT\n", "\n")
        db.executescript(schema)
        padded = vs._unit([0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]).tobytes()  # 8 floats on disk
        db.execute(
            "INSERT INTO chunk_vectors (chunk_id, run_id, url, page_key, order_no, kind, "
            "label_status, labels, heading_path, context_header, verbatim_text, embed_text, "
            "embed_hash, block_ids, evidence_ids, embedding) VALUES "
            "('c1','r','u','p',0,'text','x','{}','[]','h','t','t','e','[]','[]',?)",
            (padded,),
        )
    store = SqliteVectorStore(path)
    assert store.get_chunks(["c1"])["c1"].published_at is None
    assert store._load_matrix().matrix.shape[1] == 2  # trailing zero columns left out in memory
    assert store.vector_search([0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], 1, None) == [("c1", 1.0)]


def test_filters_apply_before_scoring(store: SqliteVectorStore) -> None:
    rows = [row(f"w{i}", "Apple Watch payment Apple Watch", offering="watch") for i in range(30)]
    rows.append(
        row("fund", "Apple Watch payment and fund", offering="funds", published="2026-09-01")
    )
    rows.append(row("old", "fund distribution", offering="funds", published="2022-01-01"))
    store.upsert_chunks(rows)
    query = TopicEmbedder().embed(["apple watch payment"])
    assert query is not None
    # the one fund passage is found though 30 watch passages score higher overall
    assert store.vector_search(query[0], 1, {"funds"})[0][0] == "fund"
    assert store.keyword_search("apple watch payment", 1, {"funds"})[0][0] == "fund"
    assert [c for c, _ in store.vector_search(query[0], 5, None, ("2026-01-01", None))] == ["fund"]
    assert store.keyword_search("fund", 5, None, (None, "2023-01-01"))[0][0] == "old"
    assert store.vector_search(query[0], 5, {"nothing"}) == []
    assert store.keyword_search("fund", 5, set()) == []
    assert [c for c, _ in store.vector_search(query[0], 2, None)] == ["w0", "w1"]  # stable ties


def test_keyword_search_leaves_out_question_words() -> None:
    assert keyword_terms("What is the PDT rate?") == ["pdt", "rate"]
    assert keyword_terms("what is it") == ["what", "is", "it"]


# -- dates ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "target", "latest"),
    [
        ("declared on September 1, 2026?", date(2026, 9, 1), False),
        ("as of Sept. 11th 2026", date(2026, 9, 11), False),
        ("market price as of 8/31/2026", date(2026, 8, 31), False),
        ("published 2026-10-01", date(2026, 10, 1), False),
        ("HPS distribution October 2026", date(2026, 10, 15), False),
        ("in 2026", date(2026, 7, 1), False),
        ("What is the latest monthly distribution?", None, True),
        ("If I changed my address recently, in the last 15 days", None, False),
        ("February 30, 2026 or March 2026", date(2026, 3, 15), False),
    ],
)
def test_dates_in_questions(question: str, target: date | None, latest: bool) -> None:
    intent = date_intent(question)
    assert (intent.target, intent.latest) == (target, latest)


def _releases(store: SqliteVectorStore) -> None:
    store.upsert_chunks(
        [
            row(
                f"m{month}",
                f"The closed-end funds declared their monthly distribution per share of "
                f"$0.{month:02d}",
                published=f"2026-{month:02d}-01",
                header=f"Monthly distributions {month}",
            )
            for month in range(1, 11)
        ]
    )


def test_a_named_date_or_latest_moves_the_matching_release_up(store: SqliteVectorStore) -> None:
    _releases(store)
    emb = TopicEmbedder()
    plain = hybrid_search(store, emb, "monthly distribution per share", top_k=3, mmr_lambda=1.0)
    assert plain[0].date_rank is None
    question = "monthly distribution per share declared on September 1, 2026"
    plain_rank = [
        h.chunk.chunk_id
        for h in hybrid_search(store, emb, question, top_k=10, use_dates=False, mmr_lambda=1.0)
    ]
    dated_rank = [
        h.chunk.chunk_id for h in hybrid_search(store, emb, question, top_k=10, mmr_lambda=1.0)
    ]
    assert dated_rank.index("m9") < plain_rank.index("m9")  # a hint: it moves up
    # with a reranker, equal scores (the same release for another month) go by the date
    dated = hybrid_search(
        store, emb, question, top_k=3, mmr_lambda=1.0, reranker=WordReranker(), min_score=-9
    )
    assert dated[0].chunk.chunk_id == "m9" and dated[0].date_rank == 1
    latest = hybrid_search(
        store, emb, "the latest monthly distribution per share", top_k=3, mmr_lambda=1.0
    )
    assert latest[0].chunk.chunk_id == "m10"
    undated = hybrid_search(
        store, emb, "the latest monthly distribution", use_dates=False, top_k=3, mmr_lambda=1.0
    )
    assert all(h.date_rank is None for h in undated)


def test_dates_do_not_reorder_when_most_candidates_have_none(store: SqliteVectorStore) -> None:
    store.upsert_chunks([row(f"u{i}", "Apple Watch payment details") for i in range(5)])
    hits = hybrid_search(store, TopicEmbedder(), "latest Apple Watch payment", top_k=3)
    assert all(h.date_rank is None for h in hits)


# -- duplicates, diversity, reranker ------------------------------------------------------


def test_the_same_text_on_many_pages_is_kept_once(store: SqliteVectorStore) -> None:
    teaser = "Andrew Steele presents a new frontier of science on aging and sleep."
    store.upsert_chunks(
        [row(f"t{i}", teaser, page=f"p{i}") for i in range(4)]
        + [row("walker", "Dr. Matthew Walker on the four macros of good sleep.", page="w")]
    )
    hits = hybrid_search(store, TopicEmbedder(), "sleep", top_k=5)
    texts = [h.chunk.verbatim_text for h in hits]
    assert texts.count(teaser) == 1 and len(hits) == 2


def test_mmr_prefers_a_different_passage_over_a_near_copy() -> None:
    vectors = {"a": [1.0, 0.0], "a2": [0.99, 0.1], "b": [0.0, 1.0]}
    relevance = {"a": 1.0, "a2": 0.95, "b": 0.85}
    assert mmr(["a", "a2", "b"], relevance, vectors, 2, 0.75) == ["a", "b"]
    assert mmr(["a", "a2", "b"], relevance, vectors, 2, 1.0) == ["a", "a2"]
    assert mmr(["a"], relevance, vectors, 2, 0.5) == ["a"]
    assert mmr(["a", "x"], {"a": 1.0, "x": 1.0}, {"a": [1.0, 0.0]}, 2, 0.5) == ["a", "x"]


def test_reranker_threshold_drops_weak_passages_and_says_not_found(
    store: SqliteVectorStore,
) -> None:
    store.upsert_chunks(
        [
            row("pay", "Vitality PLUS members make an initial payment of $25, plus tax."),
            row("disc", "Insurance products are issued by John Hancock Life Insurance Company."),
        ]
    )
    rr = WordReranker()
    hits = hybrid_search(
        store, TopicEmbedder(), "What initial payment do Vitality members make?", reranker=rr
    )
    assert [h.chunk.chunk_id for h in hits] == ["pay"] and hits[0].rerank_score is not None
    answer = answer_question(store, TopicEmbedder(), "Are kittens covered?", reranker=rr)
    assert answer.mode == "not_found" and "word-reranker" in answer.note
    found = answer_question(
        store, TopicEmbedder(), "What initial payment do Vitality members make?", reranker=rr
    )
    assert found.mode == "extractive" and "$25" in found.points[0].statement
    assert "checked for relevance by word-reranker" in found.note
    strict = answer_question(
        store,
        TopicEmbedder(),
        "What initial payment do Vitality members make?",
        reranker=rr,
        min_score=5.0,
    )
    assert strict.mode == "not_found"


# -- answer sentences and prompt order ------------------------------------------------------


def _source(text: str, header: str = "Apple Watch Series 10 joins Vitality") -> Source:
    return Source("S1", row("c", text, header=header))


def test_extractive_skips_headings_keeps_bullet_lead_ins_and_prefers_figures() -> None:
    page = _source(
        "Apple Watch Series 10 joins Vitality\n"
        "Apple Watch is a popular part of the Vitality Program for many members.\n"
        "Vitality PLUS members make an initial payment of $25, plus tax.\n"
        "We do not:\n"
        "• sell your Personal Information to third parties for money."
    )
    points = _extractive("How much is the Apple Watch initial payment in Vitality?", [page])
    assert points[0].statement == "Vitality PLUS members make an initial payment of $25, plus tax."
    assert all(p.statement != "Apple Watch Series 10 joins Vitality" for p in points)
    privacy = _extractive("Does John Hancock sell personal information to third parties?", [page])
    assert privacy[0].statement.startswith("We do not: • sell your Personal Information")
    reranked = _extractive(
        "How much is the Apple Watch initial payment?", [page], WordReranker(), min_score=-1.5
    )
    assert reranked and "$25" in reranked[0].statement
    # sentences are only ranked by the reranker; whether to answer is decided on passages
    assert _extractive("Is there anything about pets?", [page], WordReranker()) == []  # no word


def test_sources_reach_the_model_best_first_and_second_best_last() -> None:
    groups = [[{"id": f"S{i}"}] for i in range(1, 6)]
    assert [g["id"] for g in _edges(groups)] == ["S1", "S3", "S5", "S4", "S2"]


# -- federated search and the search tool ------------------------------------------------


def test_federated_search_reranks_all_websites_on_one_scale(tmp_path: Path) -> None:
    spaces = []
    stores: dict[str, SqliteVectorStore] = {}
    for name, text in (
        ("a", "Vitality members make an initial payment of $25."),
        ("b", "Our privacy notice."),
    ):
        s = SqliteVectorStore(tmp_path / name / "vectors.sqlite")
        s.set_meta({"model": "topic-model", "dims": str(WIDTH)})
        s.upsert_chunks([row(f"{name}1", text)])
        s.close()
        stores[name] = s
        spaces.append(Space(f"ns.{name}", "p", "P", name, name.upper(), "client"))

    def open_store(space: Space) -> SqliteVectorStore:
        return SqliteVectorStore(tmp_path / space.site_id / "vectors.sqlite")

    result = federated_search(
        spaces,
        "What initial payment do Vitality members make?",
        open_store=open_store,
        embedder_for=lambda _m: TopicEmbedder(),
        reranker=WordReranker(),
    )
    assert [h.space.site_id for h in result.hits] == ["a"]
    assert result.reranked_by == "word-reranker" and result.below_relevance == 1


# -- reranker loading ---------------------------------------------------------------------


def test_get_reranker_is_off_in_tests_and_a_failed_load_is_not_retried_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.config import reset_settings_cache

    assert reranker_module.get_reranker() is None  # CP_RERANK=off in every test
    monkeypatch.setenv("CP_RERANK", "auto")
    reset_settings_cache()
    attempts: list[bool] = []

    def failing(*_a: Any, download: bool = False, **_k: Any) -> Any:
        attempts.append(download)
        raise RuntimeError("not in the model cache")

    monkeypatch.setattr(reranker_module, "CrossEncoderReranker", failing)
    assert reranker_module.get_reranker() is None
    assert reranker_module.get_reranker() is None
    assert attempts == [False]  # auto never downloads, and the failure is remembered
    reranker_module.reset_reranker()
    monkeypatch.setattr(reranker_module, "CrossEncoderReranker", lambda *_a, **_k: WordReranker())
    first = reranker_module.get_reranker()
    assert first is not None and reranker_module.get_reranker() is first


def test_cross_encoder_wraps_the_model_and_clips_long_passages() -> None:
    class Backend:
        def rerank(self, query: str, texts: list[str], batch_size: int = 64) -> list[float]:
            assert all(len(t) <= 2_000 for t in texts)
            return [float(len(t)) for t in texts]

    model = reranker_module.CrossEncoderReranker("m", backend=Backend())
    assert model.score("q", ["ab", "x" * 5_000]) == [2.0, 2000.0]
    assert model.score("q", []) == []


# -- pgvector -----------------------------------------------------------------------------


class _Conn:
    """Records statements; answers like a pgvector server of the given version."""

    def __init__(self, version: str, rows: list[list[dict[str, Any]]]) -> None:
        self.version = version
        self.rows = rows
        self.sql: list[str] = []

    def execute(self, statement: Any, params: Any = None) -> Any:
        text = statement.as_string(None) if hasattr(statement, "as_string") else str(statement)
        self.sql.append(text)
        conn = self

        class Result:
            def fetchone(self) -> dict[str, Any] | None:
                return {"extversion": conn.version} if "extversion" in text else None

            def fetchall(self) -> list[dict[str, Any]]:
                return conn.rows.pop(0) if "ORDER BY embedding" in text and conn.rows else []

        return Result()

    def transaction(self) -> Any:
        class Tx:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *_exc: object) -> None:
                return None

        return Tx()


def _pg(
    monkeypatch: pytest.MonkeyPatch, version: str, rows: list[list[dict[str, Any]]]
) -> tuple[Any, _Conn]:
    conn = _Conn(version, rows)
    monkeypatch.setattr("psycopg.connect", lambda *_a, **_k: conn)
    return vs.PgVectorStore("postgresql://mock", "tenant1", 4), conn


def test_pgvector_raises_search_depth_and_walks_on_for_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, conn = _pg(
        monkeypatch, "0.8.0", [[{"chunk_id": "b", "score": 0.5}, {"chunk_id": "a", "score": 0.9}]]
    )
    assert store.iterative_scan
    hits = store.vector_search([1.0, 0.0, 0.0, 0.0], 60, {"off"}, ("2026-01-01", "2026-12-31"))
    assert hits == [("a", 0.9), ("b", 0.5)]  # relaxed order put back in score order
    assert "SET LOCAL hnsw.ef_search = 120" in conn.sql
    assert "SET LOCAL hnsw.iterative_scan = relaxed_order" in conn.sql
    assert not any("enable_indexscan" in s for s in conn.sql)
    assert any("published_at >= %s" in s for s in conn.sql)


def test_older_pgvector_reruns_a_short_filtered_search_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, conn = _pg(
        monkeypatch,
        "0.7.4",
        [
            [{"chunk_id": "a", "score": 0.9}],
            [{"chunk_id": "a", "score": 0.9}, {"chunk_id": "b", "score": 0.4}],
        ],
    )
    assert not store.iterative_scan
    assert store.vector_search([1.0, 0.0, 0.0, 0.0], 2, None) == [("a", 0.9), ("b", 0.4)]
    assert "SET LOCAL hnsw.ef_search = 100" in conn.sql
    assert "SET LOCAL enable_indexscan = off" in conn.sql


def test_the_pgvector_check_is_remembered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_probe(url: str) -> tuple[bool, str]:
        calls.append(url)
        return False, "no pgvector"

    monkeypatch.setattr(vs, "probe_pgvector", fake_probe)
    vs._PROBES.clear()
    for _ in range(3):
        store, note = vs.open_store(
            "auto", tenant="t", sqlite_root=tmp_path, db_url="postgresql://x", dims=4
        )
        store.close()
    assert calls == ["postgresql://x"] and "sqlite fallback" in note
    vs._PROBES.clear()
