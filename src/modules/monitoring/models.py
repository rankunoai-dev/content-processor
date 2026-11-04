"""Contracts of a monitoring check: what changed on each website, theme drift, alerts.

A check compares, for every website of a project's scope, its latest scrape with
the scrape before it, places new pages into the project's current theme map, and
compares that map with the one used by the previous check. Alerts rank what an
analyst should look at first. Everything is computed on this machine from stored
scrapes; nothing is fetched.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "CHECK_VERSION",
    "Alert",
    "AlertKind",
    "MonitorCheck",
    "PageChange",
    "PageRef",
    "PriceChange",
    "Severity",
    "SiteDiff",
    "TemplateChange",
    "ThemeDrift",
    "WatchSettings",
    "PageDecision",
    "RecrawlPlan",
    "SitePlan",
]

CHECK_VERSION: Final = "monitor-v1"

Severity = Literal["high", "medium", "low", "info"]
AlertKind = Literal[
    "competitor_push_in_client_theme",
    "competitor_new_theme",
    "ownership_shift",
    "price_change",
    "client_pages_removed",
    "competitor_pages_removed",
    "competitor_rewrites",
    "pillar_drift",
    "client_content_change",
    "competitor_content_growth",
    "competitor_messaging_shift",
    "competitor_new_tables",
]


class PageRef(StrictModel):
    """A page, with the theme it sits in when known."""

    url: str
    title: str | None = None
    theme_id: str | None = None
    theme: str | None = None


class PriceChange(StrictModel):
    """An amount that changed in the same wording on a page."""

    before: str
    after: str
    context: str = Field(description="The words around the amount")


class PageChange(StrictModel):
    """How one page changed between two scrapes."""

    url: str
    title: str | None = None
    theme_id: str | None = None
    theme: str | None = None
    head_changes: dict[str, dict[str, str | None]] = Field(default_factory=dict)
    sections_added: int = Field(default=0, ge=0)
    sections_removed: int = Field(default=0, ge=0)
    added_text: list[str] = Field(default_factory=list)
    removed_text: list[str] = Field(default_factory=list)
    drift: float = Field(ge=0.0, le=1.0, description="Share of the page's sections that changed")
    prices: list[PriceChange] = Field(default_factory=list)
    new_numbers: list[str] = Field(default_factory=list)
    is_pillar: bool = False
    words_before: int = Field(default=0, ge=0, description="Main-content words before")
    words_after: int = Field(default=0, ge=0, description="Main-content words after")
    headings_added: list[str] = Field(default_factory=list, description="New H2/H3 headings")
    headings_removed: list[str] = Field(default_factory=list, description="H2/H3 headings gone")
    new_terms: list[str] = Field(
        default_factory=list, description="Words in the new headings (and H1) not in the old ones"
    )
    dropped_terms: list[str] = Field(
        default_factory=list, description="Words of the old headings (and H1) no longer there"
    )
    blocks_added: dict[str, int] = Field(
        default_factory=dict, description="Tables, pricing blocks, FAQs, forms: how many more"
    )
    blocks_removed: dict[str, int] = Field(default_factory=dict, description="How many fewer")

    @property
    def words_delta(self) -> int:
        """Main-content words added (negative: removed)."""
        return self.words_after - self.words_before


class TemplateChange(StrictModel):
    """A line added or removed on many pages at once (template, navigation, related links)."""

    line: str
    added_on: int = Field(default=0, ge=0, description="Pages it appeared on")
    removed_on: int = Field(default=0, ge=0, description="Pages it disappeared from")


class SiteDiff(StrictModel):
    """One website: its latest scrape against the one before."""

    website: str
    name: str
    role: Literal["client", "competitor"]
    baseline_run: str | None = None
    latest_run: str | None = None
    baseline_at: datetime | None = None
    latest_at: datetime | None = None
    note: str = ""
    new_pages: list[PageRef] = Field(default_factory=list)
    removed_pages: list[PageRef] = Field(default_factory=list)
    changed_pages: list[PageChange] = Field(default_factory=list)
    unchanged_pages: int = Field(default=0, ge=0)
    template_changes: list[TemplateChange] = Field(default_factory=list)
    drift: float = Field(default=0.0, ge=0.0, le=1.0, description="Share of pages that changed")


class ThemeDrift(StrictModel):
    """How one theme moved: new and removed pages per website, ownership before and after."""

    theme_id: str
    label: str
    status: Literal["new", "gone", "existing"] = "existing"
    ownership_before: str | None = None
    ownership_after: str | None = None
    new_pages: dict[str, int] = Field(default_factory=dict, description="Website -> new pages")
    removed_pages: dict[str, int] = Field(default_factory=dict)
    pages_before: dict[str, int] = Field(default_factory=dict)
    pages_after: dict[str, int] = Field(default_factory=dict)


class Alert(StrictModel):
    """Something an analyst should look at, most important first."""

    alert_id: str
    severity: Severity
    kind: AlertKind
    website: str
    title: str
    detail: str
    theme_id: str | None = None
    urls: list[str] = Field(default_factory=list)


class MonitorCheck(StrictModel):
    """One monitoring check of a project."""

    check_version: Literal["monitor-v1"] = CHECK_VERSION
    check_id: str
    project_id: str
    created_at: datetime
    trigger: Literal["manual", "auto"] = "manual"
    previous_check_id: str | None = None
    theme_map_id: str | None = None
    previous_theme_map_id: str | None = None
    websites: list[SiteDiff]
    theme_drift: list[ThemeDrift] = Field(default_factory=list)
    alerts: list[Alert] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)


class WatchSettings(StrictModel):
    """How a project is watched."""

    auto_check: bool = Field(
        default=True, description="Check by itself after each new scrape (local, no traffic)"
    )
    rescrape_every_days: int = Field(
        default=0,
        ge=0,
        le=90,
        description="Scheduled re-scrape this often (core pages are checked on every pass); "
        "0 = off (it contacts the websites)",
    )
    smart_recrawl: bool = Field(
        default=True,
        description="Scheduled passes check core pages and pages the sitemap shows changed; "
        "other pages only when older than other_every_days (off: every page every pass)",
    )
    other_every_days: int = Field(
        default=30, ge=1, le=365, description="How often pages that are not core are checked"
    )
    use_sitemaps: bool = Field(
        default=True, description="Read each website's sitemap last-modified dates"
    )
    core_urls: list[str] = Field(
        default_factory=list, max_length=200, description="Pages always treated as core"
    )
    core_limit: int = Field(default=50, ge=1, le=500, description="Core pages per website")
    push_threshold: int = Field(
        default=3, ge=1, le=100, description="New competitor pages in a client theme that alert"
    )
    last_rescrape_at: datetime | None = None
    last_auto_check_at: datetime | None = None


class PageDecision(StrictModel):
    """What a scheduled pass does with one page, and why."""

    url: str
    tier: Literal["core", "other"]
    action: Literal["check", "skip"]
    reason: str
    sitemap_lastmod: datetime | None = None
    last_captured: datetime | None = None


class SitePlan(StrictModel):
    """One website's recrawl plan."""

    site_id: str
    name: str
    role: Literal["client", "competitor"]
    website: str
    baseline_run: str | None = None
    sitemap_files: list[str] = Field(default_factory=list)
    sitemap_addresses: int = Field(default=0, ge=0)
    sitemap_dates: int = Field(default=0, ge=0, description="Addresses with a last-modified date")
    sitemap_note: str = ""
    core: int = Field(default=0, ge=0)
    check: int = Field(default=0, ge=0)
    skip: int = Field(default=0, ge=0)
    run_id: str | None = Field(default=None, description="The scrape queued for it, if any")
    note: str = ""
    pages: list[PageDecision] = Field(default_factory=list)


class RecrawlPlan(StrictModel):
    """Which pages a scheduled re-scrape checks and which it carries forward."""

    plan_id: str
    project_id: str
    created_at: datetime
    trigger: str = Field(description="preview, scheduled or manual")
    core_every_days: int = Field(ge=0)
    other_every_days: int = Field(ge=1)
    sites: list[SitePlan] = Field(default_factory=list)
