"""Semantic similarity: pages that say the same thing in different words.

The word-level reports (`analysis.overlap`, the project report's matching content)
find shared wording. This module scores **meaning**, so two pages can be found to
cover the same ground with none of the same sentences, while two pages that merely
share a template are not mistaken for copies. It was built from a hand analysis of
a 303-page news room and keeps the safeguards that analysis needed:

1. **Furniture removed first.** A paragraph that appears on many pages (menus,
   footers, disclaimers, sign-up blocks) is site furniture and is dropped before any
   score; the whole-page scores of two unrelated press releases are otherwise inflated
   to about 0.9 by the shared furniture alone. Repeated text that is confined to one
   section of the website is reported on its own (a summary accidentally pasted on
   every episode page), not silently dropped.
2. **Three numbers per pair, never one.** The *meaning score* is the cosine between
   the pages' mean paragraph vectors (0 to 1). The *word overlap* is the Jaccard
   similarity of their word sets. The *same-text ratio* is how much of the text is
   literally the same (only computed where it can matter). A pair is read from all
   three: same meaning with low overlap is a rewrite or a shared topic; same meaning
   with high overlap is a copy.
3. **Recurring series are not duplicates.** Monthly declarations, quarterly notices
   and the same disclosure for five funds share a title skeleton and a template. Those
   pairs go to a *Recurring series* sheet with the series' typical scores; they are
   never counted as duplicate content, unless the text is literally the same and the
   pages carry the same date (a true copy, for example a correction republished).
4. **Tiers on own content only.** Identical (same text), Copy (>= 0.95), High
   (0.85 to 0.95: the same content said again), Moderate (0.70 to 0.85: the same
   topic, different content; a hub candidate), below that not reported.

Vectors come from the knowledge base's embedding model (the CPU fallback model on a
workstation, bge-m3 on a model server; never the words-only stand-in), cached per
paragraph in a small SQLite file so a rebuild embeds only new text. Pair scores are
computed with block matrix products so 7,000 pages stay within a workstation's memory.
"""

from __future__ import annotations

import difflib
import hashlib
import math
import re
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import numpy as np

from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.enums import ContentLayer, UrlBadge
from src.modules.content_processor.knowledge.embedders import Embedder
from src.modules.content_processor.knowledge.models import PageStamp
from src.modules.content_processor.knowledge.page_types import PageSignals, classify_page

__all__ = [
    "RepeatedBlock",
    "SemanticConfig",
    "SemanticPage",
    "SemanticPair",
    "SemanticResult",
    "Series",
    "TopicHub",
    "VectorCache",
    "analyse_semantic",
    "load_stamps",
    "tier_of",
    "title_skeleton",
]

Tier = Literal["Identical", "Copy", "High", "Moderate"]
TIER_ORDER: dict[str, int] = {"Identical": 0, "Copy": 1, "High": 2, "Moderate": 3}

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_DAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_SKELETON_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:q[1-4]|[1-4]q)\b"), " <quarter> "),
    (re.compile(r"\b(?:first|second|third|fourth)[ -]quarter\b"), " <quarter> "),
    (re.compile(rf"\b(?:{_MONTHS})\.?\b"), " <month> "),
    (re.compile(rf"\b(?:{_DAYS})\b"), " <day> "),
    (re.compile(r"\b(?:19|20)\d\d\b"), " <year> "),
    (re.compile(r"[$€£¥]\s?\d[\d,.]*|\d[\d,.]*\s?(?:usd|eur|gbp|inr|cad)\b"), " <money> "),
    (re.compile(r"\d[\d,.]*\s?%|\d[\d,.]*\s?(?:percent|per cent)\b"), " <percent> "),
    (re.compile(r"\b\d+(?:st|nd|rd|th)\b"), " <n> "),
    (re.compile(r"\d[\d,.:/-]*"), " <n> "),
)
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "by",
        "at",
        "from",
        "as",
        "is",
        "are",
        "be",
        "this",
        "that",
        "these",
        "those",
        "its",
        "your",
        "our",
        "their",
        "it",
        "we",
        "you",
        "they",
        "not",
        "no",
        "new",
        "more",
        "about",
        "into",
        "over",
        "under",
        "up",
        "out",
        "off",
        "than",
        "then",
        "so",
        "if",
        "but",
        "how",
        "what",
        "when",
        "where",
        "who",
        "why",
        "will",
        "can",
        "may",
    ]
)
_PLACEHOLDER = re.compile(r"lorem ipsum", re.IGNORECASE)
_SKIP_BADGES = frozenset({UrlBadge.BLOCKED, UrlBadge.FAILED, UrlBadge.EXCLUDED, UrlBadge.RUNNING})


