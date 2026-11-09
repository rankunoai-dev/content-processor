"""The CPU fallback embedder: a small multilingual model run in this process ($0, offline).

When Ollama with bge-m3 is not available, meaning-based search, labels by similarity,
duplicates by meaning and topic drift use a small sentence-embedding model run on the
CPU through `fastembed` (onnxruntime): by default
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384 numbers per text,
about 220 MB, 50+ languages). It finds paraphrases and synonyms, unlike the hashing
stand-in, but is weaker than bge-m3, reads at most about 128 words of each text, and is
slower on a large site.

- The model is downloaded once into `CP_MODEL_CACHE_DIR` and then works offline.
- Vectors are padded with zeros to the store's 1,024 numbers: padding changes neither
  the length of a vector nor the angle between two vectors, so similarity is exact.
- Its model name in a store is `cpu:<model>`, so search embeds queries with the same
  model, and a better model (bge-m3) rebuilds the store when it arrives.
- `fastembed` is the optional `fallback` extra; without it the chain goes straight to
  the hashing stand-in.
"""

from __future__ import annotations

import importlib
import math
import os
import threading
from pathlib import Path
from typing import Any, Final

__all__ = [
    "CPU_PREFIX",
    "DEFAULT_CPU_MODEL",
    "CpuEmbedder",
    "cpu_available",
    "shared_cpu_embedder",
]

CPU_PREFIX: Final = "cpu:"
DEFAULT_CPU_MODEL: Final = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_BATCH: Final = 32
_LOCK = threading.Lock()
_SHARED: dict[tuple[str, int], CpuEmbedder] = {}


def cpu_available() -> bool:
    """True when `fastembed` is installed."""
    try:
        importlib.import_module("fastembed")
    except ImportError:
        return False
    return True


class CpuEmbedder:
    """Sentence vectors from a fastembed model on the CPU, padded to `dims` numbers."""

    def __init__(
        self,
        model_name: str = DEFAULT_CPU_MODEL,
        *,
        dims: int = 1024,
        cache_dir: Path | None = None,
        backend: Any = None,
    ) -> None:
        """Load the model (downloaded once into `cache_dir`); raises `RuntimeError`.

        `backend` replaces fastembed's `TextEmbedding` in tests.
        """
        self.model_name = model_name
        self.model = CPU_PREFIX + model_name
        self.dims = dims
        if backend is not None:
            self._model = backend
            return
        # a downloaded model needs neither symlinks (Windows) nor a Hugging Face account
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
        try:
            fastembed = importlib.import_module("fastembed")
        except ImportError as exc:
            msg = "the CPU fallback needs fastembed: pip install 'content-processor[fallback]'"
            raise RuntimeError(msg) from exc
        try:
            self._model = fastembed.TextEmbedding(
                model_name=model_name, cache_dir=str(cache_dir) if cache_dir else None
            )
        except Exception as exc:  # noqa: BLE001 - download, unknown model, broken cache
            msg = f"the CPU model {model_name} could not be loaded: {type(exc).__name__}: {exc}"
            raise RuntimeError(msg[:400]) from exc

    def _pad(self, raw: Any) -> list[float]:
        values = [float(x) for x in raw]
        norm = math.sqrt(sum(x * x for x in values))
        if norm:
            values = [x / norm for x in values]
        if len(values) > self.dims:
            msg = (
                f"{self.model_name} gives {len(values)} numbers, more than the store's {self.dims}"
            )
            raise ValueError(msg)
        return values + [0.0] * (self.dims - len(values))

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """One vector per text, or None when the model fails."""
        if not texts:
            return []
        try:
            with _LOCK:  # one onnxruntime session; calls are serialised
                rows = list(self._model.embed(texts, batch_size=_BATCH))
            return [self._pad(r) for r in rows]
        except (RuntimeError, ValueError, MemoryError):
            return None

    def close(self) -> None:
        """Kept loaded: shared instances serve every search."""


def shared_cpu_embedder(
    model_name: str = DEFAULT_CPU_MODEL, *, dims: int = 1024, cache_dir: Path | None = None
) -> CpuEmbedder:
    """One loaded model per process (loading takes a second or two)."""
    key = (model_name, dims)
    with _LOCK:
        found = _SHARED.get(key)
    if found is None:
        found = CpuEmbedder(model_name, dims=dims, cache_dir=cache_dir)
        with _LOCK:
            _SHARED.setdefault(key, found)
    return _SHARED[key]
