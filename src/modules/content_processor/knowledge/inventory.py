"""SEO and GEO inventory of every page of a run (RFC 0003, step A).

Input is a run's `UrlRecord`s plus the stored HTML of each page: what the server
sent (`src`) and every view the browser showed (`rendered`, `stateN`). Each view is
walked once; links, images, media and headings found in several views are merged,
and every item keeps the list of views it was seen in. Nothing is fetched and no
model is called.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from selectolax.parser import HTMLParser, Node

from src.modules.content_processor.adapters.html_source import extract_html_source
from src.modules.content_processor.adapters.regions import classify_region
from src.modules.content_processor.contracts import UrlRecord
from src.modules.content_processor.enums import UiRegion, UrlBadge
from src.modules.content_processor.knowledge.inventory_models import (
    AltStatus,
    CanonicalStatus,
    HeadFacts,
    Heading,
    ImageItem,
    InventoryStatus,
    Issue,
    LinkItem,
    LinkKind,
    MediaItem,
    PageInventory,
    SeenIn,
    Severity,
    StructuredItem,
)
from src.modules.content_processor.knowledge.stamp import PayloadLoader, page_key, rendered_views

__all__ = [
    "DESCRIPTION_MAX",
    "DESCRIPTION_MIN",
    "TITLE_MAX",
    "InventoryResult",
    "check_structured",
    "inventory_page",
    "inventory_records",
    "media_provider",
]

TITLE_MAX = 60
TITLE_MIN = 15
DESCRIPTION_MAX = 160
DESCRIPTION_MIN = 70
_TEXT_CHARS = 300
_EXAMPLES = 5
_NOT_CAPTURED = frozenset({UrlBadge.BLOCKED, UrlBadge.FAILED, UrlBadge.EXCLUDED})
_SEEN_ORDER: dict[SeenIn, int] = {"source": 0, "rendered": 1, "interaction": 2}
_SKIP = frozenset({"script", "style", "template", "noscript", "head", "svg", "math"})
_NO_TEXT = frozenset({"script", "style", "template", "noscript", "svg"})
_HEADING_LEVEL = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_CHROME_REGIONS = frozenset(
    {
        UiRegion.HEADER,
        UiRegion.FOOTER,
        UiRegion.NAV_MENU,
        UiRegion.HAMBURGER_MENU,
        UiRegion.MEGA_MENU,
        UiRegion.COOKIE_BANNER,
        UiRegion.CHAT_WIDGET,
    }
)
_NEW_TAB = re.compile(r"\s*\(opens? in (a )?new (tab|window)\)", re.IGNORECASE)
_SPACES = re.compile(r"\s+")
_DOWNLOAD = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|odt|ods|odp|rtf|csv|txt|zip|epub)$", re.IGNORECASE
)
_FILE_NAME_ALT = re.compile(
    r"\.(jpe?g|png|gif|webp|avif|svg|bmp|tiff?)$|^(img|image|dsc|photo|pic|screenshot)[-_ ]?\d+$"
    r"|^[\w-]*\d{3,}[\w-]*$",
    re.IGNORECASE,
)
_GENERIC_ANCHOR = re.compile(
    r"^(click here|here|read more|learn more|more|find out more|see more|details|"
    r"this link|link|go|continue|view|view more)\.?$",
    re.IGNORECASE,
)
_LAZY_SRC = ("data-src", "data-lazy-src", "data-original", "data-lazy")
_PROVIDERS = (
    ("youtube.com", "YouTube"),
    ("youtube-nocookie.com", "YouTube"),
    ("youtu.be", "YouTube"),
    ("vimeo.com", "Vimeo"),
    ("wistia.", "Wistia"),
    ("vidyard.com", "Vidyard"),
    ("brightcove.", "Brightcove"),
    ("loom.com", "Loom"),
    ("google.com/maps", "Google Maps"),
    ("maps.google.", "Google Maps"),
    ("spotify.com", "Spotify"),
    ("soundcloud.com", "SoundCloud"),
    ("podbean.com", "Podbean"),
    ("buzzsprout.com", "Buzzsprout"),
    ("twitter.com", "X"),
    ("x.com", "X"),
    ("instagram.com", "Instagram"),
    ("facebook.com", "Facebook"),
    ("linkedin.com", "LinkedIn"),
    ("tiktok.com", "TikTok"),
    ("hubspot", "HubSpot"),
    ("typeform.com", "Typeform"),
    ("calendly.com", "Calendly"),
    ("googletagmanager.com", "Google Tag Manager"),
    ("doubleclick.net", "Google Ads"),
)

# Google's rich-result fields, as of its 2026 structured-data documentation. Each entry
# is a list of alternatives: any one of the names satisfies it.
_RULES: dict[str, tuple[list[tuple[str, ...]], list[tuple[str, ...]]]] = {
    "Product": (
        [("name",), ("offers", "review", "aggregateRating")],
        [
            ("image",),
            ("description",),
            ("brand",),
            ("sku", "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "mpn"),
        ],
    ),
    "ProductGroup": ([("name",)], [("hasVariant",), ("variesBy",), ("productGroupID",)]),
    "Offer": (
        [("price", "priceSpecification"), ("priceCurrency", "priceSpecification")],
        [("availability",)],
    ),
    "AggregateOffer": ([("lowPrice",), ("priceCurrency",)], [("highPrice",), ("offerCount",)]),
    "AggregateRating": ([("ratingValue",), ("ratingCount", "reviewCount")], []),
    "Review": ([("author",), ("reviewRating",)], []),
    "Article": (
        [],
        [("headline",), ("image",), ("datePublished",), ("dateModified",), ("author",)],
    ),
    "NewsArticle": (
        [],
        [("headline",), ("image",), ("datePublished",), ("dateModified",), ("author",)],
    ),
    "BlogPosting": (
        [],
        [("headline",), ("image",), ("datePublished",), ("dateModified",), ("author",)],
    ),
    "FAQPage": ([("mainEntity",)], []),
    "Question": ([("name",), ("acceptedAnswer", "suggestedAnswer")], []),
    "Answer": ([("text",)], []),
    "BreadcrumbList": ([("itemListElement",)], []),
    "Organization": ([], [("name",), ("url",), ("logo",)]),
    "LocalBusiness": (
        [("name",), ("address",)],
        [("telephone",), ("openingHoursSpecification", "openingHours"), ("geo",)],
    ),
    "VideoObject": (
        [("name",), ("thumbnailUrl",), ("uploadDate",)],
        [("description",), ("contentUrl", "embedUrl"), ("duration",)],
    ),
    "Event": ([("name",), ("startDate",), ("location",)], [("endDate",), ("offers",)]),
    "JobPosting": (
        [
            ("title",),
            ("description",),
            ("datePosted",),
            ("hiringOrganization",),
            ("jobLocation", "applicantLocationRequirements"),
        ],
        [("validThrough",), ("employmentType",), ("baseSalary",)],
    ),
    "HowTo": ([("name",), ("step",)], []),
    "Recipe": ([("name",), ("image",)], []),
    "Course": ([("name",), ("description",)], [("provider",)]),
    "SoftwareApplication": ([("name",), ("offers",)], [("aggregateRating", "review")]),
    "WebSite": ([], [("name",), ("url",)]),
    "Service": ([], [("name",), ("provider",), ("description",)]),
}


# -- small helpers ---------------------------------------------------------------------


def _clean(text: str | None) -> str:
    return _SPACES.sub(" ", _NEW_TAB.sub("", text or "")).strip()


def _short(text: str | None, limit: int = _TEXT_CHARS) -> str | None:
    cleaned = _clean(text)
    return cleaned[:limit] if cleaned else None


def _text_of(node: Node) -> str:
    """Readable text of an element, without scripts, styles and icon markup."""
    parts: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.tag == "-text":
            parts.append(current.text(deep=False) or "")
            continue
        if current is not node and current.tag in _NO_TEXT:
            continue
        stack.extend(reversed(list(current.iter(include_text=True))))
    return _clean(" ".join(parts))


def _attr(node: Node, name: str) -> str | None:
    value = node.attributes.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int(value: str | None) -> int | None:
    match = re.match(r"\s*(\d+)", value or "")
    return int(match.group(1)) if match else None


def _hidden(node: Node) -> bool:
    a = node.attributes
    style = (a.get("style") or "").replace(" ", "").lower()
    return (
        "hidden" in a
        or a.get("aria-hidden") == "true"
        or "display:none" in style
        or "visibility:hidden" in style
    )


def _site(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _without_fragment(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


def _same_page(a: str, b: str) -> bool:
    sa, sb = urlsplit(a), urlsplit(b)
    return (sa.hostname or "", sa.path.rstrip("/"), sa.query) == (
        sb.hostname or "",
        sb.path.rstrip("/"),
        sb.query,
    )


def _link_kind(href: str, url: str, page_url: str) -> LinkKind:
    low = href.lower()
    if low.startswith("javascript:"):
        return "javascript"
    if low.startswith("mailto:"):
        return "mailto"
    if low.startswith("tel:"):
        return "tel"
    if urlsplit(url).scheme not in ("http", "https"):
        return "other"
    if low.startswith("#") or ("#" in url and _same_page(url, page_url)):
        return "same_page"
    if _DOWNLOAD.search(urlsplit(url).path):
        return "download"
    host = urlsplit(url).hostname or ""
    page_host = urlsplit(page_url).hostname or ""
    if host == page_host:
        return "internal"
    site = _site(page_host)
    if _site(host) == site or host.endswith("." + site):
        return "subdomain"
    return "external"


def _alt_status(alt: str | None, src: str) -> AltStatus:
    if alt is None:
        return "missing"
    if not alt.strip():
        return "empty"
    stem = urlsplit(src).path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    if _FILE_NAME_ALT.search(alt.strip()) or (stem and alt.strip().lower() == stem):
        return "file_name"
    return "present"


def media_provider(url: str) -> str:
    """Who serves an embedded video, map, form or widget."""
    low = url.lower()
    for marker, name in _PROVIDERS:
        if marker in low:
            return name
    host = urlsplit(url).hostname or ""
    return _site(host) or "inline"


def _type_names(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else [value]
    return [
        t.rsplit("/", 1)[-1].rsplit("#", 1)[-1].rsplit(":", 1)[-1]
        for t in raw
        if isinstance(t, str)
    ]


# -- walking one view ------------------------------------------------------------------


@dataclass
class _Found:
    key: tuple[str, ...]
    fields: dict[str, Any]
    hidden: bool


@dataclass
class _Scan:
    links: list[_Found] = field(default_factory=list)
    images: list[_Found] = field(default_factory=list)
    media: list[_Found] = field(default_factory=list)
    headings: list[Heading] = field(default_factory=list)
    inline_images: int = 0


@dataclass
class _Walk:
    node: Node
    region: UiRegion
    hidden: bool
    link: str | None
    figure: Node | None


def _image_url(node: Node, base_url: str) -> str | None:
    src = _attr(node, "src")
    if src is None or src.startswith("data:"):
        src = next((v for name in _LAZY_SRC if (v := _attr(node, name))), src)
    if src is None:
        srcset = _attr(node, "srcset") or _attr(node, "data-srcset") or ""
        src = srcset.split(",")[0].strip().split(" ")[0] or None
    if src is None or src.startswith("data:"):
        return None
    return urljoin(base_url, src)


def _srcset_candidates(node: Node) -> int:
    def count(n: Node) -> int:
        srcset = _attr(n, "srcset") or _attr(n, "data-srcset") or ""
        return len([c for c in srcset.split(",") if c.strip()])

    total = count(node)
    parent = node.parent
    if parent is not None and parent.tag == "picture":
        total += sum(count(s) for s in parent.css("source"))
    return total


def _caption(figure: Node | None) -> str | None:
    if figure is None:
        return None
    caption = figure.css_first("figcaption")
    return _short(_text_of(caption)) if caption is not None else None


def _media_src(node: Node, base_url: str) -> str | None:
    src = _attr(node, "src") or _attr(node, "data-src") or _attr(node, "data")
    if src is None and node.tag in ("video", "audio"):
        source = node.css_first("source[src]")
        src = _attr(source, "src") if source is not None else None
    if src is None or src in ("about:blank",) or src.startswith("data:"):
        return None
    return urljoin(base_url, src)


def _scan(tree: HTMLParser, page_url: str) -> _Scan:
    scan = _Scan()
    root = tree.body or tree.root
    if root is None:
        return scan
    heading: str | None = None
    stack = [_Walk(root, UiRegion.UNKNOWN, False, None, None)]
    while stack:
        w = stack.pop()
        node, tag = w.node, w.node.tag
        if tag in _SKIP:
            continue
        region = classify_region(tag, node.attributes, w.region) if node is not root else w.region
        hidden = w.hidden or _hidden(node)
        link, figure = w.link, (node if tag == "figure" else w.figure)
        where = {"ui_region": str(region), "heading": heading}
        if tag in _HEADING_LEVEL and not hidden:
            text = _text_of(node)
            if text:
                heading = text[:_TEXT_CHARS]
                scan.headings.append(
                    Heading(level=_HEADING_LEVEL[tag], text=heading, ui_region=region)
                )
        elif tag in ("a", "area") and (href := _attr(node, "href")) is not None:
            url = href if href.lower().startswith(("javascript:",)) else urljoin(page_url, href)
            rel = sorted(set((_attr(node, "rel") or "").lower().split()))
            img = node.css_first("img")
            image_alt = _short(_attr(img, "alt")) if img is not None else None
            anchor = _text_of(node) if tag == "a" else _clean(_attr(node, "alt"))
            kind = _link_kind(href, url, page_url)
            if kind not in ("javascript",):
                link = url
            fields: dict[str, Any] = {
                "url": url[:2000],
                "anchor_text": anchor[:_TEXT_CHARS],
                "title": _short(_attr(node, "title")),
                "aria_label": _short(_attr(node, "aria-label")),
                "image_alt": image_alt,
                "kind": kind,
                "rel": rel,
                "nofollow": "nofollow" in rel,
                "sponsored": "sponsored" in rel,
                "ugc": "ugc" in rel,
                "new_tab": (_attr(node, "target") or "").lower() == "_blank",
                "insecure": page_url.startswith("https:") and url.startswith("http:"),
                **where,
            }
            scan.links.append(_Found((url, anchor), fields, hidden))
        elif tag == "img":
            src = _image_url(node, page_url)
            if src is None:
                scan.inline_images += 1
            else:
                alt = node.attributes.get("alt")
                if alt is None and "alt" in node.attributes:
                    alt = ""  # alt="" is parsed as an attribute without a value
                fields = {
                    "url": src[:2000],
                    "alt": _short(alt),
                    "alt_status": _alt_status(alt, src),
                    "title": _short(_attr(node, "title")),
                    "caption": _caption(figure),
                    "width": _int(_attr(node, "width")),
                    "height": _int(_attr(node, "height")),
                    "srcset_candidates": _srcset_candidates(node),
                    "loading": _attr(node, "loading"),
                    "fetchpriority": _attr(node, "fetchpriority"),
                    "link_url": link,
                    **where,
                }
                scan.images.append(_Found((src,), fields, hidden))
        elif tag in ("video", "audio", "iframe", "embed", "object"):
            src = _media_src(node, page_url)
            if src is not None:
                media_kind = "embed" if tag in ("embed", "object") else tag
                tracks = node.css("track") if tag in ("video", "audio") else []
                fields = {
                    "kind": media_kind,
                    "url": src[:2000],
                    "provider": media_provider(src),
                    "title": _short(_attr(node, "title") or _attr(node, "aria-label")),
                    "poster": urljoin(page_url, p) if (p := _attr(node, "poster")) else None,
                    "captions": any(
                        (_attr(t, "kind") or "subtitles") in ("captions", "subtitles")
                        for t in tracks
                    ),
                    **where,
                }
                scan.media.append(_Found((media_kind, src), fields, hidden))
        children = list(node.iter(include_text=False))
        stack.extend(_Walk(c, region, hidden, link, figure) for c in reversed(children))
    return scan


# -- merging views ---------------------------------------------------------------------


@dataclass
class _View:
    channel: str
    seen: SeenIn
    tree: HTMLParser
    scan: _Scan


def _merge(views: Sequence[_View], pick: str) -> list[dict[str, Any]]:
    merged: dict[tuple[str, ...], dict[str, Any]] = {}
    for view in views:
        found: list[_Found] = getattr(view.scan, pick)
        counts = Counter(f.key for f in found)
        hidden_here: dict[tuple[str, ...], bool] = {}
        for f in found:
            hidden_here[f.key] = hidden_here.get(f.key, True) and f.hidden
            if f.key not in merged:
                merged[f.key] = {**f.fields, "seen": set(), "hidden_all": True, "occurrences": 1}
        for key, n in counts.items():
            entry = merged[key]
            entry["seen"].add(view.seen)
            entry["hidden_all"] = entry["hidden_all"] and hidden_here[key]
            entry["occurrences"] = max(entry["occurrences"], n)
    out: list[dict[str, Any]] = []
    for order, entry in enumerate(merged.values()):
        seen = entry.pop("seen")
        if "rendered" in seen:
            seen.discard("interaction")
        entry["seen_in"] = sorted(seen, key=lambda s: _SEEN_ORDER[s])
        entry["hidden"] = entry.pop("hidden_all")
        entry["order"] = order
        out.append(entry)
    return out


# -- head ------------------------------------------------------------------------------


def _metas(tree: HTMLParser) -> Iterator[tuple[str, str]]:
    for node in tree.css("meta"):
        name = (_attr(node, "name") or _attr(node, "property") or "").lower()
        content = _attr(node, "content")
        if name and content:
            yield name, content


def _canonical(tree: HTMLParser, page_url: str) -> tuple[str | None, CanonicalStatus]:
    found: list[str] = []
    for node in tree.css("link[rel]"):
        rel = (_attr(node, "rel") or "").lower().split()
        href = _attr(node, "href")
        if "canonical" in rel and href:
            url = urljoin(page_url, href)
            if url not in found:
                found.append(url)
    if not found:
        return None, "missing"
    if len(found) > 1:
        return found[0], "multiple"
    url = found[0]
    if _without_fragment(url).lower() == _without_fragment(page_url).lower():
        return url, "self"
    if (urlsplit(url).hostname or "") == (urlsplit(page_url).hostname or ""):
        return url, "other_page"
    return url, "other_site"


def _title(tree: HTMLParser) -> str | None:
    node = tree.css_first("title")
    return _short(node.text(deep=True), 500) if node is not None else None


def _head(source: HTMLParser, rendered: HTMLParser | None, page_url: str) -> HeadFacts:
    head = HeadFacts()
    head.title = _title(source)
    head.title_length = len(head.title or "")
    head.canonical, head.canonical_status = _canonical(source, page_url)
    if rendered is not None:
        title_after = _title(rendered)
        if title_after and title_after != head.title:
            head.title_rendered = title_after
        canonical_after, _ = _canonical(rendered, page_url)
        if canonical_after and canonical_after != head.canonical:
            head.canonical_rendered = canonical_after
    for name, content in _metas(source):
        if name == "description" and head.description is None:
            head.description = _short(content, 1000)
            head.description_length = len(head.description or "")
        elif name == "robots" or name.endswith("bot") or name.startswith("googlebot"):
            directives = [d.strip().lower() for d in content.split(",") if d.strip()]
            head.robots.setdefault(name, []).extend(directives)
        elif name.startswith("og:"):
            head.open_graph.setdefault(name[3:], content[:500])
        elif name.startswith("twitter:"):
            head.twitter.setdefault(name[8:], content[:500])
        elif name == "viewport":
            head.viewport = content[:200]
        elif name == "generator":
            head.generator = content[:200]
    general = [d for bot in ("robots", "googlebot") for d in head.robots.get(bot, [])]
    head.indexable = not any(d in ("noindex", "none") for d in general)
    head.followable = not any(d in ("nofollow", "none") for d in general)
    html = source.css_first("html")
    head.lang = _attr(html, "lang") if html is not None else None
    charset = source.css_first("meta[charset]")
    head.charset = _attr(charset, "charset") if charset is not None else None
    for node in source.css("link[rel]"):
        rel = (_attr(node, "rel") or "").lower().split()
        href = _attr(node, "href")
        if not href:
            continue
        url = urljoin(page_url, href)
        kind = (_attr(node, "type") or "").lower()
        if "alternate" in rel and (lang := _attr(node, "hreflang")):
            head.hreflang.setdefault(lang.lower(), url)
        elif "alternate" in rel and kind in ("application/rss+xml", "application/atom+xml"):
            head.feeds.append(url)
        elif "icon" in rel and head.favicon is None:
            head.favicon = url
        elif "amphtml" in rel:
            head.amphtml = url
    return head


def _outline(head: HeadFacts, headings: list[Heading]) -> None:
    head.headings = headings
    head.h1 = [h.text for h in headings if h.level == 1]
    previous: int | None = None
    for h in headings:
        if h.ui_region in {str(r) for r in _CHROME_REGIONS}:
            continue
        if previous is not None and h.level > previous + 1:
            head.skipped_heading_levels.append(f"h{previous} -> h{h.level} at '{h.text[:80]}'")
        previous = h.level


# -- structured data -------------------------------------------------------------------


def _missing(node: dict[str, Any], rules: list[tuple[str, ...]], prefix: str) -> list[str]:
    return [
        prefix + " or ".join(alt)
        for alt in rules
        if not any(node.get(name) not in (None, "", [], {}) for name in alt)
    ]


def check_structured(
    node: dict[str, Any], prefix: str = "", depth: int = 0
) -> tuple[list[str], list[str]]:
    """Missing required and recommended fields of a JSON-LD node and the nodes inside it."""
    required: list[str] = []
    recommended: list[str] = []
    for kind in _type_names(node.get("@type")):
        if kind in _RULES:
            req, rec = _RULES[kind]
            required += _missing(node, req, prefix)
            recommended += _missing(node, rec, prefix)
    if depth < 4:
        for key, value in node.items():
            if key.startswith("@"):
                continue
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, dict) and child.get("@type"):
                    r, c = check_structured(child, f"{prefix}{key}.", depth + 1)
                    required += [x for x in r if x not in required]
                    recommended += [x for x in c if x not in recommended]
    return required, recommended


def _json_ld_items(blobs: Iterable[str]) -> tuple[list[StructuredItem], list[str]]:
    items: list[StructuredItem] = []
    errors: list[str] = []
    for n, raw in enumerate(blobs, start=1):
        try:
            data = json.loads(raw)
        except ValueError as exc:
            errors.append(f"JSON-LD block {n} is not valid JSON: {str(exc)[:120]}")
            continue
        nodes: list[Any] = data if isinstance(data, list) else [data]
        expanded: list[Any] = []
        for node in nodes:
            if isinstance(node, dict) and isinstance(node.get("@graph"), list):
                expanded.extend(node["@graph"])
            else:
                expanded.append(node)
        for node in expanded:
            if not isinstance(node, dict):
                continue
            required, recommended = check_structured(node)
            name = node.get("name") or node.get("headline")
            items.append(
                StructuredItem(
                    syntax="json-ld",
                    types=_type_names(node.get("@type")) or ["(no type)"],
                    name=_short(name) if isinstance(name, str) else None,
                    properties=sorted(k for k in node if not k.startswith("@")),
                    missing_required=required,
                    missing_recommended=recommended,
                    data=node,
                )
            )
    return items, errors


def _attribute_items(tree: HTMLParser) -> list[StructuredItem]:
    """Top-level microdata items and RDFa resources (property names only)."""
    items: list[StructuredItem] = []
    for syntax, scope, type_attr, prop_attr in (
        ("microdata", "[itemscope][itemtype]", "itemtype", "itemprop"),
        ("rdfa", "[typeof]", "typeof", "property"),
    ):
        for node in tree.css(scope):
            marker = "typeof" if syntax == "rdfa" else "itemscope"
            parent = node.parent
            while parent is not None and marker not in parent.attributes:
                parent = parent.parent
            if parent is not None:
                continue
            types = _type_names((_attr(node, type_attr) or "").split())
            props = sorted(
                {
                    p.rsplit(":", 1)[-1]
                    for d in node.css(f"[{prop_attr}]")
                    for p in (_attr(d, prop_attr) or "").split()
                }
            )
            present = dict.fromkeys(props, True)
            required: list[str] = []
            recommended: list[str] = []
            for kind in types:
                if kind in _RULES:
                    required += _missing(present, _RULES[kind][0], "")
                    recommended += _missing(present, _RULES[kind][1], "")
            items.append(
                StructuredItem(
                    syntax=syntax,
                    types=types or ["(no type)"],
                    properties=props,
                    missing_required=required,
                    missing_recommended=recommended,
                )
            )
    return items


# -- issues ----------------------------------------------------------------------------

_ISSUES: dict[str, tuple[Severity, str]] = {
    "title_missing": ("error", "The page has no title."),
    "title_too_long": ("warning", f"Title longer than {TITLE_MAX} characters; results cut it."),
    "title_too_short": ("notice", f"Title shorter than {TITLE_MIN} characters."),
    "title_changed_by_scripts": (
        "notice",
        "Scripts change the title after loading; crawlers that run no scripts see the original.",
    ),
    "description_missing": (
        "warning",
        "No meta description; search engines and AI tools write their own summary.",
    ),
    "description_too_long": (
        "notice",
        f"Meta description longer than {DESCRIPTION_MAX} characters.",
    ),
    "description_too_short": (
        "notice",
        f"Meta description shorter than {DESCRIPTION_MIN} characters.",
    ),
    "h1_missing": ("warning", "No main heading (h1)."),
    "h1_multiple": ("notice", "More than one main heading (h1)."),
    "heading_levels_skipped": (
        "notice",
        "Heading levels are skipped, which blurs the outline for crawlers and screen readers.",
    ),
    "canonical_missing": ("notice", "No canonical link."),
    "canonical_multiple": (
        "error",
        "More than one canonical link; search engines may ignore all of them.",
    ),
    "canonical_other_site": ("warning", "The canonical points to another site."),
    "canonical_other_page": (
        "notice",
        "The canonical points to another page, so this one is not meant to be indexed.",
    ),
    "canonical_changed_by_scripts": (
        "warning",
        "Scripts change the canonical link after the page loads.",
    ),
    "noindex": ("warning", "The page asks search engines not to index it."),
    "lang_missing": ("warning", "The page does not declare its language."),
    "hreflang_without_self": (
        "notice",
        "Language alternates are listed, but not this page itself.",
    ),
    "open_graph_incomplete": (
        "notice",
        "Open Graph (link previews) lacks a title, description or image.",
    ),
    "links_without_text": (
        "warning",
        "Links with no text, label or image alt: nothing says what they point to.",
    ),
    "links_generic_text": (
        "notice",
        "Links whose text says nothing about the target ('read more', 'click here').",
    ),
    "internal_links_nofollow": ("warning", "Links to the site's own pages marked nofollow."),
    "links_insecure": ("warning", "Links to http:// addresses from a secure page."),
    "links_need_scripts": (
        "notice",
        "Links to the site's own pages that exist only after scripts run; crawlers that run "
        "no scripts (many AI crawlers) cannot follow them.",
    ),
    "links_behind_clicks": (
        "notice",
        "Links that only appear after a click (tab, accordion, 'show more').",
    ),
    "images_alt_missing": ("warning", "Images without an alt attribute."),
    "images_alt_file_name": (
        "warning",
        "Images whose alt text is a file name or code, not a description.",
    ),
    "images_alt_empty": (
        "notice",
        'Content images marked as decoration (alt=""): crawlers and AI tools learn nothing '
        "from them; check the ones that carry meaning.",
    ),
    "images_without_dimensions": (
        "notice",
        "Images without width and height; the layout can jump while they load.",
    ),
    "videos_without_captions": ("notice", "Self-hosted videos without a captions track."),
    "structured_data_invalid": ("error", "Structured data that cannot be read."),
    "structured_data_required_missing": (
        "error",
        "Structured data missing fields Google requires for rich results.",
    ),
    "structured_data_recommended_missing": (
        "notice",
        "Structured data missing recommended fields.",
    ),
    "structured_data_missing": ("notice", "No structured data (schema.org) on the page."),
}


def _issue(out: list[Issue], code: str, examples: Sequence[str] = ()) -> None:
    severity, message = _ISSUES[code]
    out.append(
        Issue(
            code=code,
            severity=severity,
            message=message,
            count=max(1, len(examples)),
            examples=[e for e in examples if e][:_EXAMPLES],
        )
    )


def _head_issues(out: list[Issue], head: HeadFacts, url: str) -> None:
    if not head.title:
        _issue(out, "title_missing")
    elif head.title_length > TITLE_MAX:
        _issue(out, "title_too_long", [head.title])
    elif head.title_length < TITLE_MIN:
        _issue(out, "title_too_short", [head.title])
    if head.title_rendered:
        _issue(out, "title_changed_by_scripts", [head.title_rendered])
    if not head.description:
        _issue(out, "description_missing")
    elif head.description_length > DESCRIPTION_MAX:
        _issue(out, "description_too_long")
    elif head.description_length < DESCRIPTION_MIN:
        _issue(out, "description_too_short")
    if not head.h1:
        _issue(out, "h1_missing")
    elif len(head.h1) > 1:
        _issue(out, "h1_multiple", head.h1)
    if head.skipped_heading_levels:
        _issue(out, "heading_levels_skipped", head.skipped_heading_levels)
    canonical = {
        "missing": "canonical_missing",
        "multiple": "canonical_multiple",
        "other_site": "canonical_other_site",
        "other_page": "canonical_other_page",
    }.get(head.canonical_status)
    if canonical:
        _issue(out, canonical, [head.canonical or ""])
    if head.canonical_rendered:
        _issue(out, "canonical_changed_by_scripts", [head.canonical_rendered])
    if not head.indexable:
        _issue(out, "noindex")
    if not head.lang:
        _issue(out, "lang_missing")
    own = _without_fragment(url)
    if head.hreflang and not any(_without_fragment(u) == own for u in head.hreflang.values()):
        _issue(out, "hreflang_without_self")
    missing_og = [k for k in ("title", "description", "image") if k not in head.open_graph]
    if missing_og:
        _issue(out, "open_graph_incomplete", missing_og)


def _link_issues(out: list[Issue], inv: PageInventory, has_both: bool) -> None:
    chrome = {str(r) for r in _CHROME_REGIONS}
    content = [link for link in inv.links if link.ui_region not in chrome]
    own_site = ("internal", "subdomain")
    checks: list[tuple[str, list[str]]] = [
        (
            "links_without_text",
            [
                link.url
                for link in inv.links
                if link.kind != "javascript"
                and not (link.anchor_text or link.aria_label or link.image_alt or link.title)
            ],
        ),
        (
            "links_generic_text",
            [
                f"{link.anchor_text} -> {link.url}"
                for link in content
                if _GENERIC_ANCHOR.match(link.anchor_text)
            ],
        ),
        (
            "internal_links_nofollow",
            [link.url for link in inv.links if link.nofollow and link.kind in own_site],
        ),
        ("links_insecure", [link.url for link in inv.links if link.insecure]),
        (
            "links_need_scripts",
            [
                link.url
                for link in inv.links
                if has_both
                and link.kind in own_site
                and "source" not in link.seen_in
                and "rendered" in link.seen_in
            ],
        ),
        ("links_behind_clicks", [link.url for link in content if link.seen_in == ["interaction"]]),
    ]
    for code, found in checks:
        if found:
            _issue(out, code, found)


def _self_hosted(url: str, page_url: str) -> bool:
    if url.startswith("blob:"):
        return True
    host, page_host = urlsplit(url).hostname or "", urlsplit(page_url).hostname or ""
    return _site(host) == _site(page_host) or host.endswith("." + _site(page_host))


def _image_issues(out: list[Issue], inv: PageInventory) -> None:
    shown = [i for i in inv.images if not i.hidden]
    chrome = {str(r) for r in _CHROME_REGIONS}
    checks: list[tuple[str, list[str]]] = [
        ("images_alt_missing", [i.url for i in shown if i.alt_status == "missing"]),
        (
            "images_alt_empty",
            [i.url for i in shown if i.alt_status == "empty" and i.ui_region not in chrome],
        ),
        (
            "images_alt_file_name",
            [f"{i.alt} ({i.url})" for i in shown if i.alt_status == "file_name"],
        ),
        (
            "images_without_dimensions",
            [i.url for i in shown if i.width is None or i.height is None],
        ),
        (
            "videos_without_captions",
            [
                m.url
                for m in inv.media
                if m.kind == "video" and not m.captions and _self_hosted(m.url, inv.url)
            ],
        ),
    ]
    for code, found in checks:
        if found:
            _issue(out, code, found)


def _schema_issues(out: list[Issue], inv: PageInventory) -> None:
    if inv.structured_data_errors:
        _issue(out, "structured_data_invalid", inv.structured_data_errors)
    items = inv.structured_data
    required = [f"{'/'.join(s.types)}: {m}" for s in items for m in s.missing_required]
    if required:
        _issue(out, "structured_data_required_missing", required)
    recommended = [f"{'/'.join(s.types)}: {m}" for s in items for m in s.missing_recommended]
    if recommended:
        _issue(out, "structured_data_recommended_missing", recommended)
    if not items and not inv.structured_data_errors:
        _issue(out, "structured_data_missing")


def _issues(inv: PageInventory, has_source: bool, has_rendered: bool) -> list[Issue]:
    out: list[Issue] = []
    _head_issues(out, inv.head, inv.url)
    _link_issues(out, inv, has_source and has_rendered)
    _image_issues(out, inv)
    _schema_issues(out, inv)
    return out


# -- pages -----------------------------------------------------------------------------


def _counts(inv: PageInventory) -> dict[str, int]:
    kinds = Counter(link.kind for link in inv.links)
    alts = Counter(i.alt_status for i in inv.images)
    sev = Counter(i.severity for i in inv.issues)
    return {
        "links": len(inv.links),
        "links_internal": kinds["internal"] + kinds["subdomain"],
        "links_external": kinds["external"],
        "links_download": kinds["download"],
        "links_nofollow": sum(1 for link in inv.links if link.nofollow),
        "links_behind_clicks": sum(1 for link in inv.links if link.seen_in == ["interaction"]),
        "links_need_scripts": sum(
            1 for link in inv.links if "src" in inv.views and "source" not in link.seen_in
        ),
        "images": len(inv.images),
        "images_alt_missing": alts["missing"],
        "images_alt_empty": alts["empty"],
        "images_alt_file_name": alts["file_name"],
        "media": len(inv.media),
        "structured_items": len(inv.structured_data),
        "headings": len(inv.head.headings),
        "issues_error": sev["error"],
        "issues_warning": sev["warning"],
        "issues_notice": sev["notice"],
    }


def inventory_page(
    url: str,
    source_html: bytes | None,
    rendered: Sequence[tuple[str, bytes]],
    *,
    run_id: str,
    now: datetime,
    media_type: str | None = "text/html",
) -> PageInventory:
    """Inventory one page from its source HTML and its rendered views (channel, HTML)."""
    inv = PageInventory(
        run_id=run_id,
        url=url,
        page_key=page_key(url),
        inventoried_at=now,
        status="inventoried",
        media_type=media_type,
    )
    views: list[_View] = []
    for channel, body in rendered:
        tree = HTMLParser(body)
        seen: SeenIn = "rendered" if channel == "rendered" else "interaction"
        views.append(_View(channel, seen, tree, _scan(tree, url)))
    source_tree = HTMLParser(source_html) if source_html is not None else None
    if source_tree is not None:
        views.append(_View("src", "source", source_tree, _scan(source_tree, url)))
    if not views:
        inv.status = "missing_evidence"
        return inv
    inv.views = [v.channel for v in views]
    base = next((v for v in views if v.seen == "rendered"), None)
    primary = base or views[0]
    head_tree = source_tree or primary.tree
    inv.head = _head(
        head_tree, base.tree if base is not None and source_tree is not None else None, url
    )
    _outline(inv.head, primary.scan.headings)
    inv.links = [LinkItem(**f) for f in _merge(views, "links")]
    inv.images = [ImageItem(**f) for f in _merge(views, "images")]
    inv.media = [MediaItem(**f) for f in _merge(views, "media")]
    blobs = (
        extract_html_source(
            source_html, base_url=url, evidence_id="inventory", resource_id="inventory"
        ).structured_data
        if source_html is not None
        else []
    )
    if source_tree is None and base is not None:
        blobs = [s.text() for s in base.tree.css('script[type="application/ld+json"]')]
    inv.structured_data, inv.structured_data_errors = _json_ld_items(blobs)
    inv.structured_data += _attribute_items(head_tree)
    inv.issues = _issues(inv, has_source=source_tree is not None, has_rendered=base is not None)
    inv.counts = _counts(inv)
    return inv


@dataclass
class InventoryResult:
    """The run's page inventories and totals."""

    pages: list[PageInventory]
    summary: dict[str, Any]


