"""Facts read from a page's stored HTML: schema.org JSON-LD, menu trees and calls to action.

Everything here is deterministic and bounded (depth and item caps), so a hostile
or broken page cannot blow up a stamp.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from src.modules.content_processor.adapters.html_source import DiscoveredLink, normalize_text
from src.modules.content_processor.knowledge.models import Cta, MenuItem, SchemaFacts

__all__ = [
    "ParsedMenu",
    "first_h1",
    "is_asset_url",
    "is_tracking_url",
    "dom_links",
    "merge_menus",
    "menu_id",
    "parse_menus",
    "page_ctas",
    "schema_facts",
]

MenuKind = Literal["header", "footer", "breadcrumb", "other"]

_MAX_LIST = 100
_MAX_DEPTH = 8
_MENU_DEPTH = 4
_MENU_ITEMS = 400
_LABEL_CHARS = 120

_ORG_TYPES = frozenset(
    {
        "Organization",
        "Corporation",
        "InsuranceAgency",
        "FinancialService",
        "LocalBusiness",
        "BankOrCreditUnion",
        "EducationalOrganization",
        "NGO",
        "ProfessionalService",
    }
)
_SERVICE_TYPES = frozenset(
    {
        "Service",
        "FinancialProduct",
        "BankAccount",
        "LoanOrCredit",
        "InvestmentOrDeposit",
        "PaymentService",
        "ProfessionalService",
    }
)
_PRODUCT_TYPES = frozenset(
    {"Product", "SoftwareApplication", "WebApplication", "MobileApplication", "ProductModel"}
)
_ARTICLE_TYPES = frozenset(
    {"Article", "NewsArticle", "BlogPosting", "WebPage", "Report", "TechArticle", "PodcastEpisode"}
)


# -- schema.org JSON-LD ----------------------------------------------------------


def _types(node: dict[str, Any]) -> list[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return [v.rsplit("/", 1)[-1] for v in values if isinstance(v, str) and v]


def _nodes(data: Any, depth: int = 0) -> Iterator[dict[str, Any]]:
    """Every object with an @type, at any depth (bounded), including @graph members."""
    if depth > _MAX_DEPTH:
        return
    if isinstance(data, list):
        for item in data:
            yield from _nodes(item, depth + 1)
    elif isinstance(data, dict):
        if "@type" in data:
            yield data
        for value in data.values():
            if isinstance(value, dict | list):
                yield from _nodes(value, depth + 1)


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        text, _ = normalize_text(value)
        return text[: _LABEL_CHARS * 2] or None
    if isinstance(value, dict):
        return _text(value.get("name"))
    return None


def _load(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("<!--"):
        text = text[4:]
    if text.endswith("-->"):
        text = text[:-3]
    text = text.strip().rstrip(";")
    return json.loads(text, strict=False)


def _push(target: list[str], value: str | None) -> None:
    if value and value not in target and len(target) < _MAX_LIST:
        target.append(value)


def _offer(node: dict[str, Any]) -> str | None:
    name = _text(node.get("name")) or _text(node.get("itemOffered"))
    price = node.get("price", node.get("lowPrice"))
    currency = node.get("priceCurrency")
    if price is None and name is None:
        return None
    parts = [name or "offer"]
    if price is not None:
        parts.append(f": {price}" + (f" {currency}" if isinstance(currency, str) else ""))
    return "".join(parts)


def _breadcrumbs(node: dict[str, Any]) -> list[str]:
    items = node.get("itemListElement")
    if not isinstance(items, list):
        return []
    ranked: list[tuple[float, str]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name")) or _text(item.get("item"))
        position = item.get("position")
        rank = float(position) if isinstance(position, int | float) else float(i)
        if name:
            ranked.append((rank, name))
    return [name for _, name in sorted(ranked)]


def schema_facts(blobs: Iterable[str]) -> SchemaFacts:
    """Read the typed facts a page declares in its JSON-LD blocks."""
    facts = SchemaFacts()
    for raw in blobs:
        try:
            data = _load(raw)
        except ValueError:
            facts.unreadable_blocks += 1
            continue
        for node in _nodes(data):
            types = _types(node)
            name = _text(node.get("name"))
            for t in types:
                _push(facts.types, t)
            kinds = set(types)
            if kinds & _ORG_TYPES and facts.organization is None:
                facts.organization = name
            if kinds & _SERVICE_TYPES:
                _push(facts.services, name)
            if kinds & _PRODUCT_TYPES:
                _push(facts.products, name)
            if kinds & {"Offer", "AggregateOffer"}:
                _push(facts.offers, _offer(node))
            if "Question" in kinds:
                _push(facts.faq_questions, name or _text(node.get("text")))
            if "BreadcrumbList" in kinds and not facts.breadcrumbs:
                facts.breadcrumbs = _breadcrumbs(node)
            if kinds & _ARTICLE_TYPES:
                facts.headline = facts.headline or _text(node.get("headline"))
                for key, attr in (
                    ("datePublished", "date_published"),
                    ("dateModified", "date_modified"),
                ):
                    value = node.get(key)
                    if isinstance(value, str) and getattr(facts, attr) is None:
                        setattr(facts, attr, value.strip()[:40])
    return facts


# -- menus -------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedMenu:
    """A menu tree found in one page's HTML."""

    kind: MenuKind
    label: str
    items: list[MenuItem]

    @property
    def item_count(self) -> int:
        """Entries in the tree, at every level."""
        return _count(self.items)