@dataclass(frozen=True)
class SemanticConfig:
    """Thresholds, each with the reason it has that value in the module docstring."""

    furniture_min_pages: int = 5
    furniture_share: float = 0.02
    paragraph_min_words: int = 4
    min_own_words: int = 40
    paragraph_match: float = 0.75
    report_floor: float = 0.70
    high: float = 0.85
    copy: float = 0.95
    identical_text: float = 0.97
    address_text: float = 0.99
    repeated_block_words: int = 25
    repeated_block_pages: int = 3
    series_min_members: int = 3
    series_title_containment: float = 0.75
    """Shared skeleton tokens over the smaller skeleton: the fund name may change."""
    evidence_overlap: float = 0.6
    evidence_coverage: float = 0.5
    """A Copy or High pair needs this word overlap or this coverage on both sides."""
    confined_share: float = 0.5
    """A section or page type holding more of the pages than this cannot confine a block."""
    max_paragraphs: int = 200
    embed_batch: int = 64
    max_pairs: int = 200_000
    text_words_cap: int = 1500
    same_text_overlap: float = 0.9
    """Below this word overlap two texts cannot be literally the same; the ratio is skipped."""
    identical_group_fanout: int = 20
    """Identical groups larger than this list one representative pair per member."""

    def furniture_threshold(self, pages: int) -> int:
        """A paragraph on at least this many pages is furniture."""
        return max(self.furniture_min_pages, math.ceil(pages * self.furniture_share))


def tier_of(meaning: float, same_text: float | None, config: SemanticConfig) -> Tier | None:
    """The tier of a pair, or None when it is not reported."""
    if same_text is not None and same_text >= config.identical_text:
        return "Identical"
    if meaning >= config.copy:
        return "Copy"
    if meaning >= config.high:
        return "High"
    if meaning >= config.report_floor:
        return "Moderate"
    return None


def overlap_reading(overlap: float) -> str:
    """Word overlap in plain words."""
    if overlap >= 0.80:
        return "copy-level"
    if overlap >= 0.40:
        return "shared template or heavy reuse"
    if overlap >= 0.25:
        return "some shared wording"
    return "normal for unrelated pages"


def words_of(text: str) -> list[str]:
    """Lower-case word tokens."""
    return _WORD.findall(text.lower())


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity of two sets (0 when both are empty)."""
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def title_skeleton(title: str) -> str:
    """A title with its dates, numbers, amounts and quarters replaced by placeholders.

    Two pages with the same skeleton are the same publication issued again.
    """
    text = title.lower()
    for pattern, placeholder in _SKELETON_RULES:
        text = pattern.sub(placeholder, text)
    tokens = re.findall(r"<[a-z]+>|[^\W_]+", text, re.UNICODE)
    return " ".join(tokens)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unit(vector: np.ndarray[Any, Any]) -> np.ndarray[Any, np.dtype[np.float32]]:
    arr = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm else arr


class VectorCache:
    """Paragraph vectors by model and text hash, in one SQLite file (reused across builds)."""

    def __init__(self, path: Path) -> None:
        """Open (and create) the cache file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS vectors (model TEXT NOT NULL, hash TEXT NOT NULL, "
            "dims INTEGER NOT NULL, vec BLOB NOT NULL, PRIMARY KEY (model, hash))"
        )
        self._conn.commit()

    def get(self, model: str, hashes: Iterable[str]) -> dict[str, np.ndarray[Any, Any]]:
        """The vectors found, by hash (trailing zero padding removed when stored)."""
        found: dict[str, np.ndarray[Any, Any]] = {}
        items = list(dict.fromkeys(hashes))
        for start in range(0, len(items), 500):
            chunk = items[start : start + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT hash, vec FROM vectors WHERE model = ? AND hash IN ({marks})",  # noqa: S608 - placeholders only
                [model, *chunk],
            )
            for key, blob in rows:
                found[key] = np.frombuffer(blob, dtype=np.float32)
        return found

    def put(self, model: str, vectors: Mapping[str, Sequence[float]]) -> None:
        """Store vectors (unit length, trailing zeros trimmed)."""
        rows = []
        for key, values in vectors.items():
            arr = _unit(np.asarray(values, dtype=np.float32))
            nonzero = np.flatnonzero(arr)
            dims = int(nonzero[-1]) + 1 if len(nonzero) else 1
            trimmed = np.ascontiguousarray(arr[:dims])
            rows.append((model, key, dims, trimmed.tobytes()))
        self._conn.executemany(
            "INSERT OR REPLACE INTO vectors (model, hash, dims, vec) VALUES (?, ?, ?, ?)", rows
        )
        self._conn.commit()

    def count(self, model: str | None = None) -> int:
        """How many vectors are stored (for one model, or all)."""
        if model is None:
            return int(self._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])
        return int(
            self._conn.execute("SELECT COUNT(*) FROM vectors WHERE model = ?", (model,)).fetchone()[
                0
            ]
        )

    def close(self) -> None:
        """Close the file."""
        self._conn.close()


