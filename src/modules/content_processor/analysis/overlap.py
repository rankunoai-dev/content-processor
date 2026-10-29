"""Content overlap between URLs: shingles, MinHash + LSH, exact verification (I2 mode B).

Pipeline (STAGE4_ANALYSIS_AND_INTERFACES.md §2, I2; percentages as in §3):

1. Text per URL is its corpus blocks (R1 layers, reading order, main role
   first, boilerplate roles excluded by default) joined by newlines. Words
   are lower-cased Unicode word tokens; shingles are word n-grams (5 by
   default). Each shingle is hashed once (64-bit BLAKE2b).
2. URLs with identical shingle sets collapse onto one representative (exact
   duplicates; no sketch needed).
3. One-permutation MinHash with densification gives each representative a
   `num_perm`-slot signature from that single hash per shingle, and LSH
   banding over the signature proposes candidate pairs. Band and row counts
   are chosen so a pair at the candidate floor is proposed with probability
   ≥ 0.99; the parameters are reported with every result.
4. Every candidate is verified on the real shingle sets. Only verified
   numbers are reported: `jaccard_pct`, `containment_a_in_b_pct`,
   `containment_b_in_a_pct`. A sketch estimate is never shown as a result.
5. Groups are connected components over pairs at or above the threshold, or,
   with `strict=True`, greedy cliques in which every pair meets it.

Limits stated in the output: LSH proposes pairs by Jaccard, so in containment
mode a short page inside a much longer one can be missed when the pair's
Jaccard is below the candidate floor (`candidate_floor`, default 0.2 there).
Every displayed quote is an exact substring of the first URL's content.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.export.content import (
    DEFAULT_LAYERS,
    ROLE_ORDER,
    drop_duplicates,
)
from src.modules.content_processor.export.tabular import Cell, Table

__all__ = [
    "R2A_COLUMNS",
    "R2B_COLUMNS",
    "OverlapConfig",
    "OverlapDoc",
    "OverlapPair",
    "OverlapResult",
    "choose_bands",
    "docs_from_records",
    "find_overlaps",
    "r2a_table",
    "r2b_table",
    "shingle_document",
]

Metric = Literal["jaccard", "containment"]
_WORD = re.compile(r"\w+", re.UNICODE)
_MASK64 = (1 << 64) - 1
_QUOTE_LIMIT = 300
_TARGET_RECALL = 0.99
_CONTENT_ROLES: tuple[str, ...] = ("main", "unknown", "aside", "legal")

R2A_COLUMNS: tuple[str, ...] = (
    "run_id",
    "group_id",
    "group_size",
    "url",
    "badge",
    "overlap_to_group_pct",
    "shared_block_count",
    "shared_block_hashes",
    "mode",
    "metric",
    "threshold",
    "evidence_ids",
)
R2B_COLUMNS: tuple[str, ...] = (
    "run_id",
    "url_a",
    "url_b",
    "badge_a",
    "badge_b",
    "containment_a_in_b_pct",
    "containment_b_in_a_pct",
    "jaccard_pct",
    "shared_shingles",
    "shared_spans",
    "pair_basis",
    "evidence_ids",
)


@dataclass(frozen=True)
class OverlapConfig:
    """Knobs for one overlap analysis. Defaults follow the Stage 4 spec."""

    shingle_size: int = 5
    threshold: float = 0.8
    metric: Metric = "jaccard"
    strict: bool = False
    include_boilerplate: bool = False
    num_perm: int = 128
    candidate_floor: float | None = None
    """Lowest Jaccard LSH must propose; None = threshold - 0.2 (jaccard) or 0.2 (containment)."""
    max_pairs: int = 200_000

    def floor(self) -> float:
        """The Jaccard level at which candidates are still proposed with ≥ 99% probability."""
        if self.candidate_floor is not None:
            return self.candidate_floor
        if self.metric == "containment":
            return 0.2
        return max(0.05, self.threshold - 0.2)


@dataclass
class OverlapDoc:
    """One URL prepared for comparison."""

    url: str
    run_id: str
    badge: str
    text: str
    shingles: set[int]
    shingle_starts: list[tuple[int, int]]
    """Per shingle position: (start, end) character offsets in `text`."""
    shingle_seq: list[int]
    block_hashes: set[str]
    evidence_ids: list[str]


@dataclass
class OverlapPair:
    """A verified pair at or above the threshold."""

    a: OverlapDoc
    b: OverlapDoc
    shared: int
    jaccard: float
    containment_a_in_b: float
    containment_b_in_a: float
    basis: Literal["exact_duplicate", "lsh_verified"]

    def score(self, metric: Metric) -> float:
        """The value the threshold applies to."""
        if metric == "containment":
            return max(self.containment_a_in_b, self.containment_b_in_a)
        return self.jaccard


@dataclass
class OverlapResult:
    """Pairs, groups and the parameters that produced them."""

    config: OverlapConfig
    pairs: list[OverlapPair]
    groups: list[list[OverlapDoc]]
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """(url, reason) for URLs not compared, e.g. no content."""
    bands: int = 0
    rows: int = 0
    candidates_checked: int = 0
    pairs_capped: bool = False

    def summary(self) -> dict[str, object]:
        """Parameters and counts for the report bundle's manifest."""
        return {
            "shingle_size": self.config.shingle_size,
            "metric": self.config.metric,
            "threshold": self.config.threshold,
            "strict": self.config.strict,
            "include_boilerplate": self.config.include_boilerplate,
            "num_perm": self.config.num_perm,
            "lsh_bands": self.bands,
            "lsh_rows": self.rows,
            "candidate_floor": self.config.floor(),
            "candidates_checked": self.candidates_checked,
            "pairs": len(self.pairs),
            "pairs_capped": self.pairs_capped,
            "groups": len(self.groups),
            "skipped": [{"url": u, "reason": r} for u, r in self.skipped],
        }


