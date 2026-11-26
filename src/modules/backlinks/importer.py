"""Read Ahrefs website exports instead of calling the API ($0).

Two exports of Site Explorer are read:

- **Referring domains** (Backlink profile -> Referring domains -> Export): columns such
  as Domain, Domain rating (or DR), Dofollow links, Links to target, First seen, Last
  seen;
- **Best by links** (Pages -> Best by links -> Export): columns such as Page URL (or
  Target URL, URL), Title, Referring domains, Links to target (or Backlinks), URL Rating
  (or UR), HTTP code.

Columns are found by name, case and spacing aside, so exports with more, fewer or
reordered columns work. Ahrefs often exports UTF-16 with tabs; UTF-8 and UTF-16 (with or
without a byte order mark) and comma, tab or semicolon separators are all read.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Literal
from urllib.parse import urlsplit

from src.modules.backlinks.models import RefDomain, TopPage

__all__ = [
    "BacklinksImportError",
    "ExportKind",
    "decode_export",
    "read_pages_csv",
    "read_refdomains_csv",
]

ExportKind = Literal["refdomains", "pages"]

_REFDOMAIN_ALIASES: dict[str, tuple[str, ...]] = {
    "domain": ("domain", "referring domain", "ref. domain", "referring domains domain"),
    "domain_rating": ("domain rating", "dr"),
    "dofollow_links": ("dofollow links", "dofollow backlinks", "links to target dofollow"),
    "links_to_target": ("links to target", "backlinks", "links"),
    "first_seen": ("first seen", "first seen date"),
    "last_seen": ("last seen", "last seen date", "last check"),
    "is_spam": ("is spam", "spam"),
}
_PAGE_ALIASES: dict[str, tuple[str, ...]] = {
    "url": ("page url", "target url", "url", "page", "target page"),
    "title": ("title", "page title"),
    "referring_domains": ("referring domains", "ref. domains", "ref domains", "rd"),
    "links": ("links to target", "backlinks", "links", "external links"),
    "url_rating": ("url rating", "ur"),
    "http_code": ("http code", "status code", "http status code"),
}


class BacklinksImportError(ValueError):
    """The file is not the Ahrefs export it was said to be."""


def decode_export(data: bytes) -> str:
    """The export's text: UTF-16 (with or without a byte order mark) or UTF-8."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    sample = data[:2000]
    if sample and sample.count(b"\x00") * 4 >= len(sample):
        # UTF-16 without a byte order mark: the zero bytes say which half comes first
        little = sample[1::2].count(b"\x00") >= sample[0::2].count(b"\x00")
        return data.decode("utf-16-le" if little else "utf-16-be", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def _rows(data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    text = decode_export(data).lstrip("﻿")
    first = text.split("\n", 1)[0]
    delimiter = max(("\t", ";", ","), key=first.count)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        msg = "the file is empty"
        raise BacklinksImportError(msg)
    rows = [{k: (v or "") for k, v in row.items() if k} for row in reader]
    return list(reader.fieldnames), rows


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().strip("﻿\"'").casefold())


def _columns(headers: list[str], aliases: dict[str, tuple[str, ...]]) -> dict[str, str | None]:
    found = {_key(h): h for h in headers if h}
    return {
        field: next((found[a] for a in names if a in found), None)
        for field, names in aliases.items()
    }


def _number(text: str) -> float | None:
    cleaned = text.replace(",", "").replace("%", "").strip()
    if not cleaned or cleaned.casefold() in {"-", "n/a", "none"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _rating(text: str) -> float | None:
    number = _number(text)
    return None if number is None else max(0.0, min(100.0, number))


def _count(text: str) -> int:
    number = _number(text)
    return max(0, int(number)) if number is not None else 0


def _date(text: str) -> str | None:
    found = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return found.group(0) if found else None


def _domain(text: str) -> str:
    raw = text.strip()
    host = urlsplit(raw if "//" in raw else f"//{raw}").hostname or raw
    return host.casefold().removeprefix("www.")


def read_refdomains_csv(data: bytes) -> list[RefDomain]:
    """Referring domains of a "Referring domains" export; raises `BacklinksImportError`."""
    headers, rows = _rows(data)
    columns = _columns(headers, _REFDOMAIN_ALIASES)
    if columns["domain"] is None:
        pages = _columns(headers, _PAGE_ALIASES)
        if pages["url"] is not None:
            msg = (
                "this looks like a top pages export (it has a page address column): import "
                "it as top pages"
            )
            raise BacklinksImportError(msg)
        msg = "not an Ahrefs referring domains export: it needs a Domain column"
        raise BacklinksImportError(msg)
    seen: dict[str, RefDomain] = {}
    for raw in rows:

        def get(field: str, raw: dict[str, str] = raw) -> str:
            column = columns[field]
            return (raw.get(column) or "").strip() if column else ""

        name = _domain(get("domain"))
        if not name or "." not in name or name in seen:
            continue
        links = _count(get("links_to_target"))
        seen[name] = RefDomain(
            domain=name,
            domain_rating=_rating(get("domain_rating")),
            dofollow_links=_count(get("dofollow_links")) if columns["dofollow_links"] else links,
            links_to_target=links,
            first_seen=_date(get("first_seen")),
            last_seen=_date(get("last_seen")),
            is_spam=get("is_spam").casefold() in {"true", "yes", "1"},
        )
    return sorted(seen.values(), key=lambda r: (-(r.domain_rating or 0.0), r.domain))


def read_pages_csv(data: bytes) -> list[TopPage]:
    """Pages of a "Best by links" export, most referring domains first."""
    headers, rows = _rows(data)
    columns = _columns(headers, _PAGE_ALIASES)
    if columns["url"] is None or columns["referring_domains"] is None:
        if _columns(headers, _REFDOMAIN_ALIASES)["domain"] is not None:
            msg = (
                "this looks like a referring domains export (it has a Domain column): import "
                "it as referring domains"
            )
            raise BacklinksImportError(msg)
        msg = "not an Ahrefs Best by links export: it needs page URL and Referring domains columns"
        raise BacklinksImportError(msg)
    seen: dict[str, TopPage] = {}
    for raw in rows:

        def get(field: str, raw: dict[str, str] = raw) -> str:
            column = columns[field]
            return (raw.get(column) or "").strip() if column else ""

        url = get("url")
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        code = _number(get("http_code"))
        seen[url] = TopPage(
            url=url,
            title=get("title") or None,
            referring_domains=_count(get("referring_domains")),
            links=_count(get("links")),
            url_rating=_rating(get("url_rating")),
            http_code=int(code) if code is not None else None,
        )
    return sorted(seen.values(), key=lambda p: (-p.referring_domains, -p.links, p.url))
