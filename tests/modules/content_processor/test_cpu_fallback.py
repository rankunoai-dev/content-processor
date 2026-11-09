"""The fallback chain: Ollama, then the CPU model, then hashing; padding; queries by model."""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from typing import Any

import pytest

from src.core.config import reset_settings_cache
from src.modules.content_processor.knowledge import cpu_embedder as cpu_module
from src.modules.content_processor.knowledge import embedders as embedders_module
from src.modules.content_processor.knowledge.cpu_embedder import CPU_PREFIX, CpuEmbedder
from src.modules.content_processor.knowledge.embedders import (
    HASHING_MODEL,
    HashingEmbedder,
    ModelServer,
    embedder_for_model,
    model_kind,
    model_rank,
    open_embedder,
)

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


class FakeModel:
    """fastembed's TextEmbedding: 4 numbers per text, from a tiny word table."""

    TABLE = {
        "beneficiary": [3.0, 0, 0, 0],
        "bénéficiaire": [3.0, 0.1, 0, 0],
        "dividend": [0, 0, 2.0, 0],
    }

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def embed(self, texts: list[str], batch_size: int = 32) -> Iterator[list[float]]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("onnx failed")
        for t in texts:
            yield next((v for w, v in self.TABLE.items() if w in t.casefold()), [0, 0, 0, 1.0])


def _cos(a: Iterable[float], b: Iterable[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_vectors_are_padded_without_changing_similarity() -> None:
    e = CpuEmbedder(MODEL, dims=8, backend=FakeModel())
    assert e.model == CPU_PREFIX + MODEL
    a, b, c = (
        e.embed(["Change the beneficiary", "Changer le bénéficiaire", "Dividend notice"]) or []
    )
    assert len(a) == 8 and a[4:] == [0.0, 0.0, 0.0, 0.0]
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0)
    raw = (3.0 * 3.0 + 0 * 0.1) / (3.0 * math.sqrt(9.0 + 0.01))
    assert math.isclose(_cos(a, b), raw) and _cos(a, c) == 0.0
    assert CpuEmbedder(MODEL, dims=8, backend=FakeModel(fail=True)).embed(["x"]) is None
    assert CpuEmbedder(MODEL, dims=2, backend=FakeModel()).embed(["x"]) is None  # too long
    assert e.embed([]) == []


def test_model_kinds_and_ranks() -> None:
    assert model_kind(HASHING_MODEL) == "words only" and model_rank(HASHING_MODEL) == 0
    assert model_kind(CPU_PREFIX + MODEL) == "CPU fallback" and model_rank(CPU_PREFIX + MODEL) == 1
    assert model_kind("bge-m3") == "model server" and model_rank("bge-m3") == 2
    assert model_rank(None) == -1


@pytest.fixture
def server_down(monkeypatch: pytest.MonkeyPatch) -> None:
    down = ModelServer("http://localhost:11434/v1", False, detail="ConnectError")
    monkeypatch.setattr(embedders_module, "check_model_server", lambda endpoint, **_: down)


@pytest.fixture
def cpu_on(monkeypatch: pytest.MonkeyPatch) -> FakeModel:
    fake = FakeModel()
    monkeypatch.setenv("CP_FALLBACK_EMBED_MODEL", MODEL)
    reset_settings_cache()
    monkeypatch.setattr(cpu_module, "_SHARED", {})
    monkeypatch.setattr(embedders_module, "cpu_available", lambda: True)

    def shared(name: str, *, dims: int, cache_dir: Any = None) -> CpuEmbedder:
        return CpuEmbedder(name, dims=dims, backend=fake)

    monkeypatch.setattr(embedders_module, "shared_cpu_embedder", shared)
    return fake


def test_auto_takes_the_cpu_model_when_the_server_is_down(
    server_down: None, cpu_on: FakeModel
) -> None:
    embedder, note = open_embedder("auto", "http://localhost:11434/v1", "bge-m3")
    assert embedder.model == CPU_PREFIX + MODEL
    assert note.startswith(f"CPU fallback {MODEL}: no model server")
    cpu, note = open_embedder("cpu", "http://localhost:11434/v1", "bge-m3")
    assert cpu.model == CPU_PREFIX + MODEL and note.endswith("(asked for)")
    same = embedder_for_model(CPU_PREFIX + MODEL, "http://localhost:11434/v1")
    assert same.model == CPU_PREFIX + MODEL  # queries of a CPU-built store use the CPU model
    assert isinstance(embedder_for_model(HASHING_MODEL, "x"), HashingEmbedder)
    with pytest.raises(RuntimeError, match="no model server"):
        embedder_for_model("bge-m3", "http://localhost:11434/v1")


def test_auto_falls_back_to_hashing_with_the_reason(
    server_down: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embedders_module, "cpu_available", lambda: False)
    embedder, note = open_embedder("auto", "http://localhost:11434/v1", "bge-m3")
    assert embedder.model == HASHING_MODEL and "fastembed not installed" in note
    monkeypatch.setattr(embedders_module, "cpu_available", lambda: True)
    off, why = open_embedder("auto", "http://localhost:11434/v1", "bge-m3")  # setting is ""
    assert off.model == HASHING_MODEL and "CP_FALLBACK_EMBED_MODEL is empty" in why


def test_missing_fastembed_is_said(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_module(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(cpu_module.importlib, "import_module", no_module)
    assert cpu_module.cpu_available() is False
    with pytest.raises(RuntimeError, match="needs fastembed"):
        CpuEmbedder(MODEL)


def test_search_says_which_model_answered_for_each_website(tmp_path: Any) -> None:
    from src.modules.content_processor.knowledge.federated import Space, federated_search
    from src.modules.content_processor.knowledge.indexing import index_run
    from src.modules.content_processor.knowledge.labels import Labeller
    from src.modules.content_processor.knowledge.vector_store import SqliteVectorStore
    from tests.modules.content_processor.test_knowledge_vector import CATALOG, STAMPS, run_inputs

    cpu = CpuEmbedder(MODEL, dims=1024, backend=FakeModel())
    roots = {"cpu": tmp_path / "cpu" / "v.sqlite", "words": tmp_path / "words" / "v.sqlite"}
    for name, embedder in (("cpu", cpu), ("words", HashingEmbedder())):
        store = SqliteVectorStore(roots[name])
        index_run(run_inputs(), store, embedder, Labeller(STAMPS, CATALOG))
        store.close()
    spaces = [Space(n, "p", "Project", n, n.title(), "competitor") for n in roots]
    result = federated_search(
        spaces,
        "annual fee",
        open_store=lambda s: SqliteVectorStore(roots[s.namespace]),
        embedder_for=lambda model: cpu if model.startswith(CPU_PREFIX) else HashingEmbedder(),
    )
    assert [s.namespace for s in result.searched] == ["cpu", "words"]
    assert [(s.namespace, why) for s, why in result.lighter] == [
        ("cpu", "meaning from the lighter CPU fallback model (not bge-m3)")
    ]
    assert [(s.namespace, why) for s, why in result.words_only] == [
        ("words", "indexed without a meaning model: searched by exact words")
    ]
