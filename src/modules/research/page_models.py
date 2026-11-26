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
    "ConsolidationPair",
    "ConsolidationPlan",
    "LinkChange",
    "PageAlignment",
    "SearchLine",
    "TechnicalChange",
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
    ga_sessions: int | None = Field(default=None, description="GA4 sessions landing here")
    ga_organic_sessions: int | None = None
    ga_engagement_rate: float | None = None
    ga_avg_engagement_s: float | None = None
    ga_conversions: float | None = Field(default=None, description="GA4 key events")
    ga_revenue: float | None = None
    ga_ai_sessions: int | None = Field(default=None, description="Sessions from AI assistants")
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


Funnel = Literal["tofu", "mofu", "bofu"]
Pattern = Literal[
    "hand_off_to_child",
    "point_up_to_parent",
    "separate_by_intent",
    "answer_then_link",
    "differentiate_siblings",
    "localise",
    "merge_and_redirect",
    "remove_from_index",
]
SearchStatus = Literal["aligned", "also_aligned", "misaligned", "no_page", "unplaced"]


class SearchLine(StrictModel):
    """One search landing on one page of the client's website."""

    query: str
    page: str
    weight: float = Field(description="Impressions (Search Console) or estimated visits")
    clicks: int | None = None
    position: float | None = None
    funnel: Funnel
    topic: str | None = Field(default=None, description="The theme the search belongs to")
    owner: str | None = Field(default=None, description="The page that should rank for it")
    status: SearchStatus
    why: str = ""


class PageAlignment(StrictModel):
    """How much of a page's search weight comes from searches that belong on it."""

    url: str
    page_class: Literal["business", "article", "utility"]
    weight: float
    aligned: float
    share: float = Field(ge=0.0, le=1.0)
    status: Literal["strong", "within_tolerance", "needs_work", "too_little_data"]
    misaligned_to: dict[str, float] = Field(
        default_factory=dict, description="Owner page -> weight of its searches landing here"
    )


class ConsolidationPair(StrictModel):
    """A page taking searches that belong to another page, and how to resolve it."""

    group: int = Field(ge=1)
    page: str = Field(description="The page that ranks but should not")
    owner: str = Field(description="The page that should rank")
    topic: str | None = None
    pattern: Pattern
    fix: str
    weight: float
    priority: Literal["High", "Medium", "Low"]
    searches: list[SearchLine] = Field(default_factory=list)
    shared_paragraphs: int = 0
    owner_check: str = Field(
        default="", description="GA4 says the competing page converts better: confirm the owner"
    )


class LinkChange(StrictModel):
    """One internal link change, from links in page text only."""

    kind: Literal["repoint", "fix_anchor", "add_link"]
    group: int | None = None
    source: str
    destination: str = Field(description="Where the link points now (or should, for add_link)")
    new_destination: str | None = None
    anchor: str = ""
    suggested_anchor: str = ""
    reason: str


class TechnicalChange(StrictModel):
    """Redirects, noindex and sitemap changes the plan needs."""

    kind: Literal["redirect_301", "noindex_follow", "remove_from_sitemap"]
    group: int | None = None
    url: str
    target: str | None = None
    reason: str


class ConsolidationPlan(StrictModel):
    """The keyword-cannibalisation plan of the client's website (PLAYBOOK v1.0 rules)."""

    source: Literal["search_console", "search_data", "none"]
    scored_pages: int = 0
    tolerance: float = 0.10
    alignment: list[PageAlignment] = Field(default_factory=list)
    pairs: list[ConsolidationPair] = Field(default_factory=list)
    links: list[LinkChange] = Field(default_factory=list)
    technical: list[TechnicalChange] = Field(default_factory=list)
    searches: list[SearchLine] = Field(default_factory=list)
    template_paragraphs: int = Field(
        default=0, ge=0, description="Text found on 5+ pages: fix once in the template"
    )
    set_aside: dict[str, float] = Field(
        default_factory=dict,
        description="Weight left out: branded, likely automated, beyond position 20",
    )


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
    ga4_snapshot_id: str | None = None
    pages: list[PageRow] = Field(default_factory=list)
    cannibalisation: list[Cannibalisation] = Field(default_factory=list)
    zero_traffic: list[str] = Field(
        default_factory=list, description="Indexable pages that rank for nothing"
    )
    kept_by_ga4: list[str] = Field(
        default_factory=list,
        description="Rank for nothing but convert or draw visits in GA4: not to prune",
    )
    orphans: list[str] = Field(default_factory=list, description="No link from any page's text")
    near_orphans: list[str] = Field(default_factory=list, description="One link from page text")
    stuck: list[str] = Field(
        default_factory=list, description="Pages ranking 4-20 with on-page problems"
    )
    link_suggestions: list[LinkSuggestion] = Field(default_factory=list)
    ctr_opportunities: list[CtrOpportunity] = Field(default_factory=list)
    plan: ConsolidationPlan | None = None
    notes: list[str] = Field(default_factory=list)
