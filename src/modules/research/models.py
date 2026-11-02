"""Contracts of the Research module: keyword rankings per website, demand per theme.

A **snapshot** is one pull (or import) of a project's search data: for each website,
its domain overview and the keywords it ranks for (position, volume, CPC, ranking
URL), plus optional keyword metrics and search results for chosen keywords. Each
keyword is placed in a theme of the project's theme map, so demand and visibility
can be read per theme. Snapshots are kept, so positions can be compared over time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "SNAPSHOT_VERSION",
    "DomainOverview",
    "KeywordMetrics",
    "KeywordRow",
    "Mover",
    "ResearchSnapshot",
    "SerpRow",
    "Source",
    "ThemeDemand",
    "WebsiteResearch",
]

SNAPSHOT_VERSION: Final = "research-v1"

Source = Literal["semrush_api", "csv_import"]


class KeywordRow(StrictModel):
    """A keyword a website ranks for."""

    keyword: str
    position: int = Field(ge=1)
    previous_position: int | None = None
    volume: int = Field(default=0, ge=0)
    cpc: float = Field(default=0.0, ge=0.0)
    competition: float | None = None
    difficulty: float | None = None
    url: str | None = None
    traffic_share: float = Field(default=0.0, ge=0.0, description="Share of the site's traffic, %")
    theme_id: str | None = None


class DomainOverview(StrictModel):
    """A website's organic search footprint."""

    organic_keywords: int = Field(default=0, ge=0)
    organic_traffic: int = Field(default=0, ge=0)
    organic_cost: float = Field(default=0.0, ge=0.0, description="Traffic value, USD")
    rank: int | None = None


class WebsiteResearch(StrictModel):
    """One website's search data in a snapshot."""

    website: str
    name: str
    role: Literal["client", "competitor"]
    source: Source
    pulled_at: datetime
    overview: DomainOverview | None = None
    keywords: list[KeywordRow] = Field(default_factory=list)
    note: str = ""


class SerpRow(StrictModel):
    """One organic result for a keyword."""

    rank: int = Field(ge=1)
    domain: str
    url: str


class KeywordMetrics(StrictModel):
    """Demand and the search results of one keyword."""

    keyword: str
    volume: int = Field(default=0, ge=0)
    cpc: float = Field(default=0.0, ge=0.0)
    competition: float | None = None
    results: int | None = None
    serp: list[SerpRow] = Field(default_factory=list)
    theme_id: str | None = None


class ThemeDemand(StrictModel):
    """Search demand and visibility of one theme, per website."""

    theme_id: str | None
    label: str
    keywords: int = Field(ge=0)
    volume: int = Field(ge=0, description="Total monthly searches of its keywords")
    top10: dict[str, int] = Field(default_factory=dict, description="Website -> keywords in top 10")
    ranked: dict[str, int] = Field(default_factory=dict, description="Website -> keywords ranked")
    traffic: dict[str, float] = Field(
        default_factory=dict, description="Website -> estimated visits (volume x CTR by position)"
    )


class Mover(StrictModel):
    """A keyword whose position changed between two snapshots."""

    website: str
    keyword: str
    before: int | None
    after: int | None
    volume: int = 0
    url: str | None = None


class ResearchSnapshot(StrictModel):
    """One pull or import of a project's search data."""

    snapshot_version: Literal["research-v1"] = SNAPSHOT_VERSION
    snapshot_id: str
    project_id: str
    created_at: datetime
    database: str
    websites: list[WebsiteResearch]
    keywords: list[KeywordMetrics] = Field(default_factory=list)
    theme_map_id: str | None = None
    demand: list[ThemeDemand] = Field(default_factory=list)
    units_spent: int = Field(default=0, ge=0)
    notes: list[str] = Field(default_factory=list)
