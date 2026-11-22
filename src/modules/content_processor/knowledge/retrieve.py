"""One retriever for any scope: one website, a project, or several projects (RFC 0005, L1).

`retrieve(scope, query, filters, k)` runs the same stages whatever the scope:

1. **Candidates per website**, with that website's own embedding model: dense (vector
   similarity) and sparse (BM25 keywords), each up to `pool`, after every filter
   (offering, publication dates, passage role), which is applied *before* scoring so a
   rare role or a small website still returns its best passages.
2. **Global reciprocal rank fusion** of every website's rankings (RRF, k = 60): websites
   built with different models are fused by rank, never by comparing their vectors.
   A date or "latest" in the question adds a ranking by publication date (a hint,
   counted twice, never a filter).
3. **One copy of the same text** across all websites: a syndicated release, a shared
   disclaimer or a teaser repeated on every page is kept once, at its best rank.
4. **One rerank** of the best candidates of all websites together (a cross-encoder reads
   the question with each passage; one scale for every website), with **date banding**:
   near-equal scores are ordered by the date the question asks about. Passages below
   the floor are dropped, so k follows the evidence and "not found" is possible.
5. **One MMR** over the survivors: each next passage balances relevance against
   similarity to those already chosen (cosine within a website's model, word overlap
   across websites), within the per-website quota when one is asked for.

`hybrid_search` (one store, as `kb search` and `kb ask` use) and `federated_search` (a
scope of spaces, as the app and Claude Desktop use) are thin callers of the same code.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from src.modules.content_processor.knowledge.embedders import Embedder, model_kind
from src.modules.content_processor.knowledge.numeric import np
from src.modules.content_processor.knowledge.query_dates import DateIntent, date_intent
from src.modules.content_processor.knowledge.reranker import Reranker
from src.modules.content_processor.knowledge.vector_store import ChunkRow, DateRange, VectorStore

__all__ = [
    "PER_SPACE_POOL",
    "RRF_K",
    "EmbedderMismatchError",
    "Filters",
    "Retrieval",
    "RetrievedHit",
    "SearchHit",
    "Space",
    "hybrid_search",
    "keyword_only_search",
    "mmr",
    "offering_scope",
    "retrieve",
    "rrf",
    "space_namespace",
]

RRF_K = 60
POOL = 50
PER_SPACE_POOL = 20
_RERANK_POOL = 12
_MMR_LAMBDA = 0.75
_DEDUPE_CHARS = 600
_DATE_WEIGHT = 2  # the date ranking counts twice in the fusion when the question names one
_DATE_BAND = 0.5  # reranker scores this close count as a tie, broken by the date
_MMR_WINDOW = 20
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


# -- shapes ---------------------------------------------------------------------------


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


@dataclass(frozen=True)
class Filters:
    """Applied before scoring, in every website of the scope.

    `offering` is resolved per website to the offering and everything under it (a
    website without it is searched with no candidates); `published` is a date range of
    the pages; `roles` are passage roles (claim, feature, pricing, proof, faq, cta...).
    """

    offering: str | None = None
    published: DateRange | None = None
    roles: frozenset[str] | None = None


@dataclass
class SearchHit:
    """One passage found, with its rank in each search and the fused score."""

    chunk: ChunkRow
    score: float
    vector_rank: int | None = None
    vector_score: float | None = None
    keyword_rank: int | None = None
    keyword_score: float | None = None
    rerank_score: float | None = None
    date_rank: int | None = None


@dataclass
class RetrievedHit:
    """A passage of one website, with its place in the scope's ranking."""

    space: Space
    hit: SearchHit
    rank: int
    score: float


@dataclass
class Retrieval:
    """The hits, which websites were searched or skipped (with why), and the notes.

    `words_only` lists websites searched by exact words alone (indexed without a meaning
    model, or the model could not be reached); `lighter` those whose meaning came from the
    CPU fallback model; `reranked_by` names the reranker, and `below_relevance` counts the
    candidates it left out as not answering.
    """

    hits: list[RetrievedHit] = field(default_factory=list)
    searched: list[Space] = field(default_factory=list)
    skipped: list[tuple[Space, str]] = field(default_factory=list)
    words_only: list[tuple[Space, str]] = field(default_factory=list)
    lighter: list[tuple[Space, str]] = field(default_factory=list)
    reranked_by: str | None = None
    below_relevance: int = 0

    def by_site(self) -> dict[str, list[RetrievedHit]]:
        """The hits grouped by website, best first: the side-by-side comparison view."""
        out: dict[str, list[RetrievedHit]] = {}
        for hit in self.hits:
            out.setdefault(hit.space.namespace, []).append(hit)
        return out


