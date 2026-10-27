"""Contracts for step 1 of RFC 0002: page stamps and context-preserving chunks.

Every fact on a stamp is deterministic (`source = "code"`): it comes from the
run's `UrlRecord` or from the stored HTML evidence, never from a model. Later
stages add model-made labels next to these facts, each with its own source,
version and confidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "STAMP_VERSION",
    "Chunk",
    "ChunkKind",
    "Cta",
    "MenuItem",
    "MenuRef",
    "MetadataSource",
    "PageFormat",
    "PageStamp",
    "SchemaFacts",
    "SiteMenu",
    "SiteStamp",
]

STAMP_VERSION: Final = "stamp-v1"

ChunkKind = Literal["text", "faq", "panel", "table", "form_steps", "testimonial", "document_page"]
PageFormat = Literal["html", "pdf", "office", "image", "other"]
MetadataSource = Literal["stored_html", "missing_evidence", "not_html"]


class MenuItem(StrictModel):
    """One entry of a navigation menu, with its sub-entries."""

    label: str
    href: str | None = None
    children: list[MenuItem] = Field(default_factory=list)


class SiteMenu(StrictModel):
    """A distinct menu of the site, stored once per run."""

    menu_id: str
    kind: Literal["header", "footer", "breadcrumb", "other"]
    label: str = ""
    items: list[MenuItem]
    item_count: int = Field(ge=0)
    page_count: int = Field(ge=0, description="Pages of the run that carry this menu")


class MenuRef(StrictModel):
    """A menu seen on a page (the tree itself is in the site stamp)."""

    menu_id: str
    kind: Literal["header", "footer", "breadcrumb", "other"]


class SchemaFacts(StrictModel):
    """What the page declares in schema.org JSON-LD."""

    types: list[str] = Field(default_factory=list)
    organization: str | None = None
    services: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    offers: list[str] = Field(default_factory=list, description="name: price currency")
    faq_questions: list[str] = Field(default_factory=list)
    breadcrumbs: list[str] = Field(default_factory=list)
    headline: str | None = None
    date_published: str | None = None
    date_modified: str | None = None
    unreadable_blocks: int = Field(default=0, ge=0)


class Cta(StrictModel):
    """A call to action found on the page."""

    text: str
    url: str
    strength: Literal["strong", "soft"]


class PageStamp(StrictModel):
    """Deterministic facts about one URL of a run (RFC 0002, Stage A)."""

    stamp_version: Literal["stamp-v1"] = STAMP_VERSION
    run_id: str
    url: str
    final_url: str | None = None
    page_key: str
    site: str
    host: str
    stamped_at: datetime

    # Eligibility for the knowledge base
    knowledge_eligible: bool
    exclusion_reason: str | None = None
    is_asset: bool = False
    is_tracking: bool = False
    duplicate_of: str | None = Field(
        default=None, description="Same content as this URL (canonical or identical text)"
    )
    near_duplicates: list[str] = Field(
        default_factory=list,
        description="URLs with nearly the same text; kept, since they may differ in numbers",
    )
    media_type: str | None = None
    page_format: PageFormat = "other"

    # Structure
    path_segments: list[str] = Field(default_factory=list)
    path_l1: str | None = None
    path_l2: str | None = None
    path_l3: str | None = None
    path_depth: int = Field(default=0, ge=0)
    template_fingerprint: str | None = None
    breadcrumbs: list[str] = Field(default_factory=list)
    menus: list[MenuRef] = Field(default_factory=list)

    # Head metadata
    title: str | None = None
    meta_description: str | None = None
    h1: str | None = None
    canonical: str | None = None
    lang: str | None = None
    hreflang: dict[str, str] = Field(default_factory=dict)
    og: dict[str, str] = Field(default_factory=dict)
    robots_meta: str | None = None
    schema_org: SchemaFacts = Field(default_factory=SchemaFacts)
    metadata_source: MetadataSource = "stored_html"

    # Content facts
    headings: list[str] = Field(default_factory=list, description="h2/h3 outline, in order")
    page_summary: str | None = None
    word_count: int = Field(default=0, ge=0)
    main_word_count: int = Field(default=0, ge=0)
    reading_ease: float | None = Field(default=None, description="Flesch, English only")
    components: dict[str, int] = Field(default_factory=dict)
    ctas: list[Cta] = Field(default_factory=list)
    numbers: list[str] = Field(default_factory=list)

    # Dates and quality (copied from the record)
    published_at: datetime | None = None
    modified_at: datetime | None = None
    change_status: str | None = None
    badge: str
    gap_types: list[str] = Field(default_factory=list)
    chunk_count: int = Field(default=0, ge=0)


class Chunk(StrictModel):
    """A section of a page for retrieval, with everything needed to keep its context."""

    chunk_id: str
    run_id: str
    url: str
    page_key: str
    order: int = Field(ge=0)
    kind: ChunkKind
    heading_path: list[str] = Field(default_factory=list)
    parent_section_id: str
    prev_chunk_id: str | None = None
    next_chunk_id: str | None = None
    text: str = Field(description="Verbatim block text, one block per line (for citations)")
    overlap_before: str = Field(
        default="", description="Last sentence of the previous chunk when a section was split"
    )
    context_header: str
    embed_text: str = Field(description="Context header + overlap + text: what gets embedded")
    block_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    ui_regions: list[str] = Field(default_factory=list)
    word_count: int = Field(ge=0)
    token_estimate: int = Field(ge=0)
    oversized_atomic: bool = Field(
        default=False, description="An atomic unit (table, FAQ pair) larger than the target size"
    )


class SiteStamp(StrictModel):
    """Run-level facts: the site's menus (once), its name, and stamping counts."""

    stamp_version: Literal["stamp-v1"] = STAMP_VERSION
    run_id: str
    site: str
    site_name: str
    hosts: list[str]
    organization: str | None = None
    menus: list[SiteMenu] = Field(default_factory=list)
    pages: int = Field(ge=0)
    knowledge_pages: int = Field(ge=0)
    excluded: dict[str, int] = Field(default_factory=dict)
    chunks: int = Field(ge=0)
    schema_types: dict[str, int] = Field(default_factory=dict)
    metadata_coverage: dict[str, float] = Field(default_factory=dict)
