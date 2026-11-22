"""The cross-language measurement: the dataset is consistent and the scorer runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.modules.content_processor.knowledge.embedders import HashingEmbedder
from src.modules.content_processor.knowledge.meaning_eval import evaluate, load_dataset


def test_the_draft_dataset_is_consistent() -> None:
    dataset = load_dataset()
    assert dataset.status.startswith("DRAFT")
    assert len(dataset.passages) >= 10 and len(dataset.queries) >= 10
    langs = {p["lang"] for p in dataset.passages}
    assert {"en", "hi"} <= langs
    assert any(q["lang"] != "en" for q in dataset.queries)


def test_a_query_naming_an_unknown_passage_is_refused(tmp_path: Path) -> None:
    raw = json.loads(
        Path(__file__)
        .resolve()
        .parents[3]
        .joinpath("fixtures/meaning/cross_language.json")
        .read_text(encoding="utf-8")
    )
    raw["queries"][0]["relevant"] = ["p-does-not-exist"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown passages"):
        load_dataset(path)


def test_the_scorer_runs_and_reports_bounded_numbers() -> None:
    result = evaluate(HashingEmbedder())
    assert result.queries == len(load_dataset().queries)
    for value in (result.recall_at_1, result.recall_at_3, result.mean_reciprocal_rank):
        assert 0.0 <= value <= 1.0
    assert result.recall_at_1 <= result.recall_at_3
    assert set(result.per_query) == {q["id"] for q in load_dataset().queries}


def test_an_embedder_that_cannot_answer_is_reported_not_scored() -> None:
    class Broken(HashingEmbedder):
        def embed(self, texts: list[str]) -> list[list[float]] | None:
            return None

    with pytest.raises(RuntimeError, match="could not embed"):
        evaluate(Broken())
