"""Contracts for step B of RFC 0003: each page as an ordered tree, plus its page type.

The tree mirrors what a visitor sees: sections by heading, then paragraphs, lists,
tables, images and media, and the page's components (tabs, accordions, cards,
carousels) with the label of the control that opens them. Site chrome (header,
footer, menus, cookie and chat widgets) is left out; the site's menus are kept once
in the site stamp. Every text node cites the content blocks it was matched to, so
anything built from the tree can still be traced to the evidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "STRUCTURE_VERSION",
    "NodeKind",
    "PageType",
    "PageTypeLabel",
    "SiteProfile",
    "StructuredLink",
    "StructuredNode",
    "StructuredPage",
]

STRUCTURE_VERSION: Final = "structure-v1"

NodeKind = Literal[
    "section",
    "heading",
    "paragraph",
    "list",
    "table",
    "quote",
    "code",
    "image",
    "media",
    "cta",
    "form",
    "tabs",
    "tab",
    "accordion",
    "faq",
    "accordion_item",
    "card_group",
    "card",
    "carousel",
    "panel",
    "revealed",
]
PageType = Literal[
    "home",
    "product",
    "category",
    "service",
    "article",
    "case_study",
    "faq",
    "contact",
    "about",
    "careers",
    "legal",
    "pricing",
    "location",
    "event",
    "form",
    "document",
    "other",
]
Layer = Literal["visible", "collapsed", "after_click"]
"""`collapsed`: in the page but shown only once a tab or accordion is opened;
`after_click`: not in the page at all until a control was clicked."""


class StructuredLink(StrictModel):
    """A link inside a text node (kept apart so the text stays plain for citations)."""

    text: str
    url: str


class StructuredNode(StrictModel):
    """One node of the page tree."""

    kind: NodeKind
    title: str | None = Field(default=None, description="Heading, tab or accordion label")
    level: int | None = Field(default=None, ge=1, le=6, description="Heading level")
    text: str | None = None
    links: list[StructuredLink] = Field(default_factory=list)
    items: list[str] = Field(default_factory=list, description="List items or form fields")
    ordered: bool = False
    rows: list[list[str]] = Field(default_factory=list, description="Table rows, header first")
    header_row: bool = False
    url: str | None = Field(default=None, description="Image, media, link or form target")
    alt: str | None = None
    caption: str | None = None
    control: str | None = Field(
        default=None, description="Kind of control that revealed it (tab, accordion, dialog...)"
    )
    layer: Layer = "visible"
    ui_region: str | None = None
    block_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    children: list[StructuredNode] = Field(default_factory=list)


class PageTypeLabel(StrictModel):
    """What kind of page this is, with the signals that decided it."""

    page_type: PageType
    confidence: float = Field(ge=0.0, le=1.0)
    signals: list[str] = Field(default_factory=list, description="Why, in plain words")
    runner_up: PageType | None = None
    scores: dict[str, float] = Field(default_factory=dict)


class SiteProfile(StrictModel):
    """What kind of website a run covers, from its page types."""

    profile: Literal["ecommerce", "b2b_services", "publisher", "mixed", "unknown"]
    page_types: dict[str, int] = Field(default_factory=dict)
    signals: list[str] = Field(default_factory=list)


class StructuredPage(StrictModel):
    """One page as a tree, with its header facts and page type."""

    structure_version: Literal["structure-v1"] = STRUCTURE_VERSION
    run_id: str
    url: str
    page_key: str
    built_at: datetime
    status: Literal["structured", "not_captured", "not_html", "missing_evidence"]
    base_view: str | None = Field(default=None, description="View the tree was read from")
    title: str | None = None
    h1: str | None = None
    description: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    modified_at: datetime | None = None
    language: str | None = None
    breadcrumbs: list[str] = Field(default_factory=list)
    page_type: PageTypeLabel | None = None
    nodes: list[StructuredNode] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
