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
from src.modules.content_processor.export.content import (
    DEFAULT_LAYERS,
    drop_duplicates,
    whole_content,
)
from src.modules.content_processor.export.tabular import Cell, Table, expand_split_column

__all__ = [
    "DEFAULT_MIN_MATCH_PCT",
    "PAGES_COLUMNS",
    "MATCH_COLUMNS",
    "completeness",
    "match_rows",
    "page_confidence",
    "page_rows",
    "project_report_tables",
]

DEFAULT_MIN_MATCH_PCT = 10.0
NO_MATCH = "(no other URL shares this text)"

PAGES_COLUMNS = [
    "URL",
    "Content",
    "Fully extracted",
    "Extraction completeness",
    "Confidence",
    "Confirmed text %",
    "Reading confidence %",
    "Words",
    "Status",
    "What is missing",
]
HIGH, MEDIUM, LOW, NOT_RATED = "High", "Medium", "Low", "Not rated"
_PLACEHOLDER_WORDS = ("lorem ipsum",)
# Plain names of the controls the explorer can leave unopened.
_CONTROL_WORDS = {
    "tab": ("tab", "tabs"),
    "accordion": ("section", "sections"),
    "disclosure": ("section", "sections"),
    "find_reveal": ("hidden section", "hidden sections"),
    "dialog": ("pop-up", "pop-ups"),
    "hover": ("hover menu", "hover menus"),
    "menu": ("menu", "menus"),
    "carousel": ("slider", "sliders"),
    "load_more": ("'load more' button", "'load more' buttons"),
    "pagination": ("page of results", "pages of results"),
    "scroll_surface": ("scroll area", "scroll areas"),
    "frame": ("embedded frame", "embedded frames"),
    "keyboard": ("focus-only text check", "focus-only text checks"),
    "consent": ("consent banner", "consent banners"),
    "shadow_root": ("web component", "web components"),
}
A_IN_B = "URL's text found on Matching URL %"
B_IN_A = "Matching URL's text found on URL %"
MATCH_COLUMNS = ["URL", "Matching URL", A_IN_B, B_IN_A, "Example of the shared text"]

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
    GapType.VISIBLE_TEXT_NOT_EXTRACTED: "text shown on the page was not captured",
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
        "Pages: Extraction completeness",
        "In plain words, how complete the extraction is: 'Complete'; 'Incomplete: 1 tab not "
        "opened (Pricing)'; 'Complete, but the page kept changing' (everything was captured "
        "and confirmed, but the page changed while it was read); 'Placeholder page (Lorem "
        "ipsum)'; 'Skipped by rule'; 'Not captured: ...'. It always agrees with Status.",
    ),
    (
        "Pages: Confidence",
        "How far to trust the page's extraction as a whole. High: fully captured, every word "
        "confirmed (at least 95%) and read reliably (at least 95%). Medium: everything captured "
        "but something needs a check (the page kept changing, some text not confirmed, or "
        "reading confidence 80-95%). Low: incomplete (a tab, section or pop-up not opened, text "
        "in an image not read), a placeholder page, not captured, or reading or confirmed "
        "text below 80%. 'Not rated' for skipped pages.",
    ),
    (
        "Pages: Confirmed text %",
        "Of the words that WERE captured, the share confirmed against what the live page "
        "shows. It cannot count text that was never captured (an unopened tab), so read it "
        "together with 'Extraction completeness'. Empty when the page has no text (an image).",
    ),
    (
        "Pages: Reading confidence %",
        "How reliably the captured words were read, weighted by words: text taken from the "
        "page's code is 100%, text read from images (OCR) is lower.",
    ),
    ("Pages: What is missing", "In plain words, why the page is not fully extracted."),
    (
        "Matching content: URL, Matching URL",
        "Each pair of pages that share text is listed ONCE (A with B is not repeated as B "
        "with A), under the address that sorts first. To find every match of one page, look "
        f"for it in both columns. '{NO_MATCH}' when a page shares no text with any other.",
    ),
    (
        "Matching content: URL's text found on Matching URL %",
        "The share of the first page's text that also appears, word for word, on the second "
        "page (runs of five words; menus, headers and footers are left out). Exact text "
        "matching only, not meaning.",
    ),
    (
        "Matching content: Matching URL's text found on URL %",
        "The same in the other direction. The two differ when the pages differ in length: a "
        "short page can be 100% inside a long one while the long one is 20% inside the short "
        f"one. A pair is listed when either direction reaches {DEFAULT_MIN_MATCH_PCT:g}%.",
    ),
    ("Matching content: Example", "Up to two passages the two pages share, quoted as captured."),
]


