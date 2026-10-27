"""Contracts for step 2 of RFC 0002: the site's offering catalog.

Every offering and every attribute on its card carries evidence: the menu entry,
URL, structured-data field or chunk it came from. An attribute without evidence
is never written. Model output (Ollama) is accepted only when code can check it
against that evidence; everything else a model proposes is kept as a suggestion
for a reviewer, never applied.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "CATALOG_VERSION",
    "CardAttribute",
    "Catalog",
    "EntryRole",
    "Evidence",
    "MenuEntry",
    "Offering",
    "OfferingLevel",
    "OfferingType",
    "PageBasis",
    "PageLink",
    "Signal",
    "Suggestion",
]

CATALOG_VERSION: Final = "catalog-v1"

EntryRole = Literal[
    "family",
    "offering",
    "group",
    "task",
    "tool",
    "content",
    "corporate",
    "audience",
    "landing",
    "unknown",
]
OfferingType = Literal[
    "unspecified",
    "product",
    "service",
    "solution",
    "program",
    "platform",
    "consulting_practice",
    "managed_service",
    "capability",
]
Signal = Literal["header_menu", "footer_menu", "other_menu", "url_family", "schema", "page"]
OfferingLevel = Literal["family", "offering", "sub_offering"]
PageBasis = Literal["same_url", "url_family", "schema_name", "breadcrumb"]


class Evidence(StrictModel):
    """Where a fact comes from."""

    kind: Literal["menu", "url", "schema", "chunk", "page_meta"]
    ref: str = Field(description="menu id, URL or chunk id")
    detail: str = Field(default="", description="menu path, field name or quoted text")


class CardAttribute(StrictModel):
    """One value on an offering card, with its evidence and who produced it."""

    value: str
    source: Literal["code", "ollama"] = "code"
    model: str | None = None
    evidence: list[Evidence] = Field(min_length=1)


class PageLink(StrictModel):
    """A page mapped to an offering, and why."""

    url: str
    basis: PageBasis
    chunk_ids: list[str] = Field(default_factory=list)


class MenuEntry(StrictModel):
    """A menu entry that is not an offering (tools, tasks, content, audiences), kept for review."""

    label: str
    role: EntryRole
    href: str | None = None
    menu_path: list[str] = Field(default_factory=list)
    offering_id: str | None = Field(default=None, description="Offering it sits under")
    pages: list[str] = Field(
        default_factory=list,
        description="Pages not under any offering that belong to this section (news, insights)",
    )


class Offering(StrictModel):
    """One node of the catalog tree, with its card."""

    offering_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    level: OfferingLevel
    parent_id: str | None = None
    family: str | None = None
    offering_type: OfferingType = "unspecified"
    href: str | None = None
    signals: list[Signal] = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    # card
    description: CardAttribute | None = None
    audiences: list[CardAttribute] = Field(default_factory=list)
    features: list[CardAttribute] = Field(default_factory=list)
    deliverables: list[CardAttribute] = Field(default_factory=list)
    pricing: list[CardAttribute] = Field(default_factory=list)
    numbers: list[CardAttribute] = Field(default_factory=list)
    questions: list[CardAttribute] = Field(default_factory=list)
    ctas: list[CardAttribute] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list, description="Tool and task menu entries")
    pages: list[PageLink] = Field(default_factory=list)


class Suggestion(StrictModel):
    """Something a model proposed that code could not confirm: for a reviewer to decide."""

    kind: Literal["merge", "parent", "offering_type", "attribute"]
    offering_id: str
    proposal: str
    model: str
    score: float | None = None
    reason: str = ""


class Catalog(StrictModel):
    """A site's offerings as a tree, with cards, page mapping and open suggestions."""

    catalog_version: Literal["catalog-v1"] = CATALOG_VERSION
    run_id: str
    site: str
    site_name: str
    built_at: datetime
    engines: dict[str, str] = Field(default_factory=dict)
    offerings: list[Offering] = Field(default_factory=list)
    other_entries: list[MenuEntry] = Field(default_factory=list)
    unmapped_pages: list[str] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)
