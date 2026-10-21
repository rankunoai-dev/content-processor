"""HTML source extraction: client-delivered HTML into layered content blocks.

This is the *source* channel (ARCHITECTURE.md section 3). It sees what the
server sent, not what a browser renders, so every block it emits is
`EXTRACTED_UNVERIFIED` until the rendered channel confirms it. Its job is to be
exhaustive and honest about layers: visible candidates, collapsed-but-published
content, hidden content, accessibility text, metadata, source-only strings,
icon-font artefacts and third-party widget text are kept apart (gap audit D3).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urljoin

from pydantic import Field
from selectolax.parser import HTMLParser, Node

from src.core.schemas import StrictModel
from src.modules.content_processor.adapters.regions import classify_region
from src.modules.content_processor.contracts import ContentBlock, Provenance
from src.modules.content_processor.enums import (
    ContentLayer,
    EntityStatus,
    SourceFormat,
    UiRegion,
)

__all__ = [
    "DiscoveredAsset",
    "DiscoveredLink",
    "SourceExtraction",
    "block_channel",
    "extract_html_source",
    "match_key",
]

TOOL_VERSION = "html_source/1b"
Role = Literal["main", "nav", "footer", "aside", "legal", "chrome", "unknown"]

_BLOCK_TAGS = frozenset(
    {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "td",
        "th",
        "dt",
        "dd",
        "blockquote",
        "pre",
        "figcaption",
        "summary",
        "caption",
        "div",
        "section",
        "article",
        "main",
        "header",
        "footer",
        "nav",
        "aside",
        "body",
        "ul",
        "ol",
        "table",
        "tr",
        "thead",
        "tbody",
        "tfoot",
        "form",
        "fieldset",
        "legend",
        "label",
        "details",
        "address",
        "button",
        "option",
        "title",
        "dl",
        "figure",
        "hr",
        "dialog",
        "menu",
        "hgroup",
        "search",
        "center",
    }
)
# Line breaks end a line of text (K22): "Management<br>A company" is two lines in the
# browser and must not become "ManagementA company".
_LINE_BREAK_TAGS = frozenset({"br"})


def _is_block_boundary(tag: str, attrs: dict[str, str | None]) -> bool:
    """Block elements, line breaks and custom elements (whole components) end a block.

    In rendered DOMs the browser annotator also marks inline elements that CSS lays
    out as blocks (`data-cp-block`); source HTML has no such marks.
    """
    return (
        tag in _BLOCK_TAGS
        or tag in _LINE_BREAK_TAGS
        or "-" in tag
        or attrs.get("data-cp-block") == "1"
    )


_SKIP_TAGS = frozenset({"style", "svg", "math", "canvas", "head", "link", "meta"})
_HEADING = re.compile(r"^h([1-6])$")
_ICON_CLASS = re.compile(
    r"(^|\s)(material-icons|material-symbols[\w-]*|fa|fas|far|fab|fal|fad|fa-[\w-]+|icon|"
    r"icon-[\w-]+|glyphicon[\w-]*|bi|bi-[\w-]+|ms-Icon[\w-]*)(\s|$)",
    re.IGNORECASE,
)
_THIRD_PARTY = re.compile(
    r"intercom|drift-|hubspot-messages|crisp-client|tawk|zendesk|livechat|olark|"
    r"cookie-?banner|cookie-?consent|cc-window|onetrust|osano|cookiebot|gdpr|consent-banner|"
    r"adsbygoogle|ad-slot|google_ads|dfp-|trustpilot|yotpo-|bazaarvoice",
    re.IGNORECASE,
)
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?![.\d])|"
    r"left\s*:\s*-\d{4,}px|text-indent\s*:\s*-\d{4,}px",
    re.IGNORECASE,
)
_ACTION_LINK = re.compile(
    r"logout|log-out|signout|sign-out|delete|remove|add-to-cart|addtocart|checkout|"
    r"unsubscribe|vote|like\b|/cart|wishlist|mark-as-read",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(r"lorem ipsum", re.IGNORECASE)
_SPA_ROOT_IDS = frozenset({"root", "app", "__next", "__nuxt", "app-root", "___gatsby"})
_INVISIBLE = {
    "­": "soft_hyphen",
    "​": "zero_width_space",
    "‌": "zero_width_non_joiner",
    "‍": "zero_width_joiner",
    "﻿": "bom",
}
_WS = re.compile(r"\s+")


class DiscoveredLink(StrictModel):
    """An anchor found in the source."""

    url: str
    text: str = ""
    rel: str = ""
    hidden: bool = False
    action_like: bool = False


class DiscoveredAsset(StrictModel):
    """An embedded resource reference found in the source."""

    url: str
    kind: Literal["image", "frame", "media", "stylesheet", "script", "object"]
    hidden: bool = False


class SourceExtraction(StrictModel):
    """Everything the source channel found for one HTML payload."""

    blocks: list[ContentBlock] = Field(default_factory=list)
    links: list[DiscoveredLink] = Field(default_factory=list)
    assets: list[DiscoveredAsset] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    structured_data: list[str] = Field(default_factory=list)
    template_fingerprint: str
    placeholder_suspected: bool = False
    needs_render_hint: bool = False
    warnings: list[str] = Field(default_factory=list)

    def blocks_by_layer(self) -> dict[str, int]:
        """Count blocks per layer."""
        counts: dict[str, int] = {}
        for b in self.blocks:
            counts[b.layer.value] = counts.get(b.layer.value, 0) + 1
        return counts


_SUPERSCRIPT = str.maketrans("0123456789+-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾")
_SUBSCRIPT = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
_SCRIPT_POSITION = {"sup": _SUPERSCRIPT, "sub": _SUBSCRIPT}
_SCRIPTABLE = re.compile(r"^[0-9+\-=()]{1,6}$")


def _shift_script(text: str, table: dict[int, int]) -> str | None:
    """Superscript or subscript digits and signs; None when the text is not only those."""
    stripped = text.strip()
    if not _SCRIPTABLE.match(stripped):
        return None
    return stripped.translate(table)


def block_channel(state_id: str) -> str:
    """Id segment for the extraction channel (K16): `src`, `rendered`, `state2`, ...

    Block ids are `<resource>:<channel>:<order>`, so the source, rendered and
    per-state extractions of one page never share an id or region id.
    """
    suffix = state_id.rsplit("|", 1)[-1] if "|" in state_id else state_id
    if suffix == "source":
        return "src"
    return re.sub(r"[^A-Za-z0-9_-]", "", suffix) or "src"


_UNSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎", "0123456789+-=()0123456789+-=()")


def match_key(text: str) -> str:
    """Comparison form for matching blocks against visible text.

    Normalized, case-folded, and with super/subscript digits folded to plain
    ones: blocks keep "$120¹" for the reader, while `innerText` shows "$1201".
    """
    return normalize_text(text)[0].translate(_UNSCRIPT).casefold()


def normalize_text(raw: str) -> tuple[str, list[str]]:
    """NFC, invisible code points removed, whitespace collapsed; ops are recorded."""
    ops: list[str] = []
    text = unicodedata.normalize("NFC", raw)
    if text != raw:
        ops.append("nfc")
    for ch, name in _INVISIBLE.items():
        if ch in text:
            text = text.replace(ch, "")
            ops.append(f"strip_{name}")
    text = text.replace(" ", " ")
    collapsed = _WS.sub(" ", text).strip()
    if collapsed != text.strip():
        ops.append("collapse_ws")
    return collapsed, ops


@dataclass
class _Ctx:
    layer: ContentLayer
    role: Role
    hidden_reason: str | None = None
    label: str | None = None
    region: UiRegion = UiRegion.UNKNOWN


@dataclass
class _Walker:
    base_url: str
    evidence_id: str
    resource_id: str
    state_id: str
    heading_path: list[str] = field(default_factory=list)
    blocks: list[ContentBlock] = field(default_factory=list)
    links: list[DiscoveredLink] = field(default_factory=list)
    assets: list[DiscoveredAsset] = field(default_factory=list)
    structured: list[str] = field(default_factory=list)
    skeleton: list[str] = field(default_factory=list)
    seen_keys: dict[str, str] = field(default_factory=dict)
    buffer: list[str] = field(default_factory=list)
    buffer_ctx: _Ctx | None = None
    order: int = 0
    script_count: int = 0
    spa_root: bool = False

    # -- emission -----------------------------------------------------------

    def flush(self) -> None:
        if not self.buffer or self.buffer_ctx is None:
            self.buffer = []
            return
        raw = "".join(self.buffer)
        self.buffer = []
        normalized, ops = normalize_text(raw)
        if not normalized:
            return
        self.emit(raw.strip(), normalized, ops, self.buffer_ctx)

    def emit(self, raw: str, normalized: str, ops: list[str], ctx: _Ctx) -> None:
        key_src = "/".join(self.heading_path) + "|" + normalized
        block_key = hashlib.sha256(key_src.encode("utf-8")).hexdigest()[:24]
        block_id = f"{self.resource_id}:{block_channel(self.state_id)}:{self.order}"
        dedupe_of = self.seen_keys.get(block_key)
        if dedupe_of is None:
            self.seen_keys[block_key] = block_id
        self.blocks.append(
            ContentBlock(
                block_id=block_id,
                block_key=block_key,
                resource_id=self.resource_id,
                state_id=self.state_id,
                region_id=block_id,
                heading_path=list(self.heading_path),
                reading_order=self.order,
                layer=ctx.layer,
                role=ctx.role,
                language=None,
                raw_text=raw[:20000],
                normalized_text=normalized[:20000],
                normalization_ops=ops,
                method="html_source",
                tool_version=TOOL_VERSION,
                confidence=1.0,
                status=EntityStatus.EXTRACTED_UNVERIFIED,
                evidence_ids=[self.evidence_id],
                provenance=Provenance(selector=ctx.label),
                dedupe_of=dedupe_of,
                source_format=SourceFormat.HTML,
                ui_region=(
                    UiRegion.PAGE_METADATA if ctx.layer is ContentLayer.METADATA else ctx.region
                ),
            )
        )
        self.order += 1

    # -- traversal ------------------------------------------------------------

    def append_text(self, text: str, ctx: _Ctx) -> None:
        """Add inline text to the open block; a change of context starts a new block."""
        if self.buffer_ctx is not None and self.buffer_ctx != ctx and self.buffer:
            self.flush()
        self.buffer_ctx = ctx
        self.buffer.append(text)

    def walk(self, node: Node, ctx: _Ctx, *, flush_end: bool = True) -> None:
        for child in node.iter(include_text=True):
            tag = child.tag
            if tag == "-text":
                text = child.text(deep=False)
                if text:
                    self.append_text(text, ctx)
                continue
            if tag == "_comment":
                self.flush()
                comment = (child.html or "")[4:-3].strip()
                if comment:
                    n, ops = normalize_text(comment)
                    self.emit(
                        comment,
                        n,
                        ops,
                        _Ctx(
                            ContentLayer.SOURCE_ONLY, ctx.role, label="comment", region=ctx.region
                        ),
                    )
                continue
            self.element(child, ctx)
        if flush_end:
            self.flush()

    def element(self, node: Node, parent: _Ctx) -> None:  # noqa: C901 - one dispatch table
        tag = node.tag or ""
        attrs = node.attributes
        if tag in _SKIP_TAGS:
            return
        if tag == "script":
            self.script_count += 1
            self.script(node, attrs, parent)
            return
        if len(self.skeleton) < 4000:
            self.skeleton.append(tag)
        if tag == "a" and attrs.get("href"):
            self.link(node, attrs, parent)
        self.asset(tag, attrs, parent)
        if (attrs.get("id") or "").lower() in _SPA_ROOT_IDS:
            self.spa_root = True

        ctx = self.context_for(tag, attrs, parent)
        is_block = _is_block_boundary(tag, attrs)
        if is_block:
            self.flush()
        heading = _HEADING.match(tag)
        if heading:
            level = int(heading.group(1))
            title, _ = normalize_text(node.text(deep=True) or "")
            del self.heading_path[level - 1 :]
            while len(self.heading_path) < level - 1:
                self.heading_path.append("")
            self.heading_path.append(title)

        if tag in ("template",):
            self.walk(
                node, _Ctx(ContentLayer.SOURCE_ONLY, ctx.role, label="template", region=ctx.region)
            )
            self.flush()
            return
        if tag == "noscript":
            self.walk(
                node, _Ctx(ContentLayer.SOURCE_ONLY, ctx.role, label="noscript", region=ctx.region)
            )
            self.flush()
            return
        if tag == "details" and "open" not in attrs:
            for child in node.iter(include_text=True):
                if child.tag == "summary":
                    self.walk(child, ctx)
                    self.flush()
                elif child.tag == "-text":
                    continue
                else:
                    self.element(
                        child,
                        _Ctx(
                            ContentLayer.COLLAPSED_PUBLISHED,
                            ctx.role,
                            label="details",
                            region=ctx.region,
                        ),
                    )
            self.flush()
            return

        self.attribute_text(tag, attrs, ctx)
        if tag in _SCRIPT_POSITION and ctx.layer is not ContentLayer.ICON_ARTIFACT:
            shifted = _shift_script(node.text(deep=True) or "", _SCRIPT_POSITION[tag])
            if shifted is not None:
                # Case 90: "$120<sup>1</sup>" must not read as "$1201" (R9 critical tokens).
                self.append_text(shifted, ctx)
                return
        if ctx.layer is ContentLayer.ICON_ARTIFACT:
            text = node.text(deep=True) or ""
            n, ops = normalize_text(text)
            if n:
                self.flush()
                self.emit(text.strip(), n, ops, ctx)
            return
        # Inline elements (span, b, a, ...) continue the open block; only block
        # elements end it, so a sentence with a link is not cut in two.
        self.walk(node, ctx, flush_end=is_block)
        if is_block:
            self.flush()

    def context_for(self, tag: str, attrs: dict[str, str | None], parent: _Ctx) -> _Ctx:
        role: Role = parent.role
        aria_role = (attrs.get("role") or "").lower()
        is_main = tag == "main" or aria_role == "main"
        is_article = tag == "article" and parent.role in ("unknown", "main")
        if is_main or is_article:
            role = "main"
        elif tag == "nav" or aria_role == "navigation":
            role = "nav"
        elif tag == "footer" or aria_role == "contentinfo":
            role = "footer"
        elif tag == "aside" or aria_role == "complementary":
            role = "aside"
        elif tag == "header" and parent.role == "unknown":
            role = "chrome"

        layer = parent.layer
        label = parent.label
        ident = f"{attrs.get('id') or ''} {attrs.get('class') or ''}"
        if layer in (ContentLayer.RENDERED_VISIBLE, ContentLayer.COLLAPSED_PUBLISHED):
            hidden_attr = attrs.get("hidden")
            style = attrs.get("style") or ""
            if attrs.get("data-cp-hidden") == "1":
                layer, label = ContentLayer.HIDDEN, "computed_hidden"
            elif attrs.get("data-cp-icon") == "1" and tag in ("i", "span", "em", "b"):
                layer, label = ContentLayer.ICON_ARTIFACT, "computed_icon_font"
            elif hidden_attr is not None and hidden_attr.lower() == "until-found":
                layer, label = ContentLayer.COLLAPSED_PUBLISHED, "hidden=until-found"
            elif hidden_attr is not None or _HIDDEN_STYLE.search(style):
                layer, label = ContentLayer.HIDDEN, "hidden_attr_or_style"
            elif _THIRD_PARTY.search(ident):
                layer, label = ContentLayer.THIRD_PARTY_CHROME, ident.strip()[:60]
            elif _ICON_CLASS.search(attrs.get("class") or "") and tag in ("i", "span", "em", "b"):
                layer, label = ContentLayer.ICON_ARTIFACT, (attrs.get("class") or "")[:60]
        region = classify_region(tag, attrs, parent.region)
        return _Ctx(layer, role, label=label, region=region)

    def attribute_text(self, tag: str, attrs: dict[str, str | None], ctx: _Ctx) -> None:
        for name in ("aria-label", "alt", "title", "placeholder"):
            value = attrs.get(name)
            if not value:
                continue
            if name == "title" and tag not in ("img", "abbr", "a", "button", "input"):
                continue
            n, ops = normalize_text(value)
            if n:
                self.flush()
                self.emit(
                    value,
                    n,
                    ops,
                    _Ctx(
                        ContentLayer.ACCESSIBILITY_ONLY,
                        ctx.role,
                        label=f"@{name}",
                        region=ctx.region,
                    ),
                )

    def script(self, node: Node, attrs: dict[str, str | None], parent: _Ctx) -> None:
        kind = (attrs.get("type") or "").lower()
        body = node.text(deep=True) or ""
        if "ld+json" in kind:
            self.structured.append(body.strip())
            return
        if attrs.get("src"):
            self.assets.append(
                DiscoveredAsset(url=urljoin(self.base_url, attrs["src"] or ""), kind="script")
            )
            return
        stripped = body.strip()
        if stripped and re.search(
            r"__(STATE|NEXT_DATA|NUXT|INITIAL_STATE|PRELOADED_STATE)__|window\.\w+\s*=\s*\{",
            stripped,
        ):
            n, ops = normalize_text(stripped[:5000])
            self.flush()
            self.emit(
                stripped[:5000],
                n,
                ops,
                _Ctx(
                    ContentLayer.SOURCE_ONLY,
                    parent.role,
                    label="inline_state_json",
                    region=parent.region,
                ),
            )

    def link(self, node: Node, attrs: dict[str, str | None], ctx: _Ctx) -> None:
        href = (attrs.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return
        url = urljoin(self.base_url, href)
        hidden = ctx.layer is ContentLayer.HIDDEN or bool(
            _HIDDEN_STYLE.search(attrs.get("style") or "")
        )
        text, _ = normalize_text(node.text(deep=True) or "")
        self.links.append(
            DiscoveredLink(
                url=url,
                text=text[:200],
                rel=(attrs.get("rel") or "").lower(),
                hidden=hidden,
                action_like=bool(_ACTION_LINK.search(url)),
            )
        )

    def asset(self, tag: str, attrs: dict[str, str | None], ctx: _Ctx) -> None:
        hidden = ctx.layer is ContentLayer.HIDDEN
        src = attrs.get("src") or attrs.get("data-src") or ""
        if tag in ("img", "picture", "source") and (src or attrs.get("srcset")):
            first = src or (attrs.get("srcset") or "").split(",")[0].strip().split(" ")[0]
            if first:
                self.assets.append(
                    DiscoveredAsset(url=urljoin(self.base_url, first), kind="image", hidden=hidden)
                )
        elif tag == "iframe" and src:
            self.assets.append(
                DiscoveredAsset(url=urljoin(self.base_url, src), kind="frame", hidden=hidden)
            )
        elif tag in ("video", "audio", "track") and src:
            self.assets.append(
                DiscoveredAsset(url=urljoin(self.base_url, src), kind="media", hidden=hidden)
            )
        elif tag in ("object", "embed") and (attrs.get("data") or src):
            self.assets.append(
                DiscoveredAsset(
                    url=urljoin(self.base_url, attrs.get("data") or src),
                    kind="object",
                    hidden=hidden,
                )
            )


def _metadata(tree: HTMLParser, base_url: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    html = tree.css_first("html")
    if html is not None and html.attributes.get("lang"):
        meta["lang"] = str(html.attributes.get("lang"))
    title = tree.css_first("title")
    if title is not None:
        meta["title"], _ = normalize_text(title.text() or "")
    for node in tree.css("meta"):
        a = node.attributes
        name = (a.get("name") or a.get("property") or "").lower()
        content = a.get("content")
        if (
            name
            and content
            and (
                name in ("description", "robots", "viewport", "generator")
                or name.startswith(("og:", "twitter:"))
            )
        ):
            meta[name] = content.strip()[:500]
    for node in tree.css("link"):
        a = node.attributes
        rel = (a.get("rel") or "").lower()
        href = a.get("href")
        if not href:
            continue
        if "canonical" in rel:
            meta["canonical"] = urljoin(base_url, href)
        elif "alternate" in rel and a.get("hreflang"):
            meta[f"hreflang:{a.get('hreflang')}"] = urljoin(base_url, href)
        elif "amphtml" in rel:
            meta["amphtml"] = urljoin(base_url, href)
    return meta


def extract_html_source(
    body: bytes, *, base_url: str, evidence_id: str, resource_id: str, state_id: str = "source"
) -> SourceExtraction:
    """Extract layered blocks, links, assets and metadata from HTML bytes."""
    tree = HTMLParser(body)
    walker = _Walker(
        base_url=base_url, evidence_id=evidence_id, resource_id=resource_id, state_id=state_id
    )
    body_node = tree.body
    warnings: list[str] = []
    head_ctx = _Ctx(ContentLayer.SOURCE_ONLY, "unknown", label="head")
    for node in tree.css("head script"):
        walker.script(node, node.attributes, head_ctx)
    if body_node is None:
        warnings.append("no_body_element")
    else:
        walker.walk(body_node, _Ctx(ContentLayer.RENDERED_VISIBLE, "unknown"))
    meta = _metadata(tree, base_url)
    for key in ("title", "description", "og:title", "og:description"):
        value = meta.get(key)
        if value:
            n, ops = normalize_text(value)
            walker.emit(value, n, ops, _Ctx(ContentLayer.METADATA, "unknown", label=f"meta:{key}"))

    visible_text = " ".join(
        b.normalized_text
        for b in walker.blocks
        if b.layer in (ContentLayer.RENDERED_VISIBLE, ContentLayer.COLLAPSED_PUBLISHED)
    )
    placeholder = bool(_PLACEHOLDER.search(visible_text))
    needs_render = len(visible_text) < 200 and (walker.script_count >= 3 or walker.spa_root)
    fingerprint = hashlib.sha256(" ".join(walker.skeleton).encode("utf-8")).hexdigest()[:24]
    return SourceExtraction(
        blocks=walker.blocks,
        links=walker.links,
        assets=walker.assets,
        metadata=meta,
        structured_data=walker.structured,
        template_fingerprint=fingerprint,
        placeholder_suspected=placeholder,
        needs_render_hint=needs_render,
        warnings=warnings,
    )
