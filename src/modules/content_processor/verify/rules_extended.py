"""Verification rules R6, R7, R8, R11, R12, R13, volatility and evidence integrity.

Same contract as `verify.rules`: pure functions over ledger facts, typed gaps
with stable ids, no status changes (the record applies gaps to the badge).
Definitions follow ARCHITECTURE.md §10:

| Rule | Check | Gap |
|---|---|---|
| R6 | screenshot text regions map to an extraction | LOW_CONFIDENCE |
| R7 | source and render agree; no obfuscated fonts (case 114) | FIDELITY_DISAGREEMENT |
| R8 | PDF image regions got OCR; native and OCR agree | LOW_CONFIDENCE, FIDELITY_DISAGREEMENT |
| R11 | main-region words vs the host baseline | SHADOW_BLOCK_SUSPECTED |
| R12 | a page exploration or scroll budget ran out | LIMIT_HIT |
| R13 | no icon-artifact or third-party-chrome text in the corpus | FIDELITY_DISAGREEMENT |
| V-6 | the page settled inside the window | VOLATILE |
| evidence | cited evidence exists; payload hash matches | LOW_CONFIDENCE, FIDELITY_DISAGREEMENT |

R6 and R8 need an OCR engine. When none is installed the record says so in its
warnings and these rules emit nothing (operator decision 2026-10-19); numbers
and negations are R9's job (`rule_r9_critical_tokens`) and are not re-checked here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.modules.content_processor.adapters.html_source import match_key
from src.modules.content_processor.contracts import ContentBlock, Gap, Resource
from src.modules.content_processor.enums import ContentLayer, EntityStatus, GapType
from src.modules.content_processor.export.content import drop_duplicates
from src.modules.content_processor.verify.detectors import (
    DetectedRegion,
    EvidenceProblem,
    PdfRegionRef,
)
from src.modules.content_processor.verify.gaps import gap_id_for

__all__ = [
    "CORPUS_LAYERS",
    "main_word_count",
    "rule_evidence_integrity",
    "rule_r6_visual_regions",
    "rule_r7_channel_agreement",
    "rule_r8_pdf_regions",
    "rule_r11_degradation",
    "rule_r12_limits",
    "rule_r13_layer_hygiene",
    "rule_volatility",
    "unmapped_visual_lines",
    "uncovered_pdf_regions",
]

CORPUS_LAYERS = frozenset({ContentLayer.RENDERED_VISIBLE, ContentLayer.COLLAPSED_PUBLISHED})
_TOKEN = re.compile(r"\w+", re.UNICODE)
_PUA = re.compile(r"[-]")
_ICON_LIGATURE = re.compile(r"^[a-z]+(?:_[a-z]+)+$")
_MAIN_ROLES = frozenset({"main", "unknown"})
_SAMPLES = 3
R6_MIN_REGION_CHARS = 8
R6_MIN_WORDS = 3
R6_MIN_UNMAPPED = 2
R6_MIN_LINE_CONFIDENCE = 0.8
R6_PREFIX = 4
_CONSENT_TEXT = re.compile(
    r"cooki|consent|necessar|privacy|preferenc|opt[- ]?out|tracking|advertis", re.IGNORECASE
)
# Chat-widget invitations ("Live chat", "Chat with us") are interface text, not page content.
# Specific phrases only: a bare "chat" would also hide real articles about chat tools.
_CHAT_TEXT = re.compile(
    r"live chat|chat (with|now|online)\b|start (a )?chat|chat with (an? )?"
    r"(agent|representative|advisor|specialist)\b",
    re.IGNORECASE,
)
_A11Y_HINT = re.compile(
    r"^\(?(opens|open|links?) (in|to) (a )?new (tab|window)\)?\.?$", re.IGNORECASE
)
R6_TOKEN_COVERAGE = 0.6
R7_MIN_MISSING_WORDS = 20
R7_MISSING_SHARE = 0.25
R8_MIN_REGION_PIXELS = 200 * 200
R8_AGREEMENT = 0.5
R11_MIN_HOST_PAGES = 10
R11_MIN_HOST_MEDIAN = 100
R11_MAX_PAGE_WORDS = 30
R11_RATIO = 0.2


def _gap(
    run_id: str,
    resource: Resource,
    key: str,
    gap_type: GapType,
    status: EntityStatus,
    reason: str,
    *,
    retry: bool = False,
) -> Gap:
    return Gap(
        gap_id=gap_id_for(run_id, resource.resource_id, key),
        run_id=run_id,
        resource_id=resource.resource_id,
        gap_type=gap_type,
        status=status,
        reason=reason[:500],
        evidence_ids=[f"evidence:{resource.payload_sha256}"] if resource.payload_sha256 else [],
        retry_eligible=retry,
        tier_reached=resource.acquired_at_tier,
    )


def _norm(text: str) -> str:
    # Same comparison form as reconcile: struck-text markers and super/subscripts folded.
    return match_key(text)


def _squash(text: str) -> str:
    return "".join(text.split())


R7_SHINGLE = 5
R7_SHINGLE_COVERAGE = 0.9
_WORD = re.compile(r"\w+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text)


def _shingles(words: Sequence[str]) -> set[tuple[str, ...]]:
    n = R7_SHINGLE
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _source_text_rendered(text: str, haystack: str, rendered: set[tuple[str, ...]]) -> bool:
    """True when the rendered channel shows this source text.

    Exact (whitespace-free) containment first. Otherwise the words must still be
    there in order with only small insertions: sites add text at render time,
    such as an "(opens in a new tab)" hint after every external link, which the
    source HTML never had. A real loss removes most 5-word runs; an insertion
    only breaks the few that span it.
    """
    if _squash(text) in haystack:
        return True
    shingles = _shingles(_words(text))
    if not shingles:
        return False
    found = sum(1 for sh in shingles if sh in rendered)
    return found / len(shingles) >= R7_SHINGLE_COVERAGE


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(_norm(text))


def _corpus(blocks: Iterable[ContentBlock]) -> list[ContentBlock]:
    content = [b for b in blocks if b.layer in CORPUS_LAYERS]
    rendered = [b for b in content if not b.state_id.endswith("|source")]
    return rendered or content


def main_word_count(blocks: Sequence[ContentBlock]) -> int:
    """Words in the main-role corpus (the R11 measure)."""
    return sum(len(b.normalized_text.split()) for b in _corpus(blocks) if b.role in _MAIN_ROLES)


def _samples(texts: Sequence[str]) -> str:
    return " | ".join(t[:40] for t in texts[:_SAMPLES])


# -- R6 ----------------------------------------------------------------------------


def _seen(word: str, vocabulary: set[str], by_prefix: Mapping[str, list[str]]) -> bool:
    """Exact match, or an OCR-truncated form of a page word sharing its first letters.

    OCR on small screenshot text often clips words ("annuiti", "Contac",
    "Hanca"); a word counts as seen when it shares at least `R6_PREFIX`
    leading letters with a longer page word.
    """
    if word in vocabulary:
        return True
    if len(word) < R6_PREFIX:
        return False
    for token in by_prefix.get(word[:R6_PREFIX], ()):
        common = 0
        for a, b in zip(word, token, strict=False):
            if a != b:
                break
            common += 1
        if common >= R6_PREFIX and len(word) <= len(token):
            return True
    return False


def _is_interface_text(text: str) -> bool:
    """Consent banners and chat-widget invitations: left out of content by design."""
    return bool(_CONSENT_TEXT.search(text) or _CHAT_TEXT.search(text))


def unmapped_visual_lines(
    regions: Sequence[DetectedRegion], blocks: Sequence[ContentBlock]
) -> list[str]:
    """Screenshot text lines that match no block of the page (the R6 measure).

    A line maps when most of its words occur in any block of the page (any
    layer: a third-party or hidden block is an explicit reason), allowing for
    OCR-clipped words. Not judged: lines read below `R6_MIN_LINE_CONFIDENCE`,
    lines with fewer than `R6_MIN_WORDS` real words, and consent-banner text
    (the consent widget is left out of the content by design; the base
    screenshot is taken before the explorer answers it).
    """
    vocabulary = {t for b in blocks for t in _tokens(b.normalized_text)}
    by_prefix: dict[str, list[str]] = {}
    for token in vocabulary:
        if len(token) >= R6_PREFIX:
            by_prefix.setdefault(token[:R6_PREFIX], []).append(token)
    unmapped: list[str] = []
    for region in regions:
        if region.confidence < R6_MIN_LINE_CONFIDENCE or _is_interface_text(region.text):
            continue
        words = [w for w in _tokens(region.text) if len(w) >= 3 and not w.isdigit()]
        if len(words) < R6_MIN_WORDS or len("".join(words)) < R6_MIN_REGION_CHARS:
            continue
        covered = sum(1 for w in words if _seen(w, vocabulary, by_prefix)) / len(words)
        if covered < R6_TOKEN_COVERAGE:
            unmapped.append(region.text.strip())
    return unmapped


def rule_r6_visual_regions(
    run_id: str,
    target: Resource,
    *,
    regions: Sequence[DetectedRegion] | None,
    blocks: Sequence[ContentBlock],
) -> list[Gap]:
    """R6: text the screenshot shows must map to an extraction or an explicit reason.

    A review gap needs at least `R6_MIN_UNMAPPED` unmapped lines: missed
    content spans lines, while a single stray line is usually OCR noise or
    overlay text, which the record reports as a warning instead.
    `regions=None` means the detector did not run (the record warns).
    """
    if regions is None:
        return []
    unmapped = unmapped_visual_lines(regions, blocks)
    if len(unmapped) < R6_MIN_UNMAPPED:
        return []
    return [
        _gap(
            run_id,
            target,
            "R6:unmapped",
            GapType.LOW_CONFIDENCE,
            EntityStatus.EXTRACTED_UNVERIFIED,
            f"visual_text_unextracted:regions={len(unmapped)}:{_samples(unmapped)}",
        )
    ]


# -- R7 ----------------------------------------------------------------------------


def rule_r7_channel_agreement(
    run_id: str, target: Resource, *, blocks: Sequence[ContentBlock]
) -> list[Gap]:
    """R7: the rendered corpus agrees with the source channel and is real text.

    Two checks: (1) obfuscated-font output, text made of private-use glyph
    codes that only a custom font turns into words (case 114); (2) a large
    share of the source channel's main text missing from the render (a moved
    block is not missing: the comparison is on text, not position).
    """
    gaps: list[Gap] = []
    corpus = _corpus(blocks)
    text = "".join(b.normalized_text for b in corpus)
    pua = len(_PUA.findall(text))
    letters = sum(1 for ch in text if ch.isalpha()) or 1
    if pua >= 3 and pua / letters >= 0.01:
        gaps.append(
            _gap(
                run_id,
                target,
                "R7:obfuscated_font",
                GapType.FIDELITY_DISAGREEMENT,
                EntityStatus.EXTRACTED_UNVERIFIED,
                f"obfuscated_font_suspected:pua_chars={pua}",
            )
        )
    rendered = [b for b in corpus if not b.state_id.endswith("|source")]
    if rendered:
        # Compared without whitespace: rendered blocks split at CSS line breaks
        # rejoin as "Hancock (opens in a new tab) , a unit", the same words.
        rendered_text = " ".join(_norm(b.normalized_text) for b in rendered)
        haystack = _squash(rendered_text)
        rendered_shingles = _shingles(_words(rendered_text))
        source_main = drop_duplicates(
            [
                b
                for b in blocks
                if b.state_id.endswith("|source")
                and b.layer is ContentLayer.RENDERED_VISIBLE
                and b.role in _MAIN_ROLES
            ]
        )
        total = sum(len(b.normalized_text.split()) for b in source_main)
        missing = [
            b
            for b in source_main
            if not _source_text_rendered(_norm(b.normalized_text), haystack, rendered_shingles)
        ]
        missing_words = sum(len(b.normalized_text.split()) for b in missing)
        if (
            missing_words >= R7_MIN_MISSING_WORDS
            and total
            and missing_words / total >= R7_MISSING_SHARE
        ):
            gaps.append(
                _gap(
                    run_id,
                    target,
                    "R7:source_not_rendered",
                    GapType.FIDELITY_DISAGREEMENT,
                    EntityStatus.EXTRACTED_UNVERIFIED,
                    f"source_text_not_rendered:words={missing_words}/{total}:"
                    f"{_samples([b.normalized_text for b in missing])}",
                )
            )
    return gaps


# -- R8 ----------------------------------------------------------------------------


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _ocr_blocks_for(region: PdfRegionRef, blocks: Sequence[ContentBlock]) -> list[ContentBlock]:
    return [
        b
        for b in blocks
        if "ocr" in b.method.lower()
        and b.page_index == region.page_index
        and (b.bbox is None or _overlaps(b.bbox, region.bbox))
    ]


def uncovered_pdf_regions(
    regions: Sequence[PdfRegionRef], blocks: Sequence[ContentBlock]
) -> list[PdfRegionRef]:
    """Image regions large enough to hold text that no OCR block covers."""
    return [
        r
        for r in regions
        if (r.is_full_page or r.width * r.height >= R8_MIN_REGION_PIXELS)
        and not _ocr_blocks_for(r, blocks)
    ]


def rule_r8_pdf_regions(
    run_id: str,
    target: Resource,
    *,
    regions: Sequence[PdfRegionRef] | None,
    blocks: Sequence[ContentBlock],
    ocr_available: bool,
) -> list[Gap]:
    """R8: image regions on PDF pages got OCR, and native and OCR text agree.

    An uncovered region is a gap only when OCR could have run; without an
    engine the record warns instead. Where native text and OCR text cover the
    same area, low word agreement (a stale or wrong OCR layer, case 49) is a
    disagreement.
    """
    if regions is None:
        return []
    gaps: list[Gap] = []
    uncovered = uncovered_pdf_regions(regions, blocks)
    if uncovered and ocr_available:
        pages = sorted({r.page_index + 1 for r in uncovered})
        gaps.append(
            _gap(
                run_id,
                target,
                "R8:not_ocred",
                GapType.LOW_CONFIDENCE,
                EntityStatus.EXTRACTED_UNVERIFIED,
                f"pdf_image_regions_not_ocred:regions={len(uncovered)}:pages={pages[:10]}",
                retry=True,
            )
        )
    disagree: list[int] = []
    for region in regions:
        ocr = _ocr_blocks_for(region, blocks)
        native = [
            b
            for b in blocks
            if b.method == "pymupdf/native"
            and b.page_index == region.page_index
            and b.bbox is not None
            and _overlaps(b.bbox, region.bbox)
        ]
        if not ocr or not native:
            continue
        ocr_words = set(_tokens(" ".join(b.normalized_text for b in ocr)))
        native_words = set(_tokens(" ".join(b.normalized_text for b in native)))
        union = ocr_words | native_words
        if union and len(ocr_words & native_words) / len(union) < R8_AGREEMENT:
            disagree.append(region.page_index + 1)
    if disagree:
        gaps.append(
            _gap(
                run_id,
                target,
                "R8:native_vs_ocr",
                GapType.FIDELITY_DISAGREEMENT,
                EntityStatus.EXTRACTED_UNVERIFIED,
                f"pdf_native_ocr_disagree:pages={sorted(set(disagree))[:10]}",
            )
        )
    return gaps


# -- R11 ---------------------------------------------------------------------------


def rule_r11_degradation(
    run_id: str,
    target: Resource,
    *,
    main_words: int,
    host_median: float,
    host_pages: int,
) -> list[Gap]:
    """R11: a page far thinner than its host's baseline may be a degraded variant.

    Judged only against a meaningful baseline (enough pages, a substantive
    median), and only for pages that claim content. Canary comparison (A-9)
    is not built yet.
    """
    if (
        target.status not in (EntityStatus.VERIFIED_EXTRACTED, EntityStatus.EXTRACTED_UNVERIFIED)
        or host_pages < R11_MIN_HOST_PAGES
        or host_median < R11_MIN_HOST_MEDIAN
        or main_words >= R11_MAX_PAGE_WORDS
        or main_words >= R11_RATIO * host_median
    ):
        return []
    return [
        _gap(
            run_id,
            target,
            "R11:thin",
            GapType.SHADOW_BLOCK_SUSPECTED,
            EntityStatus.EXTRACTED_UNVERIFIED,
            f"main_words={main_words}:host_median={round(host_median)}:host_pages={host_pages}",
            retry=True,
        )
    ]


# -- R12 ---------------------------------------------------------------------------


def _stop_reasons(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "stop_reason" and isinstance(item, str):
                found.append(item)
            else:
                found.extend(_stop_reasons(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_stop_reasons(item))
    return found


def rule_r12_limits(
    run_id: str, target: Resource, *, summaries: Mapping[str, Mapping[str, Any]]
) -> list[Gap]:
    """R12: a page whose exploration or scrolling ran out of budget says so.

    Run-level budgets (K9) and truncation (R10) have their own gaps; this
    covers per-page budgets recorded in the render and explore summaries.
    """
    hits = sorted({s for s in _stop_reasons(dict(summaries)) if s.endswith("_budget_exhausted")})
    if not hits:
        return []
    return [
        _gap(
            run_id,
            target,
            "R12:page_budget",
            GapType.LIMIT_HIT,
            EntityStatus.PARTIAL,
            "page_budget:" + ",".join(hits),
            retry=True,
        )
    ]


# -- R13 ---------------------------------------------------------------------------


def rule_r13_layer_hygiene(
    run_id: str, target: Resource, *, blocks: Sequence[ContentBlock]
) -> list[Gap]:
    """R13: no icon-artifact or third-party-chrome text in the main corpus.

    Icon artifacts: a corpus block that is only a ligature name
    (`arrow_forward`) or private-use glyphs. Chrome leakage: a main-role
    corpus block identical to a third-party-chrome block of the same page.
    """
    corpus = _corpus(blocks)
    chrome = {
        _norm(b.normalized_text)
        for b in blocks
        if b.layer is ContentLayer.THIRD_PARTY_CHROME and b.normalized_text.strip()
    }
    icons: list[str] = []
    leaked: list[str] = []
    for block in corpus:
        text = block.normalized_text.strip()
        if _ICON_LIGATURE.match(text) or (text and not _PUA.sub("", text).strip()):
            icons.append(block.block_id)
        elif block.role == "main" and _norm(text) in chrome and not _A11Y_HINT.match(text):
            leaked.append(block.block_id)
    gaps: list[Gap] = []
    for kind, ids in (("icon_artifact", icons), ("third_party_chrome", leaked)):
        if ids:
            gaps.append(
                _gap(
                    run_id,
                    target,
                    f"R13:{kind}",
                    GapType.FIDELITY_DISAGREEMENT,
                    EntityStatus.EXTRACTED_UNVERIFIED,
                    f"layer_leak:{kind}:blocks={len(ids)}:{','.join(ids[:_SAMPLES])}",
                )
            )
    return gaps


# -- V-6 and evidence --------------------------------------------------------------


def rule_volatility(
    run_id: str, target: Resource, *, summaries: Mapping[str, Mapping[str, Any]]
) -> list[Gap]:
    """V-6: a page whose text and node counts never stopped changing (cases 99, 123)."""
    settle = summaries.get("render", {}).get("settle")
    if not isinstance(settle, Mapping) or settle.get("stable") is not False:
        return []
    return [
        _gap(
            run_id,
            target,
            "V6:volatile",
            GapType.VOLATILE,
            EntityStatus.EXTRACTED_UNVERIFIED,
            f"settle_window_exhausted:polls={settle.get('polls', '?')}",
            retry=True,
        )
    ]


def rule_evidence_integrity(
    run_id: str, target: Resource, *, problems: Sequence[EvidenceProblem]
) -> list[Gap]:
    """Cited evidence must exist and the payload must still match its hash."""
    gaps: list[Gap] = []
    missing = [p.sha256 for p in problems if p.kind == "missing"]
    changed = [p.sha256 for p in problems if p.kind == "hash_mismatch"]
    if changed:
        gaps.append(
            _gap(
                run_id,
                target,
                "EV:hash_mismatch",
                GapType.FIDELITY_DISAGREEMENT,
                EntityStatus.EXTRACTED_UNVERIFIED,
                f"evidence_hash_mismatch:{','.join(s[:16] for s in changed)}",
            )
        )
    if missing:
        gaps.append(
            _gap(
                run_id,
                target,
                "EV:missing",
                GapType.LOW_CONFIDENCE,
                EntityStatus.EXTRACTED_UNVERIFIED,
                f"evidence_missing:count={len(missing)}:{','.join(s[:16] for s in missing[:3])}",
            )
        )
    return gaps
