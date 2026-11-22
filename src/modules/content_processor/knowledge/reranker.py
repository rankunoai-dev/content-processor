"""A cross-encoder reranker: reads the question together with each passage and scores fit.

The first stage (hybrid search: meaning vectors plus BM25 words, fused by reciprocal rank
fusion) is fast but compares the question and a passage only through their separate
vectors and shared words. A cross-encoder reads both at once, so its score says how well
the passage *answers* the question, on a scale that holds across questions: that makes
a relevance threshold possible (the meaning score of the bi-encoder does not separate
answerable from unanswerable questions; measured on John Hancock, see the RFC note in
`docs/app/RETRIEVAL.md`).

The default model, `jinaai/jina-reranker-v2-base-multilingual`, reads 100+ languages,
like bge-m3. It runs on the CPU through fastembed and needs about 1.1 GB of memory, so
by default (`CP_RERANK=auto`) it is used only when it is already in the model cache; a
search never downloads it.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Sequence
from typing import Any, Protocol

from src.core.logger import get_logger

__all__ = ["CrossEncoderReranker", "Reranker", "get_reranker", "reset_reranker"]

_log = get_logger(__name__)
_MAX_PASSAGE_CHARS = 2_000
_RETRY_SECONDS = 300.0


class Reranker(Protocol):
    """Anything that scores (question, passage) pairs; higher is a better answer."""

    model: str

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """One score per passage, in the given order."""
        ...


class CrossEncoderReranker:
    """fastembed's `TextCrossEncoder`, loaded once per process."""

    def __init__(
        self,
        model_name: str,
        *,
        cache_dir: str | None = None,
        download: bool = False,
        backend: Any = None,
        max_chars: int = _MAX_PASSAGE_CHARS,
    ) -> None:
        """Load the model; raises `RuntimeError` when it cannot (missing and no download).

        `backend` replaces the fastembed model in tests (any object with `rerank`).
        """
        self.model = model_name
        self.max_chars = max_chars
        if backend is not None:
            self._model = backend
            return
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as exc:
            msg = "the reranker needs fastembed: pip install 'content-processor[fallback]'"
            raise RuntimeError(msg) from exc
        try:
            self._model = TextCrossEncoder(
                model_name=model_name, cache_dir=cache_dir, local_files_only=not download
            )
        except Exception as exc:  # noqa: BLE001 - not cached, broken cache, unknown model
            how = "" if download else " (not in the model cache; CP_RERANK=on downloads it)"
            msg = f"the reranker {model_name} could not be loaded{how}: {type(exc).__name__}"
            raise RuntimeError(msg) from exc

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """Cross-encoder scores (logits: above 0 usually answers, below -3 usually not)."""
        if not passages:
            return []
        texts = [p[: self.max_chars] for p in passages]
        return [float(s) for s in self._model.rerank(query, texts, batch_size=16)]


_LOCK = threading.Lock()
_LOADED: dict[tuple[str, str, str], CrossEncoderReranker] = {}
_FAILED: dict[tuple[str, str, str], float] = {}


def reset_reranker() -> None:
    """Forget the loaded reranker and any failure (tests; after downloading the model)."""
    with _LOCK:
        _LOADED.clear()
        _FAILED.clear()


def get_reranker() -> Reranker | None:
    """The reranker the settings ask for, loaded once; None when off or not available.

    A model that cannot be loaded is not tried again for five minutes.
    """
    from src.core.config import get_settings

    settings = get_settings()
    mode = settings.cp_rerank
    if mode == "off" or not settings.cp_rerank_model:
        return None
    key = (mode, settings.cp_rerank_model, str(settings.cp_model_cache_dir))
    with _LOCK:
        if key in _LOADED:
            return _LOADED[key]
        failed = _FAILED.get(key)
        if failed is not None and time.monotonic() - failed < _RETRY_SECONDS:
            return None
        try:
            loaded = CrossEncoderReranker(
                settings.cp_rerank_model,
                cache_dir=str(settings.cp_model_cache_dir),
                download=mode == "on",
                max_chars=settings.cp_rerank_max_chars,
            )
        except RuntimeError as exc:
            _FAILED[key] = time.monotonic()
            _log.info("reranker not used: %s", exc)
            return None
        _LOADED[key] = loaded
        return loaded
