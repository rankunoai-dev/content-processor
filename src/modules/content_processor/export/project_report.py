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
from datetime import UTC, datetime

from src.modules.content_processor.analysis.overlap import (
    OverlapConfig,
    OverlapDoc,
    _shared_spans,
    docs_from_records,
)
from src.modules.content_processor.contracts import ContentBlock, UrlRecord
from src.modules.content_processor.enums import ChangeStatus, EntityStatus, GapType, UrlBadge
from src.modules.content_processor.export.content import (
    DEFAULT_LAYERS,
    drop_duplicates,
    whole_content,
)
from src.modules.content_processor.export.tabular import Cell, Table, expand_split_column
from src.modules.content_processor.recrawl.baseline import extraction_time

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
    "Last scraped",
    "Site last modified",
    "Scrape action",
    "What is missing",
]
HIGH, MEDIUM, LOW, NOT_RATED = "High", "Medium", "Low", "Not rated"
_CHANGE_REASONS = {
    "http_304_not_modified": "server said not modified",
    "payload_identical": "same file",
    "visible_text_identical": "same visible text",
    "site_modified_date_newer": "the site's date is newer",
    "visible_text_changed": "visible text changed",
    "payload_changed": "file changed",
}
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
A_IN_B = "% of A in B"
B_IN_A = "% of B in A"
MATCH_COLUMNS = [
    "Pair",
    "URL (A)",
    "Matching URL (B)",
    A_IN_B,
    B_IN_A,
    "Shared words",
    "Shared passages",
    "Shared text",
]
LINE_COLUMNS = ["Pair", "URL (A)", "Matching URL (B)", "Passage", "Line", "Text"]
PASSAGE_BREAK = "\n\n[…]\n\n"
"""Between two shared passages that are not next to each other on page A."""

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
    (
        "Pages: Last scraped",
        "When the content in this row was extracted (UTC). For a page reused from an "
        "earlier run because it had not changed, this is the earlier extraction.",
    ),
    (
        "Pages: Site last modified",
        "When the website says the page or file was last changed (structured data, page "
        "tags or the server's Last-Modified header); empty when it does not say.",
    ),
    (
        "Pages: Scrape action",
        "What this run did compared with all earlier runs: 'New page' (never captured "
        "before), 'Reused (unchanged: ...)' (the server said not modified, or the file or "
        "its visible text is identical, so the earlier extraction was kept), 'Re-scraped "
        "(updated: ...)' (the page changed and was extracted again; the visible text is "
        "compared, so an edit is caught even when the site's date was not updated), or "
        "'Re-scraped (unchanged, but the earlier capture was too old to reuse)'.",
    ),
    ("Pages: What is missing", "In plain words, why the page is not fully extracted."),
    (
        "Matching content: URL (A), Matching URL (B)",
        "Each pair of pages that share text is listed ONCE (A with B is not repeated as B "
        "with A), under the address that sorts first. To find every match of one page, look "
        f"for it in both columns. '{NO_MATCH}' when a page shares no text with any other.",
    ),
    (
        "Matching content: % of A in B",
        "The share of page A's text that also appears, word for word, on the second "
        "page B (runs of five words; menus, headers and footers are left out). Exact text "
        "matching only, not meaning.",
    ),
    (
        "Matching content: % of B in A",
        "The same in the other direction, from B to A. The two differ when the pages differ in "
        "length: a "
        "short page can be 100% inside a long one while the long one is 20% inside the short "
        f"one. A pair is listed when either direction reaches {DEFAULT_MIN_MATCH_PCT:g}%.",
    ),
    (
        "Matching content: Pair",
        "Number of the pair; the same number in 'Shared text by line' shows its text line by line.",
    ),
    (
        "Matching content: Shared words",
        "How many words the two pages share (all shared passages together).",
    ),
    (
        "Matching content: Shared passages",
        "How many separate passages of page A also appear on page B.",
    ),
    (
        "Matching content: Shared text",
        "Every passage the two pages share, in full and in page A's order, quoted as "
        "captured; '[…]' separates passages that are not next to each other on page A. A "
        "very long text continues in 'Shared text_part_2' and the next columns (an Excel "
        "cell holds at most 32,767 characters); 'Shared text_split' says when that "
        "happened. Nothing is shortened. Excel shows only about 27 lines of a cell on screen; "
        "click the cell, or use the 'Shared text by line' sheet, to read all of it.",
    ),
    (
        "Shared text by line",
        "Every line of every shared passage, one row each, with its pair number, passage "
        "number and line number: the whole shared text of every pair, readable without "
        "opening cells. Menus, headers and footers are not compared, so they are not here.",
    ),
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


