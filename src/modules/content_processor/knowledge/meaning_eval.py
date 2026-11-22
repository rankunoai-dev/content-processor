"""Measure how well an embedder finds passages by meaning across languages (RFC 0004, slice 3).

The dataset (`fixtures/meaning/cross_language.json`) pairs queries with the passages that
answer them, in other languages. `evaluate` embeds the passages once, embeds each query,
ranks the passages by cosine similarity and reports recall@1, recall@3, the mean reciprocal
rank, and the time spent. The same measurement runs for any embedder: the CPU MiniLM model,
bge-m3 on Ollama, or the words-only stand-in, so the choice is made on numbers.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.content_processor.knowledge.embedders import Embedder

__all__ = ["Dataset", "EvalResult", "evaluate", "load_dataset"]

DEFAULT_DATASET = (
    Path(__file__).resolve().parents[4] / "fixtures" / "meaning" / "cross_language.json"
)


class Dataset(StrictModel):
    """Passages, and queries with the passage ids that answer them."""

    status: str
    languages: list[str] = Field(default_factory=list)
    passages: list[dict[str, Any]]
    queries: list[dict[str, Any]]


class EvalResult(StrictModel):
    """What one embedder scored on the dataset."""

    model: str
    queries: int
    recall_at_1: float = Field(ge=0.0, le=1.0)
    recall_at_3: float = Field(ge=0.0, le=1.0)
    mean_reciprocal_rank: float = Field(ge=0.0, le=1.0)
    cross_language_recall_at_3: float = Field(ge=0.0, le=1.0)
    embed_passages_s: float = Field(ge=0.0)
    mean_query_ms: float = Field(ge=0.0)
    per_query: dict[str, float] = Field(default_factory=dict)


def load_dataset(path: Path = DEFAULT_DATASET) -> Dataset:
    """Read and check the dataset: every relevant id must name a passage."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    dataset = Dataset(**raw)
    ids = {p["id"] for p in dataset.passages}
    for query in dataset.queries:
        missing = [r for r in query["relevant"] if r not in ids]
        if missing:
            msg = f"query {query['id']} names unknown passages: {', '.join(missing)}"
            raise ValueError(msg)
    return dataset


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1.0, norms)


def evaluate(embedder: Embedder, dataset: Dataset | None = None) -> EvalResult:
    """Score `embedder` on the dataset. Raises when the embedder cannot answer."""
    data = dataset or load_dataset()
    passages = data.passages
    started = time.perf_counter()
    passage_vectors = embedder.embed([p["text"] for p in passages])
    embed_passages_s = time.perf_counter() - started
    if passage_vectors is None:
        msg = f"{embedder.model} could not embed the passages"
        raise RuntimeError(msg)
    matrix = _normalize(np.asarray(passage_vectors, dtype=np.float32))
    ids = [p["id"] for p in passages]
    lang = {p["id"]: p["lang"] for p in passages}

    hits1: list[float] = []
    hits3: list[float] = []
    reciprocal: list[float] = []
    cross: list[float] = []
    per_query: dict[str, float] = {}
    query_ms: list[float] = []
    for query in data.queries:
        started = time.perf_counter()
        vector = embedder.embed([query["text"]])
        query_ms.append((time.perf_counter() - started) * 1000)
        if vector is None:
            msg = f"{embedder.model} could not embed query {query['id']}"
            raise RuntimeError(msg)
        q = _normalize(np.asarray(vector, dtype=np.float32))[0]
        order = [ids[i] for i in np.argsort(-(matrix @ q))]
        relevant = set(query["relevant"])
        ranks = [n for n, pid in enumerate(order, start=1) if pid in relevant]
        best = ranks[0]
        hits1.append(1.0 if best <= 1 else 0.0)
        hits3.append(1.0 if best <= 3 else 0.0)
        reciprocal.append(1.0 / best)
        per_query[query["id"]] = round(1.0 / best, 4)
        other_language = [pid for pid in relevant if lang[pid] != query["lang"]]
        if other_language:
            found = any(pid in set(order[:3]) for pid in other_language)
            cross.append(1.0 if found else 0.0)

    return EvalResult(
        model=embedder.model,
        queries=len(data.queries),
        recall_at_1=round(float(np.mean(hits1)), 4),
        recall_at_3=round(float(np.mean(hits3)), 4),
        mean_reciprocal_rank=round(float(np.mean(reciprocal)), 4),
        cross_language_recall_at_3=round(float(np.mean(cross)), 4) if cross else 0.0,
        embed_passages_s=round(embed_passages_s, 3),
        mean_query_ms=round(float(np.mean(query_ms)), 2),
        per_query=per_query,
    )
