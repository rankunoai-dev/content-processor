"""Contracts of the page report: search performance per page, links, health, Search Console."""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.research.gsc import CtrOpportunity

__all__ = [
    "PAGE_REPORT_VERSION",
    "Cannibalisation",
    "LinkSuggestion",
    "PageReport",
    "PageRow",
]

PAGE_REPORT_VERSION: Final = "pages-v1"


class PageRow(StrictModel):
    """One page of the client's website: what it ranks for, its links and its health."""

    url: str
    title: str | None = None
    theme_id: str | None = None
    theme: str | None = None
    keywords: int = Field(default=0, ge=0, description="Keywords it ranks for (top 100)")
    volume: int = Field(default=0, ge=0, description="Monthly searches of those keywords")
    visits: float = Field(default=0.0, ge=0.0, description="Estimated visits (volume x CTR)")
    average_position: float | None = None
    best_position: int | None = None
    top_keyword: str | None = None
    commercial_volume: int = Field(
        default=0, ge=0, description="Searches of its commercial and transactional keywords"
    )
    inbound_body_links: int = Field(default=0, ge=0, description="Links from other pages' text")
    inbound_chrome_links: int = Field(
        default=0, ge=0, description="Links only from menus, header, footer, breadcrumbs"
    )
    pagerank: float = Field(default=0.0, ge=0.0, description="Internal PageRank, 0-100")
    word_count: int | None = None
    flags: list[str] = Field(default_factory=list, description="On-page health problems")
    gsc_clicks: int | None = None
    gsc_impressions: int | None = None
    gsc_ctr: float | None = None
    gsc_position: float | None = None
    indexable: bool = True


class Cannibalisation(StrictModel):
    """A keyword for which two or more pages of the client's website compete."""

    keyword: str
    volume: int = 0
    intent: str | None = None
    pages: list[str]
    positions: list[int | float] = Field(default_factory=list)
    source: Literal["semrush_rows", "url_swap", "search_console"]
    note: str = ""


class LinkSuggestion(StrictModel):
    """Link from a strong page of a theme to a page of the same theme that needs it."""

    source_url: str
    source_title: str | None = None
    source_pagerank: float
    target_url: str
    target_title: str | None = None
    anchor: str = Field(description="Suggested anchor text: the target's keyword")
    keyword_position: int
    keyword_volume: int
    theme: str | None = None
    reason: str


class PageReport(StrictModel):
    """Everything per page for one website, from the latest research and its scrape."""

    report_version: Literal["pages-v1"] = PAGE_REPORT_VERSION
    project_id: str
    website: str
    built_at: datetime
    research_snapshot_id: str | None = None
    run_id: str | None = None
    gsc_period: str | None = None
    gsc_imported_at: datetime | None = None
    pages: list[PageRow] = Field(default_factory=list)
    cannibalisation: list[Cannibalisation] = Field(default_factory=list)
    zero_traffic: list[str] = Field(
        default_factory=list, description="Indexable pages that rank for nothing"
    )
    orphans: list[str] = Field(default_factory=list, description="No link from any page's text")
    near_orphans: list[str] = Field(default_factory=list, description="One link from page text")
    stuck: list[str] = Field(
        default_factory=list, description="Pages ranking 4-20 with on-page problems"
    )
    link_suggestions: list[LinkSuggestion] = Field(default_factory=list)
    ctr_opportunities: list[CtrOpportunity] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