def _corpus(blocks: Sequence[ContentBlock]) -> list[ContentBlock]:
    return drop_duplicates([b for b in blocks if b.normalized_text and b.layer in DEFAULT_LAYERS])


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


def _unopened(record: UrlRecord) -> list[str]:
    """'1 tab not opened (Pricing)'-style phrases from the unvisited-control gaps."""
    by_kind: dict[str, list[str]] = defaultdict(list)
    for g in record.gaps:
        if g.gap_type is not GapType.UNVISITED_STATE or not g.reason.startswith("unvisited_"):
            continue
        kind, _, rest = g.reason[len("unvisited_") :].partition(":")
        label = rest.rpartition(":")[2].strip()
        by_kind[kind].append(label)
    phrases = []
    for kind, labels in by_kind.items():
        one, many = _CONTROL_WORDS.get(kind, ("part of the page", "parts of the page"))
        names = [x for x in dict.fromkeys(labels) if x]
        shown = f" ({', '.join(names[:3])}{', ...' if len(names) > 3 else ''})" if names else ""
        phrases.append(f"{len(labels)} {one if len(labels) == 1 else many} not opened{shown}")
    return phrases


def _is_placeholder(record: UrlRecord, text: str) -> bool:
    placeholder_gap = any(
        g.gap_type is GapType.DEGRADED_VARIANT and "placeholder" in g.reason for g in record.gaps
    )
    return placeholder_gap or any(w in text.lower() for w in _PLACEHOLDER_WORDS)


def completeness(record: UrlRecord, text: str, confirmed_pct: float | None) -> str:
    """One plain statement of how complete the extraction is; never contradicts the status."""
    badge = record.badge
    if badge is UrlBadge.EXCLUDED:
        return "Skipped by rule"
    if badge is UrlBadge.RUNNING:
        return "Not processed yet"
    if badge is UrlBadge.BLOCKED:
        return "Not captured: the website blocked access"
    if badge is UrlBadge.FAILED:
        return "Not captured: the page did not load"
    types = {g.gap_type for g in record.gaps}
    if _is_placeholder(record, text) and badge is not UrlBadge.VERIFIED_WITHIN_SCOPE:
        placeholder = "lorem ipsum" in text.lower()
        return "Placeholder page (Lorem ipsum)" if placeholder else "Placeholder or reduced page"
    gaps: list[str] = _unopened(record)
    if GapType.VISIBLE_TEXT_NOT_EXTRACTED in types:
        gaps.append("text shown on the page was not captured")
    if GapType.UNSUPPORTED_FORMAT in types:
        gaps.append("text in a file or image could not be read")
    if GapType.LIMIT_HIT in types:
        gaps.append("cut off at a size or time limit")
    if types & {GapType.ACCESS_BLOCKED, GapType.ACQUISITION_FAILED}:
        gaps.append("an embedded part could not be loaded")
    if GapType.BUDGET_STOPPED in types:
        gaps.append("the run stopped at its limit")
    if not gaps and GapType.DISCOVERY_OPEN in types:
        gaps.append("some parts of the page were not opened")
    if gaps:
        statement = "Incomplete: " + "; ".join(gaps)
        if GapType.VOLATILE in types:
            statement += "; the page also kept changing"
        return statement
    if badge is UrlBadge.VERIFIED_WITHIN_SCOPE:
        return "Complete"
    if GapType.VOLATILE in types:
        return "Complete, but the page kept changing"
    if confirmed_pct is not None and confirmed_pct < 100.0:
        return "Complete, but some text could not be confirmed"
    if GapType.FIDELITY_DISAGREEMENT in types:
        return "Complete, but the live page and its source differ"
    if types & {GapType.SOFT_404_SUSPECTED, GapType.SOFT_REDIRECT_SUSPECTED}:
        return "Captured, but the page may not be the one requested"
    if GapType.SHADOW_BLOCK_SUSPECTED in types:
        return "Captured, but the site may have shown a different page"
    return "Complete, but needs a check"


def page_confidence(
    record: UrlRecord,
    completeness_text: str,
    confirmed_pct: float | None,
    reading_pct: float | None,
) -> str:
    """High / Medium / Low for the whole page; it can never be High when the status is not."""
    if record.badge in (UrlBadge.EXCLUDED, UrlBadge.RUNNING):
        return NOT_RATED
    if (
        record.badge in (UrlBadge.PARTIAL, UrlBadge.BLOCKED, UrlBadge.FAILED)
        or completeness_text.startswith(("Incomplete", "Placeholder", "Not captured"))
        or reading_pct is None
        or reading_pct < 80.0
        or (confirmed_pct is not None and confirmed_pct < 80.0)
    ):
        return LOW
    if (
        record.badge is UrlBadge.VERIFIED_WITHIN_SCOPE
        and reading_pct >= 95.0
        and (confirmed_pct is None or confirmed_pct >= 95.0)
    ):
        return HIGH
    return MEDIUM