def _count(items: list[MenuItem]) -> int:
    return sum(1 + _count(i.children) for i in items)


_NEW_TAB = re.compile(r"\s*\(opens? in (a )?new (tab|window)\)", re.IGNORECASE)
_CONSENT = re.compile(r"onetrust|ot-sdk|cookie|consent", re.IGNORECASE)


def _clean(text: str) -> str:
    out, _ = normalize_text(text)
    return _NEW_TAB.sub("", out).strip()[:_LABEL_CHARS]


def _own_label(li: Node, base_url: str) -> tuple[str, str | None]:
    """The text and link of a list item, leaving out its nested lists."""
    parts: list[str] = []
    hrefs: list[str] = []

    def walk(node: Node) -> None:
        child = node.child
        while child is not None:
            if child.tag == "-text":
                parts.append(child.text(deep=False) or "")
            elif child.tag not in ("ul", "ol", "script", "style", "svg"):
                if child.tag == "a" and not hrefs and child.attributes.get("href"):
                    hrefs.append(urljoin(base_url, child.attributes.get("href") or ""))
                walk(child)
            child = child.next

    walk(li)
    return _clean(" ".join(parts)), (hrefs[0] if hrefs else None)


def _list_items(lst: Node, base_url: str, depth: int, budget: list[int]) -> list[MenuItem]:
    items: list[MenuItem] = []
    seen: set[tuple[str, str | None]] = set()
    for li in lst.iter(include_text=False):
        if li.tag != "li" or budget[0] <= 0:
            continue
        label, href = _own_label(li, base_url)
        children: list[MenuItem] = []
        if depth < _MENU_DEPTH:
            for sub in li.css("ul, ol"):
                if (owner := _nearest_li(sub)) is not None and owner.mem_id == li.mem_id:
                    children.extend(_list_items(sub, base_url, depth + 1, budget))
        if not label and not children:
            continue
        key = (label, href)
        if key in seen and not children:
            continue  # the same entry twice (mobile and desktop copies)
        seen.add(key)
        budget[0] -= 1
        items.append(MenuItem(label=label, href=href, children=children))
    return items


def _nearest_li(node: Node) -> Node | None:
    parent = node.parent
    while parent is not None and parent.tag != "li":
        parent = parent.parent
    return parent


def _flat_links(container: Node, base_url: str, budget: list[int]) -> list[MenuItem]:
    items: list[MenuItem] = []
    seen: set[tuple[str, str]] = set()
    for a in container.css("a[href]"):
        if budget[0] <= 0:
            break
        label = _clean(a.text(deep=True, separator=" ") or a.attributes.get("aria-label") or "")
        href = urljoin(base_url, a.attributes.get("href") or "")
        if not label or (label, href) in seen:
            continue
        seen.add((label, href))
        budget[0] -= 1
        items.append(MenuItem(label=label, href=href))
    return items


def _tab_panels(container: Node) -> dict[str, str]:
    """Panel id -> label of the tab or button that opens it (`aria-controls`)."""
    panels: dict[str, str] = {}
    for el in container.css("[aria-controls]"):
        label = _clean(el.text(deep=True, separator=" ") or el.attributes.get("aria-label") or "")
        for panel_id in (el.attributes.get("aria-controls") or "").split():
            if label:
                panels.setdefault(panel_id, label)
    return panels


def _panel_owner(node: Node, container: Node, panels: dict[str, str]) -> str | None:
    probe = node.parent
    while probe is not None and probe.mem_id != container.mem_id:
        owner = panels.get(probe.attributes.get("id") or "")
        if owner:
            return owner
        probe = probe.parent
    return None