@dataclass
class SemanticPage:
    """One page as the analysis sees it."""

    url: str
    website: str
    title: str
    h1: str
    section: str
    page_type: str
    published: str
    robots_meta: str
    canonical_self: bool | None
    paragraphs: list[str]
    captured_words: int
    placeholder: bool
    own: list[str] = field(default_factory=list)
    own_text: str = ""
    own_words: int = 0
    furniture_words: int = 0
    thin: bool = False
    skeleton: str = ""
    series_id: str | None = None
    word_set: frozenset[str] = frozenset()
    index: int = -1
    """Row in the page matrix; -1 for thin pages (no vector)."""

    @property
    def full_text(self) -> str:
        """Every captured paragraph, furniture included."""
        return "\n".join(self.paragraphs)


@dataclass
class SemanticPair:
    """Two pages and their three scores."""

    a: str
    b: str
    meaning: float
    overlap: float
    same_text: float | None
    tier: Tier
    why: str
    coverage_a: float | None = None
    coverage_b: float | None = None
    series_id: str | None = None
    group_size: int | None = None
    """For identical groups larger than the fan-out: how many pages share the text."""


@dataclass
class Series:
    """A recurring publication: the same title skeleton issued again and again."""

    series_id: str
    name: str
    members: list[str]
    median_meaning: float | None
    median_overlap: float | None
    pairs: int
    first_published: str
    last_published: str


@dataclass
class TopicHub:
    """Moderate-tier pages that chain together: the same topic, different content."""

    hub_id: str
    label: str
    members: list[str]
    pairs: int
    median_meaning: float


@dataclass
class RepeatedBlock:
    """A long paragraph repeated on several pages of one section or page type."""

    text: str
    words: int
    pages: list[str]
    confined_to: str
    removed_from_scoring: bool


@dataclass
class Flags:
    """Page-level findings that need no pair."""

    cut_off_titles: list[tuple[str, str]] = field(default_factory=list)
    """(url, reason)."""
    noindex: list[str] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)
    duplicate_addresses: list[tuple[str, str, float]] = field(default_factory=list)
    """(url a, url b, same-text ratio)."""


@dataclass
class SemanticResult:
    """Everything the semantic workbook and the manifest need."""

    model: str
    config: SemanticConfig
    pages: list[SemanticPage]
    duplicates: list[SemanticPair]
    series: list[Series]
    series_pairs: int
    cross_series_pairs: int
    unsupported_pairs: int
    hubs: list[TopicHub]
    repeated_blocks: list[RepeatedBlock]
    furniture: list[tuple[str, int]]
    flags: Flags
    capped: bool
    seconds: float
    embedded: int
    cached: int

    def tier_counts(self) -> dict[str, int]:
        """Pairs per tier."""
        counts: Counter[str] = Counter(p.tier for p in self.duplicates)
        return {tier: counts.get(tier, 0) for tier in TIER_ORDER}

    def summary(self) -> dict[str, Any]:
        """Counts for the bundle's manifest."""
        return {
            "model": self.model,
            "pages": len(self.pages),
            "compared": sum(1 for p in self.pages if not p.thin),
            "thin": sum(1 for p in self.pages if p.thin),
            "furniture_paragraphs": len(self.furniture),
            "pairs": self.tier_counts(),
            "duplicate_pairs": sum(
                1 for p in self.duplicates if p.tier in ("Identical", "Copy", "High")
            ),
            "topic_hubs": len(self.hubs),
            "series": len(self.series),
            "series_pages": sum(len(s.members) for s in self.series),
            "series_pairs": self.series_pairs,
            "cross_series_pairs": self.cross_series_pairs,
            "unsupported_pairs": self.unsupported_pairs,
            "repeated_blocks": len(self.repeated_blocks),
            "flags": {
                "cut_off_titles": len(self.flags.cut_off_titles),
                "noindex": len(self.flags.noindex),
                "placeholders": len(self.flags.placeholders),
                "duplicate_addresses": len(self.flags.duplicate_addresses),
            },
            "pairs_capped": self.capped,
            "embedded": self.embedded,
            "cached": self.cached,
            "seconds": round(self.seconds, 1),
        }


