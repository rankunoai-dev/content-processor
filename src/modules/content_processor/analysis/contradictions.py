"""Contradiction candidates across a run (I4, STAGE4_ANALYSIS_AND_INTERFACES.md §2) and report R4.

Pipeline:

1. Claims: claim-bearing sentences of corpus blocks (`analysis.claims`), each
   with a verbatim quote, its span, evidence ids and a `ClaimContext`.
2. Candidates: a pluggable `CandidateSource`. `LexicalCandidates` pairs claims
   through an inverted index on shared content terms (frequent terms are not
   used for pairing). I3 semantic similarity plugs in as another source when it
   exists; every row records its `pair_basis`.
3. Deterministic checks run first and are reported separately: numeric
   disagreement on the same subject and unit, negation flip, effective-date
   conflict.
4. Context: geography, audience, period and conditions decide the bucket. Any
   of them differing puts the pair in `context_differs`, never among the
   contradictions. Entity and product (from heading paths, a heuristic until
   O-7) are shown side by side but do not decide the bucket.
5. Judge: an optional NLI judge (`analysis.nli`), both directions, the higher
   contradiction probability kept. Off by default; deterministic checks stand
   on their own and `contradiction_pct` stays blank.
6. Confidence band per pair from the weaker claim: badge, block status, layer,
   extraction method and confidence (OCR lowers it).

A pair is reported when a deterministic check fired or the judge's
contradiction probability meets the threshold (default 70%). Judge output
annotates; it never changes a status. Reviewer decisions from an earlier R4
are carried forward by `pair_id`, which is stable across recrawls.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, Protocol

from src.modules.content_processor.analysis.claims import Claim, claims_from_records
from src.modules.content_processor.analysis.nli import DisabledJudge, NliJudge, NliVerdict
from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.export.tabular import Cell, Table

__all__ = [
    "R4_COLUMNS",
    "REVIEWER_DECISIONS",
    "CandidateSource",
    "ContradictionConfig",
    "ContradictionPair",
    "ContradictionResult",
    "LexicalCandidates",
    "find_contradictions",
    "load_reviewer_decisions",
    "r4_table",
]

Bucket = Literal["context_matched", "context_unknown", "context_differs"]
_BUCKET_ORDER: dict[str, int] = {"context_matched": 0, "context_unknown": 1, "context_differs": 2}
_STRONG_FIELDS = ("geography", "audience", "period", "conditions", "codes")
_WEAK_FIELDS = ("entity", "product_or_variant")
_BAND_ORDER = {"high": 0, "medium": 1, "low": 2}
REVIEWER_DECISIONS: frozenset[str] = frozenset(
    {"confirmed", "not_a_contradiction", "context_differs"}
)
R4_COLUMNS: tuple[str, ...] = (
    "run_id",
    "pair_id",
    "bucket",
    "url_a",
    "quote_a",
    "url_b",
    "quote_b",
    "contradiction_pct",
    "judge_status",
    "judge_model",
    "deterministic_flags",
    "context_match",
    "confidence_band",
    "confidence_reason",
    "pair_basis",
    "heading_path_a",
    "heading_path_b",
    "block_id_a",
    "block_id_b",
    "layer_a",
    "layer_b",
    "status_a",
    "status_b",
    "badge_a",
    "badge_b",
    "captured_at_a",
    "captured_at_b",
    "evidence_ids_a",
    "evidence_ids_b",
    "reviewer_decision",
)


@dataclass(frozen=True)
class ContradictionConfig:
    """Knobs for one contradiction analysis. Defaults follow the Stage 4 spec."""

    threshold: float = 0.7
    """Judge contradiction probability at or above which a pair is reported."""
    min_shared_terms: int = 2
    min_term_jaccard: float = 0.25
    same_subject_jaccard: float = 0.5
    """Term Jaccard above which numeric and date checks treat two claims as one subject."""
    negation_jaccard: float = 0.6
    max_term_df: int = 200
    """Terms in more claims than this are too common to propose pairs."""
    max_pairs: int = 100_000
    max_judged_pairs: int = 2_000
    judge_differing_context: bool = False
    include_boilerplate: bool = False


class CandidateSource(Protocol):
    """Proposes claim pairs worth checking (lexical today; I3 semantic later)."""

    @property
    def name(self) -> str:
        """Recorded as `pair_basis` on every row."""
        ...

    def pairs(
        self, claims: Sequence[Claim], config: ContradictionConfig
    ) -> Iterable[tuple[int, int]]:
        """Index pairs (i < j) into `claims`."""
        ...


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


class LexicalCandidates:
    """Pairs claims that share enough uncommon content terms."""

    name = "lexical"

    def pairs(
        self, claims: Sequence[Claim], config: ContradictionConfig
    ) -> Iterable[tuple[int, int]]:
        """Inverted index on terms; terms above `max_term_df` claims are skipped."""
        postings: dict[str, list[int]] = defaultdict(list)
        for index, claim in enumerate(claims):
            for term in claim.terms:
                postings[term].append(index)
        shared: dict[tuple[int, int], int] = defaultdict(int)
        for indices in postings.values():
            if len(indices) < 2 or len(indices) > config.max_term_df:
                continue
            for x, i in enumerate(indices):
                for j in indices[x + 1 :]:
                    shared[(i, j)] += 1
        for (i, j), count in sorted(shared.items()):
            if (
                count >= config.min_shared_terms
                and _jaccard(claims[i].terms, claims[j].terms) >= config.min_term_jaccard
            ):
                yield i, j


@dataclass
class ContradictionPair:
    """One checked pair with its evidence, flags, context and (optional) judgement."""

    pair_id: str
    a: Claim
    b: Claim
    basis: str
    flags: list[str]
    context: dict[str, str]
    bucket: Bucket
    band: str
    band_reason: str
    verdicts: list[NliVerdict] = field(default_factory=list)

    @property
    def contradiction(self) -> float | None:
        """Highest judged contradiction probability over both directions, if judged."""
        return max((v.contradiction for v in self.verdicts), default=None)

    def reported(self, threshold: float) -> bool:
        """A deterministic check fired, or the judge met the threshold."""
        score = self.contradiction
        return bool(self.flags) or (score is not None and score >= threshold)


@dataclass
class ContradictionResult:
    """Reported pairs and the parameters and counts that produced them."""

    config: ContradictionConfig
    pairs: list[ContradictionPair]
    claims: int
    candidates_checked: int
    judged: int
    judge_status: str
    judge_model: str | None
    basis: list[str]
    pairs_capped: bool = False

    def summary(self) -> dict[str, object]:
        """Parameters and counts for the report bundle's manifest."""
        buckets: dict[str, int] = {}
        for pair in self.pairs:
            buckets[pair.bucket] = buckets.get(pair.bucket, 0) + 1
        return {
            "threshold": self.config.threshold,
            "claims": self.claims,
            "candidates_checked": self.candidates_checked,
            "pairs_capped": self.pairs_capped,
            "judged_pairs": self.judged,
            "judge_status": self.judge_status,
            "judge_model": self.judge_model,
            "pair_basis": self.basis,
            "reported_pairs": len(self.pairs),
            "by_bucket": buckets,
        }


