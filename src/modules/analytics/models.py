"""Contracts of the analytics module: GA4 landing-page figures and what is read from them."""

from __future__ import annotations

from datetime import date, datetime
from typing import Final, Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "AI_SOURCES",
    "ANALYTICS_VERSION",
    "AiReferral",
    "AnalyticsReport",
    "AnalyticsSettings",
    "CtaGap",
    "Decay",
    "Ga4Row",
    "Ga4Snapshot",
    "GroupRollup",
    "LeakyPage",
    "PostEdit",
]

ANALYTICS_VERSION: Final = "analytics-v1"

# Referrers that are AI assistants (session source, lower case, without www.)
AI_SOURCES: Final[tuple[str, ...]] = (
    "chatgpt.com", "chat.openai.com", "openai.com", "perplexity.ai", "gemini.google.com",
    "bard.google.com", "copilot.microsoft.com", "claude.ai", "you.com", "phind.com",
    "poe.com", "chat.deepseek.com", "deepseek.com", "meta.ai", "grok.com", "mistral.ai",
)  # fmt: skip
ORGANIC: Final = frozenset({"organic search", "organic shopping", "organic video"})


class Ga4Row(StrictModel):
    """One landing page over the period, all channels together."""

    url: str
    sessions: int = Field(default=0, ge=0)
    engaged_sessions: int = Field(default=0, ge=0)
    engagement_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    avg_engagement_s: float = Field(default=0.0, ge=0.0, description="Per session")
    key_events: float = Field(default=0.0, ge=0.0, description="Conversions (GA4 key events)")
    revenue: float = Field(default=0.0, ge=0.0)
    organic_sessions: int = Field(default=0, ge=0)
    ai_sessions: int = Field(default=0, ge=0, description="Sessions referred by AI assistants")
    channels: dict[str, int] = Field(default_factory=dict, description="Channel -> sessions")
    ai_sources: dict[str, int] = Field(default_factory=dict, description="AI source -> sessions")

    @property
    def conversion_rate(self) -> float:
        """Key events per session."""
        return self.key_events / self.sessions if self.sessions else 0.0


class Ga4Snapshot(StrictModel):
    """One period of a website's GA4 landing pages."""

    analytics_version: Literal["analytics-v1"] = ANALYTICS_VERSION
    snapshot_id: str
    project_id: str
    website: str
    source: Literal["csv_import", "ga4_api"]
    imported_at: datetime
    start_date: date | None = None
    end_date: date | None = None
    property_id: str | None = None
    rows: list[Ga4Row] = Field(default_factory=list)
    has_channels: bool = Field(default=False, description="Channel split known (organic, ...)")
    has_sources: bool = Field(default=False, description="Session sources known (AI referrals)")
    notes: list[str] = Field(default_factory=list)

    @property
    def sessions(self) -> int:
        """Sessions of every landing page."""
        return sum(r.sessions for r in self.rows)


class AnalyticsSettings(StrictModel):
    """Per project: the GA4 property to pull and the website it measures."""

    property_id: str | None = Field(default=None, pattern=r"^\d{4,15}$")
    website: str | None = None


class GroupRollup(StrictModel):
    """GA4 figures summed over a theme or a funnel stage."""

    key: str
    label: str
    pages: int = 0
    sessions: int = 0
    organic_sessions: int = 0
    engaged_sessions: int = 0
    key_events: float = 0.0
    revenue: float = 0.0
    search_volume: int | None = Field(default=None, description="Searches a month (research)")

    @property
    def conversion_rate(self) -> float:
        """Key events per session."""
        return self.key_events / self.sessions if self.sessions else 0.0


class LeakyPage(StrictModel):
    """Real search traffic that does not engage or convert."""

    url: str
    organic_sessions: int
    gsc_clicks: int | None = None
    engagement_rate: float
    key_events: float
    reason: str


class Decay(StrictModel):
    """A page losing organic sessions or conversions since the previous period."""

    url: str
    metric: Literal["organic_sessions", "sessions", "key_events"]
    before: float
    after: float
    change: float = Field(description="Relative change, -0.4 = 40% down")


class CtaGap(StrictModel):
    """A page with a form or call to action and real traffic, but no key events."""

    url: str
    sessions: int
    ctas: list[str]
    forms: int = 0


class AiReferral(StrictModel):
    """Sessions an AI assistant sent to a page."""

    url: str
    sessions: int
    sources: dict[str, int]


class PostEdit(StrictModel):
    """A page changed between two scrapes, and its GA4 figures before and after."""

    url: str
    change: str
    sessions_before: int
    sessions_after: int
    key_events_before: float
    key_events_after: float


class AnalyticsReport(StrictModel):
    """What GA4 adds to the project: rollups, problems, AI referrals, period changes."""

    project_id: str
    website: str
    built_at: datetime
    snapshot_id: str
    previous_snapshot_id: str | None = None
    period: str = ""
    totals: dict[str, float] = Field(default_factory=dict)
    channels: dict[str, int] = Field(default_factory=dict)
    themes: list[GroupRollup] = Field(default_factory=list)
    funnel: list[GroupRollup] = Field(default_factory=list)
    leaky: list[LeakyPage] = Field(default_factory=list)
    decays: list[Decay] = Field(default_factory=list)
    cta_gaps: list[CtaGap] = Field(default_factory=list)
    ai_referrals: list[AiReferral] = Field(default_factory=list)
    post_edit: list[PostEdit] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