def load_stamps(records_path: Path) -> dict[str, PageStamp]:
    """The stamps written next to a run's records, by requested and final URL ({} if none)."""
    path = records_path.with_name(records_path.name.replace(".records.jsonl", ".stamps.jsonl"))
    if path == records_path or not path.is_file():
        return {}
    stamps: dict[str, PageStamp] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                stamp = PageStamp.model_validate_json(line)
                stamps[stamp.url] = stamp
                if stamp.final_url:
                    stamps.setdefault(stamp.final_url, stamp)
    return stamps


def _date(moment: datetime | None) -> str:
    return moment.date().isoformat() if moment else ""


def _same_address(a: str, b: str) -> bool:
    return a.rstrip("/").lower() == b.rstrip("/").lower()


def _page_type(stamp: PageStamp | None, url: str, words: int, published: str) -> str:
    signals = PageSignals(
        url=url,
        schema_types=list(stamp.schema_org.types) if stamp else [],
        og_type=stamp.og.get("type") if stamp else None,
        title=stamp.title if stamp else None,
        words=words,
        has_published_date=bool(published),
        media_type=stamp.media_type if stamp else None,
    )
    return str(classify_page(signals).page_type)


def _is_placeholder(record: UrlRecord, text: str) -> bool:
    tokens = [*record.warnings, *record.status_vector.reasons, *(g.reason for g in record.gaps)]
    if any("placeholder" in t for t in tokens):
        return True
    return bool(_PLACEHOLDER.search(text))


def _pages_from(
    records: Sequence[UrlRecord],
    websites: Mapping[str, str],
    stamps: Mapping[str, PageStamp],
    config: SemanticConfig,
) -> list[SemanticPage]:
    pages: list[SemanticPage] = []
    seen: set[str] = set()
    for record in records:
        mime = (record.sniffed_mime or "").lower()
        if (mime and "html" not in mime) or record.badge in _SKIP_BADGES:
            continue
        url = record.requested_url
        if url in seen:
            continue
        seen.add(url)
        paragraphs: list[str] = []
        for block in record.blocks:
            if block.role != "main" or block.layer != ContentLayer.RENDERED_VISIBLE:
                continue
            text = " ".join(block.normalized_text.split())
            if len(text.split()) >= config.paragraph_min_words and text not in paragraphs:
                paragraphs.append(text)
                if len(paragraphs) >= config.max_paragraphs:
                    break
        stamp = stamps.get(url) or (stamps.get(record.final_url) if record.final_url else None)
        published = _date(stamp.published_at if stamp else None) or _date(record.page_published_at)
        captured = sum(len(p.split()) for p in paragraphs)
        title = (stamp.title if stamp else None) or ""
        canonical = stamp.canonical if stamp else None
        pages.append(
            SemanticPage(
                url=url,
                website=websites.get(url, ""),
                title=title,
                h1=(stamp.h1 if stamp else None) or "",
                section=(stamp.path_l1 if stamp else None)
                or (urlsplit(url).path.strip("/").split("/")[0] if urlsplit(url).path else ""),
                page_type=_page_type(stamp, url, captured, published),
                published=published,
                robots_meta=(stamp.robots_meta if stamp else None) or "",
                canonical_self=None if not canonical else _same_address(canonical, url),
                paragraphs=paragraphs,
                captured_words=captured,
                placeholder=_is_placeholder(record, " ".join(paragraphs)),
            )
        )
    return pages


