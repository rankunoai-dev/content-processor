"""Context-preserving chunks of a page's main content (RFC 0002, section 4.2).

Rules, in the order they apply:

1. Only the main corpus is chunked: visible or collapsed-but-published blocks
   whose role and place on the page are content, not menus, footers, cookie
   banners or site utilities, and not copies of another block (`dedupe_of`).
2. Blocks keep page order: the base view first, then each explored state.
3. A section is a run of blocks under the same heading path. Tables, FAQ
   panels and forms inside a section are atomic units and are never split; a
   unit larger than the target becomes its own chunk, flagged as oversized.
4. Sections are packed into chunks of up to `TARGET_TOKENS`; a section that is
   split carries the last sentence of the previous chunk as `overlap_before`.
   A single block longer than the target is split between sentences, never
   inside one.
5. Small text chunks merge with a neighbour under the same top-level heading.
6. Each chunk carries a context header (site, page title, heading path, page
   summary) used for embedding and classification; its `text` stays verbatim
   for citations.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from src.modules.content_processor.analysis.claims import split_sentences
from src.modules.content_processor.contracts import ContentBlock
from src.modules.content_processor.enums import UiRegion
from src.modules.content_processor.knowledge.models import Chunk, ChunkKind
from src.modules.content_processor.verify.record import CORPUS_LAYERS

__all__ = [
    "HARD_CAP_TOKENS",
    "MIN_TOKENS",
    "TARGET_TOKENS",
    "PageContext",
    "chunk_blocks",
    "main_corpus",
    "token_estimate",
]

TARGET_TOKENS = 500
MIN_TOKENS = 80
HARD_CAP_TOKENS = 2000
_OVERLAP_CHARS = 300

_CHROME_REGIONS = frozenset(
    {
        UiRegion.NAV_MENU,
        UiRegion.HAMBURGER_MENU,
        UiRegion.MEGA_MENU,
        UiRegion.BREADCRUMB,
        UiRegion.FOOTER,
        UiRegion.COOKIE_BANNER,
        UiRegion.CHAT_WIDGET,
        UiRegion.AD_SLOT,
        UiRegion.SEARCH,
        UiRegion.PAGE_METADATA,
        UiRegion.MODAL,
        UiRegion.POPOVER,
        UiRegion.TOOLTIP,
        UiRegion.SIDEBAR,
    }
)
_CONTENT_REGIONS = frozenset(
    {
        UiRegion.MAIN_CONTENT,
        UiRegion.HERO,
        UiRegion.CARD,
        UiRegion.SNIPPET,
        UiRegion.TABLE,
        UiRegion.ACCORDION_PANEL,
        UiRegion.TAB_PANEL,
        UiRegion.CAROUSEL,
        UiRegion.FORM,
        UiRegion.PDF_PAGE,
        UiRegion.PDF_TABLE,
        UiRegion.PDF_FORM,
        UiRegion.PDF_ANNOTATION,
        UiRegion.IMAGE_TEXT,
        UiRegion.UNKNOWN,
    }
)
_ATOMIC_KIND: dict[UiRegion, ChunkKind] = {
    UiRegion.TABLE: "table",
    UiRegion.PDF_TABLE: "table",
    UiRegion.ACCORDION_PANEL: "panel",
    UiRegion.FORM: "form_steps",
    UiRegion.PDF_FORM: "form_steps",
}
_STATE = re.compile(r":state(\d+):")


def token_estimate(text: str) -> int:
    """Rough token count (four characters per token), enough for sizing chunks."""
    return math.ceil(len(text) / 4)


def _is_main(block: ContentBlock) -> bool:
    if block.layer not in CORPUS_LAYERS or block.dedupe_of is not None:
        return False
    if not block.normalized_text.strip():
        return False
    region = block.ui_region
    if block.role in ("main", "legal"):
        return region not in _CHROME_REGIONS
    if block.role == "unknown":
        return region is None or region in _CONTENT_REGIONS
    return False


def _state_rank(block: ContentBlock) -> int:
    match = _STATE.search(block.block_id)
    return int(match.group(1)) if match else 0


def main_corpus(blocks: Iterable[ContentBlock]) -> list[ContentBlock]:
    """The blocks a page's chunks are built from, in page order."""
    picked = [b for b in blocks if _is_main(b)]
    return sorted(picked, key=lambda b: (_state_rank(b), b.reading_order))


