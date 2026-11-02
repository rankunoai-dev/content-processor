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
        description="Re-scrape every website this often; 0 = off (it contacts the websites)",
    )
    push_threshold: int = Field(
        default=3, ge=1, le=100, description="New competitor pages in a client theme that alert"
    )
    last_rescrape_at: datetime | None = None
    last_auto_check_at: datetime | None = None