def _remove_furniture(
    pages: list[SemanticPage], config: SemanticConfig
) -> tuple[list[tuple[str, int]], list[RepeatedBlock]]:
    """Mark furniture and repeated blocks; fill each page's own content."""
    on_pages: dict[str, list[SemanticPage]] = defaultdict(list)
    for page in pages:
        for paragraph in page.paragraphs:
            on_pages[paragraph].append(page)
    threshold = config.furniture_threshold(len(pages))
    section_sizes = Counter(p.section for p in pages)
    type_sizes = Counter(p.page_type for p in pages)
    minority = max(1, int(len(pages) * config.confined_share))
    furniture: set[str] = set()
    repeated: list[RepeatedBlock] = []
    counts: list[tuple[str, int]] = []
    for paragraph, holders in on_pages.items():
        count = len(holders)
        if count < min(config.repeated_block_pages, threshold):
            continue
        confined = ""
        sections = {p.section for p in holders}
        types = {p.page_type for p in holders}
        if len(sections) == 1 and section_sizes[holders[0].section] <= minority:
            confined = (
                f"section /{holders[0].section}" if holders[0].section else "the home section"
            )
        elif len(types) == 1 and type_sizes[holders[0].page_type] <= minority:
            confined = f"page type {holders[0].page_type}"
        is_furniture = count >= threshold
        if is_furniture:
            furniture.add(paragraph)
            counts.append((paragraph, count))
        if confined and len(paragraph.split()) >= config.repeated_block_words:
            repeated.append(
                RepeatedBlock(
                    text=paragraph,
                    words=len(paragraph.split()),
                    pages=[p.url for p in holders],
                    confined_to=confined,
                    removed_from_scoring=is_furniture,
                )
            )
    for page in pages:
        page.own = [p for p in page.paragraphs if p not in furniture]
        page.own_text = "\n".join(page.own)
        page.own_words = sum(len(p.split()) for p in page.own)
        page.furniture_words = page.captured_words - page.own_words
        page.thin = page.own_words < config.min_own_words
        page.word_set = frozenset(words_of(page.own_text))
    counts.sort(key=lambda item: (-item[1], item[0]))
    repeated.sort(key=lambda r: (-len(r.pages), -r.words))
    return counts, repeated


def _embed_missing(
    texts: Sequence[str],
    embedder: Embedder,
    cache: VectorCache,
    config: SemanticConfig,
) -> tuple[int, int]:
    """Embed the paragraphs the cache does not have; returns (embedded, already cached)."""
    by_hash = {_hash(t): t for t in texts}
    known = cache.get(embedder.model, by_hash)
    missing = [key for key in by_hash if key not in known]
    for start in range(0, len(missing), config.embed_batch):
        keys = missing[start : start + config.embed_batch]
        vectors = embedder.embed([by_hash[k] for k in keys])
        if vectors is None or len(vectors) != len(keys):
            msg = f"the embedding model {embedder.model} failed on a batch of {len(keys)} texts"
            raise RuntimeError(msg)
        cache.put(embedder.model, dict(zip(keys, vectors, strict=True)))
    return len(missing), len(known)


def _page_vectors(
    page: SemanticPage, model: str, cache: VectorCache
) -> np.ndarray[Any, np.dtype[np.float32]]:
    """Unit vectors of a page's own paragraphs, one row each (padded to a common width)."""
    found = cache.get(model, [_hash(p) for p in page.own])
    vectors = [found[_hash(p)] for p in page.own if _hash(p) in found]
    if not vectors:
        return np.zeros((0, 1), dtype=np.float32)
    width = max(len(v) for v in vectors)
    matrix = np.zeros((len(vectors), width), dtype=np.float32)
    for row, vector in enumerate(vectors):
        matrix[row, : len(vector)] = vector
    return matrix


def _page_matrix(
    pages: Sequence[SemanticPage], model: str, cache: VectorCache
) -> np.ndarray[Any, np.dtype[np.float32]]:
    """One unit row per compared page: the normalised mean of its paragraph vectors."""
    rows: list[np.ndarray[Any, Any]] = []
    width = 1
    for page in pages:
        vectors = _page_vectors(page, model, cache)
        mean = _unit(vectors.sum(axis=0)) if len(vectors) else np.zeros(1, dtype=np.float32)
        width = max(width, len(mean))
        rows.append(mean)
    matrix = np.zeros((len(rows), width), dtype=np.float32)
    for index, row in enumerate(rows):
        matrix[index, : len(row)] = row
    return matrix


def _candidates(
    matrix: np.ndarray[Any, np.dtype[np.float32]], floor: float, cap: int
) -> tuple[list[tuple[int, int, float]], bool]:
    """Pairs (i < j) at or above the floor, strongest first; capped to `cap`."""
    n = len(matrix)
    found: list[tuple[int, int, float]] = []
    block = 512
    for start in range(0, n, block):
        sims = matrix[start : start + block] @ matrix.T
        rows, cols = np.nonzero(sims >= floor)
        keep = cols > rows + start
        for r, c in zip(rows[keep].tolist(), cols[keep].tolist(), strict=True):
            found.append((r + start, c, float(sims[r, c])))
        if len(found) > cap * 2:
            found.sort(key=lambda t: -t[2])
            del found[cap:]
    found.sort(key=lambda t: -t[2])
    capped = len(found) > cap
    return found[:cap], capped


