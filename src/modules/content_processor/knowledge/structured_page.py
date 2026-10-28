"""Each page as an ordered tree and as Markdown (RFC 0003, step B).

The tree is read from the page's best stored view: the composed view (shadow DOM
read through) when the explorer made one, else the browser view, else the HTML the
server sent. Site chrome is left out. Tabs and accordions are kept with the label of
the control that opens them, and their hidden panels are kept as `collapsed`; text
that only appeared after a click (in an explored state, not in the base view) is
added under the control that revealed it as `after_click`. Every text node cites
the content blocks with the same text. Code only: nothing is fetched, no model.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

from selectolax.parser import HTMLParser, Node

from src.modules.content_processor.adapters.html_source import block_channel
from src.modules.content_processor.adapters.regions import classify_region
from src.modules.content_processor.contracts import ContentBlock, UrlRecord
from src.modules.content_processor.enums import UiRegion, UrlBadge
from src.modules.content_processor.knowledge.html_facts import is_tracking_url
from src.modules.content_processor.knowledge.inventory import media_provider
from src.modules.content_processor.knowledge.page_types import (
    ADD_TO_CART,
    SERVICE_CTA,
    PageSignals,
    classify_page,
)
from src.modules.content_processor.knowledge.stamp import PayloadLoader, page_key, rendered_views
from src.modules.content_processor.knowledge.structured_models import (
    Layer,
    NodeKind,
    StructuredLink,
    StructuredNode,
    StructuredPage,
)

__all__ = [
    "COMPOSED_SELECTOR",
    "StructureResult",
    "build_structured_page",
    "page_signals",
    "structure_records",
    "markdown_name",
    "to_markdown",
]

COMPOSED_SELECTOR = "state:shadow_root:composed_tree"
_SKIP = frozenset({"script", "style", "template", "noscript", "svg", "math", "head", "link"})
_INLINE = frozenset(
    {
        "a",
        "abbr",
        "b",
        "bdi",
        "bdo",
        "br",
        "cite",
        "code",
        "data",
        "dfn",
        "em",
        "i",
        "kbd",
        "label",
        "mark",
        "q",
        "s",
        "samp",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "time",
        "u",
        "var",
        "wbr",
        "del",
        "ins",
        "font",
    }
)
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_CHROME = frozenset(
    {
        UiRegion.HEADER,
        UiRegion.FOOTER,
        UiRegion.NAV_MENU,
        UiRegion.HAMBURGER_MENU,
        UiRegion.MEGA_MENU,
        UiRegion.BREADCRUMB,
        UiRegion.COOKIE_BANNER,
        UiRegion.CHAT_WIDGET,
        UiRegion.AD_SLOT,
        UiRegion.SEARCH,
        UiRegion.MODAL,
        UiRegion.POPOVER,
        UiRegion.TOOLTIP,
    }
)
_CHROME_NAMES = frozenset(str(r) for r in _CHROME)
_CHROME_ROLES = frozenset({"nav", "footer", "chrome"})
# Media players draw their controls as text (time, buffering, captions menu).
_PLAYER_CHROME = re.compile(r"(?:^|\s)(?:vjs-(?!tech)|mejs-|plyr__|jw-)", re.IGNORECASE)
_WIDGET_ROLES = frozenset({"slider", "progressbar", "timer", "toolbar", "menu", "menubar"})
_SPACES = re.compile(r"\s+")
_PUNCT_SPACE = re.compile(r"\s+([.,;:!?)\]])")
_NEW_TAB = re.compile(r"\s*\(opens? in (a )?new (tab|window)\)", re.IGNORECASE)
_PRICE = re.compile(r"(?:[$€£¥₹]\s?\d[\d,.]*|\d[\d,.]*\s?(?:USD|EUR|GBP|INR|CAD|AUD))")
_QUESTION = re.compile(
    r"\?\s*$|^(how|what|why|when|where|who|which|can|do|does|is|are|will|should)\b", re.I
)
_CTA_MAX_WORDS = 8
_PIXEL = re.compile(r"/(rt|pixel|beacon|track|spacer|blank)\.(gif|png)\b", re.I)
# The parser ignores the CSS ` i` flag, so case-insensitive matches use these patterns.
_VARIANT_NAME = re.compile(r"size|colou?r|variant|style|capacity", re.IGNORECASE)
_FACET_CLASS = re.compile(r"facet|filter-group|refinement", re.IGNORECASE)
_BREADCRUMB = re.compile(r"breadcrumb", re.IGNORECASE)
_NOT_STRUCTURED = {UrlBadge.BLOCKED, UrlBadge.FAILED, UrlBadge.EXCLUDED}


def _ident(node: Node) -> int:
    """Identity of a parsed node (the stubs declare `mem_id` as a method; it is an int)."""
    return cast(int, node.mem_id)


# -- text helpers ----------------------------------------------------------------------


def _clean(text: str | None) -> str:
    return _SPACES.sub(" ", _NEW_TAB.sub("", text or "")).strip()


def _key(text: str) -> str:
    return _clean(text).lower()


def _attr(node: Node, name: str) -> str | None:
    value = node.attributes.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _hidden(node: Node) -> bool:
    a = node.attributes
    style = (a.get("style") or "").replace(" ", "").lower()
    return (
        "hidden" in a
        or a.get("aria-hidden") == "true"
        or "display:none" in style
        or "visibility:hidden" in style
    )


def _plain(node: Node) -> str:
    parts: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.tag == "-text":
            parts.append(current.text(deep=False) or "")
        elif current is node or current.tag not in _SKIP:
            if current.tag == "br":
                parts.append(" ")
            stack.extend(reversed(list(current.iter(include_text=True))))
    return _clean(" ".join(parts))


# -- the walk --------------------------------------------------------------------------


@dataclass
class _Ctx:
    base_url: str
    tree: HTMLParser
    region: UiRegion = UiRegion.UNKNOWN
    layer: Layer = "visible"
    consumed: set[int] = field(default_factory=set)


@dataclass
class _Inline:
    parts: list[str] = field(default_factory=list)
    links: list[StructuredLink] = field(default_factory=list)

    def add(self, node: Node, ctx: _Ctx) -> None:
        if node.tag == "-text":
            self.parts.append(node.text(deep=False) or "")
            return
        if node.tag in _SKIP or (_hidden(node) and ctx.layer == "visible"):
            return
        if node.tag == "br":
            self.parts.append("\n")
            return
        text = _plain(node)
        if node.tag == "a" and text and (href := _attr(node, "href")):
            url = urljoin(ctx.base_url, href)
            if urlsplit(url).scheme in ("http", "https", "mailto", "tel"):
                self.links.append(StructuredLink(text=text, url=url))
        self.parts.append(f" {text} " if text else "")

    def flush(self, ctx: _Ctx, out: list[StructuredNode]) -> None:
        text = _PUNCT_SPACE.sub(r"\1", _clean(" ".join(self.parts)))
        if text:
            out.append(_text_node(text, self.links, ctx))
        self.parts, self.links = [], []


def _text_node(text: str, links: list[StructuredLink], ctx: _Ctx) -> StructuredNode:
    only = links[0] if len(links) == 1 else None
    if only is not None and _key(only.text) == _key(text) and len(text.split()) <= _CTA_MAX_WORDS:
        return StructuredNode(
            kind="cta", text=text, url=only.url, layer=ctx.layer, ui_region=str(ctx.region)
        )
    return StructuredNode(
        kind="paragraph", text=text, links=list(links), layer=ctx.layer, ui_region=str(ctx.region)
    )


def _children(el: Node, ctx: _Ctx) -> list[StructuredNode]:
    out: list[StructuredNode] = []
    inline = _Inline()
    for child in el.iter(include_text=True):
        if child.tag == "-text" or child.tag in _INLINE:
            if child.tag != "-text" and child.css_first("p, div, ul, ol, table, h1, h2, h3, h4"):
                inline.flush(ctx, out)
                out += _children(child, ctx)
            else:
                inline.add(child, ctx)
            continue
        inline.flush(ctx, out)
        out += _element(child, ctx)
    inline.flush(ctx, out)
    return out


def _by_id(ctx: _Ctx, ident: str | None) -> Node | None:
    if not ident:
        return None
    for node in ctx.tree.css("[id]"):
        if node.attributes.get("id") == ident:
            return node
    return None


def _panel(
    panel: Node | None, title: str, kind: NodeKind, ctx: _Ctx, control: str
) -> StructuredNode:
    children: list[StructuredNode] = []
    layer: Layer = ctx.layer
    if panel is not None:
        ctx.consumed.add(_ident(panel))
        layer = "collapsed" if _hidden(panel) or ctx.layer == "collapsed" else ctx.layer
        children = _children(panel, replace(ctx, layer=layer))
    return StructuredNode(
        kind=kind,
        title=title,
        control=control,
        layer=layer,
        ui_region=str(ctx.region),
        children=children,
    )


def _list(el: Node, ctx: _Ctx) -> StructuredNode:
    items: list[str] = []
    for li in el.iter(include_text=False):
        if li.tag not in ("li", "dt", "dd") or (_hidden(li) and ctx.layer == "visible"):
            continue
        text = _plain(li)
        if text:
            items.append(text)
    return StructuredNode(
        kind="list",
        items=items,
        ordered=el.tag == "ol",
        layer=ctx.layer,
        ui_region=str(ctx.region),
    )


def _table(el: Node, ctx: _Ctx) -> StructuredNode:
    rows: list[list[str]] = []
    header = False
    for i, tr in enumerate(el.css("tr")):
        cells = [_plain(c) for c in tr.iter(include_text=False) if c.tag in ("th", "td")]
        if not any(cells):
            continue
        if i == 0 and (
            tr.css_first("th") is not None or tr.parent is not None and tr.parent.tag == "thead"
        ):
            header = True
        rows.append(cells)
    caption = el.css_first("caption")
    return StructuredNode(
        kind="table",
        rows=rows,
        header_row=header,
        caption=_plain(caption) if caption is not None else None,
        layer=ctx.layer,
        ui_region=str(ctx.region),
    )


def _image(el: Node, ctx: _Ctx, caption: str | None = None) -> list[StructuredNode]:
    src = _attr(el, "src")
    if src is None or src.startswith("data:"):
        src = _attr(el, "data-src") or _attr(el, "data-lazy-src")
    if not src or src.startswith("data:"):
        return []
    url = urljoin(ctx.base_url, src)
    tiny = any((_attr(el, d) or "").strip() in ("0", "1", "2") for d in ("width", "height"))
    if tiny or is_tracking_url(url) or _PIXEL.search(url):
        return []  # tracking pixels are not content
    alt = el.attributes.get("alt")
    return [
        StructuredNode(
            kind="image",
            url=url,
            alt=_clean(alt) if alt is not None else None,
            caption=caption,
            layer=ctx.layer,
            ui_region=str(ctx.region),
        )
    ]


def _tabs(el: Node, ctx: _Ctx) -> StructuredNode:
    tabs: list[StructuredNode] = []
    for tab in el.css('[role="tab"]'):
        label = _plain(tab)
        panel = _by_id(ctx, _attr(tab, "aria-controls"))
        tabs.append(_panel(panel, label, "tab", ctx, "tab"))
    return StructuredNode(kind="tabs", ui_region=str(ctx.region), layer=ctx.layer, children=tabs)


def _element(el: Node, ctx: _Ctx) -> list[StructuredNode]:
    tag = el.tag
    if tag in _SKIP or _ident(el) in ctx.consumed:
        return []
    if tag != "video" and _PLAYER_CHROME.search(_attr(el, "class") or ""):
        return [n for v in el.css("video") for n in _element(v, ctx)]  # keep the video only
    if (_attr(el, "role") or "").lower() in _WIDGET_ROLES:
        return []
    region = classify_region(tag, el.attributes, ctx.region)
    if region in _CHROME:
        return []
    own = region if region != ctx.region else None
    if _hidden(el) and ctx.layer == "visible":
        return []
    ctx = replace(ctx, region=region)
    role = (_attr(el, "role") or "").lower()
    if tag in _HEADINGS:
        text = _plain(el)
        return (
            [
                StructuredNode(
                    kind="heading",
                    level=_HEADINGS[tag],
                    title=text,
                    layer=ctx.layer,
                    ui_region=str(region),
                )
            ]
            if text
            else []
        )
    if tag == "p":
        out: list[StructuredNode] = []
        _children_inline(el, ctx, out)
        return out
    if tag in ("ul", "ol", "dl"):
        node = _list(el, ctx)
        return [node] if node.items else []
    if tag == "table":
        node = _table(el, ctx)
        return [node] if node.rows else []
    if tag == "blockquote":
        text = _plain(el)
        return (
            [StructuredNode(kind="quote", text=text, layer=ctx.layer, ui_region=str(region))]
            if text
            else []
        )
    if tag == "pre":
        return [
            StructuredNode(
                kind="code", text=el.text(deep=True) or "", layer=ctx.layer, ui_region=str(region)
            )
        ]
    if tag == "img":
        return _image(el, ctx)
    if tag == "figure":
        cap = el.css_first("figcaption")
        caption = _plain(cap) if cap is not None else None
        imgs = [n for img in el.css("img") for n in _image(img, ctx, caption)]
        rest = [
            n
            for c in el.iter(include_text=False)
            if c.tag not in ("img", "figcaption", "picture")
            for n in _element(c, ctx)
        ]
        return imgs + rest
    if tag in ("video", "iframe", "audio", "embed"):
        src = _attr(el, "src") or _attr(el, "data-src")
        if src is None and tag in ("video", "audio"):
            source = el.css_first("source[src]")
            src = _attr(source, "src") if source is not None else None
        if not src or src.startswith(("data:", "about:")):
            return []
        url = urljoin(ctx.base_url, src)
        provider = media_provider(url)
        named = _attr(el, "title") or _attr(el, "aria-label")
        return [
            StructuredNode(
                kind="media",
                url=url,
                title=named or (provider if provider != "inline" else "streamed on the page"),
                control=tag,
                layer=ctx.layer,
                ui_region=str(region),
            )
        ]
    if tag == "form":
        fields = [_clean(_plain(lbl)) for lbl in el.css("label") if _plain(lbl)] or [
            _attr(i, "placeholder") or _attr(i, "name") or ""
            for i in el.css("input, select, textarea")
            if _attr(i, "type") not in ("hidden", "submit")
        ]
        legend = el.css_first("legend")
        return [
            StructuredNode(
                kind="form",
                title=_attr(el, "aria-label") or (_plain(legend) if legend is not None else None),
                items=[f for f in fields if f],
                url=urljoin(ctx.base_url, _attr(el, "action") or ""),
                layer=ctx.layer,
                ui_region=str(region),
            )
        ]
    if tag == "details":
        summary = el.css_first("summary")
        title = _plain(summary) if summary is not None else ""
        if summary is not None:
            ctx.consumed.add(_ident(summary))
        layer: Layer = ctx.layer if "open" in el.attributes else "collapsed"
        children = _children(el, replace(ctx, layer=layer))
        return [
            StructuredNode(
                kind="accordion_item",
                title=title,
                control="accordion",
                layer=layer,
                ui_region=str(region),
                children=children,
            )
        ]
    if role == "tablist":
        return [_tabs(el, ctx)]
    if tag == "button" or role == "button":
        controls = _attr(el, "aria-controls")
        if _attr(el, "aria-expanded") is not None and controls:
            return [_panel(_by_id(ctx, controls), _plain(el), "accordion_item", ctx, "accordion")]
        return []  # other buttons are controls, not content
    if own is UiRegion.CARD:
        return [
            StructuredNode(
                kind="card", layer=ctx.layer, ui_region=str(region), children=_children(el, ctx)
            )
        ]
    if own is UiRegion.CAROUSEL:
        return [
            StructuredNode(
                kind="carousel", layer=ctx.layer, ui_region=str(region), children=_children(el, ctx)
            )
        ]
    if own in (UiRegion.TAB_PANEL, UiRegion.ACCORDION_PANEL):
        layer = "collapsed" if _hidden(el) else ctx.layer
        return [
            StructuredNode(
                kind="panel",
                layer=layer,
                ui_region=str(region),
                children=_children(el, replace(ctx, layer=layer)),
            )
        ]
    return _children(el, ctx)


def _children_inline(el: Node, ctx: _Ctx, out: list[StructuredNode]) -> None:
    inline = _Inline()
    for child in el.iter(include_text=True):
        inline.add(child, ctx)
    inline.flush(ctx, out)


# -- shaping the flat stream into a tree -------------------------------------------------


def _card_title(card: StructuredNode) -> None:
    for i, child in enumerate(card.children):
        if child.kind == "heading":
            card.title = child.title
            del card.children[i]
            return
        if child.kind == "section":
            card.title = child.title
            card.children[i : i + 1] = child.children
            return


_GROUPS = frozenset({"accordion", "faq", "card_group"})


def _unwrap(nodes: list[StructuredNode]) -> list[StructuredNode]:
    """A panel that only holds accordion items or cards is a wrapper, not content."""
    out: list[StructuredNode] = []
    for node in nodes:
        if (
            node.kind == "panel"
            and node.children
            and all(c.kind in ("accordion_item", "card", *_GROUPS) for c in node.children)
        ):
            out += _unwrap(node.children)
        else:
            out.append(node)
    return out


def _group(nodes: list[StructuredNode]) -> list[StructuredNode]:
    """Wrap runs of cards and accordion items; label accordions of questions as FAQ."""
    out: list[StructuredNode] = []
    for node in _unwrap(nodes):
        accordions = ("accordion", "faq")
        if node.kind in accordions and out and out[-1].kind in accordions:
            out[-1].children += _group(node.children)
            continue
        node.children = _group(node.children)
        if node.kind == "card":
            _card_title(node)
        if node.kind in ("card", "accordion_item"):
            wrapper: NodeKind = "card_group" if node.kind == "card" else "accordion"
            if out and out[-1].kind == wrapper:
                out[-1].children.append(node)
                continue
            out.append(
                StructuredNode(
                    kind=wrapper, layer=node.layer, ui_region=node.ui_region, children=[node]
                )
            )
            continue
        out.append(node)
    for node in out:
        if node.kind in ("accordion", "faq") and node.children:
            node.kind = "accordion"
            asked = sum(1 for c in node.children if _QUESTION.search(c.title or ""))
            if asked * 2 > len(node.children):
                node.kind = "faq"
            node.layer = (
                "visible" if any(c.layer == "visible" for c in node.children) else node.layer
            )
    return out


def _sectionize(nodes: list[StructuredNode]) -> list[StructuredNode]:
    """Turn headings into sections that hold what follows them, nested by level."""
    root: list[StructuredNode] = []
    stack: list[StructuredNode] = []
    for node in nodes:
        node.children = _sectionize(node.children)
        if node.kind == "heading" and node.level is not None:
            section = StructuredNode(
                kind="section",
                title=node.title,
                level=node.level,
                layer=node.layer,
                ui_region=node.ui_region,
            )
            while stack and (stack[-1].level or 0) >= node.level:
                stack.pop()
            (stack[-1].children if stack else root).append(section)
            stack.append(section)
            continue
        (stack[-1].children if stack else root).append(node)
    return root


# -- evidence ----------------------------------------------------------------------------


def _node_text(node: StructuredNode) -> list[str]:
    if node.kind in ("paragraph", "quote", "cta", "code") and node.text:
        return [node.text]
    if node.kind in ("list", "form"):
        return list(node.items)
    if node.kind == "table":
        return [" ".join(r) for r in node.rows]
    if node.title and node.kind in ("section", "tab", "accordion_item", "card", "revealed"):
        return [node.title]
    return []


def _walk(nodes: Iterable[StructuredNode]) -> Iterable[StructuredNode]:
    for node in nodes:
        yield node
        yield from _walk(node.children)


def _cite(nodes: list[StructuredNode], blocks: Sequence[ContentBlock]) -> None:
    index: dict[str, ContentBlock] = {}
    for b in blocks:
        index.setdefault(_key(b.normalized_text or b.raw_text), b)
    for node in _walk(nodes):
        for text in _node_text(node):
            found = index.get(_key(text))
            if found is not None and found.block_id not in node.block_ids:
                node.block_ids.append(found.block_id)
                node.evidence_ids += [e for e in found.evidence_ids if e not in node.evidence_ids]


# -- after-click content -----------------------------------------------------------------


def _revealed(
    nodes: list[StructuredNode], blocks: Sequence[ContentBlock], base: str, frames: set[str]
) -> list[StructuredNode]:
    """Text found only in explored states, grouped under the control that revealed it."""
    seen = " \n ".join(_key(t) for n in _walk(nodes) for t in _node_text(n))
    groups: dict[tuple[str, str], list[ContentBlock]] = {}
    for b in blocks:
        channel = block_channel(b.state_id)
        if (
            channel in ("src", base)
            or channel in frames
            or str(b.ui_region) in _CHROME_NAMES
            or b.role in _CHROME_ROLES
        ):
            continue
        selector = b.provenance.selector or ""
        if not selector.startswith("state:") or selector == COMPOSED_SELECTOR:
            continue
        text = _key(b.normalized_text or b.raw_text)
        if not text or text in seen:
            continue
        _, kind, label = (selector.split(":", 2) + ["", ""])[:3]
        group = groups.setdefault((kind, label), [])
        if all(_key(x.normalized_text) != text for x in group):
            group.append(b)
    targets = [n for n in _walk(nodes) if n.kind in ("accordion_item", "tab") and n.title]
    extra: list[StructuredNode] = []
    for (kind, label), found in groups.items():
        paragraphs = [
            StructuredNode(
                kind="paragraph",
                text=_clean(b.raw_text),
                layer="after_click",
                ui_region=str(b.ui_region) if b.ui_region else None,
                block_ids=[b.block_id],
                evidence_ids=list(b.evidence_ids),
            )
            for b in found
        ]
        prefix = _key(label)[:30]
        target = next(
            (t for t in targets if prefix and _key(t.title or "").startswith(prefix)), None
        )
        if target is not None:
            target.children += paragraphs
            continue
        extra.append(
            StructuredNode(
                kind="revealed", title=label, control=kind, layer="after_click", children=paragraphs
            )
        )
    return extra


# -- page facts and signals ------------------------------------------------------------


def _json_ld(tree: HTMLParser) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text() or "")
        except ValueError:
            continue
        stack: list[Any] = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                out.append(item)
                stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
    return out


def _types(nodes: Iterable[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for node in nodes:
        raw = node.get("@type")
        for t in raw if isinstance(raw, list) else [raw]:
            if isinstance(t, str):
                name = t.rsplit("/", 1)[-1]
                if name not in found:
                    found.append(name)
    return found


def _author(nodes: Iterable[dict[str, Any]], tree: HTMLParser) -> str | None:
    for node in nodes:
        author = node.get("author")
        for a in author if isinstance(author, list) else [author]:
            if isinstance(a, dict) and isinstance(a.get("name"), str):
                return _clean(a["name"])
            if isinstance(a, str) and a.strip():
                return _clean(a)
    meta = tree.css_first('meta[name="author"]')
    if meta is not None and _attr(meta, "content"):
        return _clean(_attr(meta, "content"))
    byline = tree.css_first('[rel="author"], .author, .byline, [itemprop="author"]')
    return _clean(_plain(byline))[:120] or None if byline is not None else None


def _count_matching(
    tree: HTMLParser, css: str, attrs: Sequence[str], pattern: re.Pattern[str]
) -> int:
    return sum(1 for n in tree.css(css) if any(pattern.search(_attr(n, a) or "") for a in attrs))


def _breadcrumbs(tree: HTMLParser, ld: Sequence[dict[str, Any]]) -> list[str]:
    """The trail from structured data, else from the page's breadcrumb element."""
    for node in ld:
        if "BreadcrumbList" not in _types([node]):
            continue
        items = node.get("itemListElement")
        names: list[str] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            inner = item.get("item")
            name = item.get("name") or (inner.get("name") if isinstance(inner, dict) else None)
            if isinstance(name, str) and _clean(name):
                names.append(_clean(name))
        if names:
            return names
    for attr in ("aria-label", "class"):
        for el in tree.css(f"[{attr}]"):
            if _BREADCRUMB.search(_attr(el, attr) or ""):
                parts = [_plain(x) for x in el.css("li")] or [_plain(x) for x in el.css("a")]
                if any(parts):
                    return [x for x in parts if x]
    return []