@dataclass(frozen=True)
class PageContext:
    """What every chunk of a page must say about where it comes from."""

    run_id: str
    url: str
    page_key: str
    site: str
    title: str | None
    summary: str | None


@dataclass
class _Piece:
    """One block, or one sentence-bounded part of a long block."""

    block: ContentBlock
    text: str


@dataclass
class _Unit:
    """Pieces that must stay together: a table, an FAQ panel, a form, or one block."""

    kind: ChunkKind
    atomic: bool
    pieces: list[_Piece]

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pieces)

    @property
    def tokens(self) -> int:
        return token_estimate(self.text)


@dataclass
class _Draft:
    heading_path: list[str]
    kind: ChunkKind
    pieces: list[_Piece] = field(default_factory=list)
    overlap_before: str = ""
    oversized: bool = False

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pieces)

    @property
    def tokens(self) -> int:
        return token_estimate(self.text)


def _split_long(block: ContentBlock) -> list[_Piece]:
    text = block.normalized_text
    if token_estimate(text) <= TARGET_TOKENS:
        return [_Piece(block, text)]
    pieces: list[_Piece] = []
    current = ""
    for start, end in split_sentences(text):
        sentence = text[start:end]
        candidate = f"{current} {sentence}".strip()
        if current and token_estimate(candidate) > TARGET_TOKENS:
            pieces.append(_Piece(block, current))
            current = sentence
        else:
            current = candidate
    if current:
        pieces.append(_Piece(block, current))
    return pieces


def _atomic_kind(block: ContentBlock) -> ChunkKind | None:
    return _ATOMIC_KIND.get(block.ui_region) if block.ui_region is not None else None


def _units(section: Sequence[ContentBlock]) -> list[_Unit]:
    units: list[_Unit] = []
    for block in section:
        kind = _atomic_kind(block)
        last = units[-1] if units else None
        if kind is not None:
            if (
                last is not None
                and last.atomic
                and last.pieces[-1].block.ui_region == block.ui_region
            ):
                last.pieces.append(_Piece(block, block.normalized_text))
            else:
                units.append(_Unit(kind, True, [_Piece(block, block.normalized_text)]))
            continue
        page_kind: ChunkKind = "document_page" if block.ui_region == UiRegion.PDF_PAGE else "text"
        units.extend(_Unit(page_kind, False, [piece]) for piece in _split_long(block))
    for unit in units:
        # An accordion is an FAQ only when it is phrased as questions.
        if unit.kind == "panel" and any(p.text.rstrip().endswith("?") for p in unit.pieces):
            unit.kind = "faq"
    return units


def _cap_atomic(unit: _Unit) -> list[list[_Piece]]:
    """Split an atomic unit only when it passes the hard cap, between blocks."""
    groups: list[list[_Piece]] = [[]]
    for piece in unit.pieces:
        trial = "\n".join(p.text for p in [*groups[-1], piece])
        if groups[-1] and token_estimate(trial) > HARD_CAP_TOKENS:
            groups.append([])
        groups[-1].append(piece)
    return groups


def _last_sentence(text: str) -> str:
    spans = split_sentences(text)
    if not spans:
        return ""
    start, end = spans[-1]
    return text[start:end][-_OVERLAP_CHARS:]


def _drafts(heading_path: list[str], units: list[_Unit]) -> list[_Draft]:
    drafts: list[_Draft] = []
    current: _Draft | None = None

    def close() -> None:
        nonlocal current
        if current is not None and current.pieces:
            drafts.append(current)
        current = None

    for unit in units:
        if unit.atomic and unit.tokens > TARGET_TOKENS:
            close()
            for group in _cap_atomic(unit):
                drafts.append(_Draft(heading_path, unit.kind, group, oversized=True))
            continue
        if current is not None and (
            current.kind != unit.kind
            and (unit.atomic or current.kind != "text" or unit.kind != "text")
        ):
            close()
        if current is not None and token_estimate(current.text + "\n" + unit.text) > TARGET_TOKENS:
            previous = current
            close()
            current = _Draft(heading_path, unit.kind, overlap_before=_last_sentence(previous.text))
        if current is None:
            current = _Draft(heading_path, unit.kind)
        current.pieces.extend(unit.pieces)
    close()
    return drafts