def _menu_tree(container: Node, base_url: str) -> list[MenuItem]:
    budget = [_MENU_ITEMS]
    top_lists = [
        lst
        for lst in container.css("ul, ol")
        if _nearest_li(lst) is None or not _inside(_nearest_li(lst), container)
    ]
    if not top_lists:
        return _flat_links(container, base_url, budget)
    panels = _tab_panels(container)
    items: list[MenuItem] = []
    for lst in top_lists:
        target = items
        owner_label = _panel_owner(lst, container, panels)
        if owner_label:
            # A mega-menu panel: its lists belong under the tab that opens it.
            owner = next((i for i in items if i.label == owner_label), None)
            if owner is None:
                owner = MenuItem(label=owner_label)
                items.append(owner)
            target = owner.children
        keys = {(i.label, i.href) for i in target}
        for item in _list_items(lst, base_url, 1, budget):
            key = (item.label, item.href)
            if key in keys:
                continue  # a second copy of the same menu (mobile drawer)
            keys.add(key)
            target.append(item)
    return items


def _inside(node: Node | None, container: Node) -> bool:
    while node is not None:
        if node.mem_id == container.mem_id:
            return True
        node = node.parent
    return False


def _is_consent(node: Node) -> bool:
    probe: Node | None = node
    while probe is not None:
        marks = f"{probe.attributes.get('id') or ''} {probe.attributes.get('class') or ''}"
        if _CONSENT.search(marks):
            return True
        probe = probe.parent
    return False


def _kind(node: Node) -> MenuKind:
    attrs = " ".join(
        str(node.attributes.get(k) or "") for k in ("aria-label", "class", "id")
    ).lower()
    if "breadcrumb" in attrs:
        return "breadcrumb"
    probe: Node | None = node
    while probe is not None:
        if probe.tag == "footer" or (probe.attributes.get("role") or "") == "contentinfo":
            return "footer"
        if probe.tag == "header" or (probe.attributes.get("role") or "") == "banner":
            return "header"
        probe = probe.parent
    return "other"


def parse_menus(tree: HTMLParser, base_url: str) -> list[ParsedMenu]:
    """Navigation trees of a page: header, footer, breadcrumb and other nav elements."""
    menus: list[ParsedMenu] = []
    taken: list[Node] = []
    navs = tree.css("nav, [role=navigation]")
    # Breadcrumbs first, so a breadcrumb inside the header is still read as one.
    candidates = (
        [n for n in navs if _kind(n) == "breadcrumb"]
        + tree.css("header, footer, [role=banner], [role=contentinfo]")
        + navs
    )
    for node in candidates:
        if any(_inside(node, outer) for outer in taken) or _is_consent(node):
            continue
        kind = _kind(node)
        if kind == "breadcrumb":
            items = _flat_links(node, base_url, [_MENU_ITEMS])
            text = _clean(node.text(deep=True, separator=" › ") or "")
            if not items and text:
                items = [MenuItem(label=part.strip()) for part in text.split("›") if part.strip()]
        else:
            items = _menu_tree(node, base_url)
        if not items:
            continue
        taken.append(node)
        label = _clean(str(node.attributes.get("aria-label") or ""))
        menus.append(ParsedMenu(kind=kind, label=label, items=items))
    return menus


def _merge_items(into: list[MenuItem], items: Iterable[MenuItem]) -> None:
    index = {(i.label, i.href): i for i in into}
    by_label = {i.label: i for i in into}
    for item in items:
        match = index.get((item.label, item.href))
        if match is None:
            # A tab button (no link) and a link with the same label are one entry.
            same = by_label.get(item.label)
            if same is not None and (same.href is None or item.href is None):
                match = same
                match.href = match.href or item.href
        if match is None:
            copy = MenuItem(label=item.label, href=item.href)
            _merge_items(copy.children, item.children)
            into.append(copy)
            index[(item.label, item.href)] = copy
            by_label.setdefault(item.label, copy)
        else:
            _merge_items(match.children, item.children)


def merge_menus(menus: Iterable[ParsedMenu]) -> list[ParsedMenu]:
    """One tree per menu (kind and label), uniting what each view of the page showed.

    A mega menu shows one panel per opened state; merging the states gives the
    whole tree. Entries are matched on label and link, so nothing is lost.
    """
    merged: dict[tuple[str, str], ParsedMenu] = {}
    for menu in menus:
        key = (menu.kind, menu.label)
        if key not in merged:
            merged[key] = ParsedMenu(kind=menu.kind, label=menu.label, items=[])
        _merge_items(merged[key].items, menu.items)
    return list(merged.values())


def menu_id(menu: ParsedMenu) -> str:
    """Stable id of a menu (kind and label), so a site menu is stored once per run."""
    payload = json.dumps([menu.kind, menu.label], ensure_ascii=False)
    return "menu-" + hashlib.sha256(payload.encode()).hexdigest()[:12]


def first_h1(tree: HTMLParser) -> str | None:
    """Text of the first non-empty h1."""
    for node in tree.css("h1"):
        text = _clean(node.text(deep=True, separator=" ") or "")
        if text:
            return text
    return None


