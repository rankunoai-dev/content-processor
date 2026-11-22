"""Topic drift: did a changed page keep its subject, or does it now say something else?

Line-level drift (`diff.py`) counts how many lines were added or removed; a page
whose sentences were all reworded scores 100% drift even when it still says the
same thing. Here the **meaning** of the old and new versions is compared: each
version's own lines (template lines removed, as in the diff) are embedded with the
website's knowledge-base model, the page vector is the normalised mean of its line
vectors, and the score is the cosine of the two page vectors (0 to 1). A score
below about 0.85 means the page was repositioned onto another topic.

Only real meaning models are used (the model server's, or the CPU fallback model);
the words-only stand-in never scores meaning. Line vectors are cached by model and
text hash (`analysis.semantic.VectorCache`), so a check embeds only new text.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Final

import numpy as np

from src.modules.content_processor.analysis.semantic import VectorCache
from src.modules.content_processor.knowledge.embedders import Embedder

__all__ = ["MIN_WORDS", "TOPIC_DRIFT", "page_meaning"]

TOPIC_DRIFT: Final = 0.85
MIN_WORDS: Final = 20  # a version with fewer own words is too thin to judge
_MIN_LINE_WORDS: Final = 3  # buttons and labels carry no topic
_MAX_LINES: Final = 200
_BATCH: Final = 64


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _usable(lines: Sequence[str]) -> list[str]:
    kept = [t for t in dict.fromkeys(lines) if len(t.split()) >= _MIN_LINE_WORDS]
    return kept[:_MAX_LINES]


def _vector(lines: list[str], found: Mapping[str, np.ndarray[Any, Any]], width: int) -> Any:
    total = np.zeros(width, dtype=np.float32)
    for line in lines:
        vec = found.get(_hash(line))
        if vec is not None:
            total[: len(vec)] += vec
    norm = float(np.linalg.norm(total))
    return total / norm if norm else None


def page_meaning(
    versions: Mapping[str, tuple[Sequence[str], Sequence[str]]],
    embedder: Embedder,
    cache: VectorCache,
    *,
    batch: int = _BATCH,
) -> dict[str, float]:
    """Meaning similarity (0-1) of each page's old and new lines, by URL.

    Pages with too little text on either side get no score. Raises `RuntimeError`
    when the embedding model fails.
    """
    usable = {url: (_usable(before), _usable(after)) for url, (before, after) in versions.items()}
    usable = {
        url: pair
        for url, pair in usable.items()
        if all(sum(len(t.split()) for t in side) >= MIN_WORDS for side in pair)
    }
    texts = {_hash(t): t for before, after in usable.values() for t in (*before, *after)}
    if not texts:
        return {}
    known = cache.get(embedder.model, texts)
    missing = [k for k in texts if k not in known]
    for start in range(0, len(missing), batch):
        keys = missing[start : start + batch]
        vectors = embedder.embed([texts[k] for k in keys])
        if vectors is None or len(vectors) != len(keys):
            msg = f"the embedding model {embedder.model} failed on {len(keys)} texts"
            raise RuntimeError(msg)
        cache.put(embedder.model, dict(zip(keys, vectors, strict=True)))
    found = cache.get(embedder.model, texts)
    width = max((len(v) for v in found.values()), default=1)
    scores: dict[str, float] = {}
    for url, (before, after) in usable.items():
        old, new = _vector(before, found, width), _vector(after, found, width)
        if old is not None and new is not None:
            scores[url] = round(min(max(float(old @ new), 0.0), 1.0), 3)
    return scores
