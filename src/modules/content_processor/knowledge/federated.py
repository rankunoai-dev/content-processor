"""Search several knowledge spaces at once: one website, a project, or several projects.

Every website of every project has its own knowledge space (its own store
namespace, see docs/app/ARCHITECTURE_PROJECTS_AND_SITES.md). A question is asked
of a *scope*: the list of spaces the bot may read. Each space is searched on its
own, with the embedding model it was built with, and the per-space rankings are
fused by rank (RRF), so spaces built with different models can be searched
together without comparing their vectors. Every hit says which project and
website it came from. A space that cannot be searched (never indexed, or its
model is not available) is skipped and named, never silently left out.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from src.modules.content_processor.knowledge.embedders import Embedder
from src.modules.content_processor.knowledge.retrieval import SearchHit, hybrid_search, rrf
from src.modules.content_processor.knowledge.vector_store import VectorStore

__all__ = ["FederatedHit", "FederatedResult", "Space", "federated_search", "space_namespace"]

PER_SPACE_POOL = 20


def space_namespace(tenant: str, project_id: str, site_id: str) -> str:
    """The store namespace of one website of one project."""
    return f"{tenant}.{project_id}.{site_id}"


@dataclass(frozen=True)
class Space:
    """One website's knowledge space, with the names a person recognises."""

    namespace: str
    project_id: str
    project_name: str
    site_id: str
    site_name: str
    role: str


@dataclass
class FederatedHit:
    """A passage found in one space, with its place in the fused ranking."""

    space: Space
    hit: SearchHit
    rank: int
    score: float


@dataclass
class FederatedResult:
    """The fused hits, which spaces were searched and which were skipped (with why)."""

    hits: list[FederatedHit] = field(default_factory=list)
    searched: list[Space] = field(default_factory=list)
    skipped: list[tuple[Space, str]] = field(default_factory=list)

    def by_site(self) -> dict[str, list[FederatedHit]]:
        """The hits grouped by website, best first: the side-by-side comparison view."""
        out: dict[str, list[FederatedHit]] = {}
        for hit in self.hits:
            out.setdefault(hit.space.namespace, []).append(hit)
        return out


def federated_search(
    spaces: Sequence[Space],
    query: str,
    *,
    open_store: Callable[[Space], VectorStore],
    embedder_for: Callable[[str], Embedder],
    top_k: int = 8,
    per_site: int | None = None,
    pool: int = PER_SPACE_POOL,
) -> FederatedResult:
    """Search every space in scope and fuse the results.

    Args:
        spaces: The scope: only these spaces are read.
        query: The question or search words.
        open_store: Opens a space's store (the caller decides SQLite or Postgres).
        embedder_for: The embedder for a model name, as a space's metadata names it.
        top_k: Hits to return overall.
        per_site: When set, up to this many hits per website instead (comparisons).
        pool: Hits taken from each space before fusing.
    """
    result = FederatedResult()
    rankings: list[list[tuple[str, float]]] = []
    found: dict[str, tuple[Space, SearchHit]] = {}
    embedders: dict[str, Embedder] = {}
    try:
        for space in spaces:
            store = open_store(space)
            try:
                model = store.meta().get("model")
                if not model:
                    result.skipped.append((space, "not indexed yet: scrape this website first"))
                    continue
                try:
                    embedder = embedders.get(model) or embedder_for(model)
                    embedders[model] = embedder
                    hits = hybrid_search(store, embedder, query, top_k=pool)
                except (RuntimeError, ValueError) as exc:
                    result.skipped.append((space, f"could not be searched: {exc}"))
                    continue
            finally:
                store.close()
            result.searched.append(space)
            ranking: list[tuple[str, float]] = []
            for hit in hits:
                key = f"{space.namespace}\x1f{hit.chunk.chunk_id}"
                found[key] = (space, hit)
                ranking.append((key, hit.score))
            rankings.append(ranking)
    finally:
        for embedder in embedders.values():
            embedder.close()

    fused = rrf(rankings)
    ordered = sorted(fused, key=lambda key: (-fused[key], key))
    if per_site is not None:
        taken: dict[str, int] = {}
        picked = []
        for key in ordered:
            namespace = found[key][0].namespace
            if taken.get(namespace, 0) < per_site:
                taken[namespace] = taken.get(namespace, 0) + 1
                picked.append(key)
        ordered = picked
    else:
        ordered = ordered[:top_k]
    for rank, key in enumerate(ordered, start=1):
        space, hit = found[key]
        result.hits.append(FederatedHit(space=space, hit=hit, rank=rank, score=fused[key]))
    return result