def _same_text(a: SemanticPage, b: SemanticPage, cap: int) -> float:
    wa, wb = words_of(a.own_text)[:cap], words_of(b.own_text)[:cap]
    if not wa or not wb:
        return 0.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def _coverage(
    a: np.ndarray[Any, Any], b: np.ndarray[Any, Any], own_a: Sequence[str], match: float
) -> float:
    """Share of a's own words inside paragraphs that match some paragraph of b."""
    if not len(a) or not len(b):
        return 0.0
    width = max(a.shape[1], b.shape[1])
    pa = np.zeros((a.shape[0], width), dtype=np.float32)
    pb = np.zeros((b.shape[0], width), dtype=np.float32)
    pa[:, : a.shape[1]] = a
    pb[:, : b.shape[1]] = b
    best = (pa @ pb.T).max(axis=1)
    total = sum(len(p.split()) for p in own_a)
    matched = sum(len(p.split()) for p, score in zip(own_a, best, strict=True) if score >= match)
    return matched / total if total else 0.0


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for item in self.parent:
            out[self.find(item)].append(item)
        return out


def _assign_series(pages: list[SemanticPage], config: SemanticConfig) -> list[Series]:
    """Group pages into recurring series by title skeleton (exact, then near-identical)."""
    for page in pages:
        base = page.title or page.h1 or urlsplit(page.url).path.rstrip("/").rsplit("/", 1)[-1]
        page.skeleton = title_skeleton(base)
    by_skeleton: dict[str, list[SemanticPage]] = defaultdict(list)
    for page in pages:
        if page.skeleton:
            by_skeleton[page.skeleton].append(page)
    tokens = {s: frozenset(t for t in s.split() if t not in _STOP) for s in by_skeleton}
    uf = _UnionFind()
    blocks: dict[str, list[str]] = defaultdict(list)
    for skeleton, toks in tokens.items():
        uf.find(skeleton)
        parts = skeleton.split()
        if len(toks) < 3:
            continue
        blocks[f"first:{parts[0]}"].append(skeleton)
        blocks[f"last:{parts[-1]}"].append(skeleton)
    for block in blocks.values():
        if len(block) > 400:
            continue  # a block this wide is a generic word, not a series signal
        for i, left in enumerate(block):
            for right in block[i + 1 :]:
                smaller = min(len(tokens[left]), len(tokens[right]))
                if smaller < 4:
                    continue
                shared = len(tokens[left] & tokens[right]) / smaller
                if shared >= config.series_title_containment:
                    uf.union(left, right)
    series: list[Series] = []
    for root, skeletons in uf.groups().items():
        members = [p for s in skeletons for p in by_skeleton[s]]
        dated = {p.published for p in members if p.published}
        if len(dated) < 2:
            continue  # a series is issued again and again; undated or same-day pages are not one
        recurring_pair = len(members) == 2 and (
            len({p.title for p in members}) > 1 or len({p.published for p in members}) > 1
        )
        if len(members) < config.series_min_members and not recurring_pair:
            continue
        name = Counter(p.title for p in members).most_common(1)[0][0] or root
        series_id = "series-" + _hash(root)[:10]
        for page in members:
            page.series_id = series_id
        dates = sorted(p.published for p in members if p.published)
        series.append(
            Series(
                series_id=series_id,
                name=name,
                members=[p.url for p in members],
                median_meaning=None,
                median_overlap=None,
                pairs=0,
                first_published=dates[0] if dates else "",
                last_published=dates[-1] if dates else "",
            )
        )
    series.sort(key=lambda s: (-len(s.members), s.name))
    return series


def _series_stats(
    series: list[Series],
    by_url: Mapping[str, SemanticPage],
    matrix: np.ndarray[Any, np.dtype[np.float32]],
) -> None:
    for item in series:
        compared = [by_url[u] for u in item.members if by_url[u].index >= 0][:200]
        if len(compared) < 2:
            continue
        sub = matrix[[p.index for p in compared]]
        sims = sub @ sub.T
        upper = sims[np.triu_indices(len(compared), k=1)]
        overlaps = [
            jaccard(compared[i].word_set, compared[j].word_set)
            for i in range(len(compared))
            for j in range(i + 1, len(compared))
        ]
        item.median_meaning = round(float(np.median(upper)), 3)
        item.median_overlap = round(float(np.median(overlaps)), 3)
        item.pairs = len(upper)


