"""Indexing safety (RFC 0004, B4, B5, B7): splits, size checks, and the upgrade probe."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.modules.content_processor.knowledge.embedders import HashingEmbedder
from src.modules.content_processor.knowledge.indexing import (
    IndexReport,
    ModelMismatchError,
    _embed_all,
    index_run,
)
from src.modules.content_processor.knowledge.labels import Labeller
from src.modules.content_processor.knowledge.vector_store import SqliteVectorStore
from tests.modules.content_processor.test_knowledge_vector import (
    CATALOG,
    STAMPS,
    CountingEmbedder,
    run_inputs,
)


class FlakyBatches(HashingEmbedder):
    """Refuses requests with more than `limit` texts, like a server that times out on big ones."""

    def __init__(self, limit: int) -> None:
        super().__init__(dims=64)
        self.limit = limit
        self.model = "bge-m3"

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        if len(texts) > self.limit:
            return None
        return super().embed(texts)


class ChangingSize(HashingEmbedder):
    """Answers the first batch with 64-number vectors and later ones with 32-number vectors."""

    def __init__(self) -> None:
        super().__init__(dims=64)
        self.model = "bge-m3"
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        self.calls += 1
        dims = 64 if self.calls == 1 else 32
        return [[1.0] * dims for _ in texts]


class NoAnswer(HashingEmbedder):
    """A model server that is down: never returns vectors."""

    def __init__(self) -> None:
        super().__init__(dims=64)
        self.model = "bge-m3"

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        return None


def test_a_batch_that_fails_is_split_until_the_pieces_succeed() -> None:
    texts = {f"k{i}": f"text {i}" for i in range(10)}
    report = IndexReport()
    vectors = _embed_all(FlakyBatches(limit=2), texts, report, None)
    assert set(vectors) == set(texts)
    assert report.embed_calls > 2  # the split pieces were requested too


def test_vectors_of_different_sizes_in_one_run_are_refused() -> None:
    texts = {f"k{i}": f"text {i}" for i in range(40)}
    with pytest.raises(RuntimeError, match="cannot hold both"):
        _embed_all(ChangingSize(), texts, IndexReport(), None)


def test_a_model_that_is_down_does_not_cost_a_better_store_its_vectors(tmp_path: Path) -> None:
    store = SqliteVectorStore(tmp_path / "vectors.sqlite")
    index_run(run_inputs(), store, CountingEmbedder(), Labeller(STAMPS, CATALOG))
    before = store.counts()
    stored = store.meta()["model"]
    with pytest.raises(ModelMismatchError, match="nothing was changed"):
        index_run(run_inputs(), store, NoAnswer(), Labeller(STAMPS, CATALOG))
    assert store.meta()["model"] == stored
    assert store.counts() == before
    store.close()