def _url_of(record: UrlRecord) -> str:
    return (record.final_url or record.requested_url).split("#", 1)[0]


def inventory_records(
    records: Sequence[UrlRecord], load_payload: PayloadLoader, *, now: datetime | None = None
) -> InventoryResult:
    """Inventory every page of a run from its stored HTML."""
    moment = now or datetime.now(UTC)
    pages: list[PageInventory] = []
    for record in records:
        url = _url_of(record)
        mime = record.sniffed_mime
        status: InventoryStatus | None = None
        if record.badge in _NOT_CAPTURED:
            status = "not_captured"
        elif "html" not in (mime or "").lower():
            status = "not_html"
        if status is not None:
            pages.append(
                PageInventory(
                    run_id=record.run_id,
                    url=url,
                    page_key=page_key(url),
                    inventoried_at=moment,
                    status=status,
                    media_type=mime,
                )
            )
            continue
        body = load_payload(record.payload_sha256) if record.payload_sha256 else None
        views = [
            (c, v) for c, sha in rendered_views(record) if (v := load_payload(sha)) is not None
        ]
        pages.append(
            inventory_page(url, body, views, run_id=record.run_id, now=moment, media_type=mime)
        )
    return InventoryResult(pages, _summary(pages))


def _summary(pages: Sequence[PageInventory]) -> dict[str, Any]:
    done = [p for p in pages if p.status == "inventoried"]
    totals: Counter[str] = Counter()
    for p in done:
        totals.update(p.counts)
    affected: Counter[str] = Counter(i.code for p in done for i in p.issues)
    return {
        "pages": len(pages),
        "inventoried": len(done),
        "not_captured": sum(1 for p in pages if p.status == "not_captured"),
        "not_html": sum(1 for p in pages if p.status == "not_html"),
        "missing_evidence": sum(1 for p in pages if p.status == "missing_evidence"),
        "totals": dict(sorted(totals.items())),
        "pages_with_issue": dict(affected.most_common()),
    }
