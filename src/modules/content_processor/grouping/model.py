"""Data contracts for autonomous URL grouping (Bit 1).

A grouping run takes one `PageSignal` per URL (status, media type, body) and
returns a `GroupingResult`: groups of URLs that share a path family and a DOM
template, plus every URL that was excluded and why. Excluded URLs are never
dropped silently; they are listed with a reason so denominators stay honest.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "ExcludedUrl",
    "GroupingResult",
    "GroupReason",
    "PageSignal",
    "UrlGroup",
]

GroupReason = Literal[
    "template_match",  # same path family, DOM skeleton above threshold
    "singleton",  # no eligible neighbour in its path family
    "spa_shell",  # script-heavy shell with little markup: grouped by prefix only, flagged
]


class PageSignal(StrictModel):
    """What the grouper needs to know about one fetched URL."""

    url: str
    http_status: int | None = Field(default=None, ge=0)
    media_type: str | None = None
    body: str = Field(default="", description="HTML text; empty when not fetched")


class UrlGroup(StrictModel):
    """URLs that share a path family and a DOM template."""

    group_id: str
    path_prefix: str
    members: list[str] = Field(min_length=1)
    reason: GroupReason
    template_fingerprint: str | None = Field(
        default=None, description="sha256 of the representative skeleton; None for singletons"
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Mean pairwise skeleton similarity")


class ExcludedUrl(StrictModel):
    """A URL kept out of grouping, with the reason it is not a template member."""

    url: str
    reason: str = Field(min_length=1)


class GroupingResult(StrictModel):
    """Output of one grouping run."""

    groups: list[UrlGroup] = Field(default_factory=list)
    excluded: list[ExcludedUrl] = Field(default_factory=list)
    similarity_threshold: float = Field(ge=0.0, le=1.0)