def _hub_label(pages: Sequence[SemanticPage], common: frozenset[str]) -> str:
    words = Counter(
        w
        for p in pages
        for w in dict.fromkeys(words_of(p.title or p.h1))
        if w not in _STOP and w not in common and len(w) > 2
    )
    top = [w for w, c in words.most_common(6) if c >= 2][:3] or [w for w, _ in words.most_common(3)]
    return " / ".join(top)


def _flags(pages: Sequence[SemanticPage], config: SemanticConfig) -> Flags:
    flags = Flags()
    by_title: dict[str, list[SemanticPage]] = defaultdict(list)
    for page in pages:
        if "noindex" in page.robots_meta.lower():
            flags.noindex.append(page.url)
        if page.placeholder:
            flags.placeholders.append(page.url)
        title = page.title.strip()
        if not title:
            continue
        by_title[title.lower()].append(page)
        h1 = page.h1.strip()
        stem = title.split("|")[0].strip().rstrip(" .…-–:").lower()
        if h1 and stem and len(h1) > len(stem) + 3:
            if h1.lower().startswith(stem):
                flags.cut_off_titles.append(
                    (page.url, f"the title stops before the heading does: “{title}” vs “{h1}”")
                )
            elif h1.lower().endswith(stem):
                flags.cut_off_titles.append(
                    (page.url, f"the title lost the start of the heading: “{title}” vs “{h1}”")
                )
    for title, all_holders in by_title.items():
        holders = [p for p in all_holders if p.series_id is None]
        if len(holders) < 2 or len(title) < 50:
            continue
        if len({_hash(p.own_text) for p in holders}) > 1:
            for page in holders:
                flags.cut_off_titles.append(
                    (page.url, f"{len(holders)} different pages share the long title “{title}”")
                )
    return flags