def page_signals(
    tree: HTMLParser, url: str, nodes: Sequence[StructuredNode], words: int
) -> PageSignals:
    """The classifier's inputs, read from the page and its tree."""
    ld = _json_ld(tree)
    og = tree.css_first('meta[property="og:type"]')
    buttons = [_plain(b) for b in tree.css('button, a, input[type="submit"]')]
    cards = [n for n in _walk(nodes) if n.kind == "card"]
    priced = sum(
        1 for c in cards if any(_PRICE.search(t) for d in _walk(c.children) for t in _node_text(d))
    )
    main_text = " ".join(t for n in _walk(nodes) for t in _node_text(n))
    faq = sum(len(n.children) for n in _walk(nodes) if n.kind == "faq")
    faq += sum(1 for d in ld if "Question" in _types([d]))
    forms = tree.css("form")
    return PageSignals(
        url=url,
        schema_types=_types(ld),
        og_type=_attr(og, "content") if og is not None else None,
        words=words,
        prices=len(_PRICE.findall(main_text)),
        cart_buttons=sum(1 for b in buttons if ADD_TO_CART.search(b)),
        service_ctas=[b for b in buttons if SERVICE_CTA.search(b)][:3],
        product_cards=priced,
        cards=len(cards),
        has_article_tag=tree.css_first("article") is not None,
        has_author=_author(ld, tree) is not None,
        has_published_date=any("datePublished" in d for d in ld)
        or tree.css_first('meta[property="article:published_time"], time[datetime]') is not None,
        forms=len(forms),
        form_fields=sum(len(f.css("input, select, textarea")) for f in forms),
        faq_items=faq,
        variant_controls=_count_matching(tree, "select", ("name", "id"), _VARIANT_NAME)
        + len(tree.css('[data-variant], [role="radiogroup"]')),
        facets=_count_matching(tree, "[class]", ("class",), _FACET_CLASS)
        + len(tree.css("[data-facet]")),
    )