class EmbedderMismatchError(RuntimeError):
    """The query was prepared with a different model than the store's vectors were made with.

    Vectors from two models are not comparable (and differ in size), so the search refuses
    instead of returning meaningless ranking. A scope search falls back to exact words for
    that website; a single-store search reports it.
    """


# -- small pieces ---------------------------------------------------------------------


def rrf(rankings: Sequence[Sequence[tuple[str, float]]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal rank fusion: sum of 1 / (k + rank) over the rankings an id appears in."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, (item, _score) in enumerate(ranking, start=1):
            fused[item] = fused.get(item, 0.0) + 1.0 / (k + rank)
    return fused


def _settings() -> tuple[float, int, float]:
    """(MMR lambda, rerank pool, minimum rerank score) from the settings."""
    try:
        from src.core.config import get_settings

        cfg = get_settings()
        return cfg.cp_mmr_lambda, cfg.cp_rerank_pool, cfg.cp_rerank_min_score
    except Exception:  # noqa: BLE001 - settings unavailable: the documented defaults
        return _MMR_LAMBDA, _RERANK_POOL, -0.5


def _text_key(text: str) -> str:
    return " ".join(_WORD.findall(text.casefold()))[:_DEDUPE_CHARS]


def _words(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(text.casefold()) if len(w) > 2)


def _date_ranking(order: Sequence[str], rows: dict[str, ChunkRow], intent: DateIntent) -> list[str]:
    """Candidates by closeness to the named date (or newest first); [] when most lack a date."""
    dated = [c for c in order if c in rows and rows[c].published_at]
    if not intent.any or len(dated) * 2 < min(len(order), 20):
        return []
    position = {c: i for i, c in enumerate(order)}
    if intent.target is not None:
        target = intent.target

        def distance(cid: str) -> int:
            day = date.fromisoformat(str(rows[cid].published_at)[:10])
            return abs((day - target).days)

        return sorted(dated, key=lambda c: (distance(c), position[c]))
    return sorted(dated, key=lambda c: (str(rows[c].published_at), -position[c]), reverse=True)


def offering_scope(store: VectorStore, offering: str | None) -> set[str] | None:
    """The offering and all offerings under it; None for no filter."""
    if offering is None:
        return None
    parents = store.offering_parents()
    if offering not in parents:
        known = ", ".join(sorted(parents)) or "none (index a run with a catalog first)"
        msg = f"unknown offering {offering!r}; known: {known}"
        raise ValueError(msg)
    scope = {offering}
    changed = True
    while changed:
        changed = False
        for child, parent in parents.items():
            if parent in scope and child not in scope:
                scope.add(child)
                changed = True
    return scope


def mmr(
    candidates: Sequence[str],
    relevance: dict[str, float],
    vectors: dict[str, list[float]],
    k: int,
    lam: float,
    *,
    similarity: Callable[[str, str], float] | None = None,
    group_of: Callable[[str], str] | None = None,
    quota: int | None = None,
) -> list[str]:
    """Maximal marginal relevance: pick the best balance of relevance and novelty.

    Each pick maximises lam * relevance - (1 - lam) * its highest similarity to a passage
    already picked; `relevance` is on a 0-1 scale. Similarity is the cosine of `vectors`,
    or `similarity(a, b)` when given (it may use the vectors itself). With `quota`, no more
    than that many picks come from one `group_of` (one website).
    """
    unit: dict[str, Any] = {}
    for c in candidates:
        if c in vectors:
            v = np.asarray(vectors[c], dtype=np.float32)
            norm = float(np.linalg.norm(v))
            unit[c] = v / norm if norm else v

    def cosine(a: str, b: str) -> float:
        if a not in unit or b not in unit:
            return 0.0
        width = min(len(unit[a]), len(unit[b]))
        return float(unit[a][:width] @ unit[b][:width])

    sim = similarity or cosine
    position = {c: i for i, c in enumerate(candidates)}
    taken: dict[str, int] = {}
    picked: list[str] = []
    left = list(candidates)
    while left and len(picked) < k:
        if quota is not None and group_of is not None:
            left = [c for c in left if taken.get(group_of(c), 0) < quota]
            if not left:
                break
        if lam >= 1.0 or not picked:
            best = left[0]  # the ranking's order (rerank with date banding) comes first
        else:

            def gain(c: str) -> float:
                return lam * relevance[c] - (1 - lam) * max(sim(c, p) for p in picked)

            best = max(left, key=lambda c: (gain(c), -position[c]))
        picked.append(best)
        left.remove(best)
        if group_of is not None:
            taken[group_of(best)] = taken.get(group_of(best), 0) + 1
    return picked


# -- the retriever ----------------------------------------------------------------------


@dataclass
class _Source:
    """One opened website: its store, and its embedder (None: searched by words only)."""

    space: Space
    store: VectorStore
    embedder: Embedder | None


def _key(space: Space, chunk_id: str) -> str:
    return f"{space.namespace}\x1f{chunk_id}"


def _run(
    sources: Sequence[_Source],
    query: str,
    filters: Filters,
    *,
    k: int,
    per_site: int | None,
    pool: int,
    reranker: Reranker | None,
    min_score: float | None,
    mmr_lambda: float | None,
    use_dates: bool,
    strict: bool,
    result: Retrieval,
) -> None:
    """The five stages over opened websites; fills `result.hits`.

    `strict` (one store): a model mismatch or a failed query embedding raises; otherwise
    that website is searched by words only and the reason is noted.
    """
    lam, rerank_pool, default_min = _settings()
    lam = lam if mmr_lambda is None else mmr_lambda
    floor = default_min if min_score is None else min_score
    width = max(pool, k, per_site or 0)
    rankings: list[list[tuple[str, float]]] = []
    by_key: dict[str, tuple[_Source, str]] = {}
    v_rank: dict[str, tuple[int, float]] = {}
    k_rank: dict[str, tuple[int, float]] = {}
    query_vectors: dict[str, list[float] | None] = {}
    for source in sources:
        store = source.store
        try:
            offerings = offering_scope(store, filters.offering)
        except ValueError:
            if strict:
                raise
            result.skipped.append((source.space, f"has no offering {filters.offering!r}"))
            continue
        roles = set(filters.roles) if filters.roles is not None else None
        by_vector: list[tuple[str, float]] = []
        embedder = source.embedder
        if embedder is not None:
            stored = store.meta().get("model")
            reason = ""
            if stored and stored != embedder.model:
                reason = (
                    f"this site was indexed with {stored}, but the query was prepared with "
                    f"{embedder.model}. Bring {stored} back (run: ollama pull {stored}) or "
                    "re-index the site with the model you want."
                )
                if strict:
                    raise EmbedderMismatchError(reason)
            else:
                if embedder.model not in query_vectors:
                    try:
                        embedded = embedder.embed([query])
                    except (RuntimeError, ValueError, OSError):
                        if strict:
                            raise
                        embedded = None  # model server down: this website by words only
                    query_vectors[embedder.model] = embedded[0] if embedded else None
                vector = query_vectors[embedder.model]
                if vector is None:
                    reason = f"could not embed the query with {embedder.model}"
                    if strict:
                        raise RuntimeError(reason)
                else:
                    by_vector = store.vector_search(
                        vector, width, offerings, filters.published, roles
                    )
                    if model_kind(embedder.model) == "CPU fallback":
                        result.lighter.append(
                            (
                                source.space,
                                "meaning from the lighter CPU fallback model (not bge-m3)",
                            )
                        )
            if reason:
                result.words_only.append((source.space, f"searched by exact words only: {reason}"))
        by_keyword = store.keyword_search(query, width, offerings, filters.published, roles)
        result.searched.append(source.space)
        for found, ranks in ((by_vector, v_rank), (by_keyword, k_rank)):
            ranking = []
            for n, (chunk_id, score) in enumerate(found, start=1):
                key = _key(source.space, chunk_id)
                by_key[key] = (source, chunk_id)
                ranks[key] = (n, score)
                ranking.append((key, score))
            if ranking:
                rankings.append(ranking)
    if not rankings:
        return

    # 2. global fusion (a website's best passages are always considered, for quotas)
    fused = rrf(rankings)
    order = sorted(fused, key=lambda key: (-fused[key], key))
    keep = set(order[: max(width, k)])
    if per_site:
        seen_site: dict[str, int] = {}
        for key in order:
            ns = by_key[key][0].space.namespace
            if seen_site.get(ns, 0) < per_site * 3:
                seen_site[ns] = seen_site.get(ns, 0) + 1
                keep.add(key)
    order = [key for key in order if key in keep]
    rows: dict[str, ChunkRow] = {}
    for source in sources:
        wanted = [by_key[key][1] for key in order if by_key[key][0] is source]
        if wanted:
            for chunk_id, row in source.store.get_chunks(wanted).items():
                rows[_key(source.space, chunk_id)] = row
    order = [key for key in order if key in rows]
    date_order = _date_ranking(order, rows, date_intent(query) if use_dates else DateIntent())
    if date_order:
        by_date = [(key, 0.0) for key in date_order]
        fused = rrf([*rankings, *([by_date] * _DATE_WEIGHT)])
        order = sorted((key for key in order), key=lambda key: (-fused[key], key))

    # 3. one copy of the same text, across every website
    seen_text: set[str] = set()
    candidates: list[str] = []
    for key in order:
        text = _text_key(rows[key].verbatim_text)
        if text not in seen_text:
            seen_text.add(text)
            candidates.append(key)

    def site(key: str) -> str:
        return by_key[key][0].space.namespace

    top_fused = max((fused[c] for c in candidates), default=1.0) or 1.0
    relevance = {c: fused[c] / top_fused for c in candidates}

    # 4. one rerank, date banding, the relevance floor
    rerank: dict[str, float] = {}
    if reranker is not None and candidates:
        head = candidates[: max(rerank_pool, k)]
        if per_site:  # every website's best candidates compete
            counts: dict[str, int] = {}
            for key in candidates:
                if counts.get(site(key), 0) < per_site and key not in head:
                    head.append(key)
                counts[site(key)] = counts.get(site(key), 0) + 1
        scores = reranker.score(
            query, [f"{rows[c].context_header}\n{rows[c].verbatim_text}" for c in head]
        )
        rerank = dict(zip(head, scores, strict=True))
        d_pos = {c: n for n, c in enumerate(date_order)}

        def reranked(c: str) -> tuple[float, int]:
            band = round(rerank[c] / _DATE_BAND) if date_order else rerank[c]
            return (-band, d_pos.get(c, len(d_pos)) if date_order else 0)

        kept = [c for c in sorted(head, key=reranked) if rerank[c] >= floor]
        result.reranked_by = reranker.model
        result.below_relevance = len(head) - len(kept)
        candidates = kept
        relevance = {c: 1.0 / (1.0 + math.exp(-rerank[c])) for c in candidates}
    if not candidates:
        return

    # 5. one MMR over the survivors (cosine within a website, word overlap across)
    wanted_k = per_site * len({site(c) for c in candidates}) if per_site else k
    window = candidates[: max(wanted_k * 4, _MMR_WINDOW)]
    vectors: dict[str, list[float]] = {}
    if lam < 1.0:
        for source in sources:
            ids = [by_key[c][1] for c in window if by_key[c][0] is source]
            if ids and source.embedder is not None:
                for chunk_id, vec in source.store.vectors(ids).items():
                    vectors[_key(source.space, chunk_id)] = vec
    words = {c: _words(rows[c].verbatim_text) for c in window} if lam < 1.0 else {}
    cosine_unit: dict[str, Any] = {}
    for c, vec in vectors.items():
        v = np.asarray(vec, dtype=np.float32)
        norm = float(np.linalg.norm(v))
        cosine_unit[c] = v / norm if norm else v

    def similarity(a: str, b: str) -> float:
        if site(a) == site(b) and a in cosine_unit and b in cosine_unit:
            width_ab = min(len(cosine_unit[a]), len(cosine_unit[b]))
            return float(cosine_unit[a][:width_ab] @ cosine_unit[b][:width_ab])
        wa, wb = words.get(a, frozenset()), words.get(b, frozenset())
        return len(wa & wb) / len(wa | wb) if wa and wb else 0.0

    best = mmr(
        window,
        relevance,
        {},
        wanted_k,
        lam,
        similarity=similarity,
        group_of=site,
        quota=per_site,
    )
    d_rank = {key: n for n, key in enumerate(date_order, start=1)}
    for rank, key in enumerate(best, start=1):
        source, _chunk_id = by_key[key]
        vr, kr = v_rank.get(key), k_rank.get(key)
        hit = SearchHit(
            chunk=rows[key],
            score=round(fused[key], 6),
            vector_rank=vr[0] if vr else None,
            vector_score=round(vr[1], 4) if vr else None,
            keyword_rank=kr[0] if kr else None,
            keyword_score=round(kr[1], 4) if kr else None,
            rerank_score=round(rerank[key], 4) if key in rerank else None,
            date_rank=d_rank.get(key),
        )
        result.hits.append(RetrievedHit(space=source.space, hit=hit, rank=rank, score=fused[key]))


@contextmanager
def _opened(
    scope: Sequence[Space],
    open_store: Callable[[Space], VectorStore],
    embedder_for: Callable[[str], Embedder],
    result: Retrieval,
) -> Iterator[list[_Source]]:
    stores: list[VectorStore] = []
    embedders: dict[str, Embedder] = {}
    sources: list[_Source] = []
    try:
        for space in scope:
            store = open_store(space)
            stores.append(store)
            model = store.meta().get("model")
            if not model:
                result.skipped.append((space, "not indexed yet: scrape this website first"))
                continue
            embedder: Embedder | None = None
            try:
                embedder = embedders.get(model) or embedder_for(model)
                embedders[model] = embedder
            except (RuntimeError, ValueError) as exc:
                result.words_only.append((space, f"searched by exact words only: {exc}"))
            if embedder is not None and model_kind(model) == "words only":
                # the stand-in's word vectors still rank, but no meaning is behind them
                result.words_only.append(
                    (space, "indexed without a meaning model: searched by exact words")
                )
            sources.append(_Source(space, store, embedder))
        yield sources
    finally:
        for store in stores:
            store.close()
        for embedder in embedders.values():
            embedder.close()


def retrieve(
    scope: Sequence[Space],
    query: str,
    *,
    open_store: Callable[[Space], VectorStore],
    embedder_for: Callable[[str], Embedder],
    filters: Filters | None = None,
    k: int = 8,
    per_site: int | None = None,
    pool: int = PER_SPACE_POOL,
    reranker: Reranker | None = None,
    min_score: float | None = None,
    mmr_lambda: float | None = None,
    use_dates: bool = True,
) -> Retrieval:
    """The best passages for `query` across the scope (see the module docstring).

    Args:
        scope: The websites' knowledge spaces; only these are read.
        query: The question or search words.
        open_store: Opens a space's store (the caller decides SQLite or Postgres).
        embedder_for: The embedder for a model name, as a space's metadata names it.
        filters: Offering, publication dates and passage roles, applied before scoring.
        k: Passages to return (fewer when the reranker finds fewer that answer).
        per_site: When set, up to this many passages per website instead (comparisons).
        pool: Candidates taken from each website by each search before fusing.
        reranker: Scores the candidates against the question; below `min_score`
            (default `CP_RERANK_MIN_SCORE`) they are dropped.
        min_score: The reranker score a passage needs.
        mmr_lambda: Relevance against novelty (default `CP_MMR_LAMBDA`; 1.0 = relevance).
        use_dates: Let a date or "latest" in the question reorder the candidates.
    """
    result = Retrieval()
    with _opened(scope, open_store, embedder_for, result) as sources:
        _run(
            sources,
            query,
            filters or Filters(),
            k=k,
            per_site=per_site,
            pool=pool,
            reranker=reranker,
            min_score=min_score,
            mmr_lambda=mmr_lambda,
            use_dates=use_dates,
            strict=False,
            result=result,
        )
    return result


_ONE_STORE = Space("", "", "", "", "", "")


def hybrid_search(
    store: VectorStore,
    embedder: Embedder,
    query: str,
    *,
    top_k: int = 5,
    offering: str | None = None,
    pool: int = POOL,
    published: DateRange | None = None,
    roles: frozenset[str] | None = None,
    reranker: Reranker | None = None,
    min_score: float | None = None,
    mmr_lambda: float | None = None,
    use_dates: bool = True,
) -> list[SearchHit]:
    """The `top_k` best passages of one open store: `retrieve` with a scope of one.

    Raises `EmbedderMismatchError` when the store was indexed with another model,
    `RuntimeError` when the query cannot be embedded, `ValueError` for an unknown offering.
    """
    result = Retrieval()
    _run(
        [_Source(_ONE_STORE, store, embedder)],
        query,
        Filters(offering, published, roles),
        k=top_k,
        per_site=None,
        pool=pool,
        reranker=reranker,
        min_score=min_score,
        mmr_lambda=mmr_lambda,
        use_dates=use_dates,
        strict=True,
        result=result,
    )
    return [h.hit for h in result.hits]


def keyword_only_search(
    store: VectorStore,
    query: str,
    *,
    top_k: int = 5,
    offering: str | None = None,
    pool: int = POOL,
) -> list[SearchHit]:
    """The `top_k` best passages by exact words alone (when the meaning model is down)."""
    scope = offering_scope(store, offering)
    by_keyword = store.keyword_search(query, max(pool, top_k), scope)[:top_k]
    rows = store.get_chunks([cid for cid, _score in by_keyword])
    return [
        SearchHit(
            chunk=rows[cid],
            score=round(score, 6),
            vector_rank=None,
            vector_score=None,
            keyword_rank=n,
            keyword_score=round(score, 4),
        )
        for n, (cid, score) in enumerate(by_keyword, start=1)
        if cid in rows
    ]
