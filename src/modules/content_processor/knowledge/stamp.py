"""Stamp every URL of a run with deterministic facts and build its chunks (RFC 0002, Stage A).

Input is a run's `UrlRecord`s plus the stored payload of each page (read back by
hash). Nothing here calls a model or the network.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser

from src.modules.content_processor.adapters.html_source import (
    DiscoveredLink,
    SourceExtraction,
    block_channel,
    extract_html_source,
)
from src.modules.content_processor.analysis.claims import quantities_in, split_sentences
from src.modules.content_processor.analysis.overlap import OverlapConfig, find_overlaps
from src.modules.content_processor.contracts import ContentBlock, UrlRecord
from src.modules.content_processor.enums import GapType, UiRegion, UrlBadge
from src.modules.content_processor.knowledge.chunking import PageContext, chunk_blocks, main_corpus
from src.modules.content_processor.knowledge.html_facts import (
    ParsedMenu,
    dom_links,
    first_h1,
    is_asset_url,
    is_tracking_url,
    menu_id,
    merge_menus,
    page_ctas,
    parse_menus,
    schema_facts,
)
from src.modules.content_processor.knowledge.models import (
    Chunk,
    MenuRef,
    MetadataSource,
    PageFormat,
    PageStamp,
    SchemaFacts,
    SiteMenu,
    SiteStamp,
)
from src.modules.content_processor.verify.record import CORPUS_LAYERS

__all__ = [
    "NEAR_DUPLICATE_JACCARD",
    "PLACEHOLDER_SHARE",
    "PayloadLoader",
    "StampResult",
    "page_key",
    "rendered_views",
    "stamp_records",
]

PayloadLoader = Callable[[str], bytes | None]
"""Returns the stored bytes for a payload hash, or None when they are not available."""

NEAR_DUPLICATE_JACCARD = 0.9
PLACEHOLDER_SHARE = 0.3
_MAX_NUMBERS = 100
_SUMMARY_CHARS = 300
_MIN_SUMMARY_WORDS = 8
_MIN_FLESCH_WORDS = 100
_OFFICE_MIME = ("msword", "officedocument", "ms-excel", "ms-powerpoint", "opendocument")
_ERROR_TITLE = re.compile(
    r"^\s*(errors?|404|410|500|page not found|not found|oops)\b", re.IGNORECASE
)
_PLACEHOLDER = re.compile(r"\b[lj]orem ipsum dolor\b|\bsed ut perspiciatis\b", re.IGNORECASE)
_VOWELS = re.compile(r"[aeiouy]+")
_WORD = re.compile(r"[A-Za-z]+")
_NOT_ELIGIBLE_BADGES = {
    UrlBadge.BLOCKED: "blocked",
    UrlBadge.FAILED: "failed",
    UrlBadge.EXCLUDED: "excluded_by_policy",
    UrlBadge.RUNNING: "not_finished",
}


@dataclass
class StampResult:
    """Stamps, chunks and run-level facts for one run."""

    stamps: list[PageStamp]
    chunks: list[Chunk]
    site: SiteStamp


@dataclass
class _Html:
    """What the server sent (head, JSON-LD) and what the browser showed (menus, links)."""

    extraction: SourceExtraction
    h1: str | None
    menus: list[ParsedMenu]
    schema: SchemaFacts
    links: list[DiscoveredLink]
    rendered_views: int = 0


@dataclass
class _Page:
    record: UrlRecord
    url: str
    html: _Html | None
    metadata_source: MetadataSource
    stamp: PageStamp | None = None


def _url_of(record: UrlRecord) -> str:
    return (record.final_url or record.requested_url).split("#", 1)[0]


def page_key(url: str) -> str:
    """Stable id of a page: the URL without fragment or trailing slash."""
    key = url.split("#", 1)[0].rstrip("/")
    return "pg-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _format(mime: str | None) -> PageFormat:
    m = (mime or "").lower()
    if "html" in m:
        return "html"
    if m == "application/pdf":
        return "pdf"
    if any(x in m for x in _OFFICE_MIME):
        return "office"
    if m.startswith("image/"):
        return "image"
    return "other"


def _site_of(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def rendered_views(record: UrlRecord) -> list[tuple[str, str]]:
    """(channel, evidence hash) of the rendered views: base view first, then explored states."""
    by_channel: dict[str, str] = {}
    for block in record.blocks:
        channel = block_channel(block.state_id)
        if channel == "src" or channel in by_channel or not block.evidence_ids:
            continue
        by_channel[channel] = block.evidence_ids[0].removeprefix("evidence:")
    order = sorted(by_channel, key=lambda c: (c != "rendered", len(c), c))
    return [(c, by_channel[c]) for c in order]


def _parse_html(body: bytes, url: str, views: Sequence[bytes]) -> _Html:
    extraction = extract_html_source(body, base_url=url, evidence_id="stamp", resource_id="stamp")
    source = HTMLParser(body)
    trees = [HTMLParser(v) for v in views]
    menus = merge_menus(m for tree in [source, *trees] for m in parse_menus(tree, url))
    h1 = next((h for tree in [*trees[:1], source] if (h := first_h1(tree))), None)
    links = [link for tree in [*trees, source] for link in dom_links(tree, url)]
    return _Html(
        extraction=extraction,
        h1=h1,
        menus=menus,
        schema=schema_facts(extraction.structured_data),
        links=links,
        rendered_views=len(trees),
    )


def _syllables(word: str) -> int:
    w = word.lower()
    count = len(_VOWELS.findall(w))
    if w.endswith("e") and not w.endswith(("le", "ee")) and count > 1:
        count -= 1
    return max(count, 1)


def reading_ease(text: str) -> float | None:
    """Flesch reading ease, or None for short texts."""
    words = _WORD.findall(text)
    if len(words) < _MIN_FLESCH_WORDS:
        return None
    sentences = max(len(split_sentences(text)), 1)
    syllables = sum(_syllables(w) for w in words)
    score = 206.835 - 1.015 * (len(words) / sentences) - 84.6 * (syllables / len(words))
    return round(score, 1)


def _first_sentence(blocks: Sequence[ContentBlock]) -> str | None:
    for block in blocks:
        text = block.normalized_text
        if block.ui_region == UiRegion.HEADER or text in block.heading_path:
            continue  # the page's own title or a heading, not a summary
        for start, end in split_sentences(text):
            sentence = text[start:end]
            if len(sentence.split()) >= _MIN_SUMMARY_WORDS:
                return sentence[:_SUMMARY_CHARS]
    return None


def _headings(blocks: Sequence[ContentBlock]) -> list[str]:
    seen: dict[str, None] = {}
    for block in blocks:
        for heading in block.heading_path:
            if heading:
                seen.setdefault(heading, None)
    return list(seen)


def _visible(blocks: Sequence[ContentBlock]) -> list[ContentBlock]:
    return [b for b in blocks if b.layer in CORPUS_LAYERS and b.dedupe_of is None]


def _words(blocks: Sequence[ContentBlock]) -> int:
    return sum(len(b.normalized_text.split()) for b in blocks)


def _numbers(blocks: Sequence[ContentBlock]) -> list[str]:
    found: dict[str, None] = {}
    for block in blocks:
        for q in quantities_in(block.normalized_text):
            found.setdefault(q.text, None)
            if len(found) >= _MAX_NUMBERS:
                return list(found)
    return list(found)


def _exclusion(
    page: _Page, mime: str | None, main: Sequence[ContentBlock], title: str | None
) -> str | None:
    record = page.record
    reason = _NOT_ELIGIBLE_BADGES.get(record.badge)
    if reason:
        return reason
    if is_tracking_url(page.url):
        return "tracking"
    if is_asset_url(page.url, mime):
        return "asset"
    if any(g.gap_type == GapType.SOFT_404_SUSPECTED for g in record.gaps):
        return "soft_404"
    if title and _ERROR_TITLE.match(title):
        return "error_page"
    placeholder = _words([b for b in main if _PLACEHOLDER.search(b.normalized_text)])
    if placeholder and placeholder >= PLACEHOLDER_SHARE * _words(main):
        return "placeholder_text"
    if not main:
        return "no_main_content"
    return None


def _breadcrumbs(html: _Html | None, blocks: Sequence[ContentBlock]) -> list[str]:
    if html is not None and html.schema.breadcrumbs:
        return html.schema.breadcrumbs
    for menu in html.menus if html is not None else []:
        if menu.kind == "breadcrumb":
            return [item.label for item in menu.items]
    crumbs = [b.normalized_text for b in blocks if b.ui_region == UiRegion.BREADCRUMB]
    return list(dict.fromkeys(c for c in crumbs if c))


def _stamp_page(page: _Page, run_id: str, now: datetime) -> PageStamp:
    record = page.record
    url = page.url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    segments = [s for s in parts.path.split("/") if s]
    mime = record.sniffed_mime
    visible = _visible(record.blocks)
    main = main_corpus(record.blocks)
    html = page.html
    meta = html.extraction.metadata if html is not None else {}
    schema = html.schema if html is not None else SchemaFacts()
    main_text = "\n".join(b.normalized_text for b in main)
    lang = meta.get("lang") or next((b.language for b in main if b.language), None)
    title = (meta.get("title") or "").casefold()
    summary = next(
        (
            text
            for text in (meta.get("description"), meta.get("og:description"))
            if text and text.casefold() not in title  # a description that only repeats the title
        ),
        None,
    ) or _first_sentence(main)
    exclusion = _exclusion(page, mime, main, meta.get("title"))
    return PageStamp(
        run_id=run_id,
        url=record.requested_url,
        final_url=record.final_url,
        page_key=page_key(url),
        site=_site_of(host),
        host=host,
        stamped_at=now,
        knowledge_eligible=exclusion is None,
        exclusion_reason=exclusion,
        is_asset=exclusion == "asset",
        is_tracking=exclusion == "tracking",
        media_type=mime,
        page_format=_format(mime),
        path_segments=segments,
        path_l1=segments[0] if segments else None,
        path_l2=segments[1] if len(segments) > 1 else None,
        path_l3=segments[2] if len(segments) > 2 else None,
        path_depth=len(segments),
        template_fingerprint=record.template_fingerprint,
        breadcrumbs=_breadcrumbs(html, visible),
        title=meta.get("title") or None,
        meta_description=meta.get("description"),
        h1=(html.h1 if html is not None else None)
        or next((b.heading_path[0] for b in main if b.heading_path), None),
        canonical=meta.get("canonical"),
        lang=lang,
        hreflang={k.split(":", 1)[1]: v for k, v in meta.items() if k.startswith("hreflang:")},
        og={k: v for k, v in meta.items() if k.startswith(("og:", "twitter:"))},
        robots_meta=meta.get("robots"),
        schema_org=schema,
        metadata_source=page.metadata_source,
        headings=_headings(main),
        page_summary=(summary or "")[:_SUMMARY_CHARS] or None,
        word_count=_words(visible),
        main_word_count=_words(main),
        reading_ease=reading_ease(main_text) if (lang or "").lower().startswith("en") else None,
        components=dict(Counter(str(b.ui_region or "none") for b in visible)),
        ctas=page_ctas(html.links) if html is not None else [],
        numbers=_numbers(main),
        published_at=record.page_published_at,
        modified_at=record.page_modified_at,
        change_status=str(record.change_status) if record.change_status else None,
        badge=str(record.badge),
        gap_types=sorted({str(g.gap_type) for g in record.gaps}),
    )


def _load_page(record: UrlRecord, load_payload: PayloadLoader) -> _Page:
    url = _url_of(record)
    if _format(record.sniffed_mime) != "html":
        return _Page(record, url, None, "not_html")
    body = load_payload(record.payload_sha256) if record.payload_sha256 else None
    if body is None:
        return _Page(record, url, None, "missing_evidence")
    views = [v for _, sha in rendered_views(record) if (v := load_payload(sha)) is not None]
    return _Page(record, url, _parse_html(body, url, views), "stored_html")


def _mark_duplicates(pages: list[_Page]) -> None:
    """Canonical and identical-text duplicates leave the knowledge base; near ones are noted."""
    by_url = {p.url.rstrip("/"): p for p in pages}
    for page in pages:
        stamp = page.stamp
        if stamp is None or not stamp.knowledge_eligible or not stamp.canonical:
            continue
        target = by_url.get(stamp.canonical.split("#", 1)[0].rstrip("/"))
        if (
            target is not None
            and target is not page
            and target.stamp is not None
            and target.stamp.knowledge_eligible
        ):
            stamp.duplicate_of = target.url
    eligible = [p for p in pages if p.stamp is not None and p.stamp.knowledge_eligible]
    by_record = {p.record.requested_url: p for p in eligible}
    result = find_overlaps(
        [p.record for p in eligible], OverlapConfig(threshold=NEAR_DUPLICATE_JACCARD)
    )
    for pair in result.pairs:
        a, b = by_record.get(pair.a.url), by_record.get(pair.b.url)
        if a is None or b is None or a.stamp is None or b.stamp is None:
            continue
        if pair.basis == "exact_duplicate" or pair.jaccard >= 1.0:
            keep, drop = sorted((a, b), key=lambda p: (len(p.url), p.url))
            if drop.stamp is not None and drop.stamp.duplicate_of is None:
                drop.stamp.duplicate_of = keep.url
        else:
            a.stamp.near_duplicates.append(b.url)
            b.stamp.near_duplicates.append(a.url)
    for page in pages:
        if page.stamp is not None and page.stamp.duplicate_of:
            page.stamp.knowledge_eligible = False
            page.stamp.exclusion_reason = "duplicate"


def _site_name(pages: Sequence[_Page], fallback: str) -> tuple[str, str | None]:
    names = Counter(
        p.html.extraction.metadata["og:site_name"]
        for p in pages
        if p.html is not None and p.html.extraction.metadata.get("og:site_name")
    )
    orgs = Counter(
        p.html.schema.organization
        for p in pages
        if p.html is not None and p.html.schema.organization
    )
    organization = orgs.most_common(1)[0][0] if orgs else None
    if names:
        return names.most_common(1)[0][0], organization
    suffixes = Counter(
        title.rsplit(" | ", 1)[1].strip()
        for p in pages
        if p.html is not None and " | " in (title := p.html.extraction.metadata.get("title", ""))
    )
    html_pages = sum(1 for p in pages if p.html is not None)
    if suffixes:
        suffix, count = suffixes.most_common(1)[0]
        if count * 2 >= html_pages:
            return suffix, organization
    return organization or fallback, organization


def _short_title(title: str | None, site_name: str) -> str | None:
    if title and title.endswith(f" | {site_name}"):
        return title[: -len(site_name) - 3].strip() or title
    return title


def stamp_records(
    records: Sequence[UrlRecord], load_payload: PayloadLoader, *, now: datetime | None = None
) -> StampResult:
    """Stamp every record and chunk every page that belongs in the knowledge base."""
    if not records:
        msg = "no records to stamp"
        raise ValueError(msg)
    when = now or datetime.now(UTC)
    run_id = records[0].run_id
    pages = [_load_page(r, load_payload) for r in records]
    for page in pages:
        page.stamp = _stamp_page(page, run_id, when)
    _mark_duplicates(pages)

    hosts = sorted({p.stamp.host for p in pages if p.stamp is not None and p.stamp.host})
    fallback = Counter(p.stamp.site for p in pages if p.stamp is not None).most_common(1)[0][0]
    site_name, organization = _site_name(pages, fallback)

    page_counts: Counter[str] = Counter()
    for page in pages:
        if page.html is None or page.stamp is None:
            continue
        refs: dict[str, MenuRef] = {}
        for menu in page.html.menus:
            if menu.kind != "breadcrumb":  # breadcrumbs are per page, kept on the stamp
                refs.setdefault(menu_id(menu), MenuRef(menu_id=menu_id(menu), kind=menu.kind))
        page_counts.update(refs.keys())
        page.stamp.menus = list(refs.values())
    # Each site menu once: the union of what every page showed of it.
    site_menus = [
        SiteMenu(
            menu_id=menu_id(menu),
            kind=menu.kind,
            label=menu.label,
            items=menu.items,
            item_count=menu.item_count,
            page_count=page_counts[menu_id(menu)],
        )
        for menu in merge_menus(
            m for p in pages if p.html is not None for m in p.html.menus if m.kind != "breadcrumb"
        )
    ]

    chunks: list[Chunk] = []
    for page in pages:
        stamp = page.stamp
        if stamp is None or not stamp.knowledge_eligible:
            continue
        ctx = PageContext(
            run_id=run_id,
            url=page.url,
            page_key=stamp.page_key,
            site=site_name,
            title=_short_title(stamp.title, site_name) or stamp.h1,
            summary=stamp.page_summary,
        )
        page_chunks = chunk_blocks(ctx, page.record.blocks)
        stamp.chunk_count = len(page_chunks)
        chunks.extend(page_chunks)

    stamps = [p.stamp for p in pages if p.stamp is not None]
    html_stamps = [s for s in stamps if s.metadata_source == "stored_html"]
    coverage_fields: dict[str, Callable[[PageStamp], bool]] = {
        "title": lambda s: bool(s.title),
        "meta_description": lambda s: bool(s.meta_description),
        "canonical": lambda s: bool(s.canonical),
        "og": lambda s: bool(s.og),
        "lang": lambda s: bool(s.lang),
        "h1": lambda s: bool(s.h1),
        "json_ld": lambda s: bool(s.schema_org.types),
        "menus": lambda s: bool(s.menus),
        "breadcrumbs": lambda s: bool(s.breadcrumbs),
    }
    coverage = (
        {
            name: round(sum(1 for s in html_stamps if test(s)) / len(html_stamps), 3)
            for name, test in coverage_fields.items()
        }
        if html_stamps
        else {}
    )
    site = SiteStamp(
        run_id=run_id,
        site=fallback,
        site_name=site_name,
        hosts=hosts,
        organization=organization,
        menus=sorted(site_menus, key=lambda m: (-m.page_count, m.kind, m.menu_id)),
        pages=len(stamps),
        knowledge_pages=sum(1 for s in stamps if s.knowledge_eligible),
        excluded=dict(Counter(s.exclusion_reason for s in stamps if s.exclusion_reason)),
        chunks=len(chunks),
        schema_types=dict(Counter(t for s in stamps for t in s.schema_org.types)),
        metadata_coverage=coverage,
    )
    return StampResult(stamps=stamps, chunks=chunks, site=site)