# -- building a page -------------------------------------------------------------------


def _count(nodes: list[StructuredNode]) -> dict[str, int]:
    counts: dict[str, int] = {}
    cited = total = words = 0
    for node in _walk(nodes):
        counts[node.kind] = counts.get(node.kind, 0) + 1
        texts = _node_text(node)
        if texts and node.kind != "section":
            total += 1
            cited += bool(node.block_ids)
            words += sum(len(t.split()) for t in texts)
    counts["text_nodes"] = total
    counts["text_nodes_cited"] = cited
    counts["words"] = words
    return counts


def _base_view(record: UrlRecord) -> tuple[str | None, set[str]]:
    """The view to read the tree from, and the channels that are frames."""
    selectors: dict[str, str] = {}
    for b in record.blocks:
        selectors.setdefault(block_channel(b.state_id), b.provenance.selector or "")
    frames = {c for c, s in selectors.items() if s.startswith("state:frame:")}
    composed = next((c for c, s in selectors.items() if s == COMPOSED_SELECTOR), None)
    if composed is not None:
        return composed, frames
    if "rendered" in selectors:
        return "rendered", frames
    return None, frames


def build_structured_page(
    url: str,
    html: bytes,
    *,
    run_id: str,
    now: datetime,
    blocks: Sequence[ContentBlock] = (),
    base_view: str = "src",
    frames: set[str] | None = None,
    source_html: bytes | None = None,
    published_at: datetime | None = None,
    modified_at: datetime | None = None,
) -> StructuredPage:
    """Build the tree of one page from the HTML of its base view."""
    tree = HTMLParser(html)
    head_tree = HTMLParser(source_html) if source_html is not None else tree
    ctx = _Ctx(base_url=url, tree=tree)
    root = tree.css_first("main") or tree.body or tree.root
    flat = _children(root, ctx) if root is not None else []
    nodes = _sectionize(_group(flat))
    nodes += _revealed(nodes, blocks, base_view, frames or set())
    _cite(nodes, blocks)
    title_node = head_tree.css_first("title") or tree.css_first("title")
    desc = head_tree.css_first('meta[name="description"]') or tree.css_first(
        'meta[name="description"]'
    )
    html_el = head_tree.css_first("html")
    ld = _json_ld(head_tree) or _json_ld(tree)
    h1 = next((n.title for n in _walk(nodes) if n.kind == "section" and n.level == 1), None)
    counts = _count(nodes)
    page = StructuredPage(
        run_id=run_id,
        url=url,
        page_key=page_key(url),
        built_at=now,
        status="structured",
        base_view=base_view,
        title=_clean(title_node.text()) if title_node is not None else None,
        h1=h1,
        description=_attr(desc, "content") if desc is not None else None,
        author=_author(ld, tree),
        published_at=published_at,
        modified_at=modified_at,
        language=_attr(html_el, "lang") if html_el is not None else None,
        breadcrumbs=_breadcrumbs(tree, ld),
        nodes=nodes,
        counts=counts,
    )
    page.page_type = classify_page(page_signals(tree, url, nodes, counts["words"]))
    return page


