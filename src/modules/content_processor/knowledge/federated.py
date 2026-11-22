"""Search several knowledge spaces at once: one website, a project, or several projects.

Every website of every project has its own knowledge space (its own store namespace,
see docs/app/ARCHITECTURE_PROJECTS_AND_SITES.md). A question is asked of a *scope*: the
list of spaces the bot may read. The search itself is `retrieve.retrieve` (RFC 0005):
candidates per website with its own model and pre-filters, then one global fusion, one
copy of repeated text, one rerank and one MMR. This module keeps the older names.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from src.modules.content_processor.knowledge.embedders import Embedder
from src.modules.content_processor.knowledge.reranker import Reranker
from src.modules.content_processor.knowledge.retrieve import (
    PER_SPACE_POOL,
    Filters,
    Retrieval,
    RetrievedHit,
    Space,
    retrieve,
    space_namespace,
)
from src.modules.content_processor.knowledge.vector_store import DateRange, VectorStore

__all__ = [
    "PER_SPACE_POOL",
    "FederatedHit",
    "FederatedResult",
    "Space",
    "federated_search",
    "space_namespace",
]

FederatedHit = RetrievedHit
FederatedResult = Retrieval


def federated_search(
    spaces: Sequence[Space],
    query: str,
    *,
    open_store: Callable[[Space], VectorStore],
    embedder_for: Callable[[str], Embedder],
    top_k: int = 8,
    per_site: int | None = None,
    pool: int = PER_SPACE_POOL,
    reranker: Reranker | None = None,
    min_score: float | None = None,
    published: DateRange | None = None,
    roles: frozenset[str] | None = None,
) -> Retrieval:
    """`retrieve` with the older argument names (see `retrieve` for each one)."""
    return retrieve(
        spaces,
        query,
        open_store=open_store,
        embedder_for=embedder_for,
        filters=Filters(published=published, roles=roles),
        k=top_k,
        per_site=per_site,
        pool=pool,
        reranker=reranker,
        min_score=min_score,
    )
