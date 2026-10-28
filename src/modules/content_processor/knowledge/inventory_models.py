"""Contracts for step A of RFC 0003: the SEO and GEO inventory of a page.

Everything here is read by code from the stored HTML of a run (what the server
sent and every view the browser showed); nothing is fetched again and no model is
called. Each item says in which views it was found, so "only appears after scripts
run" and "only appears after a click" can be told apart from plain HTML.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "INVENTORY_VERSION",
    "AltStatus",
    "CanonicalStatus",
    "HeadFacts",
    "Heading",
    "ImageItem",
    "InventoryStatus",
    "Issue",
    "LinkItem",
    "LinkKind",
    "MediaItem",
    "PageInventory",
    "SeenIn",
    "Severity",
    "StructuredItem",
]

INVENTORY_VERSION: Final = "inventory-v1"

SeenIn = Literal["source", "rendered", "interaction"]
"""`source`: in the HTML the server sent (readable without running scripts);
`rendered`: in the page as the browser showed it; `interaction`: only in a view
opened by a click (tab, accordion, "show more")."""
LinkKind = Literal[
    "internal",
    "subdomain",
    "external",
    "same_page",
    "download",
    "mailto",
    "tel",
    "javascript",
    "other",
]
AltStatus = Literal["present", "empty", "missing", "file_name"]
CanonicalStatus = Literal["self", "other_page", "other_site", "missing", "multiple"]
Severity = Literal["error", "warning", "notice"]
InventoryStatus = Literal["inventoried", "not_captured", "not_html", "missing_evidence"]
"""`not_captured`: the visit was blocked, failed or excluded, so there is no page to read."""


class LinkItem(StrictModel):
    """One link target with one anchor text in one part of the page."""

    url: str
    anchor_text: str = ""
    title: str | None = None
    aria_label: str | None = None
    image_alt: str | None = Field(
        default=None, description="Alt text of an image used as the link (its anchor for search)"
    )
    kind: LinkKind
    rel: list[str] = Field(default_factory=list)
    nofollow: bool = False
    sponsored: bool = False
    ugc: bool = False
    new_tab: bool = False
    insecure: bool = Field(default=False, description="http:// link from an https:// page")
    ui_region: str
    heading: str | None = Field(default=None, description="Nearest heading above the link")
    order: int = Field(ge=0, description="Position among the page's links")
    occurrences: int = Field(default=1, ge=1, description="Times it appears in one view")
    hidden: bool = False
    seen_in: list[SeenIn]


class ImageItem(StrictModel):
    """One image on the page."""

    url: str
    alt: str | None = None
    alt_status: AltStatus
    title: str | None = None
    caption: str | None = None
    width: int | None = None
    height: int | None = None
    srcset_candidates: int = Field(default=0, ge=0)
    loading: str | None = None
    fetchpriority: str | None = None
    link_url: str | None = Field(default=None, description="Where the image links to, if anywhere")
    ui_region: str
    heading: str | None = None
    order: int = Field(ge=0)
    occurrences: int = Field(default=1, ge=1)
    hidden: bool = False
    seen_in: list[SeenIn]


class MediaItem(StrictModel):
    """A video, audio clip, iframe or embedded object."""

    kind: Literal["video", "audio", "iframe", "embed"]
    url: str
    provider: str
    title: str | None = None
    poster: str | None = None
    captions: bool = Field(default=False, description="Has a captions or subtitles track")
    ui_region: str
    heading: str | None = None
    order: int = Field(ge=0)
    occurrences: int = Field(default=1, ge=1)
    hidden: bool = False
    seen_in: list[SeenIn]


class Heading(StrictModel):
    """One heading of the page outline."""

    level: int = Field(ge=1, le=6)
    text: str
    ui_region: str


class HeadFacts(StrictModel):
    """What the page tells search engines and AI crawlers about itself."""

    title: str | None = None
    title_length: int = Field(default=0, ge=0)
    title_rendered: str | None = Field(
        default=None, description="Title after scripts ran, only when it differs"
    )
    description: str | None = None
    description_length: int = Field(default=0, ge=0)
    canonical: str | None = None
    canonical_status: CanonicalStatus = "missing"
    canonical_rendered: str | None = Field(
        default=None, description="Canonical after scripts ran, only when it differs"
    )
    robots: dict[str, list[str]] = Field(
        default_factory=dict, description="Robots meta tags by bot name ('robots', 'googlebot'...)"
    )
    indexable: bool = True
    followable: bool = True
    lang: str | None = None
    hreflang: dict[str, str] = Field(default_factory=dict)
    open_graph: dict[str, str] = Field(default_factory=dict)
    twitter: dict[str, str] = Field(default_factory=dict)
    viewport: str | None = None
    favicon: str | None = None
    charset: str | None = None
    generator: str | None = None
    amphtml: str | None = None
    feeds: list[str] = Field(default_factory=list, description="RSS / Atom alternates")
    h1: list[str] = Field(default_factory=list)
    headings: list[Heading] = Field(default_factory=list)
    skipped_heading_levels: list[str] = Field(
        default_factory=list, description="e.g. 'h2 -> h4 at <heading>'"
    )


class StructuredItem(StrictModel):
    """One structured-data item (JSON-LD node, microdata item or RDFa resource)."""

    syntax: Literal["json-ld", "microdata", "rdfa"]
    types: list[str]
    name: str | None = None
    properties: list[str] = Field(default_factory=list)
    missing_required: list[str] = Field(default_factory=list)
    missing_recommended: list[str] = Field(default_factory=list)
    data: dict[str, Any] | None = Field(default=None, description="The JSON-LD node as found")


class Issue(StrictModel):
    """A finding a reviewer can act on, with how many items it concerns."""

    code: str
    severity: Severity
    message: str
    count: int = Field(default=1, ge=1)
    examples: list[str] = Field(default_factory=list)


class PageInventory(StrictModel):
    """Everything SEO- and GEO-relevant found on one page of a run."""

    inventory_version: Literal["inventory-v1"] = INVENTORY_VERSION
    run_id: str
    url: str
    page_key: str
    inventoried_at: datetime
    status: InventoryStatus
    media_type: str | None = None
    views: list[str] = Field(default_factory=list, description="Views read: src, rendered, stateN")
    head: HeadFacts = Field(default_factory=HeadFacts)
    links: list[LinkItem] = Field(default_factory=list)
    images: list[ImageItem] = Field(default_factory=list)
    media: list[MediaItem] = Field(default_factory=list)
    structured_data: list[StructuredItem] = Field(default_factory=list)
    structured_data_errors: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