@dataclass
class StructureResult:
    """A run's structured pages and totals."""

    pages: list[StructuredPage]
    summary: dict[str, Any]


def structure_records(
    records: Sequence[UrlRecord], load_payload: PayloadLoader, *, now: datetime | None = None
) -> StructureResult:
    """Build the tree of every page of a run from its stored views."""
    from src.modules.content_processor.knowledge.page_types import site_profile

    moment = now or datetime.now(UTC)
    pages: list[StructuredPage] = []
    for record in records:
        url = (record.final_url or record.requested_url).split("#", 1)[0]
        stub = {"run_id": record.run_id, "url": url, "page_key": page_key(url), "built_at": moment}
        mime = (record.sniffed_mime or "").lower()
        if record.badge in _NOT_STRUCTURED:
            pages.append(StructuredPage(**stub, status="not_captured"))
            continue
        if "html" not in mime:
            label = classify_page(PageSignals(url=url, media_type=mime or None))
            pages.append(StructuredPage(**stub, status="not_html", page_type=label))
            continue
        base, frames = _base_view(record)
        views = dict(rendered_views(record))
        source = load_payload(record.payload_sha256) if record.payload_sha256 else None
        html = load_payload(views[base]) if base is not None and base in views else None
        if html is None:
            base, html = "src", source
        if html is None:
            pages.append(StructuredPage(**stub, status="missing_evidence"))
            continue
        pages.append(
            build_structured_page(
                url,
                html,
                run_id=record.run_id,
                now=moment,
                blocks=record.blocks,
                base_view=base or "src",
                frames=frames,
                source_html=source,
                published_at=record.page_published_at,
                modified_at=record.page_modified_at,
            )
        )
    typed = [p.page_type for p in pages if p.page_type is not None]
    profile = site_profile(typed, cart_pages=0)
    done = [p for p in pages if p.status == "structured"]
    summary: dict[str, Any] = {
        "pages": len(pages),
        "structured": len(done),
        "not_captured": sum(1 for p in pages if p.status == "not_captured"),
        "not_html": sum(1 for p in pages if p.status == "not_html"),
        "missing_evidence": sum(1 for p in pages if p.status == "missing_evidence"),
        "site_profile": profile.model_dump(),
        "text_nodes": sum(p.counts.get("text_nodes", 0) for p in done),
        "text_nodes_cited": sum(p.counts.get("text_nodes_cited", 0) for p in done),
    }
    return StructureResult(pages, summary)


