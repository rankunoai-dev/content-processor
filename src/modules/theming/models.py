"""Contracts of a theme map: themes, their topics, pillar pages and who owns each theme.

A theme map is built for one project, across all its websites (the client and its
competitors, including competitors it has in other projects), in one shared word
space so the websites are compared like for like. Theme ids are derived from the
theme's defining terms, so a later build of the same project gives a theme the same
id when its content is the same: Monitoring and Gap Analysis can follow themes over time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "THEME_MAP_VERSION",
    "FunnelStage",
    "Ownership",
    "PageRole",
    "PageTheme",
    "Pillar",
    "SiteCoverage",
    "Theme",
    "ThemeMap",
    "ThemeWebsite",
    "Topic",
]

THEME_MAP_VERSION: Final = "themes-v1"

FunnelStage = Literal["learn", "compare", "decide", "onboard", "support", "unknown"]
Ownership = Literal["client_owned", "contested", "competitor_led", "client_gap", "client_only"]
PageRole = Literal["pillar", "spoke", "unclustered"]


class ThemeWebsite(StrictModel):
    """A website of the map."""

    website: str = Field(description="Its key (shortest domain)")
    name: str
    role: Literal["client", "competitor"]
    project: str = Field(description="The project whose scrape was used (freshest copy)")
    run_id: str
    pages: int = Field(ge=0)


class Pillar(StrictModel):
    """The page that anchors a theme on one website, and why it was chosen."""

    url: str
    title: str | None = None
    score: float
    reasons: list[str] = Field(default_factory=list)


class SiteCoverage(StrictModel):
    """How much of a theme one website covers."""

    website: str
    pages: int = Field(ge=0)
    words: int = Field(ge=0)
    share: float = Field(ge=0.0, le=1.0, description="Its share of the theme's pages")
    pillar: Pillar | None = None


class Topic(StrictModel):
    """A sub-topic inside a theme."""

    topic_id: str
    label: str
    terms: list[str]
    pages: list[str]
    websites: dict[str, int] = Field(default_factory=dict, description="Pages per website")


class Theme(StrictModel):
    """A theme: its terms, topics, coverage per website, pillars and ownership."""

    theme_id: str
    label: str
    terms: list[str]
    pages: int = Field(ge=0)
    coverage: list[SiteCoverage]
    ownership: Ownership
    ownership_reason: str
    topics: list[Topic] = Field(default_factory=list)
    funnel: dict[str, int] = Field(default_factory=dict, description="Pages per funnel stage")
    offerings: list[str] = Field(default_factory=list, description="Catalog offerings it matches")


class PageTheme(StrictModel):
    """Where one page sits in the map."""

    url: str
    website: str
    title: str | None = None
    theme_id: str | None = None
    topic_id: str | None = None
    role: PageRole = "unclustered"
    similarity: float = Field(default=0.0, description="Cosine similarity to its theme's centre")
    funnel_stage: FunnelStage = "unknown"
    funnel_reason: str = ""
    page_type: str | None = None
    words: int = Field(default=0, ge=0)


class ThemeMap(StrictModel):
    """One build of a project's theme map."""

    map_version: Literal["themes-v1"] = THEME_MAP_VERSION
    map_id: str
    project_id: str
    built_at: datetime
    method: str = Field(description="How pages were compared and grouped")
    settings: dict[str, float] = Field(default_factory=dict)
    websites: list[ThemeWebsite]
    themes: list[Theme]
    pages: list[PageTheme]
    skipped: list[str] = Field(default_factory=list, description="Websites left out, with why")
    stats: dict[str, int] = Field(default_factory=dict)