# -- deterministic checks -------------------------------------------------------


def _numeric_flags(a: Claim, b: Claim, subject: float, config: ContradictionConfig) -> list[str]:
    if subject < config.same_subject_jaccard:
        return []
    flags: list[str] = []
    units = {q.unit for q in a.quantities} & {q.unit for q in b.quantities}
    for unit in sorted(units):
        va = {q.value for q in a.quantities if q.unit == unit}
        vb = {q.value for q in b.quantities if q.unit == unit}
        if va.isdisjoint(vb):
            ta = "/".join(q.text for q in a.quantities if q.unit == unit)
            tb = "/".join(q.text for q in b.quantities if q.unit == unit)
            flags.append(f"numeric:{unit}:{ta}!={tb}")
    return flags


def _negation_flags(a: Claim, b: Claim, subject: float, config: ContradictionConfig) -> list[str]:
    if a.negated != b.negated and subject >= config.negation_jaccard:
        return ["negation_flip"]
    return []


def _date_flags(a: Claim, b: Claim, subject: float, config: ContradictionConfig) -> list[str]:
    if subject < config.same_subject_jaccard or a.as_of or b.as_of:
        return []  # "as of" values are snapshots in time, not effective dates (K19)
    flags: list[str] = []
    ca, cb = a.context, b.context
    if ca.effective_from and cb.effective_from and ca.effective_from != cb.effective_from:
        flags.append(f"date_conflict:from:{ca.effective_from}!={cb.effective_from}")
    if ca.effective_to and cb.effective_to and ca.effective_to != cb.effective_to:
        flags.append(f"date_conflict:to:{ca.effective_to}!={cb.effective_to}")
    return flags


# -- context ---------------------------------------------------------------------


def _period(claim: Claim) -> tuple[date, date] | None:
    if claim.as_of:
        return (claim.as_of, claim.as_of)
    ctx = claim.context
    if ctx.effective_from or ctx.effective_to:
        return (ctx.effective_from or date.min, ctx.effective_to or date.max)
    if claim.years:
        return (date(min(claim.years), 1, 1), date(max(claim.years), 12, 31))
    return None


def _compare_codes(left: frozenset[str], right: frozenset[str]) -> str:
    """Entity codes (tickers, plan codes): disjoint sets are different entities."""
    if not left or not right:
        return "unknown"
    if left & right:
        return "match"
    return f"differs({','.join(sorted(left))}|{','.join(sorted(right))})"