def _common_prefix(a: list[str], b: list[str]) -> list[str]:
    out: list[str] = []
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        out.append(x)
    return out


def _top(path: list[str]) -> str:
    return path[0] if path else ""


def _mergeable(a: _Draft, b: _Draft) -> bool:
    """Whether a small chunk may join its neighbour.

    Same top-level heading (or no heading yet), compatible kinds (equal, or one
    is plain text), neither oversized, and the result within the target size.
    """
    return (
        (a.kind == b.kind or "text" in (a.kind, b.kind))
        and not a.oversized
        and not b.oversized
        and (
            _top(a.heading_path) == _top(b.heading_path) or not a.heading_path or not b.heading_path
        )
        and token_estimate(a.text + "\n" + b.text) <= TARGET_TOKENS
    )


def _merged_path(a: list[str], b: list[str]) -> list[str]:
    if not a or not b:
        return a or b
    return _common_prefix(a, b)


def _merge_small(drafts: list[_Draft]) -> list[_Draft]:
    out: list[_Draft] = []
    for draft in drafts:
        prev = out[-1] if out else None
        small = prev is not None and (prev.tokens < MIN_TOKENS or draft.tokens < MIN_TOKENS)
        if prev is not None and small and _mergeable(prev, draft):
            prev.pieces.extend(draft.pieces)
            prev.heading_path = _merged_path(prev.heading_path, draft.heading_path)
            if prev.kind != draft.kind and draft.tokens > prev.tokens - draft.tokens:
                prev.kind = draft.kind  # the merged chunk is the kind of its larger part
            continue
        out.append(draft)
    return out


def _sections(blocks: Sequence[ContentBlock]) -> list[tuple[list[str], list[ContentBlock]]]:
    sections: list[tuple[list[str], list[ContentBlock]]] = []
    for block in blocks:
        path = [h for h in block.heading_path if h.strip()]
        if sections and sections[-1][0] == path:
            sections[-1][1].append(block)
        else:
            sections.append((path, [block]))
    return sections


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _context_header(ctx: PageContext, heading_path: list[str]) -> str:
    trail = [ctx.site]
    if ctx.title:
        trail.append(ctx.title)
    title = (ctx.title or "").casefold()
    trail.extend(h for h in heading_path if h and h.casefold() != title)
    header = " › ".join(trail)
    if ctx.summary:
        header += f"\nPage summary: {ctx.summary}"
    return header


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def chunk_blocks(ctx: PageContext, blocks: Iterable[ContentBlock]) -> list[Chunk]:
    """Chunks of a page's main content, linked in order, each with its page context."""
    drafts: list[_Draft] = []
    for path, section in _sections(main_corpus(blocks)):
        drafts.extend(_drafts(path, _units(section)))
    drafts = _merge_small(drafts)

    chunks: list[Chunk] = []
    for order, draft in enumerate(drafts):
        block_ids = _dedupe(p.block.block_id for p in draft.pieces)
        chunk_id = "chk-" + _hash(ctx.page_key, str(order), block_ids[0], block_ids[-1])[:20]
        text = draft.text
        header = _context_header(ctx, draft.heading_path)
        embed = header + ("\n… " + draft.overlap_before if draft.overlap_before else "")
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                run_id=ctx.run_id,
                url=ctx.url,
                page_key=ctx.page_key,
                order=order,
                kind=draft.kind,
                heading_path=draft.heading_path,
                parent_section_id="sec-" + _hash(ctx.page_key, _top(draft.heading_path))[:16],
                text=text,
                overlap_before=draft.overlap_before,
                context_header=header,
                embed_text=f"{embed}\n\n{text}",
                block_ids=block_ids,
                evidence_ids=_dedupe(e for p in draft.pieces for e in p.block.evidence_ids),
                ui_regions=_dedupe(
                    str(p.block.ui_region) for p in draft.pieces if p.block.ui_region is not None
                ),
                word_count=len(text.split()),
                token_estimate=token_estimate(text),
                oversized_atomic=draft.oversized,
            )
        )
    for i, chunk in enumerate(chunks):
        chunk.prev_chunk_id = chunks[i - 1].chunk_id if i else None
        chunk.next_chunk_id = chunks[i + 1].chunk_id if i + 1 < len(chunks) else None
    return chunks
