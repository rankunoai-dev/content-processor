"""Context builder (RFC 0005, L2): small-to-big, for every caller.

Search finds small passages (about 400 tokens each, so a match is precise); a reader
needs what surrounds them: the lead-in above a list, the rest of a table, the sentence
that names the fund. The builder hands over each hit with its surrounding *section*
(the run of passages on the same page under the same top heading, the chunker's
`parent_section_id`) and then, budget allowing, one passage across the section edge on
each side, as the older next/previous step did.

The budget is the whole text handed over, hits included, in tokens of about four
characters, set per reader: `CP_CONTEXT_TOKENS` for a local model's answer (a small
window), `CP_SEARCH_CONTEXT_TOKENS` for search results that Claude Desktop or a person
reads. The hits are always kept (search already chose how many);
the rest is shared fairly: one passage per hit per round, best hit first, nearest
passages first, alternating after and before, so a long section of the best hit cannot
starve the others. Hits on the same page that meet are merged into one stretch. No
re-indexing is needed: passages already carry their page, order and heading path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Literal

from src.core.config import get_settings
from src.modules.content_processor.knowledge.chunking import token_estimate
from src.modules.content_processor.knowledge.retrieve import Retrieval, Space
from src.modules.content_processor.knowledge.vector_store import ChunkRow, VectorStore

__all__ = ["Context", "Reader", "Stretch", "build_context", "context_budget", "context_for"]


Reader = Literal["answer", "search"]


def context_budget(budget: int | None = None, reader: Reader = "answer") -> int:
    """The caller's budget, or the reader's setting.

    "answer": `CP_CONTEXT_TOKENS`, for a local model's answer; "search":
    `CP_SEARCH_CONTEXT_TOKENS`, for search results read by Claude Desktop or a person.
    """
    if budget is not None:
        return max(0, budget)
    settings = get_settings()
    return settings.cp_search_context_tokens if reader == "search" else settings.cp_context_tokens


def _top(chunk: ChunkRow) -> str:
    return chunk.heading_path[0] if chunk.heading_path else ""


@dataclass
class Stretch:
    """A run of consecutive passages of one page around one or more hits."""

    space: Space
    url: str
    page_key: str
    chunks: list[ChunkRow]  # page order
    matched: list[str]  # chunk ids of the hits in it, best first
    rank: int  # the best hit's place in the search result (0 = first)
    section: str  # the top heading the hits sit under ("" when the page has none)
    complete: bool = False  # the whole section is included

    @property
    def text(self) -> str:
        """The stretch as one text, passages separated by a blank line."""
        return "\n\n".join(c.verbatim_text for c in self.chunks)

    @property
    def tokens(self) -> int:
        """Its size in tokens (about four characters each)."""
        return sum(token_estimate(c.verbatim_text) for c in self.chunks)

    def around(self, chunk_id: str) -> tuple[list[ChunkRow], list[ChunkRow]]:
        """The passages before and after one hit inside this stretch."""
        at = next(i for i, c in enumerate(self.chunks) if c.chunk_id == chunk_id)
        return self.chunks[:at], self.chunks[at + 1 :]


@dataclass
class Context:
    """The stretches in hit order, and what the budget allowed."""

    stretches: list[Stretch] = field(default_factory=list)
    budget: int = 0
    tokens: int = 0
    added: int = 0  # passages added around the hits
    cut: int = 0  # stretches whose section did not fit in the budget

    def stretch_of(self, space: Space, chunk_id: str) -> Stretch | None:
        """The stretch that holds one hit."""
        for s in self.stretches:
            if s.space == space and chunk_id in s.matched:
                return s
        return None


@dataclass
class _Growing:
    space: Space
    page: list[ChunkRow]  # the whole page, in order
    lo: int  # first included index
    hi: int  # last included index
    s_lo: int  # the section's first index
    s_hi: int  # the section's last index
    matched: list[str]
    rank: int
    turn: int = 0  # alternates after / before

    def candidates(self) -> Iterator[int]:
        """Indexes to add next, in preference order: inside the section, then one across."""
        after, before = self.hi + 1, self.lo - 1
        inside = [i for i in (after, before) if self.s_lo <= i <= self.s_hi]
        if self.turn % 2 and len(inside) == 2:
            inside.reverse()
        yield from inside
        if not inside:  # the section is complete: one passage across each edge
            for i in (after, before):
                if 0 <= i < len(self.page) and (i == self.s_hi + 1 or i == self.s_lo - 1):
                    yield i


def _section(page: list[ChunkRow], at: int) -> tuple[int, int]:
    top = _top(page[at])
    lo = at
    while lo > 0 and _top(page[lo - 1]) == top:
        lo -= 1
    hi = at
    while hi + 1 < len(page) and _top(page[hi + 1]) == top:
        hi += 1
    return lo, hi


def build_context(
    hits: Sequence[tuple[Space, ChunkRow]],
    page_chunks: Callable[[Space, str], list[ChunkRow]],
    *,
    budget: int | None = None,
    reader: Reader = "answer",
) -> Context:
    """Hits (best first) with their surrounding sections, within the token budget."""
    limit = context_budget(budget, reader)
    pages: dict[tuple[Space, str], list[ChunkRow]] = {}
    growing: list[_Growing] = []
    for rank, (space, chunk) in enumerate(hits):
        key = (space, chunk.page_key)
        if key not in pages:
            rows = [c for c in page_chunks(space, chunk.url) if c.page_key == chunk.page_key]
            pages[key] = sorted(rows, key=lambda c: c.order) or [chunk]
        page = pages[key]
        at = next((i for i, c in enumerate(page) if c.chunk_id == chunk.chunk_id), None)
        if at is None:  # the page was re-indexed since the search: the hit alone
            page = [chunk]
            at = 0
        s_lo, s_hi = _section(page, at) if limit else (at, at)
        growing.append(_Growing(space, page, at, at, s_lo, s_hi, [chunk.chunk_id], rank))
    growing = _merge(growing)
    used = sum(token_estimate(g.page[i].verbatim_text) for g in growing for i in _span(g))
    added = 0
    stalled: set[int] = set()
    while len(stalled) < len(growing):
        for n, g in enumerate(growing):
            if n in stalled:
                continue
            grew = False
            for i in g.candidates():
                cost = token_estimate(g.page[i].verbatim_text)
                if used + cost > limit:
                    continue
                g.lo, g.hi = min(g.lo, i), max(g.hi, i)
                g.turn += 1
                used += cost
                added += 1
                grew = True
                break
            if not grew:
                stalled.add(n)
        merged = _merge(growing)
        if len(merged) != len(growing):  # two stretches met: start the fair rounds again
            growing = merged
            stalled = set()
            used = sum(token_estimate(g.page[i].verbatim_text) for g in growing for i in _span(g))
    stretches = [
        Stretch(
            space=g.space,
            url=g.page[g.lo].url,
            page_key=g.page[g.lo].page_key,
            chunks=g.page[g.lo : g.hi + 1],
            matched=g.matched,
            rank=g.rank,
            section=_top(next(c for c in g.page if c.chunk_id == g.matched[0])),
            complete=g.lo <= g.s_lo and g.hi >= g.s_hi,
        )
        for g in sorted(growing, key=lambda g: g.rank)
    ]
    return Context(
        stretches=stretches,
        budget=limit,
        tokens=sum(s.tokens for s in stretches),
        added=added,
        cut=sum(1 for s in stretches if not s.complete),
    )


def _span(g: _Growing) -> range:
    return range(g.lo, g.hi + 1)


def _merge(growing: list[_Growing]) -> list[_Growing]:
    """Stretches of the same page that overlap or touch become one (the better rank)."""
    out: list[_Growing] = []
    for g in sorted(growing, key=lambda g: g.rank):
        for o in out:
            if o.space == g.space and o.page is g.page and g.lo <= o.hi + 1 and o.lo <= g.hi + 1:
                o.lo, o.hi = min(o.lo, g.lo), max(o.hi, g.hi)
                o.s_lo, o.s_hi = min(o.s_lo, g.s_lo), max(o.s_hi, g.s_hi)
                o.matched += [m for m in g.matched if m not in o.matched]
                break
        else:
            out.append(g)
    return out


def context_for(
    result: Retrieval,
    open_store: Callable[[Space], VectorStore],
    *,
    budget: int | None = None,
    reader: Reader = "search",
) -> Context:
    """`build_context` for a scope's search result; each website's store opened once."""
    with ExitStack() as stack:
        stores: dict[Space, VectorStore] = {}

        def page_chunks(space: Space, url: str) -> list[ChunkRow]:
            if space not in stores:
                store = open_store(space)
                stack.callback(store.close)
                stores[space] = store
            return stores[space].page_chunks(url)

        return build_context(
            [(h.space, h.hit.chunk) for h in result.hits],
            page_chunks,
            budget=budget,
            reader=reader,
        )