def analyse_semantic(
    records: Sequence[UrlRecord],
    websites: Mapping[str, str],
    stamps: Mapping[str, PageStamp],
    embedder: Embedder,
    cache: VectorCache,
    config: SemanticConfig | None = None,
) -> SemanticResult:
    """Score every pair of pages on their own content and sort the findings into sheets."""
    config = config or SemanticConfig()
    started = time.monotonic()
    pages = _pages_from(records, websites, stamps, config)
    furniture, repeated = _remove_furniture(pages, config)
    series = _assign_series(pages, config)
    by_url = {p.url: p for p in pages}

    compared = [p for p in pages if not p.thin]
    for index, page in enumerate(compared):
        page.index = index
    texts = list(dict.fromkeys(p for page in compared for p in page.own))
    embedded, cached = _embed_missing(texts, embedder, cache, config)
    matrix = _page_matrix(compared, embedder.model, cache)
    candidates, capped = _candidates(matrix, config.report_floor, config.max_pairs)

    duplicates: list[SemanticPair] = []
    series_pairs = 0
    cross_series_pairs = 0
    unsupported_pairs = 0
    address_pairs: list[tuple[str, str, float]] = []
    vectors_lru: dict[str, np.ndarray[Any, Any]] = {}

    def paragraph_vectors(page: SemanticPage) -> np.ndarray[Any, Any]:
        if page.url not in vectors_lru:
            if len(vectors_lru) >= 400:
                vectors_lru.pop(next(iter(vectors_lru)))
            vectors_lru[page.url] = _page_vectors(page, embedder.model, cache)
        return vectors_lru[page.url]

    for i, j, meaning in candidates:
        a, b = compared[i], compared[j]
        overlap = jaccard(a.word_set, b.word_set)
        in_series = a.series_id is not None and a.series_id == b.series_id
        cross_series = a.series_id is not None and b.series_id is not None and not in_series
        same_date = not a.published or not b.published or a.published == b.published
        # The same-text ratio is slow; it is computed only where it can decide something:
        # literal sameness needs most words in common, and inside a series only a same-day
        # copy changes the outcome.
        same_text = None
        if overlap >= config.same_text_overlap and (same_date or not in_series):
            same_text = 1.0 if a.own_text == b.own_text else _same_text(a, b, config.text_words_cap)
        tier = tier_of(meaning, same_text, config)
        if tier is None:
            continue
        true_copy = same_text is not None and same_text >= config.identical_text and same_date
        if in_series and not true_copy:
            series_pairs += 1
            continue
        if cross_series and tier != "Identical":
            cross_series_pairs += 1  # two templates of one news room: the same topic by design
            continue
        if tier == "Moderate" and (a.series_id or b.series_id):
            cross_series_pairs += 1
            continue
        coverage_a = coverage_b = None
        if tier != "Identical":
            va, vb = paragraph_vectors(a), paragraph_vectors(b)
            coverage_a = round(_coverage(va, vb, a.own, config.paragraph_match), 2)
            coverage_b = round(_coverage(vb, va, b.own, config.paragraph_match), 2)
            supported = (
                overlap >= config.evidence_overlap
                or min(coverage_a, coverage_b) >= config.evidence_coverage
            )
            if tier in ("Copy", "High") and not supported:
                # the pages point the same way, but paragraph by paragraph they do not match
                if a.series_id or b.series_id:
                    unsupported_pairs += 1
                    continue
                tier = "Moderate"
        if same_text is not None and same_text >= config.address_text:
            address_pairs.append((a.url, b.url, round(same_text, 3)))
        if tier == "Identical":
            why = "the same text" + (
                " at another address" if same_text and same_text >= config.address_text else ""
            )
        elif tier == "Copy":
            why = "a copy with small changes" if overlap >= 0.6 else "the same content, reworded"
        elif tier == "High":
            why = (
                "the same template text with different figures"
                if overlap >= 0.4
                else "the same content said again in other words"
            )
        else:
            why = "the same topic, different content (hub candidate)"
        pair = SemanticPair(
            a=a.url,
            b=b.url,
            meaning=round(meaning, 3),
            overlap=round(overlap, 3),
            same_text=round(same_text, 3) if same_text is not None else None,
            tier=tier,
            why=why,
            coverage_a=coverage_a,
            coverage_b=coverage_b,
            series_id=a.series_id if in_series else None,
        )
        duplicates.append(pair)

    # Pages with little or no own content: identical when their whole captured text matches.
    by_full: dict[str, list[SemanticPage]] = defaultdict(list)
    for page in pages:
        if page.thin and page.paragraphs:
            by_full[_hash(page.full_text)].append(page)
    for group in by_full.values():
        if len(group) < 2:
            continue
        pairs = (
            [(group[0], other) for other in group[1:]]
            if len(group) > config.identical_group_fanout
            else [(group[i], group[j]) for i in range(len(group)) for j in range(i + 1, len(group))]
        )
        for a, b in pairs:
            address_pairs.append((a.url, b.url, 1.0))
            duplicates.append(
                SemanticPair(
                    a=a.url,
                    b=b.url,
                    meaning=1.0,
                    overlap=1.0,
                    same_text=1.0,
                    tier="Identical",
                    why="the same text at another address (whole captured page)",
                    group_size=len(group) if len(group) > config.identical_group_fanout else None,
                )
            )

    duplicates.sort(key=lambda p: (TIER_ORDER[p.tier], -p.meaning, -(p.same_text or 0.0)))
    _series_stats(series, by_url, matrix)

    uf = _UnionFind()
    for pair in duplicates:
        if pair.tier == "Moderate":
            uf.union(pair.a, pair.b)
    common = frozenset(
        w
        for w, c in Counter(w for p in pages for w in set(words_of(p.title))).items()
        if c > len(pages) / 2
    )
    hubs: list[TopicHub] = []
    moderate = [p for p in duplicates if p.tier == "Moderate"]
    for root, members in uf.groups().items():
        if len(members) < 2:
            continue
        inside = [p for p in moderate if uf.find(p.a) == root]
        hubs.append(
            TopicHub(
                hub_id="hub-" + _hash(root)[:10],
                label=_hub_label([by_url[u] for u in members], common),
                members=sorted(members),
                pairs=len(inside),
                median_meaning=round(float(np.median([p.meaning for p in inside])), 3),
            )
        )
    hubs.sort(key=lambda h: (-len(h.members), -h.median_meaning))

    flags = _flags(pages, config)
    flags.duplicate_addresses = address_pairs
    return SemanticResult(
        model=embedder.model,
        config=config,
        pages=pages,
        duplicates=duplicates,
        series=series,
        series_pairs=series_pairs,
        cross_series_pairs=cross_series_pairs,
        unsupported_pairs=unsupported_pairs,
        hubs=hubs,
        repeated_blocks=repeated,
        furniture=furniture,
        flags=flags,
        capped=capped,
        seconds=time.monotonic() - started,
        embedded=embedded,
        cached=cached,
    )