def _compare(left: str | None, right: str | None) -> str:
    if not left or not right:
        return "unknown"
    return "match" if left.casefold() == right.casefold() else f"differs({left}|{right})"


def _context(a: Claim, b: Claim) -> tuple[dict[str, str], Bucket]:
    ca, cb = a.context, b.context
    fields = {
        "entity": _compare(ca.entity, cb.entity),
        "product_or_variant": _compare(ca.product_or_variant, cb.product_or_variant),
        "geography": _compare(ca.geography, cb.geography),
        "audience": _compare(ca.audience, cb.audience),
        "conditions": _compare("; ".join(ca.conditions), "; ".join(cb.conditions)),
    }
    fields["codes"] = _compare_codes(a.codes, b.codes)
    pa, pb = _period(a), _period(b)
    if a.years and b.years and a.years.isdisjoint(b.years):
        # Different years named on each side: different editions (yearly notices), K19.
        fields["period"] = f"differs({min(a.years)}|{min(b.years)})"
    elif pa is None or pb is None:
        fields["period"] = "unknown"
    elif pa[0] <= pb[1] and pb[0] <= pa[1]:
        fields["period"] = "match"
    else:
        fields["period"] = f"differs({pa[0].year}-{pa[1].year if pa[1] != date.max else ''}|"
        fields["period"] += f"{pb[0].year}-{pb[1].year if pb[1] != date.max else ''})"
    strong = [fields[k] for k in _STRONG_FIELDS]
    if any(v.startswith("differs") for v in strong):
        return fields, "context_differs"
    if any(v == "match" for v in strong) or any(fields[k] == "match" for k in _WEAK_FIELDS):
        return fields, "context_matched"
    return fields, "context_unknown"


# -- confidence --------------------------------------------------------------------


def _band(claim: Claim) -> tuple[str, list[str]]:
    level, reasons = "high", []
    if "ocr" in claim.method.lower() or claim.block_confidence < 0.8:
        level = "low"
        reasons.append(f"{claim.method} source, {claim.block_confidence:.2f} confidence")
    if claim.badge in ("BLOCKED", "FAILED"):
        level = "low"
        reasons.append(f"badge {claim.badge}")
    elif claim.badge != "VERIFIED_WITHIN_SCOPE":
        level = level if level == "low" else "medium"
        reasons.append(f"badge {claim.badge}")
    if claim.block_status != "VERIFIED_EXTRACTED":
        level = level if level == "low" else "medium"
        reasons.append(f"block {claim.block_status}")
    if claim.layer != "rendered_visible":
        level = level if level == "low" else "medium"
        reasons.append(f"layer {claim.layer}")
    return level, reasons


def _pair_band(a: Claim, b: Claim) -> tuple[str, str]:
    la, ra = _band(a)
    lb, rb = _band(b)
    level = la if _BAND_ORDER[la] >= _BAND_ORDER[lb] else lb
    reasons = [f"a: {r}" for r in ra] + [f"b: {r}" for r in rb]
    return level, "; ".join(reasons)


# -- analysis -----------------------------------------------------------------------


def _pair_id(a: Claim, b: Claim) -> str:
    keys = sorted((a.claim_key, b.claim_key))
    return "cp-" + hashlib.sha256("\x1f".join(keys).encode()).hexdigest()[:16]


