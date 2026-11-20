"""Place a lite-scanned website's unscraped sitemap pages into the theme map's themes.

Only the address is known for those pages, so a page is placed by the words of its
address (`/insurance/term-life-quotes` -> term, life, quotes) against each theme's
defining terms. A page is counted for a theme when it matches at least one term and no
other theme matches as well; ties and pages with no match are left out. The counts are
**inferred**: shown next to the scraped pages, never used to decide who owns a theme.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Final
from urllib.parse import urlsplit

from src.modules.content_processor.recrawl.baseline import url_key
from src.modules.lite_scan.models import LiteInventory
from src.modules.theming.models import ThemeMap

__all__ = ["add_inferred", "address_words", "place"]

_WORD: Final = re.compile(r"[a-z]+")
_SKIP: Final = frozenset(
    {"www", "html", "htm", "php", "aspx", "index", "page", "the", "and", "for"}
)


def _norm(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def address_words(url: str) -> set[str]:
    """The words of a page address's path (lower case, plurals folded)."""
    path = urlsplit(url).path.lower()
    return {_norm(w) for w in _WORD.findall(path) if len(w) >= 3 and w not in _SKIP}


def place(url: str, themes: Mapping[str, Iterable[str]]) -> str | None:
    """The theme id an address belongs to by its words, or None (no match, or a tie)."""
    words = address_words(url)
    if not words:
        return None
    scores: dict[str, int] = {}
    for theme_id, terms in themes.items():
        score = 0
        for term in terms:
            parts = [_norm(p) for p in term.lower().split()]
            if parts and all(p in words for p in parts):
                score += len(parts)
        if score:
            scores[theme_id] = score
    if not scores:
        return None
    best = max(scores.values())
    top = [t for t, s in scores.items() if s == best]
    return top[0] if len(top) == 1 else None


def add_inferred(theme_map: ThemeMap, inventories: Iterable[LiteInventory]) -> ThemeMap:
    """The map with lite-scanned websites marked and their sitemap-only pages counted."""
    by_run = {inv.run_id: inv for inv in inventories if inv.run_id}
    if not by_run:
        return theme_map
    terms = {t.theme_id: t.terms for t in theme_map.themes}
    inferred: dict[tuple[str, str], int] = {}
    websites = []
    for site in theme_map.websites:
        inv = by_run.get(site.run_id)
        if inv is None:
            websites.append(site)
            continue
        scraped = {k for p in theme_map.pages if p.website == site.website if (k := url_key(p.url))}
        scraped |= {k for p in inv.sample if (k := url_key(p.url))}
        for url in inv.inventory:
            key = url_key(url)
            if not key or key in scraped:
                continue
            theme_id = place(url, terms)
            if theme_id:
                inferred[(theme_id, site.website)] = inferred.get((theme_id, site.website), 0) + 1
        websites.append(
            site.model_copy(update={"scan": "lite", "inventory": inv.pages, "basis": inv.basis()})
        )
    themes = []
    for theme in theme_map.themes:
        coverage = [
            c.model_copy(update={"inferred": inferred.get((theme.theme_id, c.website), 0)})
            for c in theme.coverage
        ]
        themes.append(theme.model_copy(update={"coverage": coverage}))
    stats = dict(theme_map.stats)
    stats["inferred_pages"] = sum(inferred.values())
    return theme_map.model_copy(update={"websites": websites, "themes": themes, "stats": stats})