def page_rows(records: Sequence[UrlRecord]) -> list[dict[str, Cell]]:
    """Sheet "Pages": one row per requested URL."""
    rows: list[dict[str, Cell]] = []
    for record in sorted(records, key=lambda r: r.requested_url):
        corpus = _corpus(record.blocks)
        total = sum(_words(b) for b in corpus)
        confirmed = sum(_words(b) for b in corpus if b.status is EntityStatus.VERIFIED_EXTRACTED)
        weighted = sum(_words(b) * b.confidence for b in corpus)
        text = whole_content(record.blocks)
        confirmed_pct = round(100.0 * confirmed / total, 1) if total else None
        reading_pct = round(100.0 * weighted / total, 1) if total else None
        complete = completeness(record, text, confirmed_pct)
        rows.append(
            {
                "URL": record.requested_url,
                "Content": text,
                "Fully extracted": _FULLY.get(record.badge, "No"),
                "Extraction completeness": complete,
                "Confidence": page_confidence(record, complete, confirmed_pct, reading_pct),
                "Confirmed text %": confirmed_pct,
                "Reading confidence %": reading_pct,
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
    """Sheet "Matching content": each pair of URLs once, with both directions.

    A pair is listed when either direction reaches `min_match_pct`. It sits under
    the URL that sorts first, so A-B is never repeated as B-A; the two percentages
    say how much of each page's text the other one carries. A URL in no pair gets
    one row saying so.
    """
    docs, _ = docs_from_records(records, OverlapConfig(include_boilerplate=False))
    pairs: dict[int, list[tuple[float, float, int]]] = defaultdict(list)
    in_a_pair: set[int] = set()
    for (a, b), count in _containment(docs).items():
        first, second = (a, b) if docs[a].url <= docs[b].url else (b, a)
        first_in_second = 100.0 * count / len(docs[first].shingles)
        second_in_first = 100.0 * count / len(docs[second].shingles)
        if max(first_in_second, second_in_first) < min_match_pct:
            continue
        pairs[first].append((first_in_second, second_in_first, second))
        in_a_pair.update((first, second))
    rows: list[dict[str, Cell]] = []
    for i in sorted(range(len(docs)), key=lambda i: docs[i].url):
        found = sorted(pairs.get(i, []), key=lambda m: (-max(m[0], m[1]), docs[m[2]].url))
        if not found and i not in in_a_pair:
            rows.append(
                {
                    "URL": docs[i].url,
                    "Matching URL": NO_MATCH,
                    A_IN_B: None,
                    B_IN_A: None,
                    "Example of the shared text": "",
                }
            )
            continue
        for first_in_second, second_in_first, j in found:
            rows.append(
                {
                    "URL": docs[i].url,
                    "Matching URL": docs[j].url,
                    A_IN_B: round(first_in_second, 1),
                    B_IN_A: round(second_in_first, 1),
                    "Example of the shared text": " … ".join(
                        _shared_spans(docs[i], docs[j], limit=2)
                    ),
                }
            )
    return rows


def project_report_tables(
    records: Sequence[UrlRecord], *, min_match_pct: float = DEFAULT_MIN_MATCH_PCT
) -> list[Table]:
    """The three sheets of the project report, ready for `write_xlsx`."""
    pages = page_rows(records)
    page_columns = expand_split_column(pages, PAGES_COLUMNS, "Content")
    content_columns = [c for c in page_columns if c == "Content" or c.startswith("Content_part_")]
    notes: list[dict[str, Cell]] = [{"Column": n, "Meaning": m} for n, m in _NOTES]
    return [
        Table(
            name="Pages",
            columns=page_columns,
            rows=pages,
            wrap_columns=frozenset(content_columns),
            widths={
                "URL": 60,
                **dict.fromkeys(content_columns, 100),
                "Extraction completeness": 45,
                "What is missing": 45,
            },
        ),
        Table(
            name="Matching content",
            columns=MATCH_COLUMNS,
            rows=match_rows(records, min_match_pct=min_match_pct),
        ),
        Table(name="How to read", columns=["Column", "Meaning"], rows=notes),
    ]
