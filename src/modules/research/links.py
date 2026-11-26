"""The website's internal link graph from its scrape: orphans, PageRank, link suggestions.

Only **links in page text** count (`ui_region` not in `CHROME`: not menus, header,
footer, breadcrumbs, cookie banners or menu panels). A menu link reaches every page the
same way, so it says little about which pages a site treats as important; links written
into content do. Nofollow links are left out.

- **Orphan**: no other page links to it from its text (the home page is never one);
  **near orphan**: one such link.
- **PageRank** (damping 0.85, power iteration on body links), scaled so the strongest
  page is 100: where internal link equity collects.
- **Link suggestions**: pages that are close to the top (positions 4-20) but have few
  links from text get up to three suggested sources: the strongest pages of the same
  theme that do not link to them yet, with the target's keyword as anchor text.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

from src.modules.content_processor.knowledge.inventory_models import PageInventory

__all__ = ["CHROME", "LinkGraph", "build_graph", "page_key", "pagerank"]

CHROME: Final = frozenset(
    {"footer", "nav_menu", "header", "breadcrumb", "cookie_banner", "modal", "navigation"}
)
_DAMPING: Final = 0.85
_ROUNDS: Final = 60
_TOLERANCE: Final = 1e-9


def page_key(url: str) -> str:
    """One key per page: host without www, path without trailing slash; no query or anchor."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").casefold().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    return f"{host}{path}"


@dataclass
class LinkGraph:
    """Pages (by key) and the body links between them."""

    urls: dict[str, str]  # key -> the page's address as captured
    titles: dict[str, str | None]
    indexable: dict[str, bool]
    body_out: dict[str, set[str]] = field(default_factory=dict)
    body_in: dict[str, set[str]] = field(default_factory=dict)
    chrome_in: dict[str, set[str]] = field(default_factory=dict)
    rank: dict[str, float] = field(default_factory=dict)

    def is_home(self, key: str) -> bool:
        """The site's root page."""
        return key.endswith("/") and key.count("/") == 1

    def orphans(self) -> list[str]:
        """Indexable pages no other page links to from its text (home excluded)."""
        return sorted(
            self.urls[k]
            for k in self.urls
            if self.indexable.get(k, True) and not self.body_in.get(k) and not self.is_home(k)
        )

    def near_orphans(self) -> list[str]:
        """Indexable pages with exactly one link from another page's text."""
        return sorted(
            self.urls[k]
            for k in self.urls
            if self.indexable.get(k, True) and len(self.body_in.get(k, ())) == 1
        )


def build_graph(pages: Iterable[PageInventory]) -> LinkGraph:
    """The graph of one website's inventoried pages (links to unscraped pages dropped)."""
    captured = [p for p in pages if p.status == "inventoried"]
    graph = LinkGraph(
        urls={page_key(p.url): p.url for p in captured},
        titles={page_key(p.url): p.head.title for p in captured},
        indexable={page_key(p.url): p.head.indexable for p in captured},
    )
    for p in captured:
        source = page_key(p.url)
        for link in p.links:
            if link.kind != "internal" or link.nofollow:
                continue
            target = page_key(link.url)
            if target == source or target not in graph.urls:
                continue
            if link.ui_region in CHROME:
                graph.chrome_in.setdefault(target, set()).add(source)
            else:
                graph.body_out.setdefault(source, set()).add(target)
                graph.body_in.setdefault(target, set()).add(source)
    graph.rank = pagerank(list(graph.urls), graph.body_out)
    return graph


def pagerank(nodes: Sequence[str], out_links: Mapping[str, set[str]]) -> dict[str, float]:
    """PageRank of every node (0-100, the strongest is 100); dangling pages share evenly."""
    n = len(nodes)
    if not n:
        return {}
    score = dict.fromkeys(nodes, 1.0 / n)
    for _ in range(_ROUNDS):
        dangling = sum(score[k] for k in nodes if not out_links.get(k))
        base = (1 - _DAMPING) / n + _DAMPING * dangling / n
        new = dict.fromkeys(nodes, base)
        for k in nodes:
            targets = out_links.get(k)
            if targets:
                share = _DAMPING * score[k] / len(targets)
                for t in targets:
                    new[t] += share
        change = sum(abs(new[k] - score[k]) for k in nodes)
        score = new
        if change < _TOLERANCE:
            break
    top = max(score.values()) or 1.0
    return {k: round(100 * v / top, 2) for k, v in score.items()}