def find_contradictions(
    records: Sequence[UrlRecord],
    config: ContradictionConfig | None = None,
    *,
    judge: NliJudge | None = None,
    sources: Sequence[CandidateSource] | None = None,
) -> ContradictionResult:
    """Find contradiction candidates across a run's records.

    Args:
        records: One `UrlRecord` per URL; their corpus blocks are the claims' source.
        config: Thresholds and caps; defaults follow the Stage 4 spec.
        judge: NLI judge; `DisabledJudge` when None (deterministic checks only).
        sources: Candidate sources; `[LexicalCandidates()]` when None.
    """
    cfg = config or ContradictionConfig()
    nli: NliJudge = judge or DisabledJudge()
    srcs: Sequence[CandidateSource] = sources or [LexicalCandidates()]
    claims = sorted(
        claims_from_records(records, include_boilerplate=cfg.include_boilerplate),
        key=lambda c: (c.url, c.block_id, c.start),
    )

    basis_by_pair: dict[tuple[int, int], str] = {}
    capped = False
    for source in srcs:
        for i, j in source.pairs(claims, cfg):
            key = (min(i, j), max(i, j))
            if key in basis_by_pair or i == j:
                continue
            if len(basis_by_pair) >= cfg.max_pairs:
                capped = True
                break
            basis_by_pair[key] = source.name

    checked: list[ContradictionPair] = []
    for (i, j), basis in sorted(basis_by_pair.items()):
        a, b = claims[i], claims[j]
        if a.text.casefold() == b.text.casefold():
            continue  # the same statement repeated is overlap (I2), not a contradiction
        subject = _jaccard(a.terms, b.terms)
        flags = (
            _numeric_flags(a, b, subject, cfg)
            + _negation_flags(a, b, subject, cfg)
            + _date_flags(a, b, subject, cfg)
        )
        context, bucket = _context(a, b)
        band, reason = _pair_band(a, b)
        checked.append(
            ContradictionPair(
                pair_id=_pair_id(a, b),
                a=a,
                b=b,
                basis=basis,
                flags=flags,
                context=context,
                bucket=bucket,
                band=band,
                band_reason=reason,
            )
        )

    judged = 0
    if nli.model is not None:
        queue = sorted(
            (p for p in checked if cfg.judge_differing_context or p.bucket != "context_differs"),
            key=lambda p: (not p.flags, _BUCKET_ORDER[p.bucket], p.pair_id),
        )
        for pair in queue[: cfg.max_judged_pairs]:
            for premise, hypothesis in ((pair.a.text, pair.b.text), (pair.b.text, pair.a.text)):
                verdict = nli.judge(premise, hypothesis)
                if verdict is not None:
                    pair.verdicts.append(verdict)
            judged += 1

    reported = [p for p in checked if p.reported(cfg.threshold)]
    reported.sort(
        key=lambda p: (
            _BUCKET_ORDER[p.bucket],
            -(p.contradiction or 0.0),
            -len(p.flags),
            p.pair_id,
        )
    )
    return ContradictionResult(
        config=cfg,
        pairs=reported,
        claims=len(claims),
        candidates_checked=len(checked),
        judged=judged,
        judge_status=nli.status,
        judge_model=nli.model,
        basis=[s.name for s in srcs],
        pairs_capped=capped,
    )


# -- report R4 ----------------------------------------------------------------------


def r4_table(result: ContradictionResult, decisions: Mapping[str, str] | None = None) -> Table:
    """Report R4: one row per reported pair; `reviewer_decision` carried forward or blank."""
    carried = decisions or {}
    rows: list[dict[str, Cell]] = []
    for p in result.pairs:
        score = p.contradiction
        rows.append(
            {
                "run_id": p.a.run_id,
                "pair_id": p.pair_id,
                "bucket": p.bucket,
                "url_a": p.a.url,
                "quote_a": p.a.text,
                "url_b": p.b.url,
                "quote_b": p.b.text,
                "contradiction_pct": None if score is None else round(score * 100, 2),
                "judge_status": result.judge_status,
                "judge_model": result.judge_model,
                "deterministic_flags": " ".join(p.flags),
                "context_match": "; ".join(f"{k}={v}" for k, v in p.context.items()),
                "confidence_band": p.band,
                "confidence_reason": p.band_reason,
                "pair_basis": p.basis,
                "heading_path_a": " > ".join(p.a.heading_path),
                "heading_path_b": " > ".join(p.b.heading_path),
                "block_id_a": p.a.block_id,
                "block_id_b": p.b.block_id,
                "layer_a": p.a.layer,
                "layer_b": p.b.layer,
                "status_a": p.a.block_status,
                "status_b": p.b.block_status,
                "badge_a": p.a.badge,
                "badge_b": p.b.badge,
                "captured_at_a": p.a.captured_at.isoformat(),
                "captured_at_b": p.b.captured_at.isoformat(),
                "evidence_ids_a": " ".join(p.a.evidence_ids),
                "evidence_ids_b": " ".join(p.b.evidence_ids),
                "reviewer_decision": carried.get(p.pair_id, ""),
            }
        )
    return Table(name="R4 contradiction candidates", columns=list(R4_COLUMNS), rows=rows)


def load_reviewer_decisions(path: Path) -> dict[str, str]:
    """Read `pair_id → reviewer_decision` from an earlier R4 (CSV or JSONL).

    Blank decisions are skipped. Anything other than `confirmed`,
    `not_a_contradiction` or `context_differs` is an error naming the line,
    so a typo is never silently dropped.
    """
    rows: list[tuple[int, Mapping[str, object]]] = []
    if path.suffix.lower() == ".jsonl":
        with path.open(encoding="utf-8") as handle:
            rows = [(n, json.loads(line)) for n, line in enumerate(handle, 1) if line.strip()]
    else:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = [(n, row) for n, row in enumerate(csv.DictReader(handle), 2)]
    decisions: dict[str, str] = {}
    for number, row in rows:
        decision = str(row.get("reviewer_decision") or "").strip().lower()
        if not decision:
            continue
        if decision not in REVIEWER_DECISIONS:
            msg = f"{path}:{number}: unknown reviewer_decision {decision!r}"
            raise ValueError(msg)
        decisions[str(row["pair_id"])] = decision
    return decisions