_CHROME_TAGS = frozenset({"header", "footer", "nav"})
_CHROME_ROLES = frozenset({"banner", "contentinfo", "navigation"})


def _hidden(node: Node) -> bool:
    probe: Node | None = node
    while probe is not None:
        a = probe.attributes
        style = (a.get("style") or "").replace(" ", "").lower()
        if "hidden" in a or a.get("aria-hidden") == "true" or "display:none" in style:
            return True
        probe = probe.parent
    return False


def _in_chrome(node: Node) -> bool:
    probe = node.parent
    while probe is not None:
        if probe.tag in _CHROME_TAGS or (probe.attributes.get("role") or "") in _CHROME_ROLES:
            return True
        marks = f"{probe.attributes.get('id') or ''} {probe.attributes.get('class') or ''}"
        if _CONSENT.search(marks):
            return True
        probe = probe.parent
    return False


def _link_text(a: Node) -> str:
    parts: list[str] = []

    def walk(node: Node) -> None:
        child = node.child
        while child is not None:
            if child.tag == "-text":
                parts.append(child.text(deep=False) or "")
            elif child.tag not in ("svg", "script", "style", "img"):
                walk(child)
            child = child.next

    walk(a)
    return _clean(" ".join(parts) or a.attributes.get("aria-label") or "")


def dom_links(tree: HTMLParser, base_url: str) -> list[DiscoveredLink]:
    """Links in a page's content (not its header, footer, menus or consent panel), in order."""
    links: list[DiscoveredLink] = []
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        if href.startswith(("javascript:", "#")) or _in_chrome(a):
            continue
        links.append(
            DiscoveredLink(url=urljoin(base_url, href), text=_link_text(a), hidden=_hidden(a))
        )
    return links


# -- calls to action -----------------------------------------------------------------

_STRONG_CTA = re.compile(
    r"\b(get (a |your )?(quote|started)|apply|buy|sign up|register|contact( us)?|request|book"
    r"|schedule|enrol+|talk to|speak (to|with)|call( us)?|start|download|subscribe|demo"
    r"|find an? (advisor|agent|financial professional)|get in touch)\b",
    re.IGNORECASE,
)
_SOFT_CTA = re.compile(
    r"\b(learn more|read more|explore|discover|find out|see (how|more|all)|view|watch|listen)\b",
    re.IGNORECASE,
)
_CTA_MAX_CHARS = 60
_CTA_MAX = 50


def page_ctas(links: Iterable[DiscoveredLink]) -> list[Cta]:
    """Visible links that ask the reader to act, strongest first in page order."""
    found: list[Cta] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        text = _clean(link.text)
        if link.hidden or not text or len(text) > _CTA_MAX_CHARS:
            continue
        strength: Literal["strong", "soft"] | None = None
        if _STRONG_CTA.search(text):
            strength = "strong"
        elif _SOFT_CTA.search(text):
            strength = "soft"
        if strength is None or (text.lower(), link.url) in seen:
            continue
        seen.add((text.lower(), link.url))
        found.append(Cta(text=text, url=link.url, strength=strength))
        if len(found) >= _CTA_MAX:
            break
    return sorted(found, key=lambda c: c.strength != "strong")


# -- assets and tracking -------------------------------------------------------------

_DOC_EXT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|odt|ods|odp|rtf|csv|txt)$", re.IGNORECASE)
_ASSET_EXT = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|bmp|tiff?|avif|mp4|webm|mov|m4v|mp3|wav|ogg|m4a"
    r"|woff2?|ttf|otf|eot|css|js|map)$",
    re.IGNORECASE,
)
_ASSET_PATHS = ("/content/dam/", "/adobe/dynamicmedia/", "/is/image/", "/is/content/")
_TRACKING = re.compile(
    r"(/akam/|/b/ss/|/pixel|/beacon|/collect\b|/tr/?\?|/analytics|/gtm\.js|/__utm"
    r"|doubleclick|google-analytics|/ping\b)",
    re.IGNORECASE,
)
_ASSET_MIME = ("image/", "video/", "audio/", "font/")


def is_asset_url(url: str, mime: str | None) -> bool:
    """An image, media file, font or static resource, not a page or a document."""
    path = url.split("?", 1)[0].split("#", 1)[0]
    if _DOC_EXT.search(path):
        return False
    if mime and mime.startswith(_ASSET_MIME):
        return True
    if _ASSET_EXT.search(path):
        return True
    return any(p in path.lower() for p in _ASSET_PATHS) and not (mime or "").startswith("text/")


def is_tracking_url(url: str) -> bool:
    """A tracking pixel, beacon or bot-manager endpoint."""
    return bool(_TRACKING.search(url))
