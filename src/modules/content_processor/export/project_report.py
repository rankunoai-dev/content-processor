"""Project report: one workbook per run, written for an analyst.

Sheet "Pages": every requested URL with its whole content, whether it was fully
extracted, the share of its captured text that was confirmed, the reading
confidence, and in plain words what is missing.

Sheet "Matching content": for every URL, the other URLs that carry the same
text and how much of it, by exact text matching only (runs of five words;
menus, headers and footers left out so pages do not all "match" through the
shared site chrome). The percentage is directional: the share of *this* URL's
text that also appears on the matching URL, so a short page can be 100% inside
a long one while the long one is only 20% inside the short one.

Sheet "How to read": what every column means.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence

from src.modules.content_processor.analysis.overlap import (
    OverlapConfig,
    OverlapDoc,
    _shared_spans,
    docs_from_records,
)
from src.modules.content_processor.contracts import ContentBlock, UrlRecord
from src.modules.content_processor.enums import EntityStatus, GapType, UrlBadge
from src.modules.content_processor.export.content import DEFAULT_LAYERS, whole_content
from src.modules.content_processor.export.tabular import Cell, Table, expand_split_column

__all__ = [
    "DEFAULT_MIN_MATCH_PCT",
    "PAGES_COLUMNS",
    "MATCH_COLUMNS",
    "match_rows",
    "page_rows",
    "project_report_tables",
]

DEFAULT_MIN_MATCH_PCT = 10.0
NO_MATCH = "(no other URL shares this text)"

PAGES_COLUMNS = [
    "URL",
    "Content",
    "Fully extracted",
    "Extracted %",
    "Confidence %",
    "Words",
    "Status",
    "What is missing",
]
MATCH_COLUMNS = ["URL", "Matching URL", "Same content %", "Example of the shared text"]

_STATUS = {
    UrlBadge.VERIFIED_WITHIN_SCOPE: "Fully captured",
    UrlBadge.PARTIAL: "Partly captured",
    UrlBadge.NEEDS_REVIEW: "Needs a check",
    UrlBadge.BLOCKED: "Site blocked us",
    UrlBadge.FAILED: "Couldn't load",
    UrlBadge.EXCLUDED: "Skipped by rule",
    UrlBadge.RUNNING: "Still in progress",
}
_FULLY = {
    UrlBadge.VERIFIED_WITHIN_SCOPE: "Yes",
    UrlBadge.EXCLUDED: "Skipped",
    UrlBadge.RUNNING: "Not yet",
}
_GAP_WORDS = {
    GapType.ACCESS_BLOCKED: "the website blocked access",
    GapType.ACQUISITION_FAILED: "the page did not load",
    GapType.UNSUPPORTED_FORMAT: "a file or image whose text could not be read",
    GapType.BUDGET_STOPPED: "the run stopped at its limit",
    GapType.DISCOVERY_OPEN: "some parts of the page were not opened",
    GapType.FIDELITY_DISAGREEMENT: "the live page and its source disagree",
    GapType.LOW_CONFIDENCE: "some text could not be confirmed",
    GapType.UNVISITED_STATE: "a tab, menu or button was not opened",
    GapType.LIMIT_HIT: "the page was cut off at a size or time limit",
    GapType.POLICY_EXCLUDED: "skipped by a rule",
    GapType.SHADOW_BLOCK_SUSPECTED: "the site may have shown us a different page",
    GapType.DEGRADED_VARIANT: "looks like a placeholder or reduced page",
    GapType.VOLATILE: "the page kept changing while it was read",
    GapType.SOFT_REDIRECT_SUSPECTED: "possibly redirected to another page",
    GapType.SOFT_404_SUSPECTED: "looks like a 'page not found'",
}

_NOTES: list[tuple[str, str]] = [
    ("Pages: URL", "The address that was requested."),
    (
        "Pages: Content",
        "All text captured from the page, in reading order. Very long pages continue in "
        "'Content_part_2', 'Content_part_3', ... (an Excel cell holds 32,767 characters); "
        "nothing is cut.",
    ),
    (
        "Pages: Fully extracted",
        "Yes only when everything the run was allowed to open on the page was captured and "
        "checked against the live page. 'Skipped' means excluded on purpose (out of scope, "
        "robots.txt, a file type not processed); 'Not yet' means still in progress.",
    ),
    (
        "Pages: Extracted %",
        "Of the words captured from the page, the share confirmed against what the live page "
        "shows. Text that was never captured (an unopened tab, an image without readable "
        "text) cannot be counted here, so a page can show 100% and still not be fully "
        "extracted: 'What is missing' says why. Empty when the page has no text (an image).",
    ),
    (
        "Pages: Confidence %",
        "How sure the engine is that the captured words are read correctly, weighted by "
        "words: text taken from the page's code is 100%, text read from images (OCR) is lower.",
    ),
    ("Pages: What is missing", "In plain words, why the page is not fully extracted."),
    ("Matching content: URL", "Every page with text gets at least one row."),
    (
        "Matching content: Matching URL",
        "Another page of the same run that carries some of the same text. A URL can match "
        f"several pages, one row each. '{NO_MATCH}' when none does.",
    ),
    (
        "Matching content: Same content %",
        "The share of this URL's text that also appears, word for word, on the matching URL "
        "(runs of five words; menus, headers and footers are left out). It is directional: "
        "a short page can be 100% inside a long one while the long one is 20% inside the "
        "short one. Exact text matching only, not meaning. Matches below "
        f"{DEFAULT_MIN_MATCH_PCT:g}% are not listed.",
    ),
    ("Matching content: Example", "Up to two passages the two pages share, quoted as captured."),
]


def _corpus(blocks: Sequence[ContentBlock]) -> list[ContentBlock]:
    return [
        b for b in blocks if b.normalized_text and b.layer in DEFAULT_LAYERS and b.dedupe_of is None
    ]


def _words(block: ContentBlock) -> int:
    return len(block.normalized_text.split())


def _missing(record: UrlRecord) -> str:
    if record.badge is UrlBadge.VERIFIED_WITHIN_SCOPE:
        return ""
    reasons = list(dict.fromkeys(_GAP_WORDS.get(g.gap_type, g.gap_type.value) for g in record.gaps))
    if not reasons and record.badge is UrlBadge.NEEDS_REVIEW:
        reasons = ["some text could not be confirmed against the live page"]
    if not reasons and record.badge is UrlBadge.RUNNING:
        reasons = ["the page is still being processed"]
    return "; ".join(reasons)


def page_rows(records: Sequence[UrlRecord]) -> list[dict[str, Cell]]:
    """Sheet "Pages": one row per requested URL."""
    rows: list[dict[str, Cell]] = []
    for record in sorted(records, key=lambda r: r.requested_url):
        corpus = _corpus(record.blocks)
        total = sum(_words(b) for b in corpus)
        confirmed = sum(_words(b) for b in corpus if b.status is EntityStatus.VERIFIED_EXTRACTED)
        weighted = sum(_words(b) * b.confidence for b in corpus)
        rows.append(
            {
                "URL": record.requested_url,
                "Content": whole_content(record.blocks),
                "Fully extracted": _FULLY.get(record.badge, "No"),
                "Extracted %": round(100.0 * confirmed / total, 1) if total else None,
                "Confidence %": round(100.0 * weighted / total, 1) if total else None,
                "Words": total,
                "Status": _STATUS.get(record.badge, record.badge.value),
                "What is missing": _missing(record),
            }
        )
    return rows


def _containment(docs: Sequence[OverlapDoc]) -> dict[tuple[int, int], int]:
    """Shared shingle counts for every pair of documents that share any (exact)."""
    posting: dict[int, list[int]] = defaultdict(list)
    for i, doc in enumerate(docs):
        for sh in doc.shingles:
            posting[sh].append(i)
    shared: Counter[tuple[int, int]] = Counter()
    for owners in posting.values():
        if len(owners) < 2:
            continue
        for x, a in enumerate(owners):
            for b in owners[x + 1 :]:
                shared[(a, b)] += 1
    return dict(shared)


def match_rows(
    records: Sequence[UrlRecord], *, min_match_pct: float = DEFAULT_MIN_MATCH_PCT
) -> list[dict[str, Cell]]:
    """Sheet "Matching content": one row per (URL, matching URL), both directions."""
    docs, _ = docs_from_records(records, OverlapConfig(include_boilerplate=False))
    matches: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for (a, b), count in _containment(docs).items():
        for this, other in ((a, b), (b, a)):
            pct = 100.0 * count / len(docs[this].shingles)
            if pct >= min_match_pct:
                matches[this].append((pct, other))
    rows: list[dict[str, Cell]] = []
    order = sorted(range(len(docs)), key=lambda i: docs[i].url)
    for i in order:
        found = sorted(matches.get(i, []), key=lambda m: (-m[0], docs[m[1]].url))
        if not found:
            rows.append(
                {
                    "URL": docs[i].url,
                    "Matching URL": NO_MATCH,
                    "Same content %": None,
                    "Example of the shared text": "",
                }
            )
            continue
        for pct, j in found:
            example = " … ".join(_shared_spans(docs[i], docs[j], limit=2))
            rows.append(
                {
                    "URL": docs[i].url,
                    "Matching URL": docs[j].url,
                    "Same content %": round(pct, 1),
                    "Example of the shared text": example,
                }
            )
    return rows


def project_report_tables(
    records: Sequence[UrlRecord], *, min_match_pct: float = DEFAULT_MIN_MATCH_PCT
) -> list[Table]:
    """The three sheets of the project report, ready for `write_xlsx`."""
    pages = page_rows(records)
    page_columns = expand_split_column(pages, PAGES_COLUMNS, "Content")
    notes: list[dict[str, Cell]] = [{"Column": n, "Meaning": m} for n, m in _NOTES]
    return [
        Table(name="Pages", columns=page_columns, rows=pages),
        Table(
            name="Matching content",
            columns=MATCH_COLUMNS,
            rows=match_rows(records, min_match_pct=min_match_pct),
        ),
        Table(name="How to read", columns=["Column", "Meaning"], rows=notes),
    ]