def _when(moment: datetime | None) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M") if moment is not None else ""


def scrape_action(record: UrlRecord) -> str:
    """What this run did with the page compared with earlier runs, in plain words."""
    status = record.change_status
    basis = record.change_basis or ""
    reason = _CHANGE_REASONS.get(basis.split(";", 1)[0], "")
    if status is None:
        return "Scraped (not compared with earlier runs)"
    if status is ChangeStatus.NEW:
        return "New page"
    if status is ChangeStatus.UNCHANGED:
        if "reuse_not_allowed" in basis:
            return "Re-scraped (unchanged, but the earlier capture was too old to reuse)"
        moment = extraction_time(record) if record.reused_from_run_id else None
        since = f" since {moment.date().isoformat()}" if moment is not None else ""
        return f"Reused (unchanged{since}: {reason})" if reason else f"Reused (unchanged{since})"
    if status is ChangeStatus.CHANGED:
        note = "; the site's date was not updated" if "date_not_updated" in basis else ""
        return f"Re-scraped (updated: {reason}{note})" if reason else "Re-scraped (updated)"
    return "Scraped (change unknown)"


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
                "Last scraped": _when(extraction_time(record)),
                "Site last modified": _when(record.page_modified_at),
                "Scrape action": scrape_action(record),
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
    number = 0
    for i in sorted(range(len(docs)), key=lambda i: docs[i].url):
        found = sorted(pairs.get(i, []), key=lambda m: (-max(m[0], m[1]), docs[m[2]].url))
        if not found and i not in in_a_pair:
            rows.append(
                {
                    "Pair": None,
                    "URL (A)": docs[i].url,
                    "Matching URL (B)": NO_MATCH,
                    A_IN_B: None,
                    B_IN_A: None,
                    "Shared words": None,
                    "Shared passages": None,
                    "Shared text": "",
                }
            )
            continue
        for first_in_second, second_in_first, j in found:
            passages = _shared_spans(docs[i], docs[j], limit=None, max_chars=None)
            number += 1
            rows.append(
                {
                    "Pair": number,
                    "URL (A)": docs[i].url,
                    "Matching URL (B)": docs[j].url,
                    A_IN_B: round(first_in_second, 1),
                    B_IN_A: round(second_in_first, 1),
                    "Shared words": sum(len(p.split()) for p in passages),
                    "Shared passages": len(passages),
                    "Shared text": PASSAGE_BREAK.join(passages),
                }
            )
    return rows


def shared_line_rows(matches: Sequence[dict[str, Cell]]) -> list[dict[str, Cell]]:
    """Sheet "Shared text by line": every line of every shared passage, one row each.

    Excel shows at most about 27 wrapped lines of one cell (a row is at most 409
    points tall), so a long shared text is complete in its cell but cannot all be
    seen there. Here each line has its own row, so the whole match is readable.
    """
    rows: list[dict[str, Cell]] = []
    for match in matches:
        text = match.get("Shared text")
        if match.get("Pair") is None or not isinstance(text, str) or not text:
            continue
        for p, passage in enumerate(text.split(PASSAGE_BREAK), start=1):
            for n, line in enumerate(passage.split("\n"), start=1):
                rows.append(
                    {
                        "Pair": match["Pair"],
                        "URL (A)": match["URL (A)"],
                        "Matching URL (B)": match["Matching URL (B)"],
                        "Passage": p,
                        "Line": n,
                        "Text": line,
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
    matches = match_rows(records, min_match_pct=min_match_pct)
    lines = shared_line_rows(matches)
    match_columns = expand_split_column(matches, MATCH_COLUMNS, "Shared text")
    shared_columns = [
        c for c in match_columns if c == "Shared text" or c.startswith("Shared text_part_")
    ]
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
            columns=match_columns,
            rows=matches,
            wrap_columns=frozenset(shared_columns),
            widths={
                "URL (A)": 60,
                "Matching URL (B)": 60,
                **dict.fromkeys(shared_columns, 100),
            },
        ),
        Table(
            name="Shared text by line",
            columns=LINE_COLUMNS,
            rows=lines,
            wrap_columns=frozenset({"Text"}),
            widths={"URL (A)": 40, "Matching URL (B)": 40, "Text": 120},
        ),
        Table(name="How to read", columns=["Column", "Meaning"], rows=notes),
    ]
