"""Contracts of the Backlinks module: each website's links, and what they mean together.

A **snapshot** is one pull (or import) of a project's backlink data: for every website
of the project (the client's and the competitors'), its Domain Rating, Ahrefs rank,
backlink counts, the referring domains linking to it and its pages with the most
links. The **insights** are worked out from the snapshot by code alone: the link
intersect (websites linking to competitors but not to the client), the authority gap,
the client's most linked pages and the referring domains lost since the last snapshot.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "BACKLINKS_VERSION",
    "AuthorityGap",
    "BacklinkInsights",
    "BacklinkSnapshot",
    "BacklinkStats",
    "IntersectDomain",
    "RefDomain",
    "Source",
    "TopPage",
    "WebsiteBacklinks",
]

BACKLINKS_VERSION: Final = "backlinks-v1"

Source = Literal["ahrefs_api", "csv_import"]


class BacklinkStats(StrictModel):
    """A website's backlink counts (live now, and ever seen)."""

    live: int = Field(default=0, ge=0, description="Live backlinks")
    all_time: int = Field(default=0, ge=0, description="Backlinks ever seen")
    live_refdomains: int = Field(default=0, ge=0, description="Live referring domains")
    all_time_refdomains: int = Field(default=0, ge=0, description="Referring domains ever seen")


class RefDomain(StrictModel):
    """A website that links to one of the project's websites."""

    domain: str
    domain_rating: float | None = Field(default=None, ge=0.0, le=100.0)
    dofollow_links: int = Field(default=0, ge=0)
    links_to_target: int = Field(default=0, ge=0)
    first_seen: str | None = Field(default=None, description="YYYY-MM-DD")
    last_seen: str | None = Field(default=None, description="YYYY-MM-DD")
    is_spam: bool = False


class TopPage(StrictModel):
    """One of a website's pages with the most referring domains."""

    url: str
    title: str | None = None
    referring_domains: int = Field(default=0, ge=0)
    links: int = Field(default=0, ge=0)
    url_rating: float | None = Field(default=None, ge=0.0, le=100.0)
    http_code: int | None = None


class WebsiteBacklinks(StrictModel):
    """One website's backlink data in a snapshot."""

    website: str
    name: str
    role: Literal["client", "competitor"]
    source: Source = Field(description="Where the newest part of this website's data came from")
    pulled_at: datetime
    domain_rating: float | None = Field(default=None, ge=0.0, le=100.0)
    ahrefs_rank: int | None = None
    stats: BacklinkStats | None = None
    refdomains: list[RefDomain] = Field(default_factory=list)
    refdomains_asked: int = Field(
        default=0, ge=0, description="Rows asked for (0 for an export: the list is complete)"
    )
    top_pages: list[TopPage] = Field(default_factory=list)
    lost_domains: list[str] = Field(
        default_factory=list, description="Referring domains of the previous snapshot gone now"
    )
    new_domains: int | None = Field(
        default=None, description="Referring domains not in the previous snapshot (None: first)"
    )
    note: str = ""


class IntersectDomain(StrictModel):
    """A website linking to two or more competitors and not to the client."""

    domain: str
    domain_rating: float | None = None
    competitors: list[str]
    links: int = Field(default=0, ge=0, description="Links to those competitors together")


class AuthorityGap(StrictModel):
    """The client's authority against one competitor's."""

    competitor: str
    metric: Literal["domain_rating", "referring_domains"]
    client: float
    competitor_value: float
    gap: float = Field(description="Competitor minus client (positive: the competitor leads)")
    client_refdomains: int = 0
    competitor_refdomains: int = 0


class BacklinkInsights(StrictModel):
    """What the snapshot says together, worked out by code."""

    client: str | None = None
    min_competitors: int = Field(
        default=2, description="A domain must link to this many competitors (1 with one only)"
    )
    link_intersect: list[IntersectDomain] = Field(default_factory=list)
    authority: list[AuthorityGap] = Field(default_factory=list)
    client_top_pages: list[TopPage] = Field(default_factory=list)
    lost: dict[str, int] = Field(default_factory=dict, description="Website -> domains lost")
    notes: list[str] = Field(default_factory=list)


class BacklinkSnapshot(StrictModel):
    """One pull or import of a project's backlink data."""

    backlinks_version: Literal["backlinks-v1"] = BACKLINKS_VERSION
    snapshot_id: str
    project_id: str
    created_at: datetime
    websites: list[WebsiteBacklinks]
    insights: BacklinkInsights = Field(default_factory=lambda: BacklinkInsights())
    units_spent: int = Field(default=0, ge=0)
    notes: list[str] = Field(default_factory=list)
