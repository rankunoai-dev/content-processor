"""A query must be embedded with the model the store was built with (RFC 0004, B1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.modules.content_processor.knowledge.embedders import HashingEmbedder
from src.modules.content_processor.knowledge.retrieval import (
    EmbedderMismatchError,
    hybrid_search,
)
from src.modules.content_processor.knowledge.vector_store import ChunkRow, SqliteVectorStore


def _store(tmp_path: Path, model: str, dims: int) -> SqliteVectorStore:
    store = SqliteVectorStore(tmp_path / "vectors.sqlite")
    store.set_meta({"model": model, "dims": str(dims)})
    vector = HashingEmbedder(dims=dims).embed(["premium dividend notice to shareholders"])[0]
    store.upsert_chunks(
        [
            ChunkRow(
                chunk_id="c1",
                run_id="r",
                url="https://example.com/a",
                page_key="p",
                order=0,
                kind="text",
                heading_path=[],
                context_header="",
                verbatim_text="Premium dividend notice to shareholders.",
                embed_text="premium dividend notice",
                embed_hash="h1",
                block_ids=["b1"],
                evidence_ids=["e1"],
                embedding=vector,
            )
        ]
    )
    return store


def test_a_query_with_another_size_of_model_is_refused_with_a_clear_message(tmp_path: Path) -> None:
    store = _store(tmp_path, "bge-m3", 1024)
    with pytest.raises(EmbedderMismatchError, match="indexed with bge-m3"):
        hybrid_search(store, HashingEmbedder(dims=384), "premium dividend")


def test_a_query_with_a_different_model_of_the_same_size_is_refused_too(tmp_path: Path) -> None:
    store = _store(tmp_path, "bge-m3", 1024)
    with pytest.raises(EmbedderMismatchError):
        hybrid_search(store, HashingEmbedder(dims=1024), "premium dividend")


def test_the_same_model_searches_normally(tmp_path: Path) -> None:
    store = _store(tmp_path, "bge-m3", 1024)
    model = HashingEmbedder(dims=1024)
    model.model = "bge-m3"  # the embedder that built the store
    hits = hybrid_search(store, model, "premium dividend")
    assert hits and hits[0].chunk.chunk_id == "c1"


def test_a_store_without_a_recorded_model_is_not_refused(tmp_path: Path) -> None:
    store = SqliteVectorStore(tmp_path / "empty.sqlite")
    assert hybrid_search(store, HashingEmbedder(dims=384), "anything") == []