# -- Markdown ----------------------------------------------------------------------------


def _md_escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _with_links(text: str, links: Sequence[StructuredLink]) -> str:
    """Insert links in reading order, each only in text not already linked."""
    out: list[str] = []
    cursor = 0
    for link in links:
        at = text.find(link.text, cursor) if link.text else -1
        if at < 0:
            continue
        out.append(text[cursor:at])
        out.append(f"[{link.text}]({link.url})")
        cursor = at + len(link.text)
    out.append(text[cursor:])
    return "".join(out)


_LAYER_NOTE = {
    "visible": "",
    "collapsed": " _(shown after a click)_",
    "after_click": " _(appears after a click)_",
}


def _md(nodes: Sequence[StructuredNode], depth: int, out: list[str]) -> None:
    for n in nodes:
        note = _LAYER_NOTE[n.layer]
        if n.kind == "section":
            level = min(6, max(2, n.level or depth))
            out.append(f"{'#' * level} {n.title}{note}\n")
            _md(n.children, level + 1, out)
        elif n.kind == "paragraph":
            out.append(_with_links(n.text or "", n.links) + "\n")
        elif n.kind == "cta":
            out.append(f"[**{n.text}**]({n.url})\n")
        elif n.kind == "quote":
            out.append("> " + (n.text or "") + "\n")
        elif n.kind == "code":
            out.append("```\n" + (n.text or "").rstrip() + "\n```\n")
        elif n.kind == "list":
            marks = [f"{i}." if n.ordered else "-" for i in range(1, len(n.items) + 1)]
            out.append(
                "\n".join(f"{m} {item}" for m, item in zip(marks, n.items, strict=True)) + "\n"
            )
        elif n.kind == "table":
            rows = n.rows
            width = max(len(r) for r in rows)
            padded = [[_md_escape_cell(c) for c in r] + [""] * (width - len(r)) for r in rows]
            head = padded[0] if n.header_row else [""] * width
            body = padded[1:] if n.header_row else padded
            lines = ["| " + " | ".join(head) + " |", "|" + "---|" * width]
            lines += ["| " + " | ".join(r) + " |" for r in body]
            if n.caption:
                lines.insert(0, f"_{n.caption}_\n")
            out.append("\n".join(lines) + "\n")
        elif n.kind == "image":
            caption = f"\n_{n.caption}_" if n.caption else ""
            out.append(f"![{n.alt or ''}]({n.url}){caption}\n")
        elif n.kind == "media":
            out.append(f"[{(n.control or 'media').capitalize()}: {n.title}]({n.url})\n")
        elif n.kind == "form":
            fields = ", ".join(n.items) if n.items else "no labelled fields"
            out.append(f"**Form{': ' + n.title if n.title else ''}** ({fields})\n")
        elif n.kind in ("tabs", "accordion", "faq", "card_group", "carousel"):
            label = {
                "tabs": "Tabs",
                "accordion": "Accordion",
                "faq": "Questions and answers",
                "card_group": "Cards",
                "carousel": "Carousel",
            }[n.kind]
            out.append(f"**{label}**{note}\n")
            _md(n.children, depth, out)
        elif n.kind in ("tab", "accordion_item", "card", "revealed", "panel"):
            prefix = {
                "tab": "Tab: ",
                "accordion_item": "",
                "card": "",
                "revealed": f"After clicking '{n.title}': ",
                "panel": "",
            }[n.kind]
            title = n.title if n.kind != "revealed" else ""
            if prefix or title:
                out.append(
                    f"{'#' * min(6, depth)} {prefix}{title or ''}{note}\n".replace(":  ", ": ")
                )
            _md(n.children, min(6, depth + 1), out)


