"""Choose the sample of a lite scan from a website's addresses (no requests here)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from src.modules.content_processor.recrawl.baseline import url_key
from src.modules.lite_scan.models import LitePick, LiteSection, PickReason
from src.modules.monitoring.recrawl import core_pages

__all__ = ["Ranking", "choose_sample", "is_page", "section_of"]

_NOT_PAGES: Final = frozenset(
    {
        "pdf", "jpg", "jpeg", "png", "gif", "svg", "webp", "avif", "ico", "zip", "gz",
        "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "mp4", "mp3", "mov", "webm",
        "xml", "json", "txt", "css", "js", "woff", "woff2", "ttf", "eot",
    }
)  # fmt: skip
_NUMBER = re.compile(r"^\d+$")
_ID = re.compile(r"^(?=.*\d)[0-9a-f-]{8,}$", re.IGNORECASE)
RANKING_SHARE: Final = 0.4
"""At most this share of the sample goes to pages that rank."""
CORE_SHARE: Final = 0.25
"""At most this share goes to core pages (pricing, products, home and one level down)."""


@dataclass(frozen=True)
class Ranking:
    """What the search data says about one of the website's pages."""

    keywords: int
    traffic: float


def is_page(url: str) -> bool:
    """True for a web page address (not a file such as a PDF or an image)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    last = parts.path.rstrip("/").rsplit("/", 1)[-1]
    return "." not in last or last.rsplit(".", 1)[-1].lower() not in _NOT_PAGES


def _segment(part: str) -> str:
    if _NUMBER.match(part):
        return "{n}"
    if _ID.match(part):
        return "{id}"
    return part.lower()


def section_of(url: str) -> str:
    """The address section of a page: its folders, up to two deep (`/blog/guides`).

    A page at the top level (`/pricing`) is in section `/`. Numbers and ids are folded
    so `/news/2024/...` and `/news/2025/...` are one section.
    """
    parts = [p for p in urlsplit(url).path.split("/") if p]
    folders = parts[:-1][:2]
    return "/" + "/".join(_segment(p) for p in folders)


def _order(url: str) -> tuple[int, int, str]:
    path = urlsplit(url).path
    return (path.strip("/").count("/"), len(path), url)


def choose_sample(
    *,
    website: str,
    inventory: Sequence[str],
    known: Sequence[str] = (),
    ranking: Mapping[str, Ranking] | None = None,
    pinned: Iterable[str] = (),
    limit: int,
    per_section: int,
) -> tuple[list[LitePick], list[LiteSection]]:
    """The pages to scrape (at most `limit`) and the website's sections.

    In order: pinned pages, pages that rank (most search traffic first, up to 40% of
    the sample), core pages (up to 25%), then pages from every section in turns (the
    biggest sections first, up to `per_section` each, shallowest pages first); room
    left after that goes to the sections in proportion to their size, so a website
    with few sections still fills its sample.
    """
    ranking = ranking or {}
    universe: dict[str, str] = {}
    for url in [*inventory, *known, *ranking]:
        key = url_key(url)
        if key and key not in universe and is_page(url):
            universe[key] = url
    picks: list[LitePick] = []
    taken: set[str] = set()

    def take(url: str, reason: PickReason, detail: str = "") -> bool:
        key = url_key(url)
        if not key or key in taken or len(picks) >= limit:
            return False
        taken.add(key)
        universe.setdefault(key, url)
        picks.append(LitePick(url=url, reason=reason, detail=detail, section=section_of(url)))
        return True

    for url in pinned:
        take(url, "pinned", "pinned by a person")
    ranked = sorted(
        ((u, r) for u, r in ranking.items() if url_key(u) and is_page(u)),
        key=lambda item: (-item[1].traffic, -item[1].keywords, item[0]),
    )
    room = len(picks) + max(1, int(limit * RANKING_SHARE))
    for url, rank in ranked:
        if len(picks) >= room:
            break
        take(url, "ranking", f"ranks for {rank.keywords:,} keyword(s)")
    core = core_pages(universe.values(), website=website, theme_map=None, site=None, limit=limit)
    room = len(picks) + max(1, int(limit * CORE_SHARE))
    for key, why in core.items():
        if len(picks) >= room:
            break
        take(universe[key], "core", why)

    members: dict[str, list[str]] = {}
    for url in universe.values():
        members.setdefault(section_of(url), []).append(url)
    order = sorted(members, key=lambda s: (-len(members[s]), s))
    queues = {s: sorted(members[s], key=_order) for s in order}
    counts = {s: sum(1 for p in picks if p.section == s) for s in order}

    def next_from(section: str) -> bool:
        queue = queues[section]
        while queue:
            if take(queue.pop(0), "section", f"one of {len(members[section]):,} in {section}"):
                counts[section] += 1
                return True
        return False

    # every section first, in turns, up to `per_section` each
    progress = True
    while len(picks) < limit and progress:
        progress = False
        for section in order:
            if len(picks) >= limit:
                break
            if counts[section] < per_section and next_from(section):
                progress = True
    # room left: the section least sampled for its size gets the next page
    while len(picks) < limit:
        open_ = [s for s in order if queues[s]]
        if not open_:
            break
        section = min(open_, key=lambda s: (counts[s] / len(members[s]), order.index(s)))
        if not next_from(section):
            queues[section].clear()
    sections = [
        LiteSection(
            section=s,
            pages=len(members[s]),
            sampled=sum(1 for p in picks if p.section == s),
        )
        for s in order
    ]
    return picks, sections