def _hash64(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big")


def shingle_document(text: str, size: int) -> tuple[list[int], list[tuple[int, int]]]:
    """Hashed word n-grams in order, with the character span each one covers.

    Text shorter than `size` words yields one shingle of all its words.
    """
    tokens = [(m.group(0).lower(), m.start(), m.end()) for m in _WORD.finditer(text)]
    if not tokens:
        return [], []
    width = min(size, len(tokens))
    seq: list[int] = []
    spans: list[tuple[int, int]] = []
    for i in range(len(tokens) - width + 1):
        window = tokens[i : i + width]
        seq.append(_hash64(" ".join(t[0] for t in window)))
        spans.append((window[0][1], window[-1][2]))
    return seq, spans


def _block_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def docs_from_records(
    records: Iterable[UrlRecord], config: OverlapConfig
) -> tuple[list[OverlapDoc], list[tuple[str, str]]]:
    """Prepare comparable documents; URLs without text are returned as skipped."""
    docs: list[OverlapDoc] = []
    skipped: list[tuple[str, str]] = []
    roles = ROLE_ORDER if config.include_boilerplate else _CONTENT_ROLES
    for record in records:
        blocks = sorted(
            drop_duplicates(
                [
                    b
                    for b in record.blocks
                    if b.normalized_text and b.layer in DEFAULT_LAYERS and b.role in roles
                ]
            ),
            key=lambda b: (roles.index(b.role), b.reading_order),
        )
        # The blocks' own text, joined by newlines: every quote taken from it is
        # verbatim block text (no synthesized heading markers), as I2 requires.
        text = "\n".join(b.normalized_text for b in blocks)
        seq, spans = shingle_document(text, config.shingle_size)
        if not seq:
            skipped.append((record.requested_url, "no_content"))
            continue
        docs.append(
            OverlapDoc(
                url=record.requested_url,
                run_id=record.run_id,
                badge=record.badge.value,
                text=text,
                shingles=set(seq),
                shingle_starts=spans,
                shingle_seq=seq,
                block_hashes={_block_hash(b.normalized_text) for b in blocks},
                evidence_ids=list(dict.fromkeys(e for b in blocks for e in b.evidence_ids)),
            )
        )
    return docs, skipped


def choose_bands(num_perm: int, floor: float) -> tuple[int, int]:
    """(bands, rows) with the most rows whose recall at `floor` is still ≥ 99%."""
    best = (num_perm, 1)
    for rows in range(1, num_perm + 1):
        bands = num_perm // rows
        if bands == 0:
            break
        recall = 1.0 - (1.0 - floor**rows) ** bands
        if recall >= _TARGET_RECALL:
            best = (bands, rows)
    return best


def _signature(shingles: set[int], slots: int) -> list[int]:
    """One-permutation MinHash with rotation densification."""
    sig: list[int | None] = [None] * slots
    for h in shingles:
        slot = h % slots
        value = h // slots
        current = sig[slot]
        if current is None or value < current:
            sig[slot] = value
    filled = [i for i, v in enumerate(sig) if v is not None]
    out: list[int] = []
    for i in range(slots):
        v = sig[i]
        if v is not None:
            out.append(v)
            continue
        # Borrow from the next filled slot to the right, salted by the distance,
        # so two documents with the same empty slots stay comparable.
        nxt = next((j for j in filled if j > i), filled[0])
        distance = (nxt - i) % slots
        borrowed = sig[nxt]
        assert borrowed is not None  # noqa: S101 - nxt is a filled slot
        out.append((borrowed * 0x9E3779B97F4A7C15 + distance) & _MASK64)
    return out


def _verify(
    a: OverlapDoc, b: OverlapDoc, basis: Literal["exact_duplicate", "lsh_verified"]
) -> OverlapPair:
    shared = len(a.shingles & b.shingles)
    union = len(a.shingles) + len(b.shingles) - shared
    return OverlapPair(
        a=a,
        b=b,
        shared=shared,
        jaccard=shared / union if union else 0.0,
        containment_a_in_b=shared / len(a.shingles),
        containment_b_in_a=shared / len(b.shingles),
        basis=basis,
    )


def _ordered(a: OverlapDoc, b: OverlapDoc) -> tuple[OverlapDoc, OverlapDoc]:
    return (a, b) if a.url <= b.url else (b, a)


def find_overlaps(
    records: Iterable[UrlRecord], config: OverlapConfig | None = None
) -> OverlapResult:
    """Run the full mode-B analysis over a run's records."""
    cfg = config or OverlapConfig()
    if not 0.0 < cfg.threshold <= 1.0:
        msg = "threshold must be in (0, 1]"
        raise ValueError(msg)
    docs, skipped = docs_from_records(records, cfg)
    result = OverlapResult(config=cfg, pairs=[], groups=[], skipped=skipped)

    clusters: dict[frozenset[int], list[OverlapDoc]] = defaultdict(list)
    for doc in docs:
        clusters[frozenset(doc.shingles)].append(doc)
    reps: list[OverlapDoc] = []
    pairs: list[OverlapPair] = []
    for members in clusters.values():
        members.sort(key=lambda d: d.url)
        reps.append(members[0])
        pairs.extend(_verify(*_ordered(members[0], m), "exact_duplicate") for m in members[1:])

    bands, rows = choose_bands(cfg.num_perm, cfg.floor())
    result.bands, result.rows = bands, rows
    buckets: dict[tuple[int, tuple[int, ...]], list[int]] = defaultdict(list)
    for index, doc in enumerate(reps):
        sig = _signature(doc.shingles, cfg.num_perm)
        for band in range(bands):
            buckets[(band, tuple(sig[band * rows : (band + 1) * rows]))].append(index)
    candidates: set[tuple[int, int]] = set()
    for bucket in buckets.values():
        for pos, first in enumerate(bucket):
            for second in bucket[pos + 1 :]:
                candidates.add((first, second))
    result.candidates_checked = len(candidates)
    for first, second in sorted(candidates):
        pair = _verify(*_ordered(reps[first], reps[second]), "lsh_verified")
        if pair.score(cfg.metric) >= cfg.threshold:
            pairs.append(pair)

    # Members of an exact-duplicate cluster have the same shingle set as their
    # representative, so every member pairs with every member of the other side
    # with the representative pair's numbers.
    by_rep = {members[0].url: members for members in clusters.values()}
    expanded: list[OverlapPair] = []
    for pair in pairs:
        if pair.basis != "lsh_verified":
            expanded.append(pair)
            continue
        for left in by_rep[pair.a.url]:
            for right in by_rep[pair.b.url]:
                a, b = _ordered(left, right)
                swap = a is right  # ordering put the B-side member first
                expanded.append(
                    OverlapPair(
                        a=a,
                        b=b,
                        shared=pair.shared,
                        jaccard=pair.jaccard,
                        containment_a_in_b=(
                            pair.containment_b_in_a if swap else pair.containment_a_in_b
                        ),
                        containment_b_in_a=(
                            pair.containment_a_in_b if swap else pair.containment_b_in_a
                        ),
                        basis="lsh_verified",
                    )
                )
    expanded = [p for p in expanded if p.score(cfg.metric) >= cfg.threshold]
    expanded.sort(key=lambda p: (-p.score(cfg.metric), p.a.url, p.b.url))
    if len(expanded) > cfg.max_pairs:
        expanded = expanded[: cfg.max_pairs]
        result.pairs_capped = True
    result.pairs = expanded
    result.groups = _groups(docs, expanded, cfg)
    return result


def _groups(
    docs: Sequence[OverlapDoc], pairs: Sequence[OverlapPair], cfg: OverlapConfig
) -> list[list[OverlapDoc]]:
    by_url = {d.url: d for d in docs}
    neighbours: dict[str, set[str]] = defaultdict(set)
    for p in pairs:
        neighbours[p.a.url].add(p.b.url)
        neighbours[p.b.url].add(p.a.url)
    groups: list[list[str]] = []
    if cfg.strict:
        for url in sorted(neighbours):
            for group in groups:
                if all(member in neighbours[url] for member in group):
                    group.append(url)
                    break
            else:
                groups.append([url])
    else:
        seen: set[str] = set()
        for url in sorted(neighbours):
            if url in seen:
                continue
            stack, component = [url], []
            seen.add(url)
            while stack:
                current = stack.pop()
                component.append(current)
                for nxt in neighbours[current]:
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            groups.append(sorted(component))
    real = [sorted(g) for g in groups if len(g) > 1]
    real.sort(key=lambda g: (-len(g), g[0]))
    return [[by_url[u] for u in g] for g in real]


def _shared_spans(
    a: OverlapDoc,
    b: OverlapDoc,
    limit: int | None = 3,
    max_chars: int | None = _QUOTE_LIMIT,
) -> list[str]:
    """Verbatim quotes from A covering runs of shingles B also has, in A's order.

    `limit` caps how many passages (None: all of them) and `max_chars` how long each
    may be before it is cut with "…" (None: never cut).
    """
    quotes: list[str] = []
    i = 0
    seq = a.shingle_seq
    while i < len(seq) and (limit is None or len(quotes) < limit):
        if seq[i] not in b.shingles:
            i += 1
            continue
        j = i
        while j + 1 < len(seq) and seq[j + 1] in b.shingles:
            j += 1
        start, end = a.shingle_starts[i][0], a.shingle_starts[j][1]
        quote = a.text[start:end]
        cut = max_chars is not None and len(quote) > max_chars
        quotes.append(quote[:max_chars] + "…" if cut else quote)
        i = j + 1
    return quotes


def _pct(value: float) -> float:
    return round(value * 100, 2)


def r2b_table(result: OverlapResult) -> Table:
    """Report R2b: one row per verified pair at or above the threshold."""
    rows: list[dict[str, Cell]] = []
    for p in result.pairs:
        rows.append(
            {
                "run_id": p.a.run_id,
                "url_a": p.a.url,
                "url_b": p.b.url,
                "badge_a": p.a.badge,
                "badge_b": p.b.badge,
                "containment_a_in_b_pct": _pct(p.containment_a_in_b),
                "containment_b_in_a_pct": _pct(p.containment_b_in_a),
                "jaccard_pct": _pct(p.jaccard),
                "shared_shingles": p.shared,
                "shared_spans": " || ".join(_shared_spans(p.a, p.b)),
                "pair_basis": p.basis,
                "evidence_ids": " ".join(dict.fromkeys([*p.a.evidence_ids, *p.b.evidence_ids])),
            }
        )
    return Table(name="R2b overlap pairs", columns=list(R2B_COLUMNS), rows=rows)


def r2a_table(result: OverlapResult) -> Table:
    """Report R2a: one row per URL per group.

    `overlap_to_group_pct` is the URL's containment in the union of the other
    members' shingles; `shared_block_hashes` are its blocks that at least one
    other member also has (sha256 of the normalized text, 16 hex chars).
    """
    cfg = result.config
    rows: list[dict[str, Cell]] = []
    for number, group in enumerate(result.groups, start=1):
        for doc in group:
            others = [d for d in group if d is not doc]
            union = set().union(*(d.shingles for d in others))
            other_blocks = set().union(*(d.block_hashes for d in others))
            shared_blocks = sorted(doc.block_hashes & other_blocks)
            rows.append(
                {
                    "run_id": doc.run_id,
                    "group_id": f"g-{number:04d}",
                    "group_size": len(group),
                    "url": doc.url,
                    "badge": doc.badge,
                    "overlap_to_group_pct": _pct(len(doc.shingles & union) / len(doc.shingles)),
                    "shared_block_count": len(shared_blocks),
                    "shared_block_hashes": " ".join(shared_blocks),
                    "mode": "content_overlap_strict" if cfg.strict else "content_overlap",
                    "metric": cfg.metric,
                    "threshold": cfg.threshold,
                    "evidence_ids": " ".join(doc.evidence_ids),
                }
            )
    return Table(name="R2a overlap groups", columns=list(R2A_COLUMNS), rows=rows)
