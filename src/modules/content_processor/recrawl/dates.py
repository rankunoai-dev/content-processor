"""When did the site last update this page or file?

Signals, strongest first:

1. JSON-LD `dateModified` (schema.org, written by the publisher's CMS)
2. Meta tags: `article:modified_time`, `og:updated_time`, `last-modified`,
   `dcterms.modified`, `DC.date.modified`, `revised`
3. Microdata `itemprop="dateModified"` (`<meta content>` or `<time datetime>`)
4. PDF document info `/ModDate`
5. HTTP `Last-Modified`, unless it equals the fetch time (many CMSs stamp every
   response with "now", which says nothing about the page)

`datePublished` / `article:published_time` are reported separately. Nothing is
invented: when no signal exists the dates stay None.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from selectolax.parser import HTMLParser

from src.core.schemas import StrictModel

__all__ = ["PageDates", "extract_page_dates", "parse_date"]

_DYNAMIC_WINDOW = timedelta(seconds=120)
# Many CMS and CDNs send a web page's Last-Modified as the time the page was last
# regenerated or cached, not edited (live John Hancock: 49 of 51 pages, minutes to
# hours before the fetch, on press releases from 2021). For HTML a recent header is
# therefore not taken as the page's update date; an older one usually is a real edit.
_HTML_HEADER_MIN_AGE = timedelta(days=7)
_MODIFIED_META = (
    ("article:modified_time", "meta:article_modified_time"),
    ("og:updated_time", "meta:og_updated_time"),
    ("last-modified", "meta:last_modified"),
    ("dcterms.modified", "meta:dcterms_modified"),
    ("dc.date.modified", "meta:dc_date_modified"),
    ("revised", "meta:revised"),
    ("date.modified", "meta:date_modified"),
)
_PUBLISHED_META = (
    ("article:published_time", "meta:article_published_time"),
    ("dcterms.created", "meta:dcterms_created"),
    ("dc.date.created", "meta:dc_date_created"),
    ("date", "meta:date"),
)
_PDF_MODDATE = re.compile(rb"/ModDate\s*\(\s*D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


class PageDates(StrictModel):
    """Dates the site states about a page or file."""

    modified_at: datetime | None = None
    modified_source: str | None = None
    published_at: datetime | None = None
    published_source: str | None = None
    http_last_modified: str | None = None
    http_etag: str | None = None
    http_last_modified_is_dynamic: bool = False


def parse_date(value: str | None) -> datetime | None:
    """Parse ISO 8601 or RFC 1123 dates; naive values are taken as UTC."""
    if not value:
        return None
    text = value.strip()
    parsed: datetime | None = None
    if _ISO_DATE.match(text):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text[:10])
            except ValueError:
                return None
    else:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _walk_jsonld(node: Any, key: str, found: list[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, str):
                found.append(v)
            else:
                _walk_jsonld(v, key, found)
    elif isinstance(node, list):
        for item in node:
            _walk_jsonld(item, key, found)


def _jsonld_dates(tree: HTMLParser, key: str) -> list[datetime]:
    out: list[datetime] = []
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text(deep=True) or "")
        except json.JSONDecodeError:
            continue
        found: list[str] = []
        _walk_jsonld(data, key, found)
        out += [d for d in (parse_date(v) for v in found) if d is not None]
    return out


def _meta(tree: HTMLParser) -> dict[str, str]:
    values: dict[str, str] = {}
    for node in tree.css("meta"):
        attrs = node.attributes
        name = (attrs.get("property") or attrs.get("name") or attrs.get("http-equiv") or "").lower()
        content = attrs.get("content")
        if name and content and name not in values:
            values[name] = content
    return values


def _itemprop(tree: HTMLParser, prop: str) -> datetime | None:
    for node in tree.css(f'[itemprop="{prop}"]'):
        value = node.attributes.get("content") or node.attributes.get("datetime")
        parsed = parse_date(value)
        if parsed is not None:
            return parsed
    return None


def _pdf_moddate(body: bytes) -> datetime | None:
    match = _PDF_MODDATE.search(body)
    if match is None:
        return None
    y, mo, d, h, mi, se = (
        int(g) if g else default
        for g, default in zip(match.groups(), (0, 1, 1, 0, 0, 0), strict=True)
    )
    try:
        return datetime(y, mo, d, h, mi, se, tzinfo=UTC)
    except ValueError:
        return None


def extract_page_dates(
    *,
    body: bytes,
    headers: dict[str, str],
    sniffed_mime: str | None,
    fetched_at: datetime | None = None,
) -> PageDates:
    """Collect the modified and published dates a page or file declares."""
    now = fetched_at or datetime.now(UTC)
    http_lm = headers.get("last-modified")
    http_lm_dt = parse_date(http_lm)
    window = _HTML_HEADER_MIN_AGE if sniffed_mime == "text/html" else _DYNAMIC_WINDOW
    dynamic = http_lm_dt is not None and abs(now - http_lm_dt) < window
    dates = PageDates(
        http_last_modified=http_lm,
        http_etag=headers.get("etag"),
        http_last_modified_is_dynamic=dynamic,
    )
    if sniffed_mime == "text/html":
        tree = HTMLParser(body)
        modified = _jsonld_dates(tree, "dateModified")
        if modified:
            dates.modified_at, dates.modified_source = max(modified), "jsonld:dateModified"
        published = _jsonld_dates(tree, "datePublished")
        if published:
            dates.published_at, dates.published_source = min(published), "jsonld:datePublished"
        meta = _meta(tree)
        if dates.modified_at is None:
            for name, label in _MODIFIED_META:
                parsed = parse_date(meta.get(name))
                if parsed is not None:
                    dates.modified_at, dates.modified_source = parsed, label
                    break
        if dates.published_at is None:
            for name, label in _PUBLISHED_META:
                parsed = parse_date(meta.get(name))
                if parsed is not None:
                    dates.published_at, dates.published_source = parsed, label
                    break
        if dates.modified_at is None:
            parsed = _itemprop(tree, "dateModified")
            if parsed is not None:
                dates.modified_at, dates.modified_source = parsed, "microdata:dateModified"
        if dates.published_at is None:
            parsed = _itemprop(tree, "datePublished")
            if parsed is not None:
                dates.published_at, dates.published_source = parsed, "microdata:datePublished"
    elif sniffed_mime == "application/pdf":
        parsed = _pdf_moddate(body[:4_000_000])
        if parsed is not None:
            dates.modified_at, dates.modified_source = parsed, "pdf:ModDate"
    if dates.modified_at is None and http_lm_dt is not None and not dynamic:
        dates.modified_at, dates.modified_source = http_lm_dt, "http:last-modified"
    return dates