def _date(value: datetime) -> str:
    return f"{value:%B} {value.day}, {value.year}"


def to_markdown(page: StructuredPage) -> str:
    """The page as Markdown: header facts, then the content tree in reading order."""
    title = page.h1 or page.title or page.url
    out = [f"# {title}\n"]
    if page.description:
        out.append(f"> {page.description}\n")
    facts = [f"URL: {page.url}"]
    if page.title and page.title != title:
        facts.append(f"Page title: {page.title}")
    if page.published_at:
        facts.append(f"Published: {_date(page.published_at)}")
    if page.modified_at:
        facts.append(f"Updated: {_date(page.modified_at)}")
    if page.author:
        facts.append(f"Author: {page.author}")
    if page.page_type is not None:
        facts.append(
            f"Page type: {page.page_type.page_type} (confidence {page.page_type.confidence:.2f})"
        )
    if page.breadcrumbs:
        facts.append("Breadcrumb: " + " › ".join(page.breadcrumbs))
    out.append("  \n".join(facts) + "\n")
    out.append("---\n")
    nodes = page.nodes
    if (
        len(nodes) == 1
        and nodes[0].kind == "section"
        and nodes[0].level == 1
        and nodes[0].title == page.h1
    ):
        nodes = nodes[0].children  # the h1 is already the document title
    elif nodes and nodes[0].kind == "section" and nodes[0].level == 1 and nodes[0].title == page.h1:
        nodes = [*nodes[0].children, *nodes[1:]]
    _md(nodes, 2, out)
    return "\n".join(out).rstrip() + "\n"


def markdown_name(url: str) -> str:
    """A safe, unique file name for a page's Markdown."""
    parts = urlsplit(url)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{parts.hostname or ''}{parts.path}").strip("-").lower()
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:60].rstrip('-') or 'page'}-{digest}.md"
