"""Contracts of a gap report: findings with evidence, priority, action and status.

A finding's id comes from its kind and what it is about (a theme, an offering), so
the same gap keeps its id from one build to the next and the status a person gives
it (accepted, done, dismissed) is carried over. A gap that was open in the previous
report and is not detected any more is listed as resolved.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "GAPS_VERSION",
    "EvidenceItem",
    "Finding",
    "FindingKind",
    "FindingStatus",
    "GapReport",
    "StatusEntry",
]

GAPS_VERSION: Final = "gaps-v1"

FindingKind = Literal[
    "theme_gap",
    "theme_behind",
    "pillar_gap",
    "funnel_gap",
    "keyword_gap",
    "striking_distance",
    "competitor_momentum",
    "offering_gap",
    "unwatched_competitor",
]
FindingStatus = Literal["open", "accepted", "done", "dismissed"]


class EvidenceItem(StrictModel):
    """One fact behind a finding."""

    kind: Literal["page", "pillar", "keyword", "theme", "offering", "change", "count", "competitor"]
    label: str
    website: str | None = None
    url: str | None = None
    value: float | None = None


class StatusEntry(StrictModel):
    """What a person decided about a finding."""

    status: FindingStatus
    note: str = Field(default="", max_length=2000)
    updated_at: datetime


class Finding(StrictModel):
    """A gap the client should look at, most important first."""

    finding_id: str
    kind: FindingKind
    severity: Literal["high", "medium", "low"]
    priority: int = Field(ge=0, le=100)
    title: str
    detail: str
    action: str = Field(description="The recommended next step, in plain words")
    theme_id: str | None = None
    theme: str | None = None
    competitors: list[str] = Field(default_factory=list)
    demand: int = Field(default=0, ge=0, description="Monthly searches behind it, when known")
    evidence: list[EvidenceItem] = Field(default_factory=list)
    status: FindingStatus = "open"
    note: str = ""


class GapReport(StrictModel):
    """One build of a project's gap analysis."""

    gaps_version: Literal["gaps-v1"] = GAPS_VERSION
    report_id: str
    project_id: str
    created_at: datetime
    theme_map_id: str | None = None
    research_snapshot_id: str | None = None
    monitor_check_id: str | None = None
    missing_inputs: list[str] = Field(default_factory=list)
    findings: list[Finding]
    resolved: list[str] = Field(
        default_factory=list, description="Titles of findings open last time and gone now"
    )
    stats: dict[str, int] = Field(default_factory=dict)
